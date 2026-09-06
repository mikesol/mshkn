"""Guest access over SSH with a per-VM connection pool and real streaming."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import asyncssh

from mshkn.host import ExecResult, VmMetrics
from mshkn.host.firecracker import CONNECT_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mshkn.host import OutputLine, StreamName

logger = logging.getLogger(__name__)

# After the main process exits, keep draining output for this long: background
# children that inherited the shell's fds can keep the streams open.
STREAM_GRACE_SECONDS = 2.0
_HEALTH_CHECK_INTERVAL = 30.0
_METRICS_CMD = (
    "top -bn1 -d0.5 | grep '%Cpu' | awk '{print $8}'; "
    "free -m | awk '/^Mem:/{print $2,$3}'; "
    'df -BM / | awk \'NR==2{gsub(/M/,"",$2); gsub(/M/,"",$3); print $2,$3}\'; '
    "ps -eo pid,comm --no-headers | head -50"
)


@dataclass(frozen=True)
class _ReaderDone:
    """Sentinel a reader task queues when it stops, carrying why it stopped."""

    error: Exception | None


class ConnectFn(Protocol):
    def __call__(self, host: str, **kwargs: Any) -> Any: ...


class SshGuest:
    def __init__(self, key_path: Path, *, connect: ConnectFn = asyncssh.connect) -> None:
        self._key_path = str(key_path)
        self._connect = connect
        self._conns: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}

    # -- connections ---------------------------------------------------------

    async def _fresh(self, vm_ip: str, **extra: Any) -> Any:
        return await asyncio.wait_for(
            self._connect(
                vm_ip,
                username="root",
                client_keys=[self._key_path],
                known_hosts=None,
                **extra,
            ),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

    async def _pooled(self, vm_ip: str) -> Any:
        """Get or create the persistent connection for a VM (health-checked when idle)."""
        lock = self._locks.setdefault(vm_ip, asyncio.Lock())
        async with lock:
            conn = self._conns.get(vm_ip)
            loop = asyncio.get_running_loop()
            if conn is not None:
                now = loop.time()
                if now - self._last_used.get(vm_ip, 0.0) < _HEALTH_CHECK_INTERVAL:
                    self._last_used[vm_ip] = now
                    return conn
                try:
                    result = await asyncio.wait_for(conn.run("true", check=False), timeout=3.0)
                    if result.exit_status == 0:
                        self._last_used[vm_ip] = loop.time()
                        return conn
                except Exception:
                    logger.debug("pooled SSH connection to %s failed its health check", vm_ip)
                with contextlib.suppress(Exception):
                    conn.close()
                del self._conns[vm_ip]
            conn = await self._fresh(vm_ip, keepalive_interval=15, login_timeout=10)
            self._conns[vm_ip] = conn
            # The clock starts when the connection is usable, not when we asked
            # for it: a handshake can eat a third of the health-check interval.
            self._last_used[vm_ip] = loop.time()
            return conn

    async def warm(self, vm_ip: str) -> None:
        await self._pooled(vm_ip)

    async def evict(self, vm_ip: str) -> None:
        conn = self._conns.pop(vm_ip, None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        # The per-IP lock stays: _pooled holds it across a connect that can run
        # for CONNECT_TIMEOUT_SECONDS, and dropping it here would let a
        # concurrent _pooled build a second connection behind a second lock.
        self._last_used.pop(vm_ip, None)

    async def close(self) -> None:
        for conn in self._conns.values():
            with contextlib.suppress(Exception):
                conn.close()
        self._conns.clear()
        self._locks.clear()
        self._last_used.clear()

    # -- Guest protocol ------------------------------------------------------

    async def exec(self, vm_ip: str, command: str, *, timeout: float = 300.0) -> ExecResult:
        conn = await self._pooled(vm_ip)
        try:
            return await self._run_on(conn, command, timeout)
        except asyncssh.ChannelOpenError as exc:
            # The shared connection is out of channels (sshd MaxSessions), not
            # dead. Closing it would take down every other task's channel, so
            # run this command on a dedicated connection instead.
            logger.warning("SSH channel limit for %s, using a dedicated connection: %s", vm_ip, exc)
            dedicated = await self._fresh(vm_ip)
            try:
                return await self._run_on(dedicated, command, timeout)
            finally:
                dedicated.close()
        except asyncssh.ConnectionLost as exc:
            logger.warning("SSH connection lost for %s, reconnecting: %s", vm_ip, exc)
            await self.evict(vm_ip)
            conn = await self._pooled(vm_ip)
            return await self._run_on(conn, command, timeout)

    @staticmethod
    async def _run_on(conn: Any, command: str, timeout: float) -> ExecResult:
        result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
        return ExecResult(
            exit_code=result.exit_status or 0,
            stdout=str(result.stdout) if result.stdout else "",
            stderr=str(result.stderr) if result.stderr else "",
        )

    async def stream(
        self, vm_ip: str, command: str, *, timeout: float = 60.0
    ) -> AsyncGenerator[OutputLine, None]:
        """Yield (stream, line) as lines arrive; ends with ("exit", code).

        Uses the pooled connection; if the pooled connection cannot open
        another channel (sshd MaxSessions), falls back to a dedicated one.
        """
        conn = await self._pooled(vm_ip)
        owned = False
        try:
            process = await conn.create_process(command)
        except asyncssh.ChannelOpenError:
            conn = await self._fresh(vm_ip)
            owned = True
            try:
                process = await conn.create_process(command)
            except BaseException:
                conn.close()
                raise
        emitted_exit = False
        pump = self._pump(process, timeout)
        try:
            async for item in pump:
                emitted_exit = item[0] == "exit"
                yield item
        finally:
            await pump.aclose()
            if not emitted_exit:
                # The consumer abandoned the stream, or a reader failed. Kill
                # the remote process so its channel is released; on the pooled
                # connection it would otherwise hold a MaxSessions slot until
                # the command finished on its own.
                with contextlib.suppress(Exception):
                    process.kill()
            if owned:
                conn.close()

    @staticmethod
    async def _pump(process: Any, timeout: float) -> AsyncGenerator[OutputLine, None]:
        queue: asyncio.Queue[OutputLine | _ReaderDone] = asyncio.Queue()

        async def read(reader: Any, name: StreamName) -> None:
            try:
                async for line in reader:
                    queue.put_nowait((name, line.rstrip("\n")))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                queue.put_nowait(_ReaderDone(exc))
            else:
                queue.put_nowait(_ReaderDone(None))

        pumps = [
            asyncio.create_task(read(process.stdout, "stdout")),
            asyncio.create_task(read(process.stderr, "stderr")),
        ]
        exit_task = asyncio.create_task(process.wait())
        loop = asyncio.get_running_loop()
        hard_deadline = loop.time() + timeout
        grace_deadline: float | None = None
        finished_readers = 0
        reader_error: Exception | None = None
        try:
            while True:
                now = loop.time()
                if grace_deadline is None and exit_task.done():
                    # The exit status is known; drain stragglers briefly.
                    grace_deadline = now + STREAM_GRACE_SECONDS
                if grace_deadline is not None and finished_readers >= 2:
                    break
                # Until the process exits we are bounded by the hard timeout,
                # never by EOF on the readers: sshd sends channel EOF before
                # the exit-status request, so stopping at EOF would report an
                # exit status that has not arrived yet.
                budget = (grace_deadline if grace_deadline is not None else hard_deadline) - now
                if budget <= 0:
                    if grace_deadline is None:
                        logger.warning(
                            "stream: process did not exit within %.1fs, killing", timeout
                        )
                        process.kill()
                        grace_deadline = now + STREAM_GRACE_SECONDS
                        continue
                    break
                getter = asyncio.create_task(queue.get())
                waiters: set[asyncio.Task[Any]] = (
                    {getter} if exit_task.done() else {getter, exit_task}
                )
                done, _pending = await asyncio.wait(
                    waiters, timeout=budget, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    item = getter.result()
                    if isinstance(item, _ReaderDone):
                        finished_readers += 1
                        if item.error is not None:
                            reader_error = item.error
                            break
                    else:
                        yield item
                else:
                    getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
        finally:
            for task in pumps:
                task.cancel()
            if not exit_task.done():
                exit_task.cancel()
            await asyncio.gather(*pumps, exit_task, return_exceptions=True)
        if reader_error is not None:
            # A reader died mid-command. Reporting a clean exit here would make
            # a dropped connection indistinguishable from a successful run.
            raise reader_error
        yield ("exit", str(process.exit_status or 0))

    async def exec_bg(self, vm_ip: str, command: str) -> int:
        escaped = command.replace("'", "'\\''")
        result = await self.exec(
            vm_ip,
            f"nohup bash -c '{escaped}' > /tmp/bg-tmp-$$.log 2>&1 & "
            f"BG=$!; ln -sf /tmp/bg-tmp-$$.log /tmp/bg-$BG.log; echo $BG",
        )
        pid_str = result.stdout.strip()
        if not pid_str:
            msg = f"Failed to get PID for background command: stderr={result.stderr!r}"
            raise RuntimeError(msg)
        return int(pid_str)

    async def upload(self, vm_ip: str, remote_path: str, data: bytes) -> None:
        conn = await self._pooled(vm_ip)
        await conn.run(f"mkdir -p {Path(remote_path).parent!s}", check=True)
        async with conn.start_sftp_client() as sftp, sftp.open(remote_path, "wb") as f:
            await f.write(data)

    async def download(self, vm_ip: str, remote_path: str) -> bytes:
        conn = await self._pooled(vm_ip)
        async with conn.start_sftp_client() as sftp:
            try:
                async with sftp.open(remote_path, "rb") as f:
                    data: bytes = await f.read()
                    return data
            except asyncssh.SFTPNoSuchFile:
                raise FileNotFoundError(f"File not found: {remote_path}") from None

    async def metrics(self, vm_ip: str, *, timeout: float = 10.0) -> VmMetrics:
        result = await self.exec(vm_ip, _METRICS_CMD, timeout=timeout)
        return parse_metrics(result.stdout)


def parse_metrics(stdout: str) -> VmMetrics:
    """Parse the four-part _METRICS_CMD output."""
    lines = stdout.strip().splitlines()
    cpu_pct = 0.0
    if lines:
        with contextlib.suppress(ValueError):
            cpu_pct = round(100.0 - float(lines[0].strip().replace(",", ".")), 1)
    # Assign field by field, so a malformed second value keeps the first.
    ram_total_mb = ram_usage_mb = 0
    if len(lines) > 1 and len(parts := lines[1].split()) >= 2:
        with contextlib.suppress(ValueError):
            ram_total_mb = int(parts[0])
            ram_usage_mb = int(parts[1])
    disk_total_mb = disk_usage_mb = 0
    if len(lines) > 2 and len(parts := lines[2].split()) >= 2:
        with contextlib.suppress(ValueError):
            disk_total_mb = int(parts[0])
            disk_usage_mb = int(parts[1])
    processes: list[dict[str, object]] = []
    for line in lines[3:]:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            with contextlib.suppress(ValueError):
                processes.append({"pid": int(parts[0]), "command": parts[1]})
    return VmMetrics(
        cpu_pct=cpu_pct,
        ram_usage_mb=ram_usage_mb,
        ram_total_mb=ram_total_mb,
        disk_usage_mb=disk_usage_mb,
        disk_total_mb=disk_total_mb,
        processes=processes,
    )
