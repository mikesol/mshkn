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
    # Wrap in bash -c to ensure $$ and $! expand correctly
    escaped = command.replace("'", "'\\''")
    result = await ssh_exec(
        vm_ip,
        f"bash -c 'nohup {escaped} > /tmp/bg-$$.log 2>&1 & echo $!'",
        ssh_key_path,
    )
    pid_str = result.stdout.strip()
    if not pid_str:
        msg = f"Failed to get PID for background command: stderr={result.stderr!r}"
        raise RuntimeError(msg)
    return int(pid_str)


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
