"""E2E tests for Phase 13: Ingress mapping (webhook-triggered computers).

Tests rule CRUD, Starlark validation, dry-run /test endpoint, ingress
trigger (async/sync fork, create), rate limiting, rule rotation, and logs.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx
import pytest

from tests.e2e.conftest import (
    API_URL,
    HEADERS,
    checkpoint_computer,
    create_computer,
    delete_checkpoint,
    destroy_computer,
)

# ---------------------------------------------------------------------------
# Starlark sources
# ---------------------------------------------------------------------------

STARLARK_FORK = '''
def transform(req):
    body = req["body_json"]
    if not body or "checkpoint_id" not in body:
        return None
    result = {"action": "fork", "checkpoint_id": body["checkpoint_id"]}
    if "exec" in body:
        result["exec"] = body["exec"]
    if "self_destruct" in body:
        result["self_destruct"] = body["self_destruct"]
    return result
'''

STARLARK_CREATE = '''
def transform(req):
    return {"action": "create", "exec": "echo hello-from-ingress", "self_destruct": True}
'''

STARLARK_NONE = 'def transform(req):\n  return None'
STARLARK_ERROR = 'def transform(req):\n  return req["nonexistent"]["key"]'
STARLARK_INVALID_ACTION = 'def transform(req):\n  return {"action": "invalid"}'

STARLARK_ECHO_FIELDS = '''
def transform(req):
    return {
        "action": "create",
        "exec": "echo got-body:%s got-query:%s got-header:%s" % (
            str(req["body_json"]),
            str(req["query_params"].get("foo", "none")),
            str(req["headers"].get("x-test-header", "none")),
        ),
        "self_destruct": True,
    }
'''

STARLARK_CONDITIONAL_NONE = '''
def transform(req):
    body = req["body_json"]
    if body and body.get("skip"):
        return None
    return {"action": "create", "exec": "echo ok", "self_destruct": True}
'''


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=API_URL, headers=HEADERS, timeout=60.0
    ) as c:
        yield c


@pytest.fixture
async def long_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=API_URL, headers=HEADERS, timeout=120.0
    ) as c:
        yield c


@pytest.fixture
async def ingress_client() -> AsyncIterator[httpx.AsyncClient]:
    """Client without auth headers for unauthenticated ingress calls."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=120.0) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def create_rule(
    client: httpx.AsyncClient, name: str, source: str, **kwargs: object
) -> dict[str, object]:
    body = {"name": name, "starlark_source": source, **kwargs}
    resp = await client.post("/ingress_rules", json=body)
    resp.raise_for_status()
    return resp.json()


async def delete_rule(client: httpx.AsyncClient, rule_id: str) -> None:
    try:
        await client.delete(f"/ingress_rules/{rule_id}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# T13.1 — Rule CRUD Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_crud_lifecycle(client: httpx.AsyncClient) -> None:
    """Create, list, get, update, delete a rule."""
    rule = await create_rule(client, "test-crud", STARLARK_FORK)
    rule_id = rule["id"]
    try:
        # Create returns id starting with ir_ and an ingress_url
        assert isinstance(rule_id, str) and rule_id.startswith("ir_")
        assert "ingress_url" in rule
        assert rule["name"] == "test-crud"

        # List rules — ours should be present
        resp = await client.get("/ingress_rules")
        resp.raise_for_status()
        rules = resp.json()
        rule_ids = [r["id"] for r in rules]
        assert rule_id in rule_ids

        # Get by ID — should include starlark_source
        resp = await client.get(f"/ingress_rules/{rule_id}")
        resp.raise_for_status()
        detail = resp.json()
        assert "starlark_source" in detail
        assert "transform" in detail["starlark_source"]

        # Update name
        resp = await client.put(
            f"/ingress_rules/{rule_id}",
            json={"name": "test-crud-updated"},
        )
        resp.raise_for_status()
        assert resp.json()["name"] == "test-crud-updated"

        # Verify update persisted
        resp = await client.get(f"/ingress_rules/{rule_id}")
        resp.raise_for_status()
        assert resp.json()["name"] == "test-crud-updated"
    finally:
        # Delete
        resp = await client.delete(f"/ingress_rules/{rule_id}")
        assert resp.status_code == 204

    # After delete, get should 404
    resp = await client.get(f"/ingress_rules/{rule_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# T13.2 — Rule Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_starlark_syntax(client: httpx.AsyncClient) -> None:
    """Create rule with invalid Starlark syntax returns 422."""
    resp = await client.post(
        "/ingress_rules",
        json={"name": "bad-syntax", "starlark_source": "def transform(req:\n  pass"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_starlark_missing_transform(client: httpx.AsyncClient) -> None:
    """Create rule with valid Starlark but no transform function returns 422."""
    resp = await client.post(
        "/ingress_rules",
        json={"name": "no-transform", "starlark_source": "def foo(req):\n  return None"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_with_invalid_starlark(client: httpx.AsyncClient) -> None:
    """Update a rule with invalid Starlark returns 422; old rule unchanged."""
    rule = await create_rule(client, "update-invalid", STARLARK_FORK)
    rule_id = rule["id"]
    try:
        resp = await client.put(
            f"/ingress_rules/{rule_id}",
            json={"starlark_source": "def transform(req:\n  bad"},
        )
        assert resp.status_code == 422

        # Old rule is unchanged
        resp = await client.get(f"/ingress_rules/{rule_id}")
        resp.raise_for_status()
        assert "transform" in resp.json()["starlark_source"]
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.3 — Dry-Run Test Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_success(client: httpx.AsyncClient) -> None:
    """Test endpoint returns Starlark result with no validation errors."""
    rule = await create_rule(client, "test-dryrun", STARLARK_FORK)
    rule_id = rule["id"]
    try:
        resp = await client.post(
            f"/ingress_rules/{rule_id}/test",
            json={
                "method": "POST",
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"checkpoint_id": "ckpt-fake", "exec": "echo hi"}),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        assert data["starlark_result"]["action"] == "fork"
        assert data["starlark_result"]["checkpoint_id"] == "ckpt-fake"
        assert data["validation_errors"] == []
    finally:
        await delete_rule(client, rule_id)


@pytest.mark.asyncio
async def test_dry_run_starlark_error(client: httpx.AsyncClient) -> None:
    """Test endpoint with Starlark runtime error returns error in validation_errors."""
    rule = await create_rule(client, "test-dryrun-err", STARLARK_ERROR)
    rule_id = rule["id"]
    try:
        resp = await client.post(
            f"/ingress_rules/{rule_id}/test",
            json={"method": "POST", "body": "{}"},
        )
        resp.raise_for_status()
        data = resp.json()
        assert len(data["validation_errors"]) > 0
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.4 — Ingress Trigger (Async Fork)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingress_async_fork(
    long_client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Ingress trigger forks from checkpoint asynchronously."""
    # Setup: create computer, checkpoint, destroy
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-ingress-async")
    await destroy_computer(long_client, comp_id)

    rule = await create_rule(
        long_client, "async-fork", STARLARK_FORK, response_mode="async",
    )
    rule_id = rule["id"]
    try:
        # Trigger ingress
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={
                "checkpoint_id": ckpt_id,
                "exec": "echo ingress-works",
                "self_destruct": True,
            },
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 202

        # Wait for the async fork to complete
        new_ckpt_id = None
        for _ in range(30):
            await asyncio.sleep(2)
            ckpts_resp = await long_client.get(
                "/checkpoints", params={"label": "test-ingress-async"},
            )
            ckpts_resp.raise_for_status()
            ckpts = ckpts_resp.json()
            if len(ckpts) >= 2:
                # Find the new checkpoint (not the original)
                for c in ckpts:
                    if c["id"] != ckpt_id:
                        new_ckpt_id = c["id"]
                        break
                break

        assert new_ckpt_id is not None, "Expected new checkpoint from async fork"

        # Clean up checkpoints
        ckpts_resp = await long_client.get(
            "/checkpoints", params={"label": "test-ingress-async"},
        )
        for c in ckpts_resp.json():
            await delete_checkpoint(long_client, c["id"])
    finally:
        await delete_rule(long_client, rule_id)


# ---------------------------------------------------------------------------
# T13.5 — Ingress Trigger (Sync Fork)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingress_sync_fork(
    long_client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Ingress trigger forks synchronously and returns exec output."""
    comp_id = await create_computer(long_client)
    ckpt_id = await checkpoint_computer(long_client, comp_id, label="test-ingress-sync")
    await destroy_computer(long_client, comp_id)

    rule = await create_rule(
        long_client, "sync-fork", STARLARK_FORK, response_mode="sync",
    )
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={
                "checkpoint_id": ckpt_id,
                "exec": "echo ingress-works",
                "self_destruct": True,
            },
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "ingress-works" in (data.get("exec_stdout") or "")
        assert data.get("computer_id") is not None
        assert data.get("created_checkpoint_id") is not None

        # Clean up checkpoints
        ckpts_resp = await long_client.get(
            "/checkpoints", params={"label": "test-ingress-sync"},
        )
        for c in ckpts_resp.json():
            await delete_checkpoint(long_client, c["id"])
    finally:
        await delete_rule(long_client, rule_id)


# ---------------------------------------------------------------------------
# T13.6 — Ingress Trigger (Create)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingress_sync_create(
    long_client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Ingress trigger creates a computer synchronously with exec output."""
    rule = await create_rule(
        long_client, "sync-create", STARLARK_CREATE, response_mode="sync",
    )
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "hello-from-ingress" in (data.get("exec_stdout") or "")

        # Self-destruct should have created a checkpoint
        if data.get("created_checkpoint_id"):
            await delete_checkpoint(long_client, data["created_checkpoint_id"])
    finally:
        await delete_rule(long_client, rule_id)


# ---------------------------------------------------------------------------
# T13.7 — Starlark Transform Correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starlark_transform_fields(
    long_client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Starlark transform correctly receives body_json, query_params, headers."""
    rule = await create_rule(
        long_client, "fields-test", STARLARK_ECHO_FIELDS, response_mode="sync",
    )
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}?foo=bar",
            json={"key": "value"},
            headers={"content-type": "application/json", "x-test-header": "present"},
        )
        assert resp.status_code == 200
        data = resp.json()
        stdout = data.get("exec_stdout") or ""
        assert "got-query:bar" in stdout
        assert "got-header:present" in stdout

        if data.get("created_checkpoint_id"):
            await delete_checkpoint(long_client, data["created_checkpoint_id"])
    finally:
        await delete_rule(long_client, rule_id)


@pytest.mark.asyncio
async def test_starlark_body_json_none_for_non_json(
    client: httpx.AsyncClient,
) -> None:
    """body_json is None when content is not JSON — test via dry-run."""
    rule = await create_rule(client, "nonjson-test", STARLARK_FORK)
    rule_id = rule["id"]
    try:
        resp = await client.post(
            f"/ingress_rules/{rule_id}/test",
            json={
                "method": "POST",
                "headers": {"content-type": "text/plain"},
                "body": "not json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # body_json is None so STARLARK_FORK returns None
        assert data["starlark_result"] is None
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.8 — Ingress Returns None (204)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingress_returns_none_204(
    long_client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Ingress trigger returns 204 when Starlark transform returns None."""
    rule = await create_rule(long_client, "none-rule", STARLARK_CONDITIONAL_NONE)
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={"skip": True},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 204
    finally:
        await delete_rule(long_client, rule_id)


# ---------------------------------------------------------------------------
# T13.9 — Ingress Error Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingress_nonexistent_rule_404(
    ingress_client: httpx.AsyncClient,
) -> None:
    """POST to non-existent rule ID returns 404."""
    resp = await ingress_client.post(
        "/ingress/ir_does_not_exist",
        json={},
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ingress_disabled_rule_404(
    client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """POST to disabled rule returns 404."""
    rule = await create_rule(client, "disabled-rule", STARLARK_NONE)
    rule_id = rule["id"]
    try:
        # Disable the rule
        resp = await client.put(
            f"/ingress_rules/{rule_id}", json={"enabled": False},
        )
        resp.raise_for_status()

        # Ingress should 404
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 404
    finally:
        await delete_rule(client, rule_id)


@pytest.mark.asyncio
async def test_ingress_body_too_large_413(
    client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """POST with body exceeding max_body_bytes returns 413."""
    rule = await create_rule(
        client, "small-body", STARLARK_NONE, max_body_bytes=1024,
    )
    rule_id = rule["id"]
    try:
        big_body = "x" * 2048
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            content=big_body,
            headers={"content-type": "text/plain", "content-length": str(len(big_body))},
        )
        assert resp.status_code == 413
    finally:
        await delete_rule(client, rule_id)


@pytest.mark.asyncio
async def test_ingress_starlark_runtime_error_502(
    client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Starlark runtime error returns 502."""
    rule = await create_rule(client, "runtime-err", STARLARK_ERROR)
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={"some": "data"},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 502
    finally:
        await delete_rule(client, rule_id)


@pytest.mark.asyncio
async def test_ingress_invalid_action_502(
    client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Starlark returns invalid action returns 502."""
    rule = await create_rule(client, "bad-action", STARLARK_INVALID_ACTION)
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 502
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.10 — Rate Limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiting(
    client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Rate limiting enforces rate_limit_rpm — excess requests get 429."""
    rule = await create_rule(
        client, "rate-limit", STARLARK_NONE, rate_limit_rpm=5,
    )
    rule_id = rule["id"]
    try:
        statuses: list[int] = []
        for _ in range(10):
            resp = await ingress_client.post(
                f"/ingress/{rule_id}",
                json={"skip": True},
                headers={"content-type": "application/json"},
            )
            statuses.append(resp.status_code)

        # At least 1 should be 204 (success) and at least 1 should be 429 (limited)
        assert 204 in statuses, f"Expected at least one 204, got {statuses}"
        assert 429 in statuses, f"Expected at least one 429, got {statuses}"
        # The 429s should come after the successes
        first_429 = statuses.index(429)
        assert first_429 >= 4, f"Expected 429 to start at index >= 4, started at {first_429}"
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.11 — Rule Rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_rotation(
    client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Rotating a rule ID invalidates old URL and creates new working URL."""
    rule = await create_rule(client, "rotate-test", STARLARK_NONE)
    old_id = rule["id"]
    try:
        # Old URL works
        resp = await ingress_client.post(
            f"/ingress/{old_id}",
            json={},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 204

        # Rotate
        resp = await client.post(f"/ingress_rules/{old_id}/rotate")
        resp.raise_for_status()
        new_id = resp.json()["id"]
        assert new_id != old_id
        assert new_id.startswith("ir_")

        # Old URL should 404
        resp = await ingress_client.post(
            f"/ingress/{old_id}",
            json={},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 404

        # New URL should work
        resp = await ingress_client.post(
            f"/ingress/{new_id}",
            json={},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 204
    finally:
        # Clean up — use whichever ID exists
        await delete_rule(client, new_id if "new_id" in dir() else old_id)


# ---------------------------------------------------------------------------
# T13.12 — Ingress Logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingress_logs(
    client: httpx.AsyncClient,
    ingress_client: httpx.AsyncClient,
) -> None:
    """Ingress invocations are logged with correct statuses."""
    rule = await create_rule(client, "logs-test", STARLARK_CONDITIONAL_NONE)
    rule_id = rule["id"]
    try:
        # Trigger: None path (204 = completed)
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={"skip": True},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 204

        # Trigger: runtime error (use a different rule that errors)
        err_rule = await create_rule(client, "logs-err", STARLARK_ERROR)
        err_rule_id = err_rule["id"]
        try:
            resp = await ingress_client.post(
                f"/ingress/{err_rule_id}",
                json={"data": 1},
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 502

            # Check logs for the error rule
            resp = await client.get(f"/ingress_rules/{err_rule_id}/logs")
            resp.raise_for_status()
            err_logs = resp.json()
            assert len(err_logs) >= 1
            assert any(log["status"] == "failed" for log in err_logs)
        finally:
            await delete_rule(client, err_rule_id)

        # Check logs for the main rule
        resp = await client.get(f"/ingress_rules/{rule_id}/logs")
        resp.raise_for_status()
        logs = resp.json()
        assert len(logs) >= 1
        assert any(log["status"] == "completed" for log in logs)
    finally:
        await delete_rule(client, rule_id)
