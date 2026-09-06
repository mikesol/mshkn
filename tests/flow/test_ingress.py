"""Ingress over HTTP, end to end: how a live request becomes the dict Starlark sees,
the per-rule body limit, the rule CRUD lifecycle, every trigger response shape and its
parity with the REST endpoints, the actions a transform can ask for, and the effect a
triggered action has on the active-computer gauge."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import ExecResult
from mshkn.observability.metrics import computers_active
from mshkn.resources import Resources

if TYPE_CHECKING:
    from .conftest import Flow

STARLARK_CREATE = (
    'def transform(req):\n  return {"action": "create", "needs": {"ram": "1GB", "cores": 2},'
    ' "exec": "echo ing", "self_destruct": True, "label": "ing"}'
)
STARLARK_USES = 'def transform(req):\n  return {"action": "create", "uses": ["python"]}'


ECHO = (
    "def transform(req):\n"
    '  q = req["query_params"].get("who", "nobody")\n'
    '  j = req["body_json"]["n"] if req["body_json"] else 0\n'
    '  f = req["body_form"]["k"] if req["body_form"] else ""\n'
    '  return {"action": "create", "exec": "echo %s %d %s %s"'
    ' % (q, j, f, req["method"])}'
)
NO_ACTION = "def transform(req):\n  return None"


async def _rule(flow: Flow, source: str, **fields: object) -> str:
    body = {"name": "r", "starlark_source": source, **fields}
    resp = await flow.client.post("/ingress_rules", json=body)
    assert resp.status_code == 200, resp.text
    rule_id: str = resp.json()["id"]
    return rule_id


async def test_trigger_parses_query_json_and_form_bodies(flow: Flow) -> None:
    rule = await _rule(flow, ECHO, response_mode="sync")
    for cmd in (
        "echo alice 7  POST",
        "echo bob 0 v PUT",
        "echo carol 0  GET",
        "echo nobody 0  POST",
    ):
        flow.host.guest.script[cmd] = ExecResult(0, cmd + "\n", "")
    r1 = await flow.client.post(f"/ingress/{rule}", params={"who": "alice"}, json={"n": 7})
    assert r1.status_code == 200 and r1.json()["exec_stdout"] == "echo alice 7  POST\n"
    r2 = await flow.client.put(f"/ingress/{rule}", params={"who": "bob"}, data={"k": "v"})
    assert r2.status_code == 200 and r2.json()["exec_stdout"] == "echo bob 0 v PUT\n"
    r3 = await flow.client.get(f"/ingress/{rule}", params={"who": "carol"})
    assert r3.status_code == 200 and r3.json()["exec_stdout"] == "echo carol 0  GET\n"
    # invalid JSON under a JSON content type is tolerated: body_json stays None
    bad = await flow.client.post(
        f"/ingress/{rule}",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert bad.status_code == 200 and bad.json()["exec_stdout"] == "echo nobody 0  POST\n"


async def test_oversized_bodies_are_413_by_header_and_by_stream(flow: Flow) -> None:
    rule = await _rule(flow, NO_ACTION, max_body_bytes=1024)
    assert (await flow.client.post(f"/ingress/{rule}", content=b"x" * 2048)).status_code == 413
    # an over-declared length is refused before the body is read: only the
    # declared-length branch can reject this one, since the body itself fits
    over = await flow.client.post(
        f"/ingress/{rule}", content=b"z" * 8, headers={"content-length": "99999"}
    )
    assert over.status_code == 413
    # a lying Content-Length is caught while streaming: only the running total
    # can reject this one, since the declared length fits
    resp = await flow.client.post(
        f"/ingress/{rule}", content=b"y" * 2048, headers={"content-length": "10"}
    )
    assert resp.status_code == 413
    # the limit is per rule, so a body under it still runs
    assert (await flow.client.post(f"/ingress/{rule}", content=b"z" * 512)).status_code == 204


async def test_rule_crud_update_disable_rotate_test_and_logs(flow: Flow) -> None:
    rule = await _rule(flow, NO_ACTION)
    detail = (await flow.client.get(f"/ingress_rules/{rule}")).json()
    assert detail["starlark_source"] == NO_ACTION
    assert detail["ingress_url"] == f"https://test.dev/ingress/{rule}"
    upd = await flow.client.put(
        f"/ingress_rules/{rule}", json={"name": "renamed", "rate_limit_rpm": 5, "enabled": False}
    )
    assert upd.status_code == 200
    assert upd.json()["name"] == "renamed" and upd.json()["enabled"] is False
    assert (await flow.client.post(f"/ingress/{rule}")).status_code == 404, (
        "a disabled rule is as good as absent"
    )
    bad = await flow.client.put(
        f"/ingress_rules/{rule}", json={"starlark_source": "def nope(): pass"}
    )
    assert bad.status_code == 422 and "starlark_errors" in bad.json()["detail"]
    assert (await flow.client.get(f"/ingress_rules/{rule}")).json()["starlark_source"] == NO_ACTION
    await flow.client.put(f"/ingress_rules/{rule}", json={"enabled": True})
    assert (await flow.client.post(f"/ingress/{rule}")).status_code == 204
    rotated = (await flow.client.post(f"/ingress_rules/{rule}/rotate")).json()["id"]
    assert rotated != rule
    assert (await flow.client.get(f"/ingress_rules/{rule}")).status_code == 404
    test = await flow.client.post(
        f"/ingress_rules/{rotated}/test",
        json={
            "method": "POST",
            "path": "/",
            "body": '{"a": 1}',
            "headers": {"content-type": "application/json"},
        },
    )
    assert test.status_code == 200
    assert set(test.json()) == {"starlark_result", "validation_errors", "execution_time_ms"}
    assert test.json()["starlark_result"] is None and test.json()["validation_errors"] == []
    assert test.json()["execution_time_ms"] >= 0
    assert (await flow.client.post(f"/ingress/{rotated}")).status_code == 204
    logs = (await flow.client.get(f"/ingress_rules/{rotated}/logs")).json()
    assert [entry["status"] for entry in logs] == ["completed", "completed"], (
        "the dry run is not logged, and the log written before the rotation is still"
        " reachable through the new public id"
    )
    assert (await flow.client.delete(f"/ingress_rules/{rotated}")).status_code == 204
    assert (await flow.client.get(f"/ingress_rules/{rotated}")).status_code == 404


async def test_starlark_runtime_error_is_502_and_logged_failed(flow: Flow) -> None:
    rule = await _rule(flow, 'def transform(req):\n  return req["x"]["y"]')
    resp = await flow.client.post(f"/ingress/{rule}")
    assert resp.status_code == 502 and "Starlark execution error" in resp.json()["detail"]
    logs = (await flow.client.get(f"/ingress_rules/{rule}/logs")).json()
    assert logs[0]["status"] == "failed" and logs[0]["error_message"]


async def test_sync_fork_deferred_and_async_response_shapes(flow: Flow) -> None:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    ckpt = (await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "ing"})).json()[
        "checkpoint_id"
    ]
    await flow.client.delete(f"/computers/{cid}")
    rule = await _rule(
        flow,
        'def transform(req):\n  return {"action": "fork", "checkpoint_id": "'
        + ckpt
        + '", "exec": "true", "exclusive": "defer_on_conflict"}',
        response_mode="sync",
    )
    first = await flow.client.post(f"/ingress/{rule}")
    assert first.status_code == 200
    assert set(first.json()) == {
        "computer_id",
        "checkpoint_id",
        "exec_exit_code",
        "exec_stdout",
        "exec_stderr",
        "created_checkpoint_id",
    }
    assert first.json()["checkpoint_id"] == ckpt and first.json()["created_checkpoint_id"] is None
    second = await flow.client.post(f"/ingress/{rule}")
    assert second.status_code == 200 and second.json()["status"] == "queued"
    assert set(second.json()) == {"deferred_id", "status"}
    assert second.json()["deferred_id"].startswith("def-")
    # the async shape: accepted up front, the action runs in the background
    arule = await _rule(
        flow, 'def transform(req):\n  return {"action": "create"}', response_mode="async"
    )
    accepted = await flow.client.post(f"/ingress/{arule}")
    assert accepted.status_code == 202 and accepted.json() == {"status": "accepted"}
    await flow.runtime.tasks.drain(timeout=5.0)
    assert await flow.runtime.computers.active_count_total() == 2, (
        "the sync fork and the async create both left a computer running"
    )


async def test_ingress_changes_move_the_active_gauge(flow: Flow) -> None:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    flow.host.guest.script["true"] = ExecResult(0, "", "")
    keep = await _rule(
        flow, 'def transform(req):\n  return {"action": "create"}', response_mode="sync"
    )
    ephemeral = await _rule(
        flow,
        'def transform(req):\n  return {"action": "create", "exec": "true", "self_destruct": True}',
        response_mode="sync",
    )
    await flow.client.post(f"/ingress/{keep}")
    assert computers_active._value.get() == 1
    await flow.client.post(f"/ingress/{ephemeral}")
    assert computers_active._value.get() == 1, "the ephemeral computer was destroyed"
    assert await flow.runtime.computers.active_count_total() == 1


async def test_sync_create_honours_needs_and_uses_is_rejected(flow: Flow) -> None:
    flow.host.guest.script["echo ing"] = ExecResult(0, "ing\n", "")
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    rule = await _rule(flow, STARLARK_CREATE, response_mode="sync")
    resp = await flow.client.post(f"/ingress/{rule}", headers={})
    assert resp.status_code == 200 and resp.json()["exec_stdout"] == "ing\n"
    assert flow.host.hypervisor.booted[0][1] == Resources(mem_mib=1024, vcpus=2)
    bad = await _rule(flow, STARLARK_USES, response_mode="sync")
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
        response_mode="async",
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
        flow,
        'def transform(req):\n  return {"action": "create", "exec": "true"}',
        response_mode="sync",
    )
    ing_create = await flow.client.post(f"/ingress/{create_rule}")
    assert ing_create.status_code == 200
    assert set(ing_create.json()) == set(rest_create.json())
    # Enumerated, the way the fork shape is above: agreeing with each other is
    # not the same as agreeing with CreateResponse.
    assert set(rest_create.json()) == {
        "computer_id",
        "url",
        "recipe_id",
        "exec_exit_code",
        "exec_stdout",
        "exec_stderr",
        "created_checkpoint_id",
    }
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
        response_mode="sync",
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
        response_mode="sync",
    )
    queued = await flow.client.post(f"/ingress/{deferred_rule}")
    assert queued.status_code == 200
    assert set(queued.json()) == {"deferred_id", "status"}
    assert queued.json()["status"] == "queued"
