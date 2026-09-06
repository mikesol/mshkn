"""Checkpoint object storage via rclone (Cloudflare R2)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mshkn.host.shell import RunFn
from mshkn.host.shell import run as shell_run

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class RcloneObjectStore:
    def __init__(self, bucket: str, *, remote: str = "r2", run: RunFn = shell_run) -> None:
        self._bucket = bucket
        self._remote = remote
        self._run = run

    def _url(self, prefix: str) -> str:
        return f"{self._remote}:{self._bucket}/{prefix}/"

    async def upload_dir(self, local_dir: Path, prefix: str) -> None:
        await self._run(f"rclone copy {local_dir}/ {self._url(prefix)}")
        logger.info("Uploaded %s to %s", local_dir, self._url(prefix))

    async def download_dir(self, prefix: str, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        await self._run(f"rclone copy {self._url(prefix)} {local_dir}/")
        logger.info("Downloaded %s to %s", self._url(prefix), local_dir)

    async def delete_prefix(self, prefix: str) -> None:
        await self._run(f"rclone purge {self._url(prefix)}", check=False)
        logger.info("Deleted %s", self._url(prefix))
