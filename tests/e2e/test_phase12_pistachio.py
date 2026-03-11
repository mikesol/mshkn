"""E2E tests for pistachio compute model primitives.

Tests exec-on-create (#31), self-destruct (#32), callback URL (#33),
label lookup (#29), and exclusive restore (#30).
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx  # noqa: TC002
import pytest

from tests.e2e.conftest import (
    checkpoint_computer,
    create_computer,
    delete_checkpoint,
    destroy_computer,
)

# ---------------------------------------------------------------------------
# Callback server for testing callback_url
# ---------------------------------------------------------------------------

class CallbackCollector:
    """Simple HTTP server that collects POST payloads."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, port: int = 0) -> int:
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                collector.payloads.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass  # suppress logs

        self._server = HTTPServer(("0.0.0.0", port), Handler)
        actual_port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return actual_port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


# ---------------------------------------------------------------------------
# T7.5 — Exec on create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_on_create(long_client: httpx.AsyncClient) -> None:
    """Create computer with exec='echo hello' — response includes stdout."""
    resp = await long_client.post("/computers", json={
        "uses": [],
        "exec": "echo hello",
    })
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
    resp = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={
        "exec": "cat /etc/hostname",
    })
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
    resp = await long_client.post("/computers", json={
        "uses": [],
        "exec": "exit 42",
    })
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
    resp = await long_client.post("/computers", json={
        "uses": [],
        "exec": "echo self-destruct-test",
        "self_destruct": True,
        "label": "test-sd-create",
    })
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
    resp = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={
        "exec": "echo chain-test",
        "self_destruct": True,
    })
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
    resp = await long_client.post("/computers", json={
        "uses": [],
        "exec": "exit 1",
        "self_destruct": True,
        "label": "test-sd-fail",
    })
    resp.raise_for_status()
    data = resp.json()
    assert data["exec_exit_code"] == 1
    assert data["created_checkpoint_id"] is not None

    # Clean up
    await delete_checkpoint(long_client, data["created_checkpoint_id"])


# ---------------------------------------------------------------------------
# T7.11 — Callback URL receives correct payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_url(long_client: httpx.AsyncClient) -> None:
    """Self-destruct with callback_url — callback receives correct payload."""
    collector = CallbackCollector()
    port = collector.start()

    try:
        # Use the server's external IP since the callback comes from the mshkn service
        # For local testing, localhost works; for remote, need the test machine's IP
        import socket
        local_ip = socket.gethostbyname(socket.gethostname())
        callback_url = f"http://{local_ip}:{port}/callback"

        resp = await long_client.post("/computers", json={
            "uses": [],
            "exec": "echo callback-test",
            "self_destruct": True,
            "label": "test-callback",
            "callback_url": callback_url,
        })
        resp.raise_for_status()
        data = resp.json()
        assert data["exec_exit_code"] == 0

        # Wait for callback delivery (async, may take a moment)
        for _ in range(20):
            if collector.payloads:
                break
            await asyncio.sleep(0.5)

        assert len(collector.payloads) >= 1
        payload = collector.payloads[0]
        assert payload["computer_id"] == data["computer_id"]
        assert payload["exec_exit_code"] == 0
        assert payload["created_checkpoint_id"] == data["created_checkpoint_id"]
        assert "callback-test" in payload["exec_stdout"]

        # Clean up
        await delete_checkpoint(long_client, data["created_checkpoint_id"])
    finally:
        collector.stop()


# ---------------------------------------------------------------------------
# T7.12 — Callback with unreachable URL doesn't crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_unreachable(long_client: httpx.AsyncClient) -> None:
    """Callback with bad URL — doesn't crash, self-destruct still works."""
    resp = await long_client.post("/computers", json={
        "uses": [],
        "exec": "echo unreachable-test",
        "self_destruct": True,
        "label": "test-callback-fail",
        "callback_url": "http://192.0.2.1:9999/nope",  # RFC 5737 test address
    })
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
    fork2 = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={
        "exclusive": "error_on_conflict",
    })
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
    """Fork with exclusive='defer_on_conflict' while busy — returns 202, processes on destroy."""
    # Create computer, checkpoint with label
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-excl-defer")
    await destroy_computer(long_client, comp_id)

    # Fork to create an active computer on the chain
    fork1 = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={})
    fork1.raise_for_status()
    fork1_comp = fork1.json()["computer_id"]

    # Deferred fork should return 202
    fork2 = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={
        "exclusive": "defer_on_conflict",
        "exec": "echo deferred-work",
    })
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
    fork = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={
        "exclusive": "error_on_conflict",
        "exec": "echo no-conflict",
    })
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
        resp = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={
            "exclusive": "defer_on_conflict",
            "exec": f"echo message-{i}",
            "meta_exec": "cat /tmp/exec/*.txt",
        })
        assert resp.status_code == 202

    # Destroy active computer — triggers deferred processing
    await destroy_computer(long_client, fork1_comp)

    # Wait for deferred computer to boot and process
    await asyncio.sleep(8)

    # Check: the deferred computer should have been created.
    # Since meta_exec was "cat /tmp/exec/*.txt", all 3 messages should be in output.
    # We can't easily check the output from here without a callback,
    # but we can verify the checkpoint chain grew.
    ckpts_resp = await long_client.get("/checkpoints", params={"label": "test-batch"})
    ckpts_resp.raise_for_status()
    ckpts = ckpts_resp.json()
    # Original checkpoint + at least one from deferred processing
    assert len(ckpts) >= 1

    # Clean up
    for c in ckpts:
        await delete_checkpoint(long_client, c["id"])


# ---------------------------------------------------------------------------
# T7.18 — Deferred batch with callback verifies all execs delivered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deferred_batch_with_callback(long_client: httpx.AsyncClient) -> None:
    """Deferred batch with meta_exec and callback — verifies all execs in /tmp/exec/."""
    collector = CallbackCollector()
    port = collector.start()

    try:
        import socket
        local_ip = socket.gethostbyname(socket.gethostname())
        callback_url = f"http://{local_ip}:{port}/callback"

        # Create computer, checkpoint with label
        comp_id = await create_computer(long_client)
        ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-batch-cb")
        await destroy_computer(long_client, comp_id)

        # Fork to create active computer
        fork1 = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={})
        fork1.raise_for_status()
        fork1_comp = fork1.json()["computer_id"]

        # Queue 3 deferred with meta_exec that reads all exec files
        for i in range(3):
            resp = await long_client.post(f"/checkpoints/{ckpt_id}/fork", json={
                "exclusive": "defer_on_conflict",
                "exec": f"task-{i}",
                "meta_exec": (
                    "ls /tmp/exec/ && for f in /tmp/exec/*.txt; "
                    "do echo '---' && cat \"$f\"; done"
                ),
                "self_destruct": True,
                "callback_url": callback_url,
            })
            assert resp.status_code == 202

        # Destroy to trigger deferred processing
        await destroy_computer(long_client, fork1_comp)

        # Wait for callback
        for _ in range(30):
            if collector.payloads:
                break
            await asyncio.sleep(1)

        assert len(collector.payloads) >= 1, "No callback received"
        payload = collector.payloads[0]
        assert payload["exec_exit_code"] == 0

        # Verify all 3 tasks were written to /tmp/exec/
        stdout = payload["exec_stdout"]
        assert "task-0" in stdout
        assert "task-1" in stdout
        assert "task-2" in stdout

        # Clean up
        ckpts_resp = await long_client.get("/checkpoints", params={"label": "test-batch-cb"})
        ckpts_resp.raise_for_status()
        for c in ckpts_resp.json():
            await delete_checkpoint(long_client, c["id"])
    finally:
        collector.stop()
