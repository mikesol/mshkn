from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import get_checkpoint, get_computer
from mshkn.host import ExecResult
from mshkn.models import ComputerStatus

if TYPE_CHECKING:
    from .conftest import Flow


async def _exec(flow: Flow, computer_id: str, command: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    current = "stdout"
    url = f"/computers/{computer_id}/exec"
    async with flow.client.stream("POST", url, json={"command": command}) as resp:
        assert resp.status_code == 200
        async for raw in resp.aiter_lines():
            line = raw.strip()
            if line.startswith("event: "):
                current = line[7:]
            elif line.startswith("data: "):
                events.append((current, line[6:]))
    return events


async def test_create_exec_checkpoint_fork_destroy(flow: Flow) -> None:
    host = flow.host

    # create: a CoW volume off the base image, a booted VM on a slot, a proxy route, a DB row
    resp = await flow.client.post("/computers", json={})
    assert resp.status_code == 200, resp.text
    cid = resp.json()["computer_id"]
    row = await get_computer(flow.runtime.db, cid)
    assert row is not None and row.status is ComputerStatus.RUNNING
    assert host.blocks.volumes[row.thin_volume_id] == 0
    assert host.hypervisor.is_alive(row.firecracker_pid or -1)
    assert host.proxy.routes == {cid: row.vm_ip}
    assert host.guest.warmed == [row.vm_ip]
    # first bare create builds the template once, then restores from it
    assert len(host.hypervisor.restored) == 1

    # exec streams through the guest
    host.guest.stream_script["echo hi"] = [("stdout", "hi")]
    assert await _exec(flow, cid, "echo hi") == [("stdout", "hi"), ("exit", "0")]

    # checkpoint: sync, snapshot files, evict, frozen disk, row with no parent
    host.guest.script["sync"] = ExecResult(0, "", "")
    resp = await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "base"})
    assert resp.status_code == 200, resp.text
    ckpt_id = resp.json()["checkpoint_id"]
    ckpt = await get_checkpoint(flow.runtime.db, ckpt_id)
    assert ckpt is not None and ckpt.parent_id is None and ckpt.label == "base"
    assert any(cmd == "sync" for _, cmd in host.guest.commands)
    assert host.guest.evicted == [row.vm_ip]
    assert host.blocks.volumes[ckpt.thin_volume_id or -1] == row.thin_volume_id
    assert host.blocks.active[f"mshkn-ckpt-{ckpt_id}"] == ckpt.thin_volume_id
    assert (flow.runtime.config.checkpoint_local_dir / ckpt_id / "vmstate").exists()
    await flow.runtime.tasks.wait(f"upload:{ckpt_id}")
    assert f"acct-1/{ckpt_id}" in host.objects.prefixes

    # fork: a new VM restored from the checkpoint's disk and snapshot files
    resp = await flow.client.post(f"/checkpoints/{ckpt_id}/fork", json={})
    assert resp.status_code == 200, resp.text
    fork_id = resp.json()["computer_id"]
    fork_row = await get_computer(flow.runtime.db, fork_id)
    assert fork_row is not None and fork_row.source_checkpoint_id == ckpt_id
    assert host.blocks.volumes[fork_row.thin_volume_id] == ckpt.thin_volume_id
    assert host.hypervisor.restored[-1][0] == fork_row.thin_volume_id
    assert fork_row.vm_ip != row.vm_ip
    assert host.guest.warmed == [row.vm_ip, fork_row.vm_ip]

    # destroy both: no VMs, no routes, volumes gone, rows destroyed
    for target in (cid, fork_id):
        resp = await flow.client.delete(f"/computers/{target}")
        assert resp.status_code == 200, resp.text
    assert host.hypervisor.alive == {}
    assert host.proxy.routes == {}
    assert row.thin_volume_id not in host.blocks.volumes
    assert fork_row.thin_volume_id not in host.blocks.volumes
    assert ckpt.thin_volume_id in host.blocks.volumes  # checkpoints persist
    assert host.guest.evicted == [row.vm_ip, row.vm_ip, fork_row.vm_ip]
    for target in (cid, fork_id):
        r = await get_computer(flow.runtime.db, target)
        assert r is not None and r.status is ComputerStatus.DESTROYED
    expected_slots = sorted(int(r.tap_device[3:]) for r in (row, fork_row))
    assert sorted(host.hypervisor.torn_down) == expected_slots


async def test_unknown_recipe_is_404_and_leaves_no_host_state(flow: Flow) -> None:
    resp = await flow.client.post("/computers", json={"recipe_id": "rcp-nope"})
    assert resp.status_code == 404
    assert flow.host.hypervisor.alive == {}
    assert flow.host.blocks.volumes == {0: None}
