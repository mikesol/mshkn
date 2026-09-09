"""Phase 15: the relay on the live host (#110). A job to a public target is called
and delivered by a fork of a labelled chain; the guard refuses the host; a scoped
key is held to its scope; a delivery waits behind a running fork."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx  # noqa: TC002
import pytest

from tests.e2e.conftest import checkpoint_computer, create_computer, destroy_computer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

JOB_TIMEOUT = 120.0


async def _chain(client: httpx.AsyncClient) -> str:
    label = f"relay-{uuid.uuid4().hex[:8]}"
    cid = await create_computer(client)
    await checkpoint_computer(client, cid, label=label)
    await destroy_computer(client, cid)
    return label


async def _drop_chain(client: httpx.AsyncClient, label: str) -> None:
    for ckpt in (await client.get("/checkpoints", params={"label": label})).json():
        await client.delete(f"/checkpoints/{ckpt['id']}")


@pytest.fixture
async def chain(long_client: httpx.AsyncClient) -> AsyncIterator[str]:
    label = await _chain(long_client)
    yield label
    await _drop_chain(long_client, label)


async def _wait(client: httpx.AsyncClient, job_id: str, done: Any) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_TIMEOUT
    while time.monotonic() < deadline:
        resp = await client.get(f"/relay/{job_id}")
        assert resp.status_code == 200, resp.text
        job = dict(resp.json())
        if done(job):
            return job
        await asyncio.sleep(2)
    raise TimeoutError(f"relay job {job_id} did not settle in {JOB_TIMEOUT}s")


def _delivered(job: dict[str, Any]) -> bool:
    return job["status"] in ("completed", "failed") and (job["delivery"] or {}).get("status") in (
        "delivered",
        "failed",
    )


async def _wait_exec_log(client: httpx.AsyncClient, computer_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_TIMEOUT
    while time.monotonic() < deadline:
        resp = await client.get(f"/computers/{computer_id}/exec_log")
        if resp.status_code == 200:
            return dict(resp.json())
        await asyncio.sleep(2)
    raise TimeoutError(f"no exec log for {computer_id}")


class TestPhase15Relay:
    @pytest.mark.asyncio
    async def test_t15_1_a_job_to_a_public_target_is_delivered_to_a_chain(
        self, long_client: httpx.AsyncClient, chain: str
    ) -> None:
        resp = await long_client.post(
            "/relay",
            json={
                "target": "https://example.com/",
                "method": "GET",
                "deliver": {"label": chain, "exec": "echo woke"},
            },
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        job = await _wait(long_client, job_id, _delivered)
        assert job["status"] == "completed" and job["attempts"] == 1, job
        assert job["response"]["status"] == 200 and "Example Domain" in job["response"]["body"]
        assert job["delivery"]["status"] == "delivered" and job["delivery"]["computer_id"], job[
            "delivery"
        ]
        log = await _wait_exec_log(long_client, job["delivery"]["computer_id"])
        assert log["command"] == f"echo woke {job_id}" and job_id in log["stdout"]
        assert log["label"] == chain and log["created_checkpoint_id"]
        heads = (await long_client.get("/checkpoints", params={"label": chain})).json()
        assert len(heads) == 2
        assert "forward_headers" not in resp.text and "forward_headers" not in str(job)

    @pytest.mark.asyncio
    async def test_t15_2_the_guard_refuses_the_host(self, client: httpx.AsyncClient) -> None:
        for target in (
            "http://127.0.0.1:8000/health",
            "http://172.16.254.1/",
            "http://localhost/",
            "http://[::1]/",
            "ftp://example.com/",
        ):
            resp = await client.post("/relay", json={"target": target})
            assert resp.status_code == 422, (target, resp.text)

    @pytest.mark.asyncio
    async def test_t15_3_a_scoped_key_is_held_to_its_scope(
        self, long_client: httpx.AsyncClient, chain: str
    ) -> None:
        minted = await long_client.post(
            "/keys", json={"scopes": {"recipes": {"read": True}}, "label": "t15"}
        )
        assert minted.status_code == 200, minted.text
        plain = {"Authorization": f"Bearer {minted.json()['secret']}"}
        scope = {
            "relay": {
                "targets": ["https://example.com/"],
                "deliver": {"label": chain, "exec": "echo pinned"},
            }
        }
        minted_relay = await long_client.post("/keys", json={"scopes": scope, "label": "t15-relay"})
        assert minted_relay.status_code == 200, minted_relay.text
        scoped = {"Authorization": f"Bearer {minted_relay.json()['secret']}"}
        other_minted = await long_client.post("/keys", json={"scopes": scope, "label": "t15-other"})
        other = {"Authorization": f"Bearer {other_minted.json()['secret']}"}
        try:
            assert (
                await long_client.post(
                    "/relay",
                    json={"target": "https://example.com/", "method": "GET"},
                    headers=plain,
                )
            ).status_code == 403
            assert (
                await long_client.post(
                    "/relay",
                    json={"target": "https://www.iana.org/", "method": "GET"},
                    headers=scoped,
                )
            ).status_code == 403
            assert (
                await long_client.post(
                    "/relay",
                    json={
                        "target": "https://example.com/",
                        "method": "GET",
                        "deliver": {"label": chain, "exec": "echo mine"},
                    },
                    headers=scoped,
                )
            ).status_code == 422
            resp = await long_client.post(
                "/relay", json={"target": "https://example.com/", "method": "GET"}, headers=scoped
            )
            assert resp.status_code == 202, resp.text
            job_id = resp.json()["job_id"]
            job = await _wait(long_client, job_id, _delivered)
            assert (
                job["delivery"]["exec"] == "echo pinned"
                and job["delivery"]["status"] == "delivered"
            )
            assert (await long_client.get(f"/relay/{job_id}", headers=scoped)).status_code == 200
            assert (await long_client.get(f"/relay/{job_id}", headers=other)).status_code == 404
            assert (await long_client.get(f"/relay/{job_id}")).status_code == 200
            log = await _wait_exec_log(long_client, job["delivery"]["computer_id"])
            assert log["command"] == f"echo pinned {job_id}"
        finally:
            for key in (minted, minted_relay, other_minted):
                await long_client.delete(f"/keys/{key.json()['id']}")

    @pytest.mark.asyncio
    async def test_t15_4_a_delivery_waits_behind_a_running_fork(
        self, long_client: httpx.AsyncClient, chain: str
    ) -> None:
        sleeper = asyncio.create_task(
            long_client.post(
                "/checkpoints/fork",
                json={
                    "label": chain,
                    "exec": "sleep 40",
                    "self_destruct": True,
                    "exclusive": "error_on_conflict",
                },
                timeout=120.0,
            )
        )
        await asyncio.sleep(10)  # the sleeper's fork is admitted and running
        resp = await long_client.post(
            "/relay",
            json={
                "target": "https://example.com/",
                "method": "GET",
                "deliver": {"label": chain, "exec": "echo woke"},
            },
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        job = await _wait(long_client, job_id, _delivered)
        assert job["delivery"]["status"] == "delivered" and job["delivery"]["deferred_id"], job[
            "delivery"
        ]
        assert job["delivery"]["computer_id"] is None
        slept = await sleeper
        assert slept.status_code == 200 and slept.json()["exec_exit_code"] == 0, slept.text
        deadline = time.monotonic() + JOB_TIMEOUT
        while time.monotonic() < deadline:
            heads = (await long_client.get("/checkpoints", params={"label": chain})).json()
            if len(heads) == 3:
                break
            await asyncio.sleep(3)
        assert len(heads) == 3, "the sleeper's checkpoint, then the drained wake-up's"
        log = await _wait_exec_log(long_client, heads[0]["computer_id"])
        assert log["command"].endswith(f"echo woke {job_id}") and job_id in log["stdout"]
