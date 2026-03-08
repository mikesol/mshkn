from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mshkn.shell import run

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


async def init_thin_pool(
    pool_name: str,
    data_path: Path,
    meta_path: Path,
    data_size_gb: int,
) -> None:
    """Create dm-thin pool backed by loopback files."""
    await run(f"truncate -s {data_size_gb}G {data_path}")
    await run(f"truncate -s 256M {meta_path}")

    data_loop = (await run(f"losetup --find --show {data_path}")).strip()
    meta_loop = (await run(f"losetup --find --show {meta_path}")).strip()

    await run(f"dd if=/dev/zero of={meta_loop} bs=4096 count=1")
    data_sectors = (await run(f"blockdev --getsz {data_loop}")).strip()

    await run(
        f"dmsetup create {pool_name} "
        f"--table '0 {data_sectors} thin-pool {meta_loop} {data_loop} 128 0'"
    )
    logger.info("Created thin pool %s (data=%s, meta=%s)", pool_name, data_path, meta_path)


async def create_base_volume(
    pool_name: str,
    volume_id: int,
    volume_name: str,
    sectors: int,
    source_image: Path,
) -> None:
    """Create a thin volume and write a base image to it."""
    await run(f"dmsetup message {pool_name} 0 'create_thin {volume_id}'")
    await run(
        f"dmsetup create {volume_name} "
        f"--table '0 {sectors} thin /dev/mapper/{pool_name} {volume_id}'"
    )
    await run(f"dd if={source_image} of=/dev/mapper/{volume_name} bs=4M")
    logger.info("Created base volume %s (vol %d) from %s", volume_name, volume_id, source_image)


async def create_snapshot(
    pool_name: str,
    source_volume_id: int,
    new_volume_id: int,
    new_volume_name: str,
    sectors: int,
) -> None:
    """Create a dm-thin snapshot (CoW copy of source)."""
    await run(f"dmsetup message {pool_name} 0 'create_snap {new_volume_id} {source_volume_id}'")
    await run(
        f"dmsetup create {new_volume_name} "
        f"--table '0 {sectors} thin /dev/mapper/{pool_name} {new_volume_id}'"
    )
    logger.info(
        "Created snapshot %s (vol %d from %d)", new_volume_name, new_volume_id, source_volume_id
    )


async def remove_volume(pool_name: str, volume_name: str, volume_id: int) -> None:
    """Remove a dm-thin volume."""
    await run(f"dmsetup remove {volume_name}", check=False)
    await run(f"dmsetup message {pool_name} 0 'delete {volume_id}'", check=False)
    logger.info("Removed volume %s (vol %d)", volume_name, volume_id)
