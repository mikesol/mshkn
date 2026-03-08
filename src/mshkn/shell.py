from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ShellError(Exception):
    def __init__(self, cmd: str, returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Command failed ({returncode}): {cmd}\n{stderr}")


async def run(cmd: str, check: bool = True) -> str:
    logger.debug("shell: %s", cmd)
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode()
    stderr = stderr_bytes.decode()

    if check and proc.returncode != 0:
        raise ShellError(cmd, proc.returncode or -1, stderr)

    return stdout
