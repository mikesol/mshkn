from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mshkn.host.dmthin import DmThinBlockStore, parse_pool_status
from mshkn.host.shell import ShellError
from tests.support import ShellRecorder

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


class SleepSpy:
    """Stands in for asyncio.sleep: records the backoff a retry loop asks for.

    Recording rather than discarding is the point. A no-op patch leaves a retry
    loop that spins with no backoff at all indistinguishable from one that waits.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float, result: object = None) -> object:
        self.delays.append(delay)
        return result


async def test_snap_retries_after_orphaned_volume() -> None:
    first = ShellError("dmsetup message", 1, "device-mapper: message ioctl failed: File exists")
    run = ShellRecorder()
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
    run = ShellRecorder()
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    await store.activate(volume_id=7, name="mshkn-comp-x")
    await store.remove(volume_id=7, name="mshkn-comp-x")
    cmds = [c for c, _ in run.calls]
    assert cmds[0] == (
        "dmsetup create mshkn-comp-x --table '0 16777216 thin /dev/mapper/mshkn-pool 7'"
    )
    assert cmds[1] == "dmsetup remove mshkn-comp-x"
    assert cmds[2] == "dmsetup message mshkn-pool 0 'delete 7'"


async def test_remove_never_raises_when_dmsetup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove() is best-effort: callers rely on it never raising and do not guard it.

    Both the unmap and the pool delete can fail (device still held, volume
    already gone). Each is logged and the volume is left behind.
    """
    monkeypatch.setattr("mshkn.host.dmthin._REMOVE_RETRIES", 1)
    run = ShellRecorder(
        responses={
            "dmsetup remove": ShellError("dmsetup remove", 1, "device busy"),
            "delete 7": ShellError("dmsetup message", 1, "no such device"),
        }
    )
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    await store.remove(volume_id=7, name="mshkn-comp-x")
    assert [c for c, _ in run.calls] == [
        "dmsetup remove mshkn-comp-x",
        "dmsetup message mshkn-pool 0 'delete 7'",
    ]


async def test_mounted_mounts_and_unmounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / "mnt"))  # noqa: ARG005
    (tmp_path / "mnt").mkdir()
    run = ShellRecorder()
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    async with store.mounted("mshkn-ckpt-a", readonly=True) as path:
        assert path == tmp_path / "mnt"
        assert run.calls[-1][0] == f"mount -o ro /dev/mapper/mshkn-ckpt-a {path}"
    assert run.calls[-1][0] == f"umount {tmp_path / 'mnt'}"
    assert not (tmp_path / "mnt").exists()


async def test_mounted_removes_its_directory_when_the_mount_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed mount must not leave the scratch mount point behind."""
    mount_point = tmp_path / "mnt"
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(mount_point))  # noqa: ARG005
    mount_point.mkdir()
    failure = ShellError("mount", 32, "mount: wrong fs type")
    run = ShellRecorder(responses={"mount /dev/mapper": failure})
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)

    with pytest.raises(ShellError):
        async with store.mounted("mshkn-ckpt-a"):
            raise AssertionError("body must not run when the mount fails")

    assert not mount_point.exists()
    # The mount failed, so nothing was unmounted.
    assert not any(c.startswith("umount") for c, _ in run.calls)


async def test_max_volume_id_parses_dmsetup_table() -> None:
    table = "mshkn-base: 0 16777216 thin 252:0 0\nmshkn-comp-a: 0 16777216 thin 252:0 745\n"
    run = ShellRecorder(responses={"dmsetup table": table})
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    assert await store.max_volume_id() == 745


async def test_usage_uses_dmsetup_status() -> None:
    run = ShellRecorder(responses={"dmsetup status": STATUS})
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    usage = await store.usage()
    assert usage.data_used_ratio == pytest.approx(14044 / 409600)


async def test_activate_replaces_a_stale_device() -> None:
    stale = ShellError("dmsetup create", 1, "File exists")
    run = ShellRecorder(fail_first={"dmsetup create mshkn-x": stale})
    await DmThinBlockStore("mshkn-pool", 16, run=run).activate(volume_id=5, name="mshkn-x")
    assert [c for c, _ in run.calls] == [
        "dmsetup create mshkn-x --table '0 16 thin /dev/mapper/mshkn-pool 5'",
        "dmsetup remove mshkn-x",
        "dmsetup create mshkn-x --table '0 16 thin /dev/mapper/mshkn-pool 5'",
    ]
    assert run.calls[1][1] is False


async def test_activate_raises_other_errors_untouched() -> None:
    run = ShellRecorder(
        responses={"dmsetup create": ShellError("dmsetup create", 1, "No such device")}
    )
    with pytest.raises(ShellError, match="No such device"):
        await DmThinBlockStore("mshkn-pool", 16, run=run).activate(volume_id=5, name="mshkn-x")


async def test_snap_raises_other_errors_untouched() -> None:
    run = ShellRecorder(responses={"create_snap": ShellError("dmsetup message", 1, "No space")})
    with pytest.raises(ShellError, match="No space"):
        await DmThinBlockStore("mshkn-pool", 16, run=run).snap(source_volume_id=0, new_volume_id=9)


async def test_remove_retries_the_unmap_five_times_then_still_deletes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sleeps = SleepSpy()
    monkeypatch.setattr(asyncio, "sleep", sleeps)
    run = ShellRecorder(
        responses={"dmsetup remove mshkn-x": ShellError("dmsetup remove", 1, "busy")}
    )
    await DmThinBlockStore("mshkn-pool", 16, run=run).remove(volume_id=5, name="mshkn-x")
    cmds = [c for c, _ in run.calls]
    assert cmds.count("dmsetup remove mshkn-x") == 5
    # Five attempts, so four waits: the loop backs off between them, never after the last.
    assert sleeps.delays == [0.5, 0.5, 0.5, 0.5]
    assert cmds[-1] == "dmsetup message mshkn-pool 0 'delete 5'"
    assert any("failed after 5 attempts" in r.getMessage() for r in caplog.records)


async def test_deactivate_and_mkfs_commands() -> None:
    run = ShellRecorder()
    store = DmThinBlockStore("mshkn-pool", 16, run=run)
    await store.deactivate("mshkn-x")
    await store.mkfs("mshkn-x")
    assert [c for c, _ in run.calls] == [
        "dmsetup remove mshkn-x",
        "mkfs.ext4 -F /dev/mapper/mshkn-x",
    ]


async def test_mounted_retries_umount_then_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sleeps = SleepSpy()
    monkeypatch.setattr(asyncio, "sleep", sleeps)
    run = ShellRecorder(responses={"umount": ShellError("umount", 32, "target is busy")})
    store = DmThinBlockStore("mshkn-pool", 16, run=run)
    async with store.mounted("mshkn-x", readonly=True) as path:
        assert path.is_dir()
        assert run.calls[0][0] == f"mount -o ro /dev/mapper/mshkn-x {path}"
    assert [c for c, _ in run.calls].count(f"umount {path}") == 3
    assert sleeps.delays == [0.5, 0.5]
    assert any(
        "umount" in r.getMessage() and "failed after 3 attempts" in r.getMessage()
        for r in caplog.records
    )
    assert not path.exists()


async def test_max_volume_id_is_none_when_dmsetup_fails_and_skips_garbage() -> None:
    failing = ShellRecorder(responses={"dmsetup table": ShellError("dmsetup table", 1, "no pool")})
    assert await DmThinBlockStore("mshkn-pool", 16, run=failing).max_volume_id() is None
    table = (
        "mshkn-a: 0 16 thin 252:0 100\n"
        "weird line\n"
        "mshkn-b: 0 16 thin 252:0 notanumber\n"
        "mshkn-c: 0 16 thin 252:0 130\n"
    )
    store = DmThinBlockStore(
        "mshkn-pool", 16, run=ShellRecorder(responses={"dmsetup table": table})
    )
    assert await store.max_volume_id() == 130
