"""Create with exec + self_destruct: the computer is gone, the checkpoint and the
callback carry the run's result."""

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
