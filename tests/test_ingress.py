from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

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


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(tmp_path / "test.db")
    await run_migrations(db, Path("migrations"))
    await db.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        ("acct-test", "test-key", 10, "2026-01-01T00:00:00Z"),
    )
    await db.commit()
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
