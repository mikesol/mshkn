from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import asyncssh

logger = logging.getLogger(__name__)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


async def ssh_exec(
    vm_ip: str,
    command: str,
    ssh_key_path: Path,
    timeout: float = 300.0,
) -> ExecResult:
    """Execute a command via SSH and return the full result."""
    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn:
        result = await asyncio.wait_for(
            conn.run(command, check=False),
            timeout=timeout,
        )
    return ExecResult(
        exit_code=result.exit_status or 0,
        stdout=str(result.stdout) if result.stdout else "",
        stderr=str(result.stderr) if result.stderr else "",
    )


async def ssh_exec_stream(
    vm_ip: str,
    command: str,
    ssh_key_path: Path,
    timeout: float = 60.0,
) -> AsyncIterator[tuple[str, str]]:
    """Execute a command via SSH and yield (stream, line) tuples as they arrive.

    stream is "stdout" or "stderr". Yields ("exit", "<code>") at the end.

    Background processes that inherit the shell's stdout/stderr keep the SSH
    streams open after the main command exits.  We work around this by racing
    stream reads against ``process.wait()`` and draining whatever is left for
    a short grace period once the process exits.
    """
    log = logging.getLogger("mshkn.ssh")

    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn:
        process = await conn.create_process(command)

        collected: list[tuple[str, str]] = []

        async def read_stream(
            stream: asyncssh.SSHReader[str], name: str
        ) -> None:
            async for line in stream:
                collected.append((name, line.rstrip("\n")))

        stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))

        # Wait for the process to exit first — streams may stay open if
        # background children inherited the file descriptors.
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            log.warning("ssh_exec_stream: process did not exit within %.1fs", timeout)
            process.kill()

        # Give streams a short grace period to deliver remaining output,
        # then cancel them so we don't hang forever.
        grace = 2.0
        _done, pending = await asyncio.wait(
            [stdout_task, stderr_task], timeout=grace,
        )
        for task in pending:
            log.debug("ssh_exec_stream: cancelling lingering stream reader")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        for item in collected:
            yield item
        yield ("exit", str(process.exit_status or 0))


async def ssh_exec_bg(
    vm_ip: str,
    command: str,
    ssh_key_path: Path,
) -> int:
    """Run a command in the background via SSH, return PID."""
    # Use nohup bash -c so compound commands (for loops etc.) work.
    # Log file is named /tmp/bg-{pid}.log where pid matches the returned PID.
    escaped = command.replace("'", "'\\''")
    result = await ssh_exec(
        vm_ip,
        f"nohup bash -c '{escaped}' > /tmp/bg-tmp-$$.log 2>&1 & "
        f"BG=$!; ln -sf /tmp/bg-tmp-$$.log /tmp/bg-$BG.log; echo $BG",
        ssh_key_path,
    )
    pid_str = result.stdout.strip()
    if not pid_str:
        msg = f"Failed to get PID for background command: stderr={result.stderr!r}"
        raise RuntimeError(msg)
    return int(pid_str)


@dataclass
class VmMetrics:
    cpu_pct: float
    ram_usage_mb: int
    ram_total_mb: int
    disk_usage_mb: int
    disk_total_mb: int
    processes: list[dict[str, object]]


async def ssh_gather_metrics(
    vm_ip: str,
    ssh_key_path: Path,
    timeout: float = 10.0,
) -> VmMetrics:
    """Gather CPU, RAM, disk, and process metrics from a VM via SSH."""
    # Single compound command to minimize SSH round-trips
    cmd = (
        "top -bn1 -d0.5 | grep '%Cpu' | awk '{print $8}'; "
        "free -m | awk '/^Mem:/{print $2,$3}'; "
        "df -BM / | awk 'NR==2{gsub(/M/,\"\",$2); gsub(/M/,\"\",$3); print $2,$3}'; "
        "ps -eo pid,comm --no-headers | head -50"
    )
    result = await ssh_exec(vm_ip, cmd, ssh_key_path, timeout=timeout)
    lines = result.stdout.strip().splitlines()

    # Parse CPU idle → usage
    cpu_pct = 0.0
    if lines:
        try:
            idle = float(lines[0].strip().replace(",", "."))
            cpu_pct = round(100.0 - idle, 1)
        except ValueError:
            pass

    # Parse RAM
    ram_total_mb, ram_usage_mb = 0, 0
    if len(lines) > 1:
        parts = lines[1].split()
        if len(parts) >= 2:
            try:
                ram_total_mb = int(parts[0])
                ram_usage_mb = int(parts[1])
            except ValueError:
                pass

    # Parse disk
    disk_total_mb, disk_usage_mb = 0, 0
    if len(lines) > 2:
        parts = lines[2].split()
        if len(parts) >= 2:
            try:
                disk_total_mb = int(parts[0])
                disk_usage_mb = int(parts[1])
            except ValueError:
                pass

    # Parse processes (remaining lines)
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


async def ssh_upload(
    vm_ip: str,
    remote_path: str,
    data: bytes,
    ssh_key_path: Path,
) -> None:
    """Upload data to a file on the VM via SFTP (supports binary data)."""
    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn:
        parent = str(Path(remote_path).parent)
        await conn.run(f"mkdir -p {parent}", check=True)
        async with conn.start_sftp_client() as sftp, sftp.open(remote_path, "wb") as f:
            await f.write(data)


async def ssh_download(
    vm_ip: str,
    remote_path: str,
    ssh_key_path: Path,
) -> bytes:
    """Download a file from the VM via SFTP (supports binary data)."""
    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn, conn.start_sftp_client() as sftp:
        try:
            async with sftp.open(remote_path, "rb") as f:
                return await f.read()
        except asyncssh.SFTPNoSuchFile:
            raise FileNotFoundError(f"File not found: {remote_path}") from None
