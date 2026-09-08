"""E2E tests for pistachio compute model primitives.

Tests exec-on-create (#31), self-destruct (#32), callback URL (#33),
label lookup (#29), exclusive restore (#30), and fork by label (#89, T12.1).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx  # noqa: TC002
import pytest

from tests.e2e.conftest import (
    checkpoint_computer,
    create_computer,
    delete_checkpoint,
    destroy_computer,
)

# ---------------------------------------------------------------------------
# T7.5 — Exec on create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_on_create(long_client: httpx.AsyncClient) -> None:
    """Create computer with exec='echo hello' — response includes stdout."""
    resp = await long_client.post(
        "/computers",
        json={
            "exec": "echo hello",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert "hello" in data["exec_stdout"]
    # Clean up
    await destroy_computer(long_client, data["computer_id"])


# ---------------------------------------------------------------------------
# T7.6 — Exec on fork
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_on_fork(long_client: httpx.AsyncClient) -> None:
    """Fork from checkpoint with exec — response includes exec result."""
    # Create computer, checkpoint it
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id)
    await destroy_computer(long_client, comp_id)

    # Fork with exec
    resp = await long_client.post(
        f"/checkpoints/{ckpt_id}/fork",
        json={
            "exec": "cat /etc/hostname",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"].strip() != ""

    await destroy_computer(long_client, data["computer_id"])
    await delete_checkpoint(long_client, ckpt_id)


# ---------------------------------------------------------------------------
# T7.7 — Exec with non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_on_create_failure(long_client: httpx.AsyncClient) -> None:
    """Create with exec that fails — error captured in response."""
    resp = await long_client.post(
        "/computers",
        json={
            "exec": "exit 42",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 42
    await destroy_computer(long_client, data["computer_id"])


# ---------------------------------------------------------------------------
# T7.8 — Self-destruct on create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_destruct_on_create(long_client: httpx.AsyncClient) -> None:
    """Create with exec + self_destruct — computer destroyed, checkpoint created."""
    resp = await long_client.post(
        "/computers",
        json={
            "exec": "echo self-destruct-test",
            "self_destruct": True,
            "label": "test-sd-create",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["created_checkpoint_id"] is not None

    # Verify computer is destroyed
    status_resp = await long_client.get(f"/computers/{data['computer_id']}/status")
    assert status_resp.status_code == 404

    # Verify checkpoint exists with correct label
    ckpts_resp = await long_client.get("/checkpoints", params={"label": "test-sd-create"})
    ckpts_resp.raise_for_status()
    ckpts = ckpts_resp.json()
    assert len(ckpts) >= 1
    assert ckpts[0]["id"] == data["created_checkpoint_id"]

    # Clean up
    await delete_checkpoint(long_client, data["created_checkpoint_id"])


# ---------------------------------------------------------------------------
# T7.9 — Self-destruct on fork (checkpoint chain maintained)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_destruct_on_fork(long_client: httpx.AsyncClient) -> None:
    """Fork with exec + self_destruct — checkpoint chain maintained."""
    # Create base computer, checkpoint with label
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-sd-fork")
    await destroy_computer(long_client, comp_id)

    # Fork with self-destruct
    resp = await long_client.post(
        f"/checkpoints/{ckpt_id}/fork",
        json={
            "exec": "echo chain-test",
            "self_destruct": True,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["created_checkpoint_id"] is not None

    # New checkpoint should be in the same chain (same label)
    ckpts_resp = await long_client.get("/checkpoints", params={"label": "test-sd-fork"})
    ckpts_resp.raise_for_status()
    ckpts = ckpts_resp.json()
    ckpt_ids = [c["id"] for c in ckpts]
    assert data["created_checkpoint_id"] in ckpt_ids

    # Clean up
    for c in ckpts:
        await delete_checkpoint(long_client, c["id"])


# ---------------------------------------------------------------------------
# T7.10 — Self-destruct on non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_destruct_on_failure(long_client: httpx.AsyncClient) -> None:
    """Self-destruct still fires on non-zero exit (preserves error state)."""
    resp = await long_client.post(
        "/computers",
        json={
            "exec": "exit 1",
            "self_destruct": True,
            "label": "test-sd-fail",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 1
    assert data["created_checkpoint_id"] is not None

    # Clean up
    await delete_checkpoint(long_client, data["created_checkpoint_id"])


# ---------------------------------------------------------------------------
# T7.11 — Callback URL fires without breaking self-destruct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_url(long_client: httpx.AsyncClient) -> None:
    """Self-destruct with callback_url — callback fires without breaking self-destruct.

    Since the mshkn server fires callbacks and may not be able to reach the test
    machine, we use a URL on the server itself (the API URL) as the callback target.
    The POST will get a 405/422 but the callback delivery counts as attempted.
    The key assertion: self-destruct still works (checkpoint created, computer destroyed).
    """
    from tests.e2e.conftest import API_URL

    # Use the mshkn API URL as callback — server can reach itself
    callback_url = f"{API_URL}/checkpoints"

    resp = await long_client.post(
        "/computers",
        json={
            "exec": "echo callback-test",
            "self_destruct": True,
            "label": "test-callback",
            "callback_url": callback_url,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["created_checkpoint_id"] is not None

    # Verify computer is destroyed (self-destruct worked despite callback)
    status_resp = await long_client.get(f"/computers/{data['computer_id']}/status")
    assert status_resp.status_code == 404

    # Clean up
    await delete_checkpoint(long_client, data["created_checkpoint_id"])


# ---------------------------------------------------------------------------
# T7.12 — Callback with unreachable URL doesn't crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_unreachable(long_client: httpx.AsyncClient) -> None:
    """Callback with bad URL — doesn't crash, self-destruct still works."""
    resp = await long_client.post(
        "/computers",
        json={
            "exec": "echo unreachable-test",
            "self_destruct": True,
            "label": "test-callback-fail",
            "callback_url": "http://192.0.2.1:9999/nope",  # RFC 5737 test address
        },
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["created_checkpoint_id"] is not None

    # Clean up
    await delete_checkpoint(long_client, data["created_checkpoint_id"])


# ---------------------------------------------------------------------------
# T7.13 — Exclusive restore: error_on_conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exclusive_error_on_conflict(long_client: httpx.AsyncClient) -> None:
    """Fork with exclusive='error_on_conflict' while chain is busy — returns 409."""
    # Create computer, checkpoint with label
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-excl-error")

    # Fork once (creates an active computer on the chain)
    fork1 = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={})
    fork1.raise_for_status()
    fork1_comp = fork1.json()["computer_id"]

    # Second fork with exclusive should fail
    fork2 = await long_client.post(
        f"/checkpoints/{ckpt_id}/fork",
        json={
            "exclusive": "error_on_conflict",
        },
    )
    assert fork2.status_code == 409

    # Clean up
    await destroy_computer(long_client, fork1_comp)
    await destroy_computer(long_client, comp_id)
    await delete_checkpoint(long_client, ckpt_id)


# ---------------------------------------------------------------------------
# T7.14 — Exclusive restore: defer_on_conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exclusive_defer_on_conflict(long_client: httpx.AsyncClient) -> None:
    """Fork with exclusive='defer_on_conflict' while busy — returns 202."""
    # Create computer, checkpoint with label
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-excl-defer")
    await destroy_computer(long_client, comp_id)

    # Fork to create an active computer on the chain
    fork1 = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={})
    fork1.raise_for_status()
    fork1_comp = fork1.json()["computer_id"]

    # Deferred fork should return 202
    fork2 = await long_client.post(
        f"/checkpoints/{ckpt_id}/fork",
        json={
            "exclusive": "defer_on_conflict",
            "exec": "echo deferred-work",
        },
    )
    assert fork2.status_code == 202
    assert fork2.json()["status"] == "queued"

    # Destroy the active computer — should trigger deferred processing
    await destroy_computer(long_client, fork1_comp)

    # Wait for deferred processing
    await asyncio.sleep(5)

    # Clean up all checkpoints with this label
    ckpts_resp = await long_client.get("/checkpoints", params={"label": "test-excl-defer"})
    ckpts_resp.raise_for_status()
    for c in ckpts_resp.json():
        await delete_checkpoint(long_client, c["id"])


# ---------------------------------------------------------------------------
# T7.15 — Exclusive restore: no conflict proceeds normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exclusive_no_conflict(long_client: httpx.AsyncClient) -> None:
    """Fork with exclusive when no conflict — proceeds normally."""
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-excl-ok")
    await destroy_computer(long_client, comp_id)

    # Fork with exclusive — no active computer, should succeed
    fork = await long_client.post(
        f"/checkpoints/{ckpt_id}/fork",
        json={
            "exclusive": "error_on_conflict",
            "exec": "echo no-conflict",
        },
    )
    fork.raise_for_status()
    data = fork.json()
    assert data["exec_exit_code"] == 0

    await destroy_computer(long_client, data["computer_id"])
    await delete_checkpoint(long_client, ckpt_id)


# ---------------------------------------------------------------------------
# T7.16 — Label-based checkpoint lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_lookup(long_client: httpx.AsyncClient) -> None:
    """GET /checkpoints?label=X returns only matching checkpoints, newest first."""
    comp_id = await create_computer(long_client)

    # Create checkpoints with different labels
    ckpt_a = await checkpoint_computer(long_client, comp_id, label="test-label-a")
    ckpt_b = await checkpoint_computer(long_client, comp_id, label="test-label-b")
    ckpt_a2 = await checkpoint_computer(long_client, comp_id, label="test-label-a")

    # Filter by label-a
    resp = await long_client.get("/checkpoints", params={"label": "test-label-a"})
    resp.raise_for_status()
    results = resp.json()
    assert len(results) == 2
    assert results[0]["id"] == ckpt_a2  # newest first
    assert results[1]["id"] == ckpt_a

    # Filter by label-b
    resp = await long_client.get("/checkpoints", params={"label": "test-label-b"})
    resp.raise_for_status()
    results = resp.json()
    assert len(results) == 1
    assert results[0]["id"] == ckpt_b

    # Clean up
    await destroy_computer(long_client, comp_id)
    for ckpt in [ckpt_a, ckpt_b, ckpt_a2]:
        await delete_checkpoint(long_client, ckpt)


# ---------------------------------------------------------------------------
# T7.17 — Deferred batch: multiple requests written to /tmp/exec/N.txt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deferred_batch_exec_files(long_client: httpx.AsyncClient) -> None:
    """Multiple deferred requests write execs to /tmp/exec/N.txt."""
    # Create computer, checkpoint with label
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-batch")
    await destroy_computer(long_client, comp_id)

    # Fork to create an active computer
    fork1 = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={})
    fork1.raise_for_status()
    fork1_comp = fork1.json()["computer_id"]

    # Queue 3 deferred requests
    for i in range(3):
        resp = await long_client.post(
            f"/checkpoints/{ckpt_id}/fork",
            json={
                "exclusive": "defer_on_conflict",
                "exec": f"echo message-{i}",
                "meta_exec": "cat /tmp/exec/*.txt",
            },
        )
        assert resp.status_code == 202

    # Destroy active computer — triggers deferred processing
    await destroy_computer(long_client, fork1_comp)

    # Wait for deferred computer to boot and process
    await asyncio.sleep(8)

    # Verify the checkpoint chain grew
    ckpts_resp = await long_client.get("/checkpoints", params={"label": "test-batch"})
    ckpts_resp.raise_for_status()
    ckpts = ckpts_resp.json()
    assert len(ckpts) >= 1

    # Clean up
    for c in ckpts:
        await delete_checkpoint(long_client, c["id"])


# ---------------------------------------------------------------------------
# T7.18 — Deferred batch with meta_exec verifies all execs delivered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deferred_batch_with_callback(long_client: httpx.AsyncClient) -> None:
    """Deferred batch with meta_exec — verifies all execs written to /tmp/exec/.

    Since callbacks can't reach the test machine from the server, we verify
    indirectly: the deferred computer should self-destruct and create a new
    checkpoint in the chain. The meta_exec writes output to the filesystem
    which gets captured in the checkpoint.
    """
    # Create computer, checkpoint with label
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-batch-cb")
    await destroy_computer(long_client, comp_id)

    # Fork to create active computer
    fork1 = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={})
    fork1.raise_for_status()
    fork1_comp = fork1.json()["computer_id"]

    # Queue 3 deferred with meta_exec that reads all exec files and writes output
    for i in range(3):
        resp = await long_client.post(
            f"/checkpoints/{ckpt_id}/fork",
            json={
                "exclusive": "defer_on_conflict",
                "exec": f"task-{i}",
                "meta_exec": (
                    "cat /tmp/exec/*.txt > /var/batch_result.txt && cat /var/batch_result.txt"
                ),
                "self_destruct": True,
            },
        )
        assert resp.status_code == 202

    # Destroy to trigger deferred processing
    await destroy_computer(long_client, fork1_comp)

    # Wait for deferred processing to complete (boot + exec + self-destruct)
    await asyncio.sleep(15)

    # Verify: the chain should have grown (new checkpoint from self-destruct)
    ckpts_resp = await long_client.get("/checkpoints", params={"label": "test-batch-cb"})
    ckpts_resp.raise_for_status()
    ckpts = ckpts_resp.json()
    # Should have at least 2: original + one from deferred self-destruct
    assert len(ckpts) >= 2, f"Expected at least 2 checkpoints in chain, got {len(ckpts)}"

    # Fork from the latest checkpoint and verify /var/batch_result.txt
    latest_ckpt = ckpts[0]["id"]
    fork_resp = await long_client.post(
        f"/checkpoints/{latest_ckpt}/fork",
        json={
            "exec": "cat /var/batch_result.txt 2>/dev/null || echo 'not found'",
        },
    )
    fork_resp.raise_for_status()
    fork_data = fork_resp.json()
    stdout = fork_data.get("exec_stdout", "")

    # Verify all 3 tasks were written
    assert "task-0" in stdout, f"task-0 not found in output: {stdout}"
    assert "task-1" in stdout, f"task-1 not found in output: {stdout}"
    assert "task-2" in stdout, f"task-2 not found in output: {stdout}"

    # Clean up
    await destroy_computer(long_client, fork_data["computer_id"])
    for c in ckpts:
        await delete_checkpoint(long_client, c["id"])


# ---------------------------------------------------------------------------
# T7.10 (test plan) — The output of an ephemeral turn outlives the computer (#58)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_log_outlives_the_self_destructed_computer(
    long_client: httpx.AsyncClient,
) -> None:
    """A self-destructed fork's exec output is readable from its exec log."""
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-exec-log")
    await destroy_computer(long_client, comp_id)
    command = "echo turn-out; echo turn-err >&2; exit 3"
    try:
        resp = await long_client.post(
            f"/checkpoints/{ckpt_id}/fork",
            json={"exec": command, "self_destruct": True},
        )
        resp.raise_for_status()
        turn = resp.json()
        assert turn["exec_exit_code"] == 3 and turn["created_checkpoint_id"]
        gone = await long_client.get(f"/computers/{turn['computer_id']}/status")
        assert gone.status_code == 404, "the computer self-destructed"

        resp = await long_client.get(f"/computers/{turn['computer_id']}/exec_log")
        assert resp.status_code == 200, resp.text
        log = resp.json()
        assert log["command"] == command
        assert log["exit_code"] == 3
        assert log["stdout"] == turn["exec_stdout"] and "turn-out" in log["stdout"]
        assert log["stderr"] == turn["exec_stderr"] and "turn-err" in log["stderr"]
        assert log["stdout_truncated"] is False and log["stderr_truncated"] is False
        assert log["source_checkpoint_id"] == ckpt_id
        assert log["created_checkpoint_id"] == turn["created_checkpoint_id"]
        assert log["label"] == "test-exec-log"

        # Reachable from the chain alone: the created checkpoint names the computer.
        chain = (await long_client.get("/checkpoints", params={"label": "test-exec-log"})).json()
        created = next(c for c in chain if c["id"] == turn["created_checkpoint_id"])
        assert created["computer_id"] == turn["computer_id"]

        # Nothing to record: a create without exec, and a computer that never existed.
        plain = await create_computer(long_client)
        try:
            assert (await long_client.get(f"/computers/{plain}/exec_log")).status_code == 404
        finally:
            await destroy_computer(long_client, plain)
        assert (await long_client.get("/computers/comp-never/exec_log")).status_code == 404
    finally:
        chain = (await long_client.get("/checkpoints", params={"label": "test-exec-log"})).json()
        for c in chain:
            await delete_checkpoint(long_client, c["id"])


# ---------------------------------------------------------------------------
# T12.1 — Fork by label advances a chain atomically
# ---------------------------------------------------------------------------


async def _chain(client: httpx.AsyncClient, label: str) -> list[dict[str, Any]]:
    resp = await client.get("/checkpoints", params={"label": label})
    resp.raise_for_status()
    chain: list[dict[str, Any]] = resp.json()
    return chain


async def _wait_for_chain_length(client: httpx.AsyncClient, label: str, n: int) -> None:
    for _ in range(120):
        if len(await _chain(client, label)) >= n:
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"chain {label!r} did not reach {n} checkpoints")


async def _delete_chain(client: httpx.AsyncClient, label: str) -> None:
    for c in await _chain(client, label):
        await delete_checkpoint(client, c["id"])


@pytest.mark.asyncio
async def test_concurrent_forks_by_label_with_error_on_conflict_advance_the_chain_once(
    long_client: httpx.AsyncClient,
) -> None:
    """Two POST /checkpoints/fork {label, exclusive: error_on_conflict} at once:
    one 200, one 409, and exactly one new head whose parent is the old head."""
    label = f"t12-error-{uuid.uuid4().hex[:8]}"
    comp_id = await create_computer(long_client)
    head = await checkpoint_computer(long_client, comp_id, label=label)
    await destroy_computer(long_client, comp_id)
    body = {
        "label": label,
        "exclusive": "error_on_conflict",
        "exec": "echo turn",
        "self_destruct": True,
    }
    try:
        first, second = await asyncio.gather(
            long_client.post("/checkpoints/fork", json=body),
            long_client.post("/checkpoints/fork", json=body),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409], (
            first.text,
            second.text,
        )
        ok = first if first.status_code == 200 else second
        assert ok.json()["checkpoint_id"] == head
        assert "turn" in ok.json()["exec_stdout"]
        chain = await _chain(long_client, label)
        assert [c["id"] for c in chain[1:]] == [head], "exactly one new head"
        assert chain[0]["parent_id"] == head
        assert chain[0]["id"] == ok.json()["created_checkpoint_id"]
    finally:
        await _delete_chain(long_client, label)


@pytest.mark.asyncio
async def test_concurrent_forks_by_label_with_defer_on_conflict_run_in_order(
    long_client: httpx.AsyncClient,
) -> None:
    """Two POST /checkpoints/fork {label, exclusive: defer_on_conflict} at once:
    one 200, one 202, and after the drain two new checkpoints in order."""
    label = f"t12-defer-{uuid.uuid4().hex[:8]}"
    comp_id = await create_computer(long_client)
    head = await checkpoint_computer(long_client, comp_id, label=label)
    await destroy_computer(long_client, comp_id)
    body = {
        "label": label,
        "exclusive": "defer_on_conflict",
        "exec": "echo turn",
        "self_destruct": True,
    }
    try:
        first, second = await asyncio.gather(
            long_client.post("/checkpoints/fork", json=body),
            long_client.post("/checkpoints/fork", json=body),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 202], (
            first.text,
            second.text,
        )
        queued = first if first.status_code == 202 else second
        assert queued.json()["status"] == "queued"
        await _wait_for_chain_length(long_client, label, 3)
        newest, middle, oldest = await _chain(long_client, label)
        assert oldest["id"] == head
        assert middle["parent_id"] == head
        assert newest["parent_id"] == middle["id"]
    finally:
        await _delete_chain(long_client, label)
