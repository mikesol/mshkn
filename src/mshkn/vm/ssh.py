from __future__ import annotations

import asyncio
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
) -> AsyncIterator[tuple[str, str]]:
    """Execute a command via SSH and yield (stream, line) tuples as they arrive.

    stream is "stdout" or "stderr". Yields ("exit", "<code>") at the end.
    """
    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn:
        process = await conn.create_process(command)

        async def read_stream(
            stream: asyncssh.SSHReader[str], name: str
        ) -> list[tuple[str, str]]:
            lines: list[tuple[str, str]] = []
            async for line in stream:
                lines.append((name, line.rstrip("\n")))
            return lines

        stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))

        stdout_lines, stderr_lines = await asyncio.gather(stdout_task, stderr_task)
        await process.wait()

        for item in stdout_lines:
            yield item
        for item in stderr_lines:
            yield item
        yield ("exit", str(process.exit_status or 0))


async def ssh_exec_bg(
    vm_ip: str,
    command: str,
    ssh_key_path: Path,
) -> int:
    """Run a command in the background via SSH, return PID."""
    result = await ssh_exec(
        vm_ip,
        f"nohup {command} > /tmp/bg-$$.log 2>&1 & echo $!",
        ssh_key_path,
    )
    return int(result.stdout.strip())


async def ssh_upload(
    vm_ip: str,
    remote_path: str,
    data: bytes,
    ssh_key_path: Path,
) -> None:
    """Upload data to a file on the VM."""
    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn:
        parent = str(Path(remote_path).parent)
        await conn.run(f"mkdir -p {parent}", check=True)
        process = await conn.create_process(f"cat > {remote_path}")
        assert process.stdin is not None
        process.stdin.write(data.decode("utf-8", errors="surrogateescape"))
        process.stdin.write_eof()
        await process.wait()


async def ssh_download(
    vm_ip: str,
    remote_path: str,
    ssh_key_path: Path,
) -> bytes:
    """Download a file from the VM."""
    result = await ssh_exec(vm_ip, f"cat {remote_path}", ssh_key_path)
    if result.exit_code != 0:
        raise FileNotFoundError(f"File not found: {remote_path}")
    return result.stdout.encode("utf-8", errors="surrogateescape")
