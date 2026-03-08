from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mshkn.shell import run

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


async def upload_checkpoint(
    local_dir: Path,
    r2_prefix: str,
    bucket: str,
) -> None:
    """Upload checkpoint files to R2."""
    await run(f"rclone copy {local_dir}/ r2:{bucket}/{r2_prefix}/")
    logger.info("Uploaded checkpoint to r2:%s/%s", bucket, r2_prefix)


async def download_checkpoint(
    r2_prefix: str,
    bucket: str,
    local_dir: Path,
) -> None:
    """Download checkpoint files from R2."""
    local_dir.mkdir(parents=True, exist_ok=True)
    await run(f"rclone copy r2:{bucket}/{r2_prefix}/ {local_dir}/")
    logger.info("Downloaded checkpoint from r2:%s/%s", bucket, r2_prefix)
