from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from mshkn.db import run_migrations
from mshkn.ingress.db import (
    delete_ingress_rule,
    get_ingress_rule_by_id,
    insert_ingress_log,
    insert_ingress_rule,
    list_ingress_logs,
    list_ingress_rules_by_account,
    prune_old_ingress_logs,
    rotate_ingress_rule_id,
    update_ingress_rule,
)
from mshkn.ingress.models import IngressLog, IngressRule
from mshkn.main import app


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(tmp_path / "test.db")
    await run_migrations(db, Path("migrations"))
    await db.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        ("acct-test", "test-key", 10, "2026-01-01T00:00:00Z"),
    )
    await db.commit()
    return db


AUTH_HEADERS = {"Authorization": "Bearer test-key-123"}


@dataclass(frozen=True)
class _FakeConfig:
    domain: str = "mshkn.dev"


async def _setup_app_db(tmp_path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(tmp_path / "test.db")
    await run_migrations(db, Path("migrations"))
    await db.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        ("acct-test", "test-key-123", 10, "2026-01-01T00:00:00Z"),
    )
    await db.commit()
    app.state.db = db
    app.state.config = _FakeConfig()
    app.state.vm_manager = AsyncMock()
    return db


def _make_rule(**overrides: object) -> IngressRule:
    defaults = {
        "internal_id": "int-001",
        "id": "ir_test123",
        "account_id": "acct-test",
        "name": "test-rule",
        "starlark_source": 'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}',
        "response_mode": "async",
        "max_body_bytes": 10485760,
        "rate_limit_rpm": 60,
        "enabled": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return IngressRule(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_insert_and_get_rule(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    rule = _make_rule()
    await insert_ingress_rule(db, rule)
    fetched = await get_ingress_rule_by_id(db, "ir_test123")
    assert fetched is not None
    assert fetched.name == "test-rule"
    assert fetched.internal_id == "int-001"
    await db.close()


@pytest.mark.asyncio
async def test_list_rules_by_account(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    await insert_ingress_rule(db, _make_rule(internal_id="a", id="ir_a", name="rule-a"))
    await insert_ingress_rule(db, _make_rule(internal_id="b", id="ir_b", name="rule-b"))
    rules = await list_ingress_rules_by_account(db, "acct-test")
    assert len(rules) == 2
    await db.close()


@pytest.mark.asyncio
async def test_update_rule(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    rule = _make_rule()
    await insert_ingress_rule(db, rule)
    rule.name = "updated-name"
    rule.enabled = False
    await update_ingress_rule(db, rule)
    fetched = await get_ingress_rule_by_id(db, "ir_test123")
    assert fetched is not None
    assert fetched.name == "updated-name"
    assert fetched.enabled is False
    await db.close()


@pytest.mark.asyncio
async def test_rotate_rule_id(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    await insert_ingress_rule(db, _make_rule())
    await rotate_ingress_rule_id(db, "int-001", "ir_new456")
    assert await get_ingress_rule_by_id(db, "ir_test123") is None
    fetched = await get_ingress_rule_by_id(db, "ir_new456")
    assert fetched is not None
    assert fetched.internal_id == "int-001"
    await db.close()


@pytest.mark.asyncio
async def test_delete_rule(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    await insert_ingress_rule(db, _make_rule())
    await delete_ingress_rule(db, "ir_test123")
    assert await get_ingress_rule_by_id(db, "ir_test123") is None
    await db.close()


@pytest.mark.asyncio
async def test_ingress_log_crud(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    await insert_ingress_rule(db, _make_rule())
    log = IngressLog(
        id="log-001",
        rule_internal_id="int-001",
        status="completed",
        starlark_result='{"action": "fork"}',
        error_message=None,
        created_at="2026-01-01T00:00:00Z",
    )
    await insert_ingress_log(db, log)
    logs = await list_ingress_logs(db, "int-001")
    assert len(logs) == 1
    assert logs[0].status == "completed"
    await db.close()


@pytest.mark.asyncio
async def test_prune_old_logs(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    await insert_ingress_rule(db, _make_rule())
    await insert_ingress_log(
        db,
        IngressLog(
            id="old",
            rule_internal_id="int-001",
            status="completed",
            starlark_result=None,
            error_message=None,
            created_at="2020-01-01T00:00:00Z",
        ),
    )
    await insert_ingress_log(
        db,
        IngressLog(
            id="new",
            rule_internal_id="int-001",
            status="completed",
            starlark_result=None,
            error_message=None,
            created_at="2099-01-01T00:00:00Z",
        ),
    )
    pruned = await prune_old_ingress_logs(db, "2026-01-01T00:00:00Z")
    assert pruned == 1
    logs = await list_ingress_logs(db, "int-001")
    assert len(logs) == 1
    assert logs[0].id == "new"
    await db.close()


# --- Starlark sandbox tests ---

from mshkn.ingress.starlark import StarlarkError, execute_transform, validate_starlark


def test_validate_starlark_valid() -> None:
    source = 'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}'
    errors = validate_starlark(source)
    assert errors == []


def test_validate_starlark_no_transform() -> None:
    source = "def other(req):\n  return None"
    errors = validate_starlark(source)
    assert len(errors) == 1
    assert "transform" in errors[0]


def test_validate_starlark_syntax_error() -> None:
    source = "def transform(req):\n  return {{{{"
    errors = validate_starlark(source)
    assert len(errors) >= 1


def test_execute_transform_fork() -> None:
    source = 'def transform(req):\n  return {"action": "fork", "checkpoint_id": req["body_json"]["cp"]}'
    req = {
        "method": "POST",
        "path": "/webhook",
        "headers": {},
        "query_params": {},
        "body_json": {"cp": "cp_abc"},
        "body_form": None,
        "body_raw": '{"cp": "cp_abc"}',
        "content_type": "application/json",
    }
    result = execute_transform(source, req)
    assert result == {"action": "fork", "checkpoint_id": "cp_abc"}


def test_execute_transform_returns_none() -> None:
    source = "def transform(req):\n  return None"
    req = {
        "method": "GET",
        "path": "/",
        "headers": {},
        "query_params": {},
        "body_json": None,
        "body_form": None,
        "body_raw": "",
        "content_type": "",
    }
    result = execute_transform(source, req)
    assert result is None


def test_execute_transform_runtime_error() -> None:
    source = 'def transform(req):\n  return req["nonexistent"]["key"]'
    req = {
        "method": "GET",
        "path": "/",
        "headers": {},
        "query_params": {},
        "body_json": None,
        "body_form": None,
        "body_raw": "",
        "content_type": "",
    }
    with pytest.raises(StarlarkError):
        execute_transform(source, req)


# --- API endpoint tests ---

from mshkn.api.ingress import _validate_transform_result


def test_validate_transform_result_none() -> None:
    assert _validate_transform_result(None) == []


def test_validate_transform_result_valid_fork() -> None:
    result = {"action": "fork", "checkpoint_id": "cp_1"}
    assert _validate_transform_result(result) == []


def test_validate_transform_result_fork_missing_checkpoint() -> None:
    result = {"action": "fork"}
    errors = _validate_transform_result(result)
    assert len(errors) == 1
    assert "checkpoint_id" in errors[0]


def test_validate_transform_result_unknown_action() -> None:
    result = {"action": "restart"}
    errors = _validate_transform_result(result)
    assert len(errors) >= 1


def test_validate_transform_result_unknown_fields() -> None:
    result = {"action": "fork", "checkpoint_id": "cp_1", "bogus": True}
    errors = _validate_transform_result(result)
    assert len(errors) == 1
    assert "bogus" in errors[0]


def test_validate_transform_result_valid_create() -> None:
    result = {"action": "create", "uses": ["python"]}
    assert _validate_transform_result(result) == []


def test_validate_transform_result_invalid_exclusive() -> None:
    result = {"action": "fork", "checkpoint_id": "cp_1", "exclusive": "wrong"}
    errors = _validate_transform_result(result)
    assert len(errors) == 1
    assert "exclusive" in errors[0]


@pytest.mark.asyncio
async def test_api_create_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/ingress_rules",
                headers=AUTH_HEADERS,
                json={
                    "name": "my-rule",
                    "starlark_source": 'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}',
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "my-rule"
            assert data["id"].startswith("ir_")
            assert data["enabled"] is True
            assert "ingress_url" in data
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_create_rule_invalid_starlark(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
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
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_list_rules(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Create two rules
            for name in ("rule-a", "rule-b"):
                await client.post(
                    "/ingress_rules",
                    headers=AUTH_HEADERS,
                    json={
                        "name": name,
                        "starlark_source": 'def transform(req):\n  return None',
                    },
                )
            resp = await client.get("/ingress_rules", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            assert len(resp.json()) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_get_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_resp = await client.post(
                "/ingress_rules",
                headers=AUTH_HEADERS,
                json={
                    "name": "get-me",
                    "starlark_source": 'def transform(req):\n  return None',
                },
            )
            rule_id = create_resp.json()["id"]
            resp = await client.get(
                f"/ingress_rules/{rule_id}", headers=AUTH_HEADERS,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "get-me"
            assert "starlark_source" in data
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_delete_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_resp = await client.post(
                "/ingress_rules",
                headers=AUTH_HEADERS,
                json={
                    "name": "delete-me",
                    "starlark_source": 'def transform(req):\n  return None',
                },
            )
            rule_id = create_resp.json()["id"]
            resp = await client.delete(
                f"/ingress_rules/{rule_id}", headers=AUTH_HEADERS,
            )
            assert resp.status_code == 204

            # Verify it's gone
            resp2 = await client.get(
                f"/ingress_rules/{rule_id}", headers=AUTH_HEADERS,
            )
            assert resp2.status_code == 404
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_rotate_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_resp = await client.post(
                "/ingress_rules",
                headers=AUTH_HEADERS,
                json={
                    "name": "rotate-me",
                    "starlark_source": 'def transform(req):\n  return None',
                },
            )
            old_id = create_resp.json()["id"]
            resp = await client.post(
                f"/ingress_rules/{old_id}/rotate", headers=AUTH_HEADERS,
            )
            assert resp.status_code == 200
            new_id = resp.json()["id"]
            assert new_id != old_id
            assert new_id.startswith("ir_")

            # Old ID gone
            resp2 = await client.get(
                f"/ingress_rules/{old_id}", headers=AUTH_HEADERS,
            )
            assert resp2.status_code == 404
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_test_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_resp = await client.post(
                "/ingress_rules",
                headers=AUTH_HEADERS,
                json={
                    "name": "test-rule",
                    "starlark_source": 'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}',
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
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_requires_auth(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/ingress_rules")
            assert resp.status_code == 401
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_trigger_404_unknown_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/ingress/ir_nonexistent")
            assert resp.status_code == 404
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_trigger_disabled_rule_404(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        # Insert a disabled rule directly
        from mshkn.ingress.db import insert_ingress_rule as _ins
        rule = IngressRule(
            internal_id="int-dis",
            id="ir_disabled",
            account_id="acct-test",
            name="disabled",
            starlark_source='def transform(req):\n  return None',
            response_mode="async",
            max_body_bytes=10485760,
            rate_limit_rpm=60,
            enabled=False,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        await _ins(db, rule)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/ingress/ir_disabled")
            assert resp.status_code == 404
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_trigger_none_returns_204(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        from mshkn.ingress.db import insert_ingress_rule as _ins
        rule = IngressRule(
            internal_id="int-none",
            id="ir_none_result",
            account_id="acct-test",
            name="none-rule",
            starlark_source='def transform(req):\n  return None',
            response_mode="async",
            max_body_bytes=10485760,
            rate_limit_rpm=60,
            enabled=True,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        await _ins(db, rule)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/ingress/ir_none_result")
            assert resp.status_code == 204
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_trigger_starlark_error_502(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        from mshkn.ingress.db import insert_ingress_rule as _ins
        rule = IngressRule(
            internal_id="int-err",
            id="ir_starlark_error",
            account_id="acct-test",
            name="error-rule",
            starlark_source='def transform(req):\n  return req["nonexistent"]["key"]',
            response_mode="async",
            max_body_bytes=10485760,
            rate_limit_rpm=60,
            enabled=True,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        await _ins(db, rule)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/ingress/ir_starlark_error")
            assert resp.status_code == 502
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_trigger_invalid_action_502(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    try:
        from mshkn.ingress.db import insert_ingress_rule as _ins
        rule = IngressRule(
            internal_id="int-bad",
            id="ir_bad_action",
            account_id="acct-test",
            name="bad-action-rule",
            starlark_source='def transform(req):\n  return {"action": "restart"}',
            response_mode="async",
            max_body_bytes=10485760,
            rate_limit_rpm=60,
            enabled=True,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        await _ins(db, rule)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/ingress/ir_bad_action")
            assert resp.status_code == 502
    finally:
        await db.close()
