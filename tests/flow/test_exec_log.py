"""The exec output of an ephemeral turn is the agent's only record of it (#58):
GET /computers/{computer_id}/exec_log returns it after the computer is gone, and
an ingress log entry names the computer its action ran on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import ExecResult

if TYPE_CHECKING:
    from .conftest import Flow

EXEC_LOG_KEYS = {
    "computer_id",
    "source_checkpoint_id",
    "created_checkpoint_id",
    "label",
    "command",
    "exit_code",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
    "created_at",
}


async def _rule(flow: Flow, source: str, *, response_mode: str) -> str:
    resp = await flow.client.post(
        "/ingress_rules",
        json={"name": "r", "starlark_source": source, "response_mode": response_mode},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


async def test_exec_log_survives_the_self_destructed_computer(flow: Flow) -> None:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    flow.host.guest.script["python brain.py"] = ExecResult(1, "Called lampas: 502\n", "boom\n")
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    ckpt = (await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "brain"})).json()[
        "checkpoint_id"
    ]
    await flow.client.delete(f"/computers/{cid}")
    fork = await flow.client.post(
        f"/checkpoints/{ckpt}/fork", json={"exec": "python brain.py", "self_destruct": True}
    )
    assert fork.status_code == 200
    body = fork.json()
    gone = await flow.client.get(f"/computers/{body['computer_id']}/status")
    assert gone.status_code == 404, "the computer self-destructed"

    resp = await flow.client.get(f"/computers/{body['computer_id']}/exec_log")
    assert resp.status_code == 200, resp.text
    log = resp.json()
    assert set(log) == EXEC_LOG_KEYS
    assert log["computer_id"] == body["computer_id"]
    assert log["source_checkpoint_id"] == ckpt
    assert log["created_checkpoint_id"] == body["created_checkpoint_id"]
    assert log["label"] == "brain" and log["command"] == "python brain.py"
    assert (log["exit_code"], log["stdout"], log["stderr"]) == (1, "Called lampas: 502\n", "boom\n")
    assert (log["stdout_truncated"], log["stderr_truncated"]) == (False, False)

    # The chain listing names the computer, so the log is reachable from a checkpoint.
    chain = (await flow.client.get("/checkpoints", params={"label": "brain"})).json()
    created = next(c for c in chain if c["id"] == body["created_checkpoint_id"])
    assert created["computer_id"] == body["computer_id"]


async def test_exec_log_is_404_for_other_accounts_and_unknown_computers(flow: Flow) -> None:
    flow.host.guest.script["true"] = ExecResult(0, "", "")
    cid = (await flow.client.post("/computers", json={"exec": "true"})).json()["computer_id"]
    assert (await flow.client.get(f"/computers/{cid}/exec_log")).status_code == 200
    assert (await flow.other_client.get(f"/computers/{cid}/exec_log")).status_code == 404
    assert (await flow.client.get("/computers/comp-nope/exec_log")).status_code == 404
    plain = (await flow.client.post("/computers", json={})).json()["computer_id"]
    assert (await flow.client.get(f"/computers/{plain}/exec_log")).status_code == 404, (
        "a create without exec has nothing to record"
    )


async def test_async_ingress_log_names_the_computer_and_its_output_is_kept(flow: Flow) -> None:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    flow.host.guest.script["echo hook"] = ExecResult(0, "hook\n", "")
    rule = await _rule(
        flow,
        'def transform(req):\n  return {"action": "create", "exec": "echo hook",'
        ' "self_destruct": True, "label": "hook"}',
        response_mode="async",
    )
    resp = await flow.client.post(f"/ingress/{rule}")
    assert resp.status_code == 202
    await flow.runtime.tasks.drain(timeout=5.0)
    logs = (await flow.client.get(f"/ingress_rules/{rule}/logs")).json()
    assert len(logs) == 1, "one trigger, one log row, updated in place when the action finishes"
    assert logs[0]["status"] == "completed" and logs[0]["computer_id"].startswith("comp-")
    exec_log = (await flow.client.get(f"/computers/{logs[0]['computer_id']}/exec_log")).json()
    assert exec_log["stdout"] == "hook\n" and exec_log["label"] == "hook"
    assert exec_log["created_checkpoint_id"].startswith("ckpt-")


async def test_sync_ingress_log_names_the_computer(flow: Flow) -> None:
    flow.host.guest.script["true"] = ExecResult(0, "", "")
    rule = await _rule(
        flow,
        'def transform(req):\n  return {"action": "create", "exec": "true"}',
        response_mode="sync",
    )
    resp = await flow.client.post(f"/ingress/{rule}")
    assert resp.status_code == 200
    logs = (await flow.client.get(f"/ingress_rules/{rule}/logs")).json()
    assert logs[0]["status"] == "completed"
    assert logs[0]["computer_id"] == resp.json()["computer_id"]


async def test_async_ingress_failure_marks_the_same_log_row_failed(flow: Flow) -> None:
    rule = await _rule(
        flow,
        'def transform(req):\n  return {"action": "fork", "checkpoint_id": "ckpt-missing"}',
        response_mode="async",
    )
    resp = await flow.client.post(f"/ingress/{rule}")
    assert resp.status_code == 202
    await flow.runtime.tasks.drain(timeout=5.0)
    logs = (await flow.client.get(f"/ingress_rules/{rule}/logs")).json()
    assert len(logs) == 1
    assert logs[0]["status"] == "failed" and logs[0]["computer_id"] is None
    assert "not found" in logs[0]["error_message"].lower()
