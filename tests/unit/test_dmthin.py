from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mshkn.host.dmthin import DmThinBlockStore, parse_pool_status
from mshkn.host.shell import ShellError

if TYPE_CHECKING:
    from pathlib import Path

STATUS = (
    "0 209715200 thin-pool 0 4211/65536 14044/409600 - rw discard_passdown queue_if_no_space - 1024"
)


def test_parse_pool_status() -> None:
    usage = parse_pool_status(STATUS)
    assert usage.metadata_used_ratio == pytest.approx(4211 / 65536)
    assert usage.data_used_ratio == pytest.approx(14044 / 409600)


def test_parse_pool_status_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="thin-pool"):
        parse_pool_status("0 100 linear 8:1 0")


class Recorder:
    def __init__(self, responses: dict[str, str | Exception] | None = None) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.responses = responses or {}

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append((cmd, check))
        for key, resp in self.responses.items():
            if key in cmd:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return ""


async def test_snap_retries_after_orphaned_volume() -> None:
    first = ShellError("dmsetup message", 1, "device-mapper: message ioctl failed: File exists")
    run = Recorder()
    state = {"n": 0}

    async def flaky(cmd: str, check: bool = True) -> str:
        run.calls.append((cmd, check))
        if "create_snap" in cmd:
            state["n"] += 1
            if state["n"] == 1:
                raise first
        return ""

    store = DmThinBlockStore("mshkn-pool", 16777216, run=flaky)
    await store.snap(source_volume_id=0, new_volume_id=7)
    cmds = [c for c, _ in run.calls]
    assert cmds == [
        "dmsetup message mshkn-pool 0 'create_snap 7 0'",
        "dmsetup message mshkn-pool 0 'delete 7'",
        "dmsetup message mshkn-pool 0 'create_snap 7 0'",
    ]


async def test_activate_and_remove_issue_expected_commands() -> None:
    run = Recorder()
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    await store.activate(volume_id=7, name="mshkn-comp-x")
    await store.remove(volume_id=7, name="mshkn-comp-x")
    cmds = [c for c, _ in run.calls]
    assert cmds[0] == (
        "dmsetup create mshkn-comp-x --table '0 16777216 thin /dev/mapper/mshkn-pool 7'"
    )
    assert cmds[1] == "dmsetup remove mshkn-comp-x"
    assert cmds[2] == "dmsetup message mshkn-pool 0 'delete 7'"


async def test_mounted_mounts_and_unmounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / "mnt"))  # noqa: ARG005
    (tmp_path / "mnt").mkdir()
    run = Recorder()
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    async with store.mounted("mshkn-ckpt-a", readonly=True) as path:
        assert path == tmp_path / "mnt"
        assert run.calls[-1][0] == f"mount -o ro /dev/mapper/mshkn-ckpt-a {path}"
    assert run.calls[-1][0] == f"umount {tmp_path / 'mnt'}"
    assert not (tmp_path / "mnt").exists()


async def test_max_volume_id_parses_dmsetup_table() -> None:
    table = "mshkn-base: 0 16777216 thin 252:0 0\nmshkn-comp-a: 0 16777216 thin 252:0 745\n"
    run = Recorder({"dmsetup table": table})
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    assert await store.max_volume_id() == 745


async def test_usage_uses_dmsetup_status() -> None:
    run = Recorder({"dmsetup status": STATUS})
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    usage = await store.usage()
    assert usage.data_used_ratio == pytest.approx(14044 / 409600)
