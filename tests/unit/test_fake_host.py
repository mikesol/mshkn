from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mshkn.errors import HostError
from mshkn.host import ExecResult, SnapshotFiles
from mshkn.host.fake import FakeHost
from mshkn.resources import DEFAULT_RESOURCES

if TYPE_CHECKING:
    from pathlib import Path


async def test_blocks_track_volumes_and_fail_next() -> None:
    host = FakeHost()
    blocks = host.blocks
    await blocks.snap(source_volume_id=0, new_volume_id=100)
    await blocks.activate(volume_id=100, name="mshkn-comp-a")
    assert blocks.volumes == {0: None, 100: 0}
    assert "mshkn-comp-a" in blocks.active
    blocks.fail_next("snap")
    with pytest.raises(HostError):
        await blocks.snap(source_volume_id=0, new_volume_id=101)
    await blocks.remove(volume_id=100, name="mshkn-comp-a")
    assert 100 not in blocks.volumes and "mshkn-comp-a" not in blocks.active
    async with blocks.mounted("x") as path:
        assert path.is_dir()


async def test_hypervisor_boots_restores_snapshots_kills(tmp_path: Path) -> None:
    host = FakeHost()
    hv = host.hypervisor
    vm = await hv.boot(
        slot=3, disk_volume_id=100, disk_name="mshkn-comp-a", resources=DEFAULT_RESOURCES
    )
    assert vm.slot == 3 and vm.vm_ip == "172.16.3.2" and vm.tap_device == "tap3"
    assert hv.is_alive(vm.pid)
    files = await hv.snapshot(vm.socket_path, tmp_path / "ckpt")
    assert files.vmstate.exists() and files.memory.exists()
    vm2 = await hv.restore(slot=4, disk_volume_id=101, disk_name="mshkn-comp-b", snapshot=files)
    assert hv.restored == [(101, files)]
    await hv.kill(vm.pid)
    assert not hv.is_alive(vm.pid) and hv.is_alive(vm2.pid)
    await hv.teardown_slot(3)
    assert 3 in hv.torn_down
    template = await hv.build_template(disk_volume_id=0, dest_dir=tmp_path / "tpl")
    assert isinstance(template, SnapshotFiles)


async def test_guest_scripts_and_records() -> None:
    host = FakeHost()
    guest = host.guest
    guest.script["python3 --version"] = ExecResult(0, "Python 3.12.3\n", "")
    r = await guest.exec("172.16.3.2", "python3 --version")
    assert r.stdout.startswith("Python 3")
    assert await guest.exec("172.16.3.2", "sync") == ExecResult(0, "", "")
    assert guest.commands == [("172.16.3.2", "python3 --version"), ("172.16.3.2", "sync")]
    guest.stream_script["ls"] = [("stdout", "a"), ("stdout", "b")]
    items = [i async for i in guest.stream("172.16.3.2", "ls")]
    assert items == [("stdout", "a"), ("stdout", "b"), ("exit", "0")]
    await guest.upload("172.16.3.2", "/tmp/f", b"data")
    assert await guest.download("172.16.3.2", "/tmp/f") == b"data"
    with pytest.raises(FileNotFoundError):
        await guest.download("172.16.3.2", "/nope")
    assert (await guest.metrics("172.16.3.2")).ram_total_mb > 0


async def test_objects_and_proxy_record(tmp_path: Path) -> None:
    host = FakeHost()
    src = tmp_path / "up"
    src.mkdir()
    (src / "vmstate").write_bytes(b"v")
    await host.objects.upload_dir(src, "acct/ckpt")
    dl = tmp_path / "dl"
    await host.objects.download_dir("acct/ckpt", dl)
    assert (dl / "vmstate").read_bytes() == b"v"
    await host.objects.delete_prefix("acct/ckpt")
    assert host.objects.prefixes == {}
    await host.proxy.add_route("comp-a", "172.16.3.2")
    assert host.proxy.routes == {"comp-a": "172.16.3.2"}
    await host.proxy.remove_route("comp-a")
    assert host.proxy.routes == {}
    assert await host.proxy.healthy()
