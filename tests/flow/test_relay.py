"""The relay end to end over the fake host (#110): a scoped key's job is called,
stored, and delivered by a fork of its chain; a busy chain defers the wake-up;
a failed upstream is delivered too; the guard and the scope hold over HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from httpx import ASGITransport

from mshkn.host import ExecResult

if TYPE_CHECKING:
    from .conftest import Flow

SCOPE = {
    "relay": {
        "targets": ["http://model/"],
        "deliver": {"label": "chain", "exec": "echo woke"},
    }
}


def _model(answer: dict[str, Any]) -> ASGITransport:
    app = FastAPI()

    @app.post("/v1/messages")
    async def messages(body: dict[str, Any]) -> dict[str, Any]:
        return {"echo": body, **answer}

    return ASGITransport(app=app)


async def _chain(flow: Flow, label: str) -> str:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    base = (await flow.client.post("/computers", json={})).json()["computer_id"]
    ckpt = (await flow.client.post(f"/computers/{base}/checkpoint", json={"label": label})).json()[
        "checkpoint_id"
    ]
    await flow.client.delete(f"/computers/{base}")
    return str(ckpt)


async def _key(flow: Flow, scopes: dict[str, Any]) -> dict[str, str]:
    minted = await flow.client.post("/keys", json={"scopes": scopes, "label": "t"})
    assert minted.status_code == 200, minted.text
    return {"Authorization": f"Bearer {minted.json()['secret']}"}


async def _job(flow: Flow, job_id: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    await flow.runtime.tasks.drain(timeout=5.0)
    got = await flow.client.get(f"/relay/{job_id}", headers=headers)
    assert got.status_code == 200, got.text
    return dict(got.json())


async def test_a_scoped_keys_job_is_called_stored_and_delivered_to_its_chain(flow: Flow) -> None:
    flow.targets["model"] = _model({"content": [{"type": "text", "text": "hi"}]})
    await _chain(flow, "chain")
    brain = await _key(flow, SCOPE)
    resp = await flow.client.post(
        "/relay",
        json={
            "target": "http://model/v1/messages",
            "body": {"q": 1},
            "forward_headers": {"x-api-key": "s"},
        },
        headers=brain,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    job = await _job(flow, job_id, brain)
    assert job["status"] == "completed" and job["response"]["status"] == 200
    assert (
        job["response"]["body"]["echo"] == {"q": 1}
        and job["response"]["body"]["content"][0]["text"] == "hi"
    )
    assert job["delivery"]["status"] == "delivered" and job["delivery"]["computer_id"]
    assert any(cmd == f"echo woke {job_id}" for _, cmd in flow.host.guest.commands)
    log = await flow.client.get(f"/computers/{job['delivery']['computer_id']}/exec_log")
    assert log.status_code == 200 and log.json()["command"] == f"echo woke {job_id}"
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert len(chain) == 2
    # another key of the same account cannot see it; the account can
    other = await _key(flow, SCOPE)
    assert (await flow.client.get(f"/relay/{job_id}", headers=other)).status_code == 404
    assert (await flow.client.get(f"/relay/{job_id}")).status_code == 200
    assert (await flow.other_client.get(f"/relay/{job_id}")).status_code == 404


async def test_a_busy_chain_defers_the_wake_up_until_the_running_fork_is_gone(flow: Flow) -> None:
    flow.targets["model"] = _model({})
    ckpt = await _chain(flow, "chain")
    running = await flow.client.post(
        f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"}
    )
    assert running.status_code == 200
    resp = await flow.client.post(
        "/relay",
        json={
            "target": "http://model/v1/messages",
            "deliver": {"label": "chain", "exec": "echo woke"},
        },
    )
    job_id = resp.json()["job_id"]
    job = await _job(flow, job_id)
    assert job["delivery"]["status"] == "delivered" and job["delivery"]["deferred_id"]
    assert job["delivery"]["computer_id"] is None
    assert not any(cmd == f"echo woke {job_id}" for _, cmd in flow.host.guest.commands)
    assert (
        await flow.client.delete(f"/computers/{running.json()['computer_id']}")
    ).status_code == 200
    await flow.runtime.tasks.drain(timeout=5.0)
    assert any(cmd == f"echo woke {job_id}" for _, cmd in flow.host.guest.commands)
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert len(chain) == 2


async def test_a_failed_upstream_is_still_delivered_and_says_why(flow: Flow) -> None:
    await _chain(flow, "chain")
    resp = await flow.client.post(
        "/relay",
        json={"target": "http://down/v1", "deliver": {"label": "chain", "exec": "echo woke"}},
    )
    assert resp.status_code == 202
    job = await _job(flow, resp.json()["job_id"])
    assert job["status"] == "failed" and job["attempts"] == 3 and "ConnectError" in job["error"]
    assert job["response"] is None and job["delivery"]["status"] == "delivered"


async def test_the_guard_and_the_scope_hold_over_http(flow: Flow) -> None:
    for target in (
        "http://127.0.0.1:8000/health",
        "http://172.16.254.1/",
        "http://[::1]/",
        "ftp://model/",
    ):
        assert (await flow.client.post("/relay", json={"target": target})).status_code == 422, (
            target
        )
    brain = await _key(flow, SCOPE)
    assert (
        await flow.client.post("/relay", json={"target": "http://other/"}, headers=brain)
    ).status_code == 403
    no_scope = await _key(flow, {"recipes": {"read": True}})
    assert (
        await flow.client.post("/relay", json={"target": "http://model/"}, headers=no_scope)
    ).status_code == 403
