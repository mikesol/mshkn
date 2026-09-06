"""Failure paths through HTTP: a host failure leaks nothing, domain errors keep
their status codes, and the active gauge tracks the database."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mshkn.db import count_active_computers, get_computer
from mshkn.observability.metrics import computers_active

if TYPE_CHECKING:
    from .conftest import Flow


async def test_boot_failure_after_snap_is_502_and_leaks_nothing(flow: Flow) -> None:
    host = flow.host
    host.hypervisor.fail_next("boot")
    resp = await flow.client.post("/computers", json={"needs": {"ram": "512MB"}})
    assert resp.status_code == 502 and resp.json() == {"detail": "host operation failed"}
    assert host.blocks.volumes == {0: None} and host.hypervisor.alive == {}
    assert flow.runtime.allocator.free_slots == frozenset({1})
    assert computers_active._value.get() == 0
    ok = await flow.client.post("/computers", json={})
    assert ok.status_code == 200


async def test_domain_errors_keep_their_codes(flow: Flow) -> None:
    assert (await flow.client.post("/computers", json={"recipe_id": "rcp-nope"})).status_code == 404
    bad_needs = await flow.client.post("/computers", json={"needs": {"ram": "lots"}})
    assert bad_needs.status_code == 422
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    assert (
        await flow.client.get(f"/computers/{cid}/download", params={"path": "/nope"})
    ).status_code == 404
    await flow.client.delete(f"/computers/{cid}")
    resp = await flow.client.post(f"/computers/{cid}/exec/bg", json={"command": "true"})
    assert resp.status_code == 400 and "destroyed" in resp.json()["detail"]
    assert (await flow.client.delete(f"/computers/{cid}")).status_code == 404


async def test_abandon_after_the_insert_refreshes_the_active_gauge(flow: Flow) -> None:
    """A create that dies after its row is inserted must leave the gauge on the db count.

    The stale gauge is only observable if something refreshes it between the
    doomed insert and the abandon, so a second create is let through in exactly
    that window: it sets the gauge to 2 (itself plus the doomed row), and the
    abandon that follows has to bring it back to 1.
    """
    reached = asyncio.Event()
    proceed = asyncio.Event()
    add_route = flow.host.proxy.add_route

    async def gated(computer_id: str, vm_ip: str) -> None:
        if not reached.is_set():
            reached.set()
            await proceed.wait()
            flow.host.proxy.fail_next("add_route")
        await add_route(computer_id, vm_ip)

    flow.host.proxy.add_route = gated  # type: ignore[method-assign]
    doomed = asyncio.create_task(flow.client.post("/computers", json={}))
    await asyncio.wait_for(reached.wait(), timeout=5.0)
    live = await flow.client.post("/computers", json={})
    assert live.status_code == 200
    assert computers_active._value.get() == 2  # the live one plus the doomed row
    proceed.set()
    assert (await doomed).status_code == 502
    assert await count_active_computers(flow.runtime.db) == 1
    assert computers_active._value.get() == 1


async def test_active_gauge_equals_the_db_count(flow: Flow) -> None:
    ids = [(await flow.client.post("/computers", json={})).json()["computer_id"] for _ in range(3)]
    assert computers_active._value.get() == 3
    await flow.client.delete(f"/computers/{ids[0]}")
    assert computers_active._value.get() == 2
    row = await get_computer(flow.runtime.db, ids[1])
    assert row is not None
    flow.host.hypervisor.alive.pop(row.firecracker_pid or -1)
    await flow.runtime.reaper.cycle()
    assert computers_active._value.get() == 1
