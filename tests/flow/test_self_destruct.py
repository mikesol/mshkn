"""exec + self_destruct on both paths: the computer is gone, and the checkpoint and
the callback carry the run's result. A create labels the checkpoint from the request;
a fork inherits the label of the checkpoint it came from."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import ExecResult

if TYPE_CHECKING:
    from .conftest import Flow


async def test_create_with_exec_self_destruct_and_callback(flow: Flow) -> None:
    flow.host.guest.script["echo out"] = ExecResult(0, "out\n", "err\n")
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    resp = await flow.client.post(
        "/computers",
        json={
            "exec": "echo out",
            "self_destruct": True,
            "label": "sd",
            "callback_url": "http://receiver/cb",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["exec_exit_code"] == 0 and body["created_checkpoint_id"].startswith("ckpt-")
    status = await flow.client.get(f"/computers/{body['computer_id']}/status")
    assert status.status_code == 404
    await flow.runtime.tasks.drain(timeout=2.0)
    assert flow.received == [
        {
            "computer_id": body["computer_id"],
            "checkpoint_id": None,
            "label": "sd",
            "exec_exit_code": 0,
            "exec_stdout": "out\n",
            "exec_stderr": "err\n",
            "created_checkpoint_id": body["created_checkpoint_id"],
        }
    ]
    listed = (await flow.client.get("/checkpoints", params={"label": "sd"})).json()
    assert listed[0]["checkpoint_id"] == body["created_checkpoint_id"]
    assert "manifest_hash" not in listed[0] and "recipe_id" in listed[0]


async def test_fork_self_destruct_inherits_the_label_and_calls_back(flow: Flow) -> None:
    host = flow.host
    host.guest.script["sync"] = ExecResult(0, "", "")
    host.guest.script["echo forked"] = ExecResult(0, "forked\n", "")
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    ckpt = (await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "chain"})).json()[
        "checkpoint_id"
    ]
    resp = await flow.client.post(
        f"/checkpoints/{ckpt}/fork",
        json={
            "exec": "echo forked",
            "self_destruct": True,
            "callback_url": "http://receiver/cb",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created_checkpoint_id"] and body["checkpoint_id"] == ckpt
    await flow.runtime.tasks.drain(timeout=2.0)
    assert flow.received == [
        {
            "computer_id": body["computer_id"],
            "checkpoint_id": ckpt,
            "label": "chain",
            "exec_exit_code": 0,
            "exec_stdout": "forked\n",
            "exec_stderr": "",
            "created_checkpoint_id": body["created_checkpoint_id"],
        }
    ]
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert {c["id"] for c in chain} == {ckpt, body["created_checkpoint_id"]}
    created = next(c for c in chain if c["id"] == body["created_checkpoint_id"])
    assert created["parent_id"] == ckpt
