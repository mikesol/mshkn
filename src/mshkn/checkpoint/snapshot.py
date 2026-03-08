from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mshkn.vm.firecracker import FirecrackerClient

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


async def create_vm_snapshot(
    socket_path: str,
    snapshot_dir: Path,
) -> tuple[Path, Path]:
    """Pause VM, snapshot, resume. Returns (vmstate_path, memory_path)."""
    vmstate_path = snapshot_dir / "vmstate"
    memory_path = snapshot_dir / "memory"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    client = FirecrackerClient(socket_path)
    try:
        await client.pause()
        await client.create_snapshot(str(vmstate_path), str(memory_path))
        await client.resume()
    finally:
        await client.close()

    logger.info("VM snapshot created at %s", snapshot_dir)
    return vmstate_path, memory_path
