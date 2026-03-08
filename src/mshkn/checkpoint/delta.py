from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from mshkn.shell import run

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


async def export_disk_delta(
    pool_name: str,
    base_volume_id: int,
    snap_volume_id: int,
    snap_volume_name: str,
    meta_device: str,
    output_dir: Path,
    block_size: int = 65536,
) -> tuple[Path, Path]:
    """Export changed blocks between base and snapshot. Returns (delta_path, manifest_path)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    delta_path = output_dir / "delta.bin"
    manifest_path = output_dir / "blocks.txt"

    await run(f"dmsetup message {pool_name} 0 'reserve_metadata_snap'")

    try:
        xml = await run(
            f"thin_delta -m --snap1 {base_volume_id} --snap2 {snap_volume_id} {meta_device}"
        )

        ranges: list[tuple[int, int]] = []
        for line in xml.splitlines():
            if "different" in line or "right_only" in line:
                m = re.search(r'begin="(\d+)".*length="(\d+)"', line)
                if m:
                    ranges.append((int(m.group(1)), int(m.group(2))))

        with manifest_path.open("w") as f:
            for begin, length in ranges:
                f.write(f"{begin} {length}\n")

        with delta_path.open("wb") as out:
            for begin, length in ranges:
                data = await run(
                    f"dd if=/dev/mapper/{snap_volume_name} bs={block_size} "
                    f"skip={begin} count={length} 2>/dev/null"
                )
                out.write(data.encode("latin-1"))

    finally:
        await run(f"dmsetup message {pool_name} 0 'release_metadata_snap'")

    logger.info(
        "Exported disk delta: %d ranges, %d bytes", len(ranges), delta_path.stat().st_size
    )
    return delta_path, manifest_path


async def import_disk_delta(
    volume_name: str,
    delta_path: Path,
    blocks_path: Path,
    block_size: int = 65536,
) -> None:
    """Apply a disk delta to a volume."""
    ranges: list[tuple[int, int]] = []
    for line in blocks_path.read_text().splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            ranges.append((int(parts[0]), int(parts[1])))

    offset = 0
    for begin, length in ranges:
        byte_count = length * block_size
        await run(
            f"dd if={delta_path} of=/dev/mapper/{volume_name} "
            f"bs={block_size} skip={offset // block_size} seek={begin} "
            f"count={length} conv=notrunc 2>/dev/null"
        )
        offset += byte_count

    logger.info("Imported disk delta: %d ranges to %s", len(ranges), volume_name)
