"""Ingress through HTTP: a sync create honouring `needs`, the retired `uses` field
rejected as unknown, and an async fork by label that runs in the background."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import ExecResult
from mshkn.resources import Resources

if TYPE_CHECKING:
    from .conftest import Flow

STARLARK_CREATE = (
    'def transform(req):\n  return {"action": "create", "needs": {"ram": "1GB", "cores": 2},'
    ' "exec": "echo ing", "self_destruct": True, "label": "ing"}'
)
STARLARK_USES = 'def transform(req):\n  return {"action": "create", "uses": ["python"]}'


async def _rule(flow: Flow, source: str, mode: str) -> str:
    resp = await flow.client.post(
        "/ingress_rules", json={"name": "r", "starlark_source": source, "response_mode": mode}
    )
    assert resp.status_code == 200, resp.text
    rule_id: str = resp.json()["id"]
    return rule_id


async def test_sync_create_honours_needs_and_uses_is_rejected(flow: Flow) -> None:
    flow.host.guest.script["echo ing"] = ExecResult(0, "ing\n", "")
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    rule = await _rule(flow, STARLARK_CREATE, "sync")
    resp = await flow.client.post(f"/ingress/{rule}", headers={})
    assert resp.status_code == 200 and resp.json()["exec_stdout"] == "ing\n"
    assert flow.host.hypervisor.booted[0][1] == Resources(mem_mib=1024, vcpus=2)
    bad = await _rule(flow, STARLARK_USES, "sync")
    resp = await flow.client.post(f"/ingress/{bad}")
    assert resp.status_code == 502 and any("uses" in e for e in resp.json()["detail"]["errors"])
    logs = (await flow.client.get(f"/ingress_rules/{bad}/logs")).json()
    assert logs[0]["status"] == "failed"


async def test_async_fork_by_label_runs_in_the_background(flow: Flow) -> None:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "chain"})
    await flow.client.delete(f"/computers/{cid}")
    rule = await _rule(
        flow,
        'def transform(req):\n  return {"action": "fork", "label": "chain",'
        ' "exec": "true", "self_destruct": True}',
        "async",
    )
    resp = await flow.client.post(f"/ingress/{rule}")
    assert resp.status_code == 202 and resp.json() == {"status": "accepted"}
    await flow.runtime.tasks.drain(timeout=5.0)
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert len(chain) == 2


async def test_sync_bodies_have_the_same_shape_as_the_rest_endpoints(flow: Flow) -> None:
    """Ingress serialises through the REST schemas, so the two agree key for key."""
    flow.host.guest.script["sync"] = ExecResult(0, "", "")

    rest_create = await flow.client.post("/computers", json={"exec": "true"})
    assert rest_create.status_code == 200
    create_rule = await _rule(
        flow, 'def transform(req):\n  return {"action": "create", "exec": "true"}', "sync"
    )
    ing_create = await flow.client.post(f"/ingress/{create_rule}")
    assert ing_create.status_code == 200
    assert set(ing_create.json()) == set(rest_create.json())
    assert ing_create.json()["url"].endswith(".test.dev")

    cid = rest_create.json()["computer_id"]
    ckpt = (await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "shape"})).json()[
        "checkpoint_id"
    ]
    await flow.client.delete(f"/computers/{cid}")
    rest_fork = await flow.client.post(f"/checkpoints/{ckpt}/fork", json={"exec": "true"})
    assert rest_fork.status_code == 200
    fork_rule = await _rule(
        flow,
        'def transform(req):\n  return {"action": "fork",'
        f' "checkpoint_id": "{ckpt}", "exec": "true"}}',
        "sync",
    )
    await flow.client.delete(f"/computers/{rest_fork.json()['computer_id']}")
    ing_fork = await flow.client.post(f"/ingress/{fork_rule}")
    assert ing_fork.status_code == 200
    assert set(ing_fork.json()) == set(rest_fork.json())
    assert ing_fork.json()["checkpoint_id"] == ckpt

    # The forked computer is still running on the label, so this one queues.
    deferred_rule = await _rule(
        flow,
        'def transform(req):\n  return {"action": "fork",'
        f' "checkpoint_id": "{ckpt}", "exec": "true",'
        ' "exclusive": "defer_on_conflict"}',
        "sync",
    )
    queued = await flow.client.post(f"/ingress/{deferred_rule}")
    assert queued.status_code == 200
    assert set(queued.json()) == {"deferred_id", "status"}
    assert queued.json()["status"] == "queued"
