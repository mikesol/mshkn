from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from httpx import ASGITransport, AsyncClient

from mshkn.config import Config
from mshkn.db.ingress import (
    delete_ingress_rule,
    get_ingress_rule_by_id,
    insert_ingress_log,
    insert_ingress_rule,
    list_ingress_logs,
    list_ingress_rules_by_account,
    rotate_ingress_rule_id,
    update_ingress_rule,
)
from mshkn.models import IngressLog, IngressLogStatus, IngressRule
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite
    from fastapi import FastAPI

AUTH_HEADERS = {"Authorization": "Bearer test-key-123"}


async def _account(db: aiosqlite.Connection, api_key: str = "test-key-123") -> None:
    await db.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        ("acct-test", api_key, 10, "2026-01-01T00:00:00Z"),
    )
    await db.commit()


async def _app(db: aiosqlite.Connection, tmp_path: Path) -> FastAPI:
    """An app whose runtime uses the production default domain (mshkn.dev)."""
    await _account(db)
    return make_app(make_runtime(db, config=Config(checkpoint_local_dir=tmp_path / "ckpts")))


_BASE_RULE = IngressRule(
    internal_id="int-001",
    id="ir_test123",
    account_id="acct-test",
    name="test-rule",
    starlark_source=('def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}'),
    response_mode="async",
    max_body_bytes=10485760,
    rate_limit_rpm=60,
    enabled=True,
    created_at="2026-01-01T00:00:00Z",
    updated_at="2026-01-01T00:00:00Z",
)


def _make_rule(**overrides: Any) -> IngressRule:
    return replace(_BASE_RULE, **overrides)


async def test_insert_and_get_rule(db: aiosqlite.Connection) -> None:
    await _account(db, "test-key")
    await insert_ingress_rule(db, _make_rule())
    fetched = await get_ingress_rule_by_id(db, "ir_test123")
    assert fetched is not None
    assert fetched.name == "test-rule"
    assert fetched.internal_id == "int-001"


async def test_list_rules_by_account(db: aiosqlite.Connection) -> None:
    await _account(db, "test-key")
    await insert_ingress_rule(db, _make_rule(internal_id="a", id="ir_a", name="rule-a"))
    await insert_ingress_rule(db, _make_rule(internal_id="b", id="ir_b", name="rule-b"))
    rules = await list_ingress_rules_by_account(db, "acct-test")
    assert len(rules) == 2


async def test_update_rule(db: aiosqlite.Connection) -> None:
    await _account(db, "test-key")
    rule = _make_rule()
    await insert_ingress_rule(db, rule)
    rule.name = "updated-name"
    rule.enabled = False
    await update_ingress_rule(db, rule)
    fetched = await get_ingress_rule_by_id(db, "ir_test123")
    assert fetched is not None
    assert fetched.name == "updated-name"
    assert fetched.enabled is False


async def test_rotate_rule_id(db: aiosqlite.Connection) -> None:
    await _account(db, "test-key")
    await insert_ingress_rule(db, _make_rule())
    await rotate_ingress_rule_id(db, "int-001", "ir_new456")
    assert await get_ingress_rule_by_id(db, "ir_test123") is None
    fetched = await get_ingress_rule_by_id(db, "ir_new456")
    assert fetched is not None
    assert fetched.internal_id == "int-001"


async def test_delete_rule(db: aiosqlite.Connection) -> None:
    await _account(db, "test-key")
    await insert_ingress_rule(db, _make_rule())
    await delete_ingress_rule(db, "ir_test123")
    assert await get_ingress_rule_by_id(db, "ir_test123") is None


async def test_ingress_log_crud(db: aiosqlite.Connection) -> None:
    await _account(db, "test-key")
    await insert_ingress_rule(db, _make_rule())
    log = IngressLog(
        id="log-001",
        rule_internal_id="int-001",
        status=IngressLogStatus.COMPLETED,
        starlark_result='{"action": "fork"}',
        error_message=None,
        created_at="2026-01-01T00:00:00Z",
    )
    await insert_ingress_log(db, log)
    logs = await list_ingress_logs(db, "int-001")
    assert len(logs) == 1
    assert logs[0].status == IngressLogStatus.COMPLETED


# --- API endpoint tests ---


async def test_api_create_rule(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/ingress_rules",
            headers=AUTH_HEADERS,
            json={
                "name": "my-rule",
                "starlark_source": (
                    'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}'
                ),
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "my-rule"
    assert data["id"].startswith("ir_")
    assert data["enabled"] is True
    assert data["ingress_url"] == f"https://mshkn.dev/ingress/{data['id']}"


async def test_api_create_rule_invalid_starlark(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/ingress_rules",
            headers=AUTH_HEADERS,
            json={
                "name": "bad-rule",
                "starlark_source": "def other(req):\n  return None",
            },
        )
    assert resp.status_code == 422
    assert "starlark_errors" in resp.json()["detail"]


async def test_api_list_rules(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for name in ("rule-a", "rule-b"):
            await client.post(
                "/ingress_rules",
                headers=AUTH_HEADERS,
                json={
                    "name": name,
                    "starlark_source": "def transform(req):\n  return None",
                },
            )
        resp = await client.get("/ingress_rules", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_api_get_rule(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            headers=AUTH_HEADERS,
            json={
                "name": "get-me",
                "starlark_source": "def transform(req):\n  return None",
            },
        )
        rule_id = create_resp.json()["id"]
        resp = await client.get(f"/ingress_rules/{rule_id}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "get-me"
    assert "starlark_source" in data


async def test_api_delete_rule(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            headers=AUTH_HEADERS,
            json={
                "name": "delete-me",
                "starlark_source": "def transform(req):\n  return None",
            },
        )
        rule_id = create_resp.json()["id"]
        resp = await client.delete(f"/ingress_rules/{rule_id}", headers=AUTH_HEADERS)
        assert resp.status_code == 204

        # Verify it's gone
        resp2 = await client.get(f"/ingress_rules/{rule_id}", headers=AUTH_HEADERS)
        assert resp2.status_code == 404


async def test_api_rotate_rule(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            headers=AUTH_HEADERS,
            json={
                "name": "rotate-me",
                "starlark_source": "def transform(req):\n  return None",
            },
        )
        old_id = create_resp.json()["id"]
        resp = await client.post(f"/ingress_rules/{old_id}/rotate", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        new_id = resp.json()["id"]
        assert new_id != old_id
        assert new_id.startswith("ir_")

        # Old ID gone
        resp2 = await client.get(f"/ingress_rules/{old_id}", headers=AUTH_HEADERS)
        assert resp2.status_code == 404


async def test_api_test_rule(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            headers=AUTH_HEADERS,
            json={
                "name": "test-rule",
                "starlark_source": (
                    'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}'
                ),
            },
        )
        rule_id = create_resp.json()["id"]
        resp = await client.post(
            f"/ingress_rules/{rule_id}/test",
            headers=AUTH_HEADERS,
            json={"method": "POST", "path": "/hook"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["starlark_result"]["action"] == "fork"
    assert data["validation_errors"] == []
    assert data["execution_time_ms"] >= 0


async def test_api_requires_auth(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/ingress_rules")
    assert resp.status_code == 401


async def test_trigger_404_unknown_rule(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/ingress/ir_nonexistent")
    assert resp.status_code == 404


async def test_trigger_disabled_rule_404(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    await insert_ingress_rule(
        db,
        _make_rule(
            internal_id="int-dis",
            id="ir_disabled",
            name="disabled",
            starlark_source="def transform(req):\n  return None",
            enabled=False,
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/ingress/ir_disabled")
    assert resp.status_code == 404


async def test_trigger_none_returns_204(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    await insert_ingress_rule(
        db,
        _make_rule(
            internal_id="int-none",
            id="ir_none_result",
            name="none-rule",
            starlark_source="def transform(req):\n  return None",
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/ingress/ir_none_result")
    assert resp.status_code == 204


async def test_trigger_starlark_error_502(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    await insert_ingress_rule(
        db,
        _make_rule(
            internal_id="int-err",
            id="ir_starlark_error",
            name="error-rule",
            starlark_source='def transform(req):\n  return req["nonexistent"]["key"]',
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/ingress/ir_starlark_error")
    assert resp.status_code == 502


async def test_trigger_invalid_action_502(db: aiosqlite.Connection, tmp_path: Path) -> None:
    app = await _app(db, tmp_path)
    await insert_ingress_rule(
        db,
        _make_rule(
            internal_id="int-bad",
            id="ir_bad_action",
            name="bad-action-rule",
            starlark_source='def transform(req):\n  return {"action": "restart"}',
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/ingress/ir_bad_action")
    assert resp.status_code == 502
