"""dm-thin block store: copy-on-write volumes on the mshkn thin pool."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.host import PoolUsage
from mshkn.host.shell import RunFn, ShellError
from mshkn.host.shell import run as shell_run

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

_REMOVE_RETRIES = 5
_UMOUNT_RETRIES = 3


def parse_pool_status(text: str) -> PoolUsage:
    """Parse `dmsetup status <pool>`: `0 <sectors> thin-pool <txn> <mu>/<mt> <du>/<dt> ...`."""
    parts = text.split()
    if len(parts) < 6 or parts[2] != "thin-pool":
        raise ValueError(f"not a thin-pool status line: {text!r}")
    meta_used, meta_total = (int(x) for x in parts[4].split("/"))
    data_used, data_total = (int(x) for x in parts[5].split("/"))
    return PoolUsage(
        data_used_ratio=data_used / data_total if data_total else 0.0,
        metadata_used_ratio=meta_used / meta_total if meta_total else 0.0,
    )


class DmThinBlockStore:
    def __init__(self, pool_name: str, sectors: int, *, run: RunFn = shell_run) -> None:
        self._pool = pool_name
        self._sectors = sectors
        self._run = run

    async def snap(self, *, source_volume_id: int, new_volume_id: int) -> None:
        """create_snap in the pool, retrying once if the target id is an orphan."""
        cmd = f"dmsetup message {self._pool} 0 'create_snap {new_volume_id} {source_volume_id}'"
        try:
            await self._run(cmd)
        except ShellError as e:
            if "File exists" not in e.stderr and "already exists" not in e.stderr:
                raise
            logger.warning("Orphaned thin volume %d in pool, deleting and retrying", new_volume_id)
            await self._run(f"dmsetup message {self._pool} 0 'delete {new_volume_id}'")
            await self._run(cmd)

    async def activate(self, *, volume_id: int, name: str) -> None:
        """Map a thin volume to /dev/mapper/<name>, replacing a stale mapping."""
        cmd = (
            f"dmsetup create {name} --table "
            f"'0 {self._sectors} thin /dev/mapper/{self._pool} {volume_id}'"
        )
        try:
            await self._run(cmd)
        except ShellError as e:
            if "File exists" not in e.stderr and "already exists" not in e.stderr:
                raise
            logger.warning("Stale device %s exists, removing and retrying", name)
            await self._run(f"dmsetup remove {name}", check=False)
            await self._run(cmd)
        logger.info("Activated volume %s (vol %d)", name, volume_id)

    async def deactivate(self, name: str) -> None:
        await self._run(f"dmsetup remove {name}")

    async def remove(self, *, volume_id: int, name: str) -> None:
        """Unmap the device (retrying while the kernel still holds it) and delete the volume."""
        for attempt in range(_REMOVE_RETRIES):
            try:
                await self._run(f"dmsetup remove {name}")
                break
            except ShellError as e:
                if attempt < _REMOVE_RETRIES - 1:
                    logger.debug(
                        "dmsetup remove %s failed (attempt %d): %s",
                        name,
                        attempt + 1,
                        e.stderr.strip(),
                    )
                    await asyncio.sleep(0.5)
                else:
                    logger.warning(
                        "dmsetup remove %s failed after %d attempts: %s",
                        name,
                        _REMOVE_RETRIES,
                        e.stderr.strip(),
                    )
        try:
            await self._run(f"dmsetup message {self._pool} 0 'delete {volume_id}'")
        except ShellError as e:
            logger.warning("dmsetup delete vol %d failed: %s", volume_id, e.stderr.strip())
        logger.info("Removed volume %s (vol %d)", name, volume_id)

    async def mkfs(self, name: str) -> None:
        await self._run(f"mkfs.ext4 -F /dev/mapper/{name}")

    @contextlib.asynccontextmanager
    async def mounted(self, name: str, *, readonly: bool = False) -> AsyncIterator[Path]:
        mount_point = Path(tempfile.mkdtemp(prefix="mshkn-mnt-"))
        opts = " -o ro" if readonly else ""
        await self._run(f"mount{opts} /dev/mapper/{name} {mount_point}")
        try:
            yield mount_point
        finally:
            for attempt in range(_UMOUNT_RETRIES):
                try:
                    await self._run(f"umount {mount_point}")
                    break
                except ShellError:
                    if attempt < _UMOUNT_RETRIES - 1:
                        await asyncio.sleep(0.5)
                    else:
                        logger.warning(
                            "umount %s failed after %d attempts", mount_point, _UMOUNT_RETRIES
                        )
            with contextlib.suppress(OSError):
                mount_point.rmdir()

    async def max_volume_id(self) -> int | None:
        """Highest thin volume id mapped on this host (catches orphans the DB forgot)."""
        try:
            output = await self._run("dmsetup table --target thin")
        except ShellError:
            return None
        max_id: int | None = None
        for line in output.strip().splitlines():
            # line shape: "<name>: 0 <sectors> thin <pool_major:minor> <volume_id>"
            parts = line.split()
            if len(parts) >= 6 and parts[3] == "thin":
                with contextlib.suppress(ValueError):
                    vol_id = int(parts[5])
                    max_id = vol_id if max_id is None or vol_id > max_id else max_id
        return max_id

    async def usage(self) -> PoolUsage:
        return parse_pool_status(await self._run(f"dmsetup status {self._pool}"))
