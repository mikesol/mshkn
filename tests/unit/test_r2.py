from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host.r2 import RcloneObjectStore

if TYPE_CHECKING:
    from pathlib import Path


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append((cmd, check))
        return ""


async def test_upload_download_delete_commands(tmp_path: Path) -> None:
    run = Recorder()
    store = RcloneObjectStore("mshkn-checkpoints", run=run)
    await store.upload_dir(tmp_path / "ckpt-1", "acct-1/ckpt-1")
    await store.download_dir("acct-1/ckpt-1", tmp_path / "dl")
    await store.delete_prefix("acct-1/ckpt-1")
    assert run.calls == [
        (f"rclone copy {tmp_path / 'ckpt-1'}/ r2:mshkn-checkpoints/acct-1/ckpt-1/", True),
        (f"rclone copy r2:mshkn-checkpoints/acct-1/ckpt-1/ {tmp_path / 'dl'}/", True),
        ("rclone purge r2:mshkn-checkpoints/acct-1/ckpt-1/", False),
    ]
    assert (tmp_path / "dl").is_dir()


async def test_custom_remote_name(tmp_path: Path) -> None:
    run = Recorder()
    store = RcloneObjectStore("bucket-x", remote="other-remote", run=run)
    await store.upload_dir(tmp_path / "src", "p")
    assert run.calls[0][0] == f"rclone copy {tmp_path / 'src'}/ other-remote:bucket-x/p/"
