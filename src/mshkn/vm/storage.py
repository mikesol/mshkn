from __future__ import annotations

import asyncio
import logging

from mshkn.shell import ShellError, run

logger = logging.getLogger(__name__)


async def pool_create_snap(
    pool_name: str,
    new_volume_id: int,
    source_volume_id: int,
) -> None:
    """Issue create_snap in the dm-thin pool, retrying on orphaned volume ID."""
    try:
        await run(f"dmsetup message {pool_name} 0 'create_snap {new_volume_id} {source_volume_id}'")
    except ShellError as e:
        if "File exists" in e.stderr or "already exists" in e.stderr:
            logger.warning(
                "Orphaned thin volume %d in pool, deleting and retrying create_snap",
                new_volume_id,
            )
            await run(f"dmsetup message {pool_name} 0 'delete {new_volume_id}'")
            await run(
                f"dmsetup message {pool_name} 0 'create_snap {new_volume_id} {source_volume_id}'"
            )
        else:
            raise


async def create_snapshot(
    pool_name: str,
    source_volume_id: int,
    new_volume_id: int,
    new_volume_name: str,
    sectors: int,
) -> None:
    """Create a dm-thin snapshot (CoW copy of source)."""
    await pool_create_snap(pool_name, new_volume_id, source_volume_id)
    # Activate the device — remove stale mapping first if it exists
    try:
        await run(
            f"dmsetup create {new_volume_name} "
            f"--table '0 {sectors} thin /dev/mapper/{pool_name} {new_volume_id}'"
        )
    except ShellError as e:
        if "File exists" in e.stderr or "already exists" in e.stderr:
            logger.warning(
                "Stale device %s exists, removing and retrying",
                new_volume_name,
            )
            await run(f"dmsetup remove {new_volume_name}", check=False)
            await run(
                f"dmsetup create {new_volume_name} "
                f"--table '0 {sectors} thin /dev/mapper/{pool_name} {new_volume_id}'"
            )
        else:
            raise
    logger.info(
        "Created snapshot %s (vol %d from %d)", new_volume_name, new_volume_id, source_volume_id
    )


async def remove_volume(pool_name: str, volume_name: str, volume_id: int) -> None:
    """Remove a dm-thin volume.

    Retries dmsetup remove because the kernel may still hold the block device
    briefly after the firecracker process exits.
    """
    max_retries = 5
    for attempt in range(max_retries):
        try:
            await run(f"dmsetup remove {volume_name}")
            break
        except ShellError as e:
            if attempt < max_retries - 1:
                logger.debug(
                    "dmsetup remove %s failed (attempt %d/%d): %s",
                    volume_name,
                    attempt + 1,
                    max_retries,
                    e.stderr.strip(),
                )
                await asyncio.sleep(0.5)
            else:
                logger.warning(
                    "dmsetup remove %s failed after %d attempts: %s",
                    volume_name,
                    max_retries,
                    e.stderr.strip(),
                )

    try:
        await run(f"dmsetup message {pool_name} 0 'delete {volume_id}'")
    except ShellError as e:
        logger.warning(
            "dmsetup delete vol %d failed: %s",
            volume_id,
            e.stderr.strip(),
        )

    logger.info("Removed volume %s (vol %d)", volume_name, volume_id)


async def mount_volume(volume_name: str, mount_point: str, readonly: bool = False) -> None:
    """Mount a dm-thin volume at the given path."""
    await run(f"mkdir -p {mount_point}")
    opts = " -o ro" if readonly else ""
    await run(f"mount{opts} /dev/mapper/{volume_name} {mount_point}")


async def umount_volume(mount_point: str) -> None:
    """Unmount a volume. Retries on busy."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await run(f"umount {mount_point}")
            return
        except ShellError:
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
            else:
                raise
