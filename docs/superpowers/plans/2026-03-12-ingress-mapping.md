# Ingress Mapping Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow external webhook sources to trigger disposable computer creation via user-defined Starlark transformation rules.

**Architecture:** New `ingress` module with CRUD API for rules (authenticated), an unauthenticated ingress endpoint that executes Starlark transforms, and integration with existing create/fork code paths. Starlark runs sandboxed in-process via `starlark-go`.

**Tech Stack:** FastAPI, aiosqlite, starlark-go (Python bindings for Google's reference Starlark implementation), Pydantic

**Spec:** `docs/superpowers/specs/2026-03-12-ingress-mapping-design.md`

---

## File Structure

| File | Purpose |
|------|---------|
| `migrations/008_ingress_rules.sql` | DB schema for ingress_rules and ingress_log tables |
| `src/mshkn/api/ingress.py` | All ingress API endpoints (CRUD + trigger + test + logs) |
| `src/mshkn/ingress/__init__.py` | Package init |
| `src/mshkn/ingress/starlark.py` | Starlark sandbox execution |
| `src/mshkn/ingress/models.py` | Pydantic request/response models for ingress |
| `src/mshkn/ingress/db.py` | DB operations for ingress_rules and ingress_log |
| `tests/test_ingress.py` | Unit tests for ingress (starlark, db, API) |
| `tests/e2e/test_phase13_ingress.py` | E2E tests against live server |
| `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` | Add Phase 13 chapter |

---

## Chunk 1: Foundation (DB + Models + Starlark Sandbox)

### Task 1: Database Migration

**Files:**
- Create: `migrations/008_ingress_rules.sql`

- [ ] **Step 1: Write the migration SQL**

```sql
-- Ingress rules: user-defined Starlark transforms that map external webhooks to API calls
CREATE TABLE IF NOT EXISTS ingress_rules (
    internal_id TEXT PRIMARY KEY,
    id TEXT UNIQUE NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    starlark_source TEXT NOT NULL,
    response_mode TEXT NOT NULL DEFAULT 'async',
    max_body_bytes INTEGER NOT NULL DEFAULT 10485760,
    rate_limit_rpm INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingress_rules_account_id ON ingress_rules(account_id);

CREATE TABLE IF NOT EXISTS ingress_log (
    id TEXT PRIMARY KEY,
    rule_internal_id TEXT NOT NULL REFERENCES ingress_rules(internal_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    starlark_result TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingress_log_rule_created ON ingress_log(rule_internal_id, created_at);
```

- [ ] **Step 2: Verify migration applies cleanly**

Run: `.venv/bin/python -c "import asyncio, aiosqlite; from pathlib import Path; from mshkn.db import run_migrations; asyncio.run((lambda: None)())"`

Actually, we'll verify this via the unit test in the next task.

- [ ] **Step 3: Commit**

```bash
git add migrations/008_ingress_rules.sql
git commit -m "feat(ingress): add migration 008 for ingress_rules and ingress_log tables"
```

### Task 2: Pydantic Models

**Files:**
- Create: `src/mshkn/ingress/__init__.py`
- Create: `src/mshkn/ingress/models.py`

- [ ] **Step 1: Create package init**

```python
# src/mshkn/ingress/__init__.py
```

Empty file.

- [ ] **Step 2: Write the models**

```python
# src/mshkn/ingress/models.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- DB dataclasses ---


@dataclass
class IngressRule:
    internal_id: str
    id: str
    account_id: str
    name: str
    starlark_source: str
    response_mode: str  # "async" | "sync"
    max_body_bytes: int
    rate_limit_rpm: int
    enabled: bool
    created_at: str
    updated_at: str


@dataclass
class IngressLog:
    id: str
    rule_internal_id: str
    status: str  # "accepted" | "completed" | "failed" | "rejected"
    starlark_result: str | None
    error_message: str | None
    created_at: str


# --- Pydantic request/response models ---


class IngressRuleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    starlark_source: str = Field(..., min_length=1)
    response_mode: Literal["async", "sync"] = "async"
    max_body_bytes: int = Field(default=10485760, ge=1024, le=104857600)
    rate_limit_rpm: int = Field(default=60, ge=1, le=10000)


class IngressRuleUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    starlark_source: str | None = Field(default=None, min_length=1)
    response_mode: Literal["async", "sync"] | None = None
    max_body_bytes: int | None = Field(default=None, ge=1024, le=104857600)
    rate_limit_rpm: int | None = Field(default=None, ge=1, le=10000)
    enabled: bool | None = None


class IngressRuleResponse(BaseModel):
    id: str
    name: str
    ingress_url: str
    response_mode: str
    max_body_bytes: int
    rate_limit_rpm: int
    enabled: bool
    created_at: str
    updated_at: str


class IngressTestRequest(BaseModel):
    method: str = "POST"
    path: str = "/"
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class IngressTestResponse(BaseModel):
    starlark_result: dict[str, Any] | None
    validation_errors: list[str]
    execution_time_ms: float


class IngressLogResponse(BaseModel):
    id: str
    status: str
    starlark_result: dict[str, Any] | None
    error_message: str | None
    created_at: str
```

- [ ] **Step 3: Commit**

```bash
git add src/mshkn/ingress/__init__.py src/mshkn/ingress/models.py
git commit -m "feat(ingress): add Pydantic models and DB dataclasses"
```

### Task 3: DB Operations

**Files:**
- Create: `src/mshkn/ingress/db.py`
- Test: `tests/test_ingress.py`

- [ ] **Step 1: Write the failing test for DB operations**

```python
# tests/test_ingress.py
from __future__ import annotations

from datetime import datetime, timezone
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
from mshkn.models import Account


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ingress.py -v`
Expected: ImportError (ingress.db module doesn't exist yet)

- [ ] **Step 3: Write the DB operations**

```python
# src/mshkn/ingress/db.py
from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.ingress.models import IngressLog, IngressRule

if TYPE_CHECKING:
    import aiosqlite


async def insert_ingress_rule(db: aiosqlite.Connection, rule: IngressRule) -> None:
    await db.execute(
        "INSERT INTO ingress_rules "
        "(internal_id, id, account_id, name, starlark_source, response_mode, "
        "max_body_bytes, rate_limit_rpm, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rule.internal_id,
            rule.id,
            rule.account_id,
            rule.name,
            rule.starlark_source,
            rule.response_mode,
            rule.max_body_bytes,
            rule.rate_limit_rpm,
            1 if rule.enabled else 0,
            rule.created_at,
            rule.updated_at,
        ),
    )
    await db.commit()


async def get_ingress_rule_by_id(
    db: aiosqlite.Connection, rule_id: str
) -> IngressRule | None:
    cursor = await db.execute(
        "SELECT internal_id, id, account_id, name, starlark_source, response_mode, "
        "max_body_bytes, rate_limit_rpm, enabled, created_at, updated_at "
        "FROM ingress_rules WHERE id = ?",
        (rule_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return IngressRule(
        internal_id=row[0],
        id=row[1],
        account_id=row[2],
        name=row[3],
        starlark_source=row[4],
        response_mode=row[5],
        max_body_bytes=row[6],
        rate_limit_rpm=row[7],
        enabled=bool(row[8]),
        created_at=row[9],
        updated_at=row[10],
    )


async def list_ingress_rules_by_account(
    db: aiosqlite.Connection, account_id: str
) -> list[IngressRule]:
    cursor = await db.execute(
        "SELECT internal_id, id, account_id, name, starlark_source, response_mode, "
        "max_body_bytes, rate_limit_rpm, enabled, created_at, updated_at "
        "FROM ingress_rules WHERE account_id = ? ORDER BY created_at",
        (account_id,),
    )
    rows = await cursor.fetchall()
    return [
        IngressRule(
            internal_id=r[0],
            id=r[1],
            account_id=r[2],
            name=r[3],
            starlark_source=r[4],
            response_mode=r[5],
            max_body_bytes=r[6],
            rate_limit_rpm=r[7],
            enabled=bool(r[8]),
            created_at=r[9],
            updated_at=r[10],
        )
        for r in rows
    ]


async def update_ingress_rule(db: aiosqlite.Connection, rule: IngressRule) -> None:
    await db.execute(
        "UPDATE ingress_rules SET name=?, starlark_source=?, response_mode=?, "
        "max_body_bytes=?, rate_limit_rpm=?, enabled=?, updated_at=? "
        "WHERE internal_id=?",
        (
            rule.name,
            rule.starlark_source,
            rule.response_mode,
            rule.max_body_bytes,
            rule.rate_limit_rpm,
            1 if rule.enabled else 0,
            rule.updated_at,
            rule.internal_id,
        ),
    )
    await db.commit()


async def rotate_ingress_rule_id(
    db: aiosqlite.Connection, internal_id: str, new_id: str
) -> None:
    await db.execute(
        "UPDATE ingress_rules SET id=?, updated_at=datetime('now') WHERE internal_id=?",
        (new_id, internal_id),
    )
    await db.commit()


async def delete_ingress_rule(db: aiosqlite.Connection, rule_id: str) -> None:
    # Get internal_id first to cascade-delete logs
    cursor = await db.execute(
        "SELECT internal_id FROM ingress_rules WHERE id=?", (rule_id,)
    )
    row = await cursor.fetchone()
    if row:
        await db.execute("DELETE FROM ingress_log WHERE rule_internal_id=?", (row[0],))
        await db.execute("DELETE FROM ingress_rules WHERE id=?", (rule_id,))
        await db.commit()


async def insert_ingress_log(db: aiosqlite.Connection, log: IngressLog) -> None:
    await db.execute(
        "INSERT INTO ingress_log (id, rule_internal_id, status, starlark_result, "
        "error_message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            log.id,
            log.rule_internal_id,
            log.status,
            log.starlark_result,
            log.error_message,
            log.created_at,
        ),
    )
    await db.commit()


async def list_ingress_logs(
    db: aiosqlite.Connection, rule_internal_id: str, limit: int = 100
) -> list[IngressLog]:
    cursor = await db.execute(
        "SELECT id, rule_internal_id, status, starlark_result, error_message, created_at "
        "FROM ingress_log WHERE rule_internal_id=? ORDER BY created_at DESC LIMIT ?",
        (rule_internal_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        IngressLog(
            id=r[0],
            rule_internal_id=r[1],
            status=r[2],
            starlark_result=r[3],
            error_message=r[4],
            created_at=r[5],
        )
        for r in rows
    ]


async def prune_old_ingress_logs(
    db: aiosqlite.Connection, before_timestamp: str
) -> int:
    cursor = await db.execute(
        "DELETE FROM ingress_log WHERE created_at < ?", (before_timestamp,)
    )
    await db.commit()
    return cursor.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ingress.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/ingress/db.py tests/test_ingress.py
git commit -m "feat(ingress): add DB operations with unit tests"
```

### Task 4: Starlark Sandbox

**Files:**
- Create: `src/mshkn/ingress/starlark.py`
- Modify: `tests/test_ingress.py` (add Starlark tests)

- [ ] **Step 1: Add starlark-go dependency**

Run: `cd /home/mikesol/Documents/GitHub/mshkn && .venv/bin/pip install starlark-go`

If `starlark-go` fails to build, fall back to `pystarlark`:
Run: `.venv/bin/pip install pystarlark`

Also add to pyproject.toml dependencies.

- [ ] **Step 2: Write failing tests for Starlark execution**

Append to `tests/test_ingress.py`:

```python
from mshkn.ingress.starlark import StarlarkError, validate_starlark, execute_transform


def test_validate_starlark_valid() -> None:
    source = 'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}'
    errors = validate_starlark(source)
    assert errors == []


def test_validate_starlark_no_transform() -> None:
    source = 'def other(req):\n  return None'
    errors = validate_starlark(source)
    assert len(errors) == 1
    assert "transform" in errors[0]


def test_validate_starlark_syntax_error() -> None:
    source = 'def transform(req):\n  return {{{{'
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
    source = 'def transform(req):\n  return None'
    req = {"method": "GET", "path": "/", "headers": {}, "query_params": {}, "body_json": None, "body_form": None, "body_raw": "", "content_type": ""}
    result = execute_transform(source, req)
    assert result is None


def test_execute_transform_runtime_error() -> None:
    source = 'def transform(req):\n  return req["nonexistent"]["key"]'
    req = {"method": "GET", "path": "/", "headers": {}, "query_params": {}, "body_json": None, "body_form": None, "body_raw": "", "content_type": ""}
    with pytest.raises(StarlarkError):
        execute_transform(source, req)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ingress.py -k starlark -v`
Expected: ImportError

- [ ] **Step 4: Implement the Starlark sandbox**

```python
# src/mshkn/ingress/starlark.py
from __future__ import annotations

import json
from typing import Any


class StarlarkError(Exception):
    """Raised when Starlark execution fails."""


def validate_starlark(source: str) -> list[str]:
    """Validate that source is valid Starlark with a transform function.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []
    try:
        import starlark_go  # type: ignore[import-untyped]

        globals_dict = starlark_go.eval(source)
    except Exception as exc:
        errors.append(f"Syntax error: {exc}")
        return errors

    # Check that transform was defined by inspecting the globals returned by eval.
    # NOTE: The exact starlark-go API may differ — the implementing agent should verify
    # how globals are returned (e.g. dict, module object, etc.) and adapt this check.
    # The key requirement: confirm 'transform' is a callable in the evaluated source.
    if not callable(globals_dict.get("transform")):
        errors.append("Starlark source must define a 'transform' function")
    return errors


def execute_transform(
    source: str,
    request_dict: dict[str, Any],
    timeout_ms: int = 1000,
) -> dict[str, Any] | None:
    """Execute the Starlark transform function against a request dict.

    Returns the transform result (dict or None).
    Raises StarlarkError on any execution failure.
    """
    try:
        import starlark_go  # type: ignore[import-untyped]

        # Starlark-go expects a call expression. We define the function,
        # then call it with the request as a JSON-decoded dict.
        call_source = source + "\n_result = transform(" + _to_starlark_literal(request_dict) + ")"
        globals_dict = starlark_go.eval(call_source)
        result = globals_dict["_result"]

        if result is None:
            return None
        if isinstance(result, dict):
            # Convert starlark dict to Python dict recursively
            return _starlark_to_python(result)
        msg = f"transform must return dict or None, got {type(result).__name__}"
        raise StarlarkError(msg)
    except StarlarkError:
        raise
    except Exception as exc:
        raise StarlarkError(str(exc)) from exc


def _to_starlark_literal(obj: Any) -> str:
    """Convert a Python object to a Starlark literal expression."""
    return json.dumps(obj)


def _starlark_to_python(obj: Any) -> Any:
    """Recursively convert Starlark types to Python types."""
    if isinstance(obj, dict):
        return {str(k): _starlark_to_python(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_starlark_to_python(v) for v in obj]
    return obj
```

**Note:** The exact `starlark-go` API may differ. The implementing agent should check the library's actual API (e.g. `starlark_go.eval()` vs `starlark_go.exec_file()`) and adjust accordingly. Key requirement: define + call `transform()`, capture the return value.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ingress.py -k starlark -v`
Expected: All 6 Starlark tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/mshkn/ingress/starlark.py tests/test_ingress.py pyproject.toml
git commit -m "feat(ingress): add Starlark sandbox with validation and execution"
```

---

## Chunk 2: CRUD API + Ingress Endpoint

### Task 5: CRUD API for Ingress Rules

**Files:**
- Create: `src/mshkn/api/ingress.py`
- Modify: `src/mshkn/main.py` (add router)
- Modify: `tests/test_ingress.py` (add API tests)

- [ ] **Step 1: Write failing API tests**

Append to `tests/test_ingress.py`:

```python
from httpx import ASGITransport, AsyncClient

from mshkn.main import app
from mshkn.db import run_migrations


async def _setup_app_db(tmp_path: Path) -> aiosqlite.Connection:
    """Set up a real DB and wire it to the app."""
    db = await aiosqlite.connect(tmp_path / "test.db")
    await run_migrations(db, Path("migrations"))
    await db.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        ("acct-test", "test-key-123", 10, "2026-01-01T00:00:00Z"),
    )
    await db.commit()
    app.state.db = db
    return db


AUTH_HEADERS = {"Authorization": "Bearer test-key-123"}
STARLARK_VALID = 'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}'


@pytest.mark.asyncio
async def test_create_ingress_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/ingress_rules",
            json={"name": "test-rule", "starlark_source": STARLARK_VALID},
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "test-rule"
    assert data["id"].startswith("ir_")
    assert "ingress_url" in data
    await db.close()


@pytest.mark.asyncio
async def test_create_rule_invalid_starlark(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/ingress_rules",
            json={"name": "bad", "starlark_source": "def other(): pass"},
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 400
    await db.close()


@pytest.mark.asyncio
async def test_list_ingress_rules(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/ingress_rules",
            json={"name": "r1", "starlark_source": STARLARK_VALID},
            headers=AUTH_HEADERS,
        )
        await client.post(
            "/ingress_rules",
            json={"name": "r2", "starlark_source": STARLARK_VALID},
            headers=AUTH_HEADERS,
        )
        resp = await client.get("/ingress_rules", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    await db.close()


@pytest.mark.asyncio
async def test_get_ingress_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            json={"name": "r1", "starlark_source": STARLARK_VALID},
            headers=AUTH_HEADERS,
        )
        rule_id = create_resp.json()["id"]
        resp = await client.get(f"/ingress_rules/{rule_id}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "r1"
    await db.close()


@pytest.mark.asyncio
async def test_delete_ingress_rule_api(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            json={"name": "r1", "starlark_source": STARLARK_VALID},
            headers=AUTH_HEADERS,
        )
        rule_id = create_resp.json()["id"]
        resp = await client.delete(f"/ingress_rules/{rule_id}", headers=AUTH_HEADERS)
    assert resp.status_code == 204
    await db.close()


@pytest.mark.asyncio
async def test_rotate_rule_id_api(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            json={"name": "r1", "starlark_source": STARLARK_VALID},
            headers=AUTH_HEADERS,
        )
        old_id = create_resp.json()["id"]
        resp = await client.post(
            f"/ingress_rules/{old_id}/rotate", headers=AUTH_HEADERS
        )
    assert resp.status_code == 200
    new_id = resp.json()["id"]
    assert new_id != old_id
    assert new_id.startswith("ir_")
    await db.close()


@pytest.mark.asyncio
async def test_test_endpoint(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            json={"name": "r1", "starlark_source": STARLARK_VALID},
            headers=AUTH_HEADERS,
        )
        rule_id = create_resp.json()["id"]
        resp = await client.post(
            f"/ingress_rules/{rule_id}/test",
            json={"method": "POST", "body": '{"cp": "cp_1"}'},
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["starlark_result"]["action"] == "fork"
    assert data["validation_errors"] == []
    await db.close()


@pytest.mark.asyncio
async def test_ingress_requires_auth(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ingress_rules")
    assert resp.status_code == 401
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ingress.py -k "test_create_ingress or test_list_ingress or test_get_ingress or test_delete_ingress_rule_api or test_rotate or test_test_endpoint or test_ingress_requires" -v`
Expected: 404 (no route) or ImportError

- [ ] **Step 3: Implement the CRUD API**

```python
# src/mshkn/api/ingress.py
from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from mshkn.api.auth import require_account
from mshkn.api.ratelimit import RateLimiter
from mshkn.ingress.db import (
    delete_ingress_rule,
    get_ingress_rule_by_id,
    insert_ingress_log,
    insert_ingress_rule,
    list_ingress_logs,
    list_ingress_rules_by_account,
    rotate_ingress_rule_id,
    update_ingress_rule,
)
from mshkn.ingress.models import (
    IngressLog,
    IngressLogResponse,
    IngressRule,
    IngressRuleCreateRequest,
    IngressRuleResponse,
    IngressRuleUpdateRequest,
    IngressTestRequest,
    IngressTestResponse,
)
from mshkn.ingress.starlark import StarlarkError, execute_transform, validate_starlark
from mshkn.models import Account

logger = logging.getLogger(__name__)

_require_account = Depends(require_account)

# Per-rule rate limiters — one per rule, keyed by rule_id, respecting rule.rate_limit_rpm
_rule_rate_limiters: dict[str, RateLimiter] = {}


def _get_rule_rate_limiter(rule_id: str, rate_limit_rpm: int) -> RateLimiter:
    """Get or create a rate limiter for a specific rule."""
    limiter = _rule_rate_limiters.get(rule_id)
    if limiter is None or limiter.max_requests != rate_limit_rpm:
        limiter = RateLimiter(max_requests=rate_limit_rpm, window_seconds=60.0)
        _rule_rate_limiters[rule_id] = limiter
    return limiter

router = APIRouter(tags=["ingress"])


def _generate_rule_id() -> str:
    return "ir_" + secrets.token_urlsafe(20)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rule_to_response(rule: IngressRule, base_url: str) -> IngressRuleResponse:
    return IngressRuleResponse(
        id=rule.id,
        name=rule.name,
        ingress_url=f"{base_url}/ingress/{rule.id}",
        response_mode=rule.response_mode,
        max_body_bytes=rule.max_body_bytes,
        rate_limit_rpm=rule.rate_limit_rpm,
        enabled=rule.enabled,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


# --- CRUD Endpoints (authenticated) ---


@router.post("/ingress_rules", status_code=201)
async def create_rule(
    body: IngressRuleCreateRequest,
    request: Request,
    account: Account = _require_account,
) -> IngressRuleResponse:
    errors = validate_starlark(body.starlark_source)
    if errors:
        raise HTTPException(status_code=400, detail={"error": "invalid_starlark", "details": errors})

    db: aiosqlite.Connection = request.app.state.db
    now = _now_iso()
    rule = IngressRule(
        internal_id=str(uuid.uuid4()),
        id=_generate_rule_id(),
        account_id=account.id,
        name=body.name,
        starlark_source=body.starlark_source,
        response_mode=body.response_mode,
        max_body_bytes=body.max_body_bytes,
        rate_limit_rpm=body.rate_limit_rpm,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    await insert_ingress_rule(db, rule)
    base_url = str(request.base_url).rstrip("/")
    return _rule_to_response(rule, base_url)


@router.get("/ingress_rules")
async def list_rules(
    request: Request,
    account: Account = _require_account,
) -> list[IngressRuleResponse]:
    db: aiosqlite.Connection = request.app.state.db
    rules = await list_ingress_rules_by_account(db, account.id)
    base_url = str(request.base_url).rstrip("/")
    return [_rule_to_response(r, base_url) for r in rules]


@router.get("/ingress_rules/{rule_id}")
async def get_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> dict[str, Any]:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    base_url = str(request.base_url).rstrip("/")
    resp = _rule_to_response(rule, base_url).model_dump()
    resp["starlark_source"] = rule.starlark_source
    return resp


@router.put("/ingress_rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: IngressRuleUpdateRequest,
    request: Request,
    account: Account = _require_account,
) -> IngressRuleResponse:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Rule not found")

    if body.starlark_source is not None:
        errors = validate_starlark(body.starlark_source)
        if errors:
            raise HTTPException(status_code=400, detail={"error": "invalid_starlark", "details": errors})
        rule.starlark_source = body.starlark_source
    if body.name is not None:
        rule.name = body.name
    if body.response_mode is not None:
        rule.response_mode = body.response_mode
    if body.max_body_bytes is not None:
        rule.max_body_bytes = body.max_body_bytes
    if body.rate_limit_rpm is not None:
        rule.rate_limit_rpm = body.rate_limit_rpm
    if body.enabled is not None:
        rule.enabled = body.enabled
    rule.updated_at = _now_iso()

    await update_ingress_rule(db, rule)
    base_url = str(request.base_url).rstrip("/")
    return _rule_to_response(rule, base_url)


@router.delete("/ingress_rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> Response:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    await delete_ingress_rule(db, rule_id)
    return Response(status_code=204)


@router.post("/ingress_rules/{rule_id}/rotate")
async def rotate_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> IngressRuleResponse:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    new_id = _generate_rule_id()
    await rotate_ingress_rule_id(db, rule.internal_id, new_id)
    # Transfer rate limiter state to new key
    _rule_rate_limiters.pop(rule_id, None)
    rule.id = new_id
    base_url = str(request.base_url).rstrip("/")
    return _rule_to_response(rule, base_url)


@router.post("/ingress_rules/{rule_id}/test")
async def test_rule(
    rule_id: str,
    body: IngressTestRequest,
    request: Request,
    account: Account = _require_account,
) -> IngressTestResponse:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Rule not found")

    import time

    # Build mock request dict
    body_json = None
    if body.body:
        try:
            body_json = json.loads(body.body)
        except json.JSONDecodeError:
            pass

    req_dict = {
        "method": body.method,
        "path": body.path,
        "headers": body.headers,
        "query_params": body.query_params,
        "body_json": body_json,
        "body_form": None,
        "body_raw": body.body or "",
        "content_type": body.headers.get("Content-Type", ""),
    }

    start = time.perf_counter()
    try:
        result = execute_transform(rule.starlark_source, req_dict)
    except StarlarkError as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return IngressTestResponse(
            starlark_result=None,
            validation_errors=[str(exc)],
            execution_time_ms=round(elapsed, 2),
        )
    elapsed = (time.perf_counter() - start) * 1000

    validation_errors = _validate_transform_result(result)

    return IngressTestResponse(
        starlark_result=result,
        validation_errors=validation_errors,
        execution_time_ms=round(elapsed, 2),
    )


@router.get("/ingress_rules/{rule_id}/logs")
async def get_rule_logs(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> list[IngressLogResponse]:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    logs = await list_ingress_logs(db, rule.internal_id)
    return [
        IngressLogResponse(
            id=log.id,
            status=log.status,
            starlark_result=json.loads(log.starlark_result) if log.starlark_result else None,
            error_message=log.error_message,
            created_at=log.created_at,
        )
        for log in logs
    ]


# --- Validation ---

VALID_FORK_FIELDS = {"action", "checkpoint_id", "exec", "self_destruct", "exclusive", "callback_url", "meta_exec"}
VALID_CREATE_FIELDS = {"action", "capabilities", "uses", "exec", "self_destruct", "callback_url", "label", "meta_exec"}
VALID_EXCLUSIVE_VALUES = {"error_on_conflict", "defer_on_conflict"}


def _validate_transform_result(result: dict[str, Any] | None) -> list[str]:
    """Validate the Starlark transform result. Returns list of errors (empty = valid)."""
    if result is None:
        return []
    errors: list[str] = []
    if not isinstance(result, dict):
        return [f"Expected dict or None, got {type(result).__name__}"]
    action = result.get("action")
    if action == "fork":
        if "checkpoint_id" not in result:
            errors.append("Fork action requires 'checkpoint_id'")
        unknown = set(result.keys()) - VALID_FORK_FIELDS
        if unknown:
            errors.append(f"Unknown fork fields: {unknown}")
        if "exclusive" in result and result["exclusive"] not in VALID_EXCLUSIVE_VALUES:
            errors.append(f"exclusive must be one of {VALID_EXCLUSIVE_VALUES}")
    elif action == "create":
        unknown = set(result.keys()) - VALID_CREATE_FIELDS
        if unknown:
            errors.append(f"Unknown create fields: {unknown}")
    else:
        errors.append(f"action must be 'fork' or 'create', got {action!r}")
    return errors
```

- [ ] **Step 4: Register the router in main.py**

Add to `src/mshkn/main.py` after the existing router imports:

```python
from mshkn.api.ingress import router as ingress_router
```

And after the existing `app.include_router` lines:

```python
app.include_router(ingress_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ingress.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/mshkn/api/ingress.py src/mshkn/main.py tests/test_ingress.py
git commit -m "feat(ingress): add CRUD API endpoints for ingress rules"
```

### Task 6: Unauthenticated Ingress Trigger Endpoint

**Files:**
- Modify: `src/mshkn/api/ingress.py` (add trigger endpoint)
- Modify: `tests/test_ingress.py` (add trigger tests)

This is the core endpoint: `POST /ingress/{rule_id}` that receives webhooks, runs Starlark, and triggers fork/create.

- [ ] **Step 1: Write failing tests for the ingress trigger**

Append to `tests/test_ingress.py`:

```python
@pytest.mark.asyncio
async def test_ingress_trigger_not_found(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/ingress/ir_nonexistent", json={"hello": "world"})
    assert resp.status_code == 404
    await db.close()


@pytest.mark.asyncio
async def test_ingress_trigger_disabled_rule(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create rule then disable it
        create_resp = await client.post(
            "/ingress_rules",
            json={"name": "r1", "starlark_source": STARLARK_VALID},
            headers=AUTH_HEADERS,
        )
        rule_id = create_resp.json()["id"]
        await client.put(
            f"/ingress_rules/{rule_id}",
            json={"enabled": False},
            headers=AUTH_HEADERS,
        )
        # Try to trigger
        resp = await client.post(f"/ingress/{rule_id}", json={"test": True})
    assert resp.status_code == 404
    await db.close()


@pytest.mark.asyncio
async def test_ingress_trigger_starlark_returns_none(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    source = 'def transform(req):\n  return None'
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            json={"name": "ignore-rule", "starlark_source": source},
            headers=AUTH_HEADERS,
        )
        rule_id = create_resp.json()["id"]
        resp = await client.post(f"/ingress/{rule_id}", json={"test": True})
    assert resp.status_code == 204
    await db.close()


@pytest.mark.asyncio
async def test_ingress_trigger_starlark_error(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    source = 'def transform(req):\n  return req["nonexistent"]["key"]'
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            json={"name": "error-rule", "starlark_source": source},
            headers=AUTH_HEADERS,
        )
        rule_id = create_resp.json()["id"]
        resp = await client.post(f"/ingress/{rule_id}", json={"test": True})
    assert resp.status_code == 502
    await db.close()


@pytest.mark.asyncio
async def test_ingress_trigger_invalid_result(tmp_path: Path) -> None:
    db = await _setup_app_db(tmp_path)
    transport = ASGITransport(app=app)
    source = 'def transform(req):\n  return {"action": "invalid_action"}'
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/ingress_rules",
            json={"name": "bad-result", "starlark_source": source},
            headers=AUTH_HEADERS,
        )
        rule_id = create_resp.json()["id"]
        resp = await client.post(f"/ingress/{rule_id}", json={"data": "x"})
    assert resp.status_code == 502
    await db.close()
```

**Note:** Testing actual fork/create execution requires a live VM manager, which we can only test in E2E. The unit tests verify the Starlark → validation pipeline.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ingress.py -k "trigger" -v`
Expected: 404 (route doesn't exist yet)

- [ ] **Step 3: Implement the ingress trigger endpoint**

Add to `src/mshkn/api/ingress.py`:

```python
# --- Unauthenticated Ingress Endpoint ---


async def _parse_ingress_body(request: Request, max_bytes: int) -> dict[str, Any]:
    """Parse the incoming request body into the dict format for Starlark."""
    content_type = request.headers.get("content-type", "")
    body_raw = ""
    body_json = None
    body_form = None

    if request.method == "GET":
        return {
            "method": "GET",
            "path": str(request.url.path),
            "headers": dict(request.headers),
            "query_params": dict(request.query_params),
            "body_json": None,
            "body_form": None,
            "body_raw": "",
            "content_type": content_type,
        }

    # Check Content-Length first if present
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        raise HTTPException(status_code=413, detail="payload_too_large")

    # Read body incrementally with size limit (handles chunked transfers)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="payload_too_large")
        chunks.append(chunk)
    body_bytes = b"".join(chunks)
    body_raw = body_bytes.decode("utf-8", errors="replace")

    if "application/json" in content_type:
        try:
            body_json = json.loads(body_raw)
        except json.JSONDecodeError:
            pass  # Leave body_json as None, body_raw still available
    elif "application/x-www-form-urlencoded" in content_type:
        try:
            from urllib.parse import parse_qs
            body_form = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(body_raw).items()}
        except Exception:
            pass

    return {
        "method": request.method,
        "path": str(request.url.path),
        "headers": dict(request.headers),
        "query_params": dict(request.query_params),
        "body_json": body_json,
        "body_form": body_form,
        "body_raw": body_raw,
        "content_type": content_type,
    }


@router.api_route("/ingress/{rule_id}", methods=["GET", "POST", "PUT", "PATCH"])
async def handle_ingress(rule_id: str, request: Request) -> Response:
    """Unauthenticated ingress endpoint — triggers Starlark transform + action."""
    db: aiosqlite.Connection = request.app.state.db

    # 1. Look up rule
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or not rule.enabled:
        raise HTTPException(status_code=404, detail="not_found")

    # 2. Rate limit (per-rule, respecting rule.rate_limit_rpm)
    limiter = _get_rule_rate_limiter(rule_id, rule.rate_limit_rpm)
    if not limiter.check(rule_id):
        raise HTTPException(status_code=429, detail="rate_limited")

    request_id = str(uuid.uuid4())

    # 3. Parse body
    try:
        req_dict = await _parse_ingress_body(request, rule.max_body_bytes)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Ingress body parse error for rule %s: %s", rule_id, exc)
        raise HTTPException(status_code=400, detail="bad_request") from exc

    # 4. Execute Starlark
    try:
        result = execute_transform(rule.starlark_source, req_dict)
    except StarlarkError as exc:
        await insert_ingress_log(
            db,
            IngressLog(
                id=request_id,
                rule_internal_id=rule.internal_id,
                status="failed",
                starlark_result=None,
                error_message=str(exc),
                created_at=_now_iso(),
            ),
        )
        raise HTTPException(
            status_code=502,
            detail={"error": "transform_error", "detail": str(exc)},
        ) from exc

    # 5. Handle None (ignore)
    if result is None:
        await insert_ingress_log(
            db,
            IngressLog(
                id=request_id,
                rule_internal_id=rule.internal_id,
                status="rejected",
                starlark_result=None,
                error_message=None,
                created_at=_now_iso(),
            ),
        )
        return Response(status_code=204)

    # 6. Validate result
    validation_errors = _validate_transform_result(result)
    if validation_errors:
        await insert_ingress_log(
            db,
            IngressLog(
                id=request_id,
                rule_internal_id=rule.internal_id,
                status="failed",
                starlark_result=json.dumps(result),
                error_message="; ".join(validation_errors),
                created_at=_now_iso(),
            ),
        )
        raise HTTPException(
            status_code=502,
            detail={"error": "invalid_transform_result", "detail": validation_errors},
        )

    # 7. Execute action
    # Look up the account that owns this rule (needed for create/fork)
    from mshkn.db import get_account_by_key

    account_cursor = await db.execute(
        "SELECT api_key FROM accounts WHERE id=?", (rule.account_id,)
    )
    account_row = await account_cursor.fetchone()
    if account_row is None:
        raise HTTPException(status_code=500, detail="rule_account_not_found")

    action = result["action"]

    if rule.response_mode == "async":
        # Fire-and-forget
        await insert_ingress_log(
            db,
            IngressLog(
                id=request_id,
                rule_internal_id=rule.internal_id,
                status="accepted",
                starlark_result=json.dumps(result),
                error_message=None,
                created_at=_now_iso(),
            ),
        )
        import asyncio

        asyncio.create_task(
            _execute_ingress_action(request.app, rule, result, request_id)
        )
        return JSONResponse(
            status_code=202,
            content={"request_id": request_id, "status": "accepted"},
        )
    else:
        # Sync — wait for result
        try:
            action_result = await _execute_ingress_action(
                request.app, rule, result, request_id
            )
            return JSONResponse(status_code=200, content=action_result)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _execute_ingress_action(
    app: Any, rule: IngressRule, result: dict[str, Any], request_id: str
) -> dict[str, Any]:
    """Execute the fork or create action from a validated Starlark result.

    This calls into the same internal code paths as the regular API endpoints.
    """
    db: aiosqlite.Connection = app.state.db
    vm_manager = app.state.vm_manager
    config = app.state.config

    action = result["action"]

    if action == "fork":
        from mshkn.api.checkpoints import _do_fork

        checkpoint_id = result["checkpoint_id"]
        fork_result = await _do_fork(
            db=db,
            vm_manager=vm_manager,
            config=config,
            account_id=rule.account_id,
            checkpoint_id=checkpoint_id,
            exec_cmd=result.get("exec"),
            self_destruct=result.get("self_destruct", False),
            callback_url=result.get("callback_url"),
            exclusive=result.get("exclusive"),
            meta_exec=result.get("meta_exec"),
        )
        # Update log to completed
        await insert_ingress_log(
            db,
            IngressLog(
                id=request_id + "-done",
                rule_internal_id=rule.internal_id,
                status="completed",
                starlark_result=json.dumps(result),
                error_message=None,
                created_at=_now_iso(),
            ),
        )
        return fork_result

    elif action == "create":
        from mshkn.api.computers import _do_create

        create_result = await _do_create(
            db=db,
            vm_manager=vm_manager,
            config=config,
            account_id=rule.account_id,
            uses=result.get("capabilities") or result.get("uses") or [],
            exec_cmd=result.get("exec"),
            self_destruct=result.get("self_destruct", False),
            callback_url=result.get("callback_url"),
            label=result.get("label"),
            meta_exec=result.get("meta_exec"),
        )
        await insert_ingress_log(
            db,
            IngressLog(
                id=request_id + "-done",
                rule_internal_id=rule.internal_id,
                status="completed",
                starlark_result=json.dumps(result),
                error_message=None,
                created_at=_now_iso(),
            ),
        )
        return create_result

    msg = f"Unknown action: {action}"
    raise ValueError(msg)
```

**Critical refactoring — `_do_fork` and `_do_create`:**

The existing endpoint handlers mix HTTP concerns with business logic. Extract reusable internal functions:

**In `src/mshkn/api/computers.py`:**
1. Find the `create_computer` endpoint function (currently handles CreateRequest parsing → VM creation → exec → self-destruct → response).
2. Extract everything after request validation into:
```python
async def _do_create(
    db: aiosqlite.Connection,
    vm_manager: VMManager,
    config: Config,
    account_id: str,
    uses: list[str],
    exec_cmd: str | None = None,
    self_destruct: bool = False,
    callback_url: str | None = None,
    label: str | None = None,
    meta_exec: str | None = None,
) -> dict[str, Any]:
    """Core create logic — shared by HTTP endpoint and ingress trigger.

    Returns dict with: computer_id, url, manifest_hash, exec_exit_code,
    exec_stdout, exec_stderr, created_checkpoint_id.
    """
    # Move existing body from create_computer here:
    # - Build Manifest from uses
    # - Check vm_limit
    # - Call vm_manager.create_computer(...)
    # - If exec_cmd: run exec
    # - If self_destruct: checkpoint + destroy
    # - Return response dict
```
3. Update `create_computer` endpoint to call `_do_create`:
```python
@router.post("")
async def create_computer(body: CreateRequest, request: Request, account: Account = _require_account):
    db = request.app.state.db
    vm_manager = request.app.state.vm_manager
    config = request.app.state.config
    return await _do_create(db, vm_manager, config, account.id, body.uses, body.exec, body.self_destruct, body.callback_url, body.label, body.meta_exec)
```

**In `src/mshkn/api/checkpoints.py`:**
1. Find the `fork_checkpoint` endpoint function (handles exclusive restore, deferred queuing, VM fork, exec, self-destruct, callback).
2. Extract into:
```python
async def _do_fork(
    db: aiosqlite.Connection,
    vm_manager: VMManager,
    config: Config,
    account_id: str,
    checkpoint_id: str,
    exec_cmd: str | None = None,
    self_destruct: bool = False,
    callback_url: str | None = None,
    exclusive: str | None = None,
    meta_exec: str | None = None,
) -> dict[str, Any]:
    """Core fork logic — shared by HTTP endpoint and ingress trigger.

    Returns dict with: computer_id, checkpoint_id, exec_exit_code,
    exec_stdout, exec_stderr, created_checkpoint_id.
    """
    # Move existing body from fork_checkpoint here:
    # - Look up checkpoint, verify ownership
    # - Handle exclusive (error_on_conflict / defer_on_conflict)
    # - Call vm_manager.fork_from_checkpoint(...)
    # - If exec_cmd: run exec
    # - If self_destruct: checkpoint + destroy
    # - Return response dict
```
3. Update `fork_checkpoint` endpoint to call `_do_fork`.

**Verification:** After refactoring, run existing unit and E2E tests to ensure no regressions. The refactoring must be purely mechanical — same logic, just moved into a callable function.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ingress.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/api/ingress.py src/mshkn/api/computers.py src/mshkn/api/checkpoints.py tests/test_ingress.py
git commit -m "feat(ingress): add unauthenticated ingress trigger endpoint with Starlark execution"
```

---

## Chunk 3: E2E Tests + Test Plan + Integration

### Task 7: Add Phase 13 to Test Plan Doc

**Files:**
- Modify: `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md`

- [ ] **Step 1: Add Phase 13 chapter before the Pass/Fail Criteria section**

Insert before `## Pass/Fail Criteria`:

```markdown
## Phase 13: "Can It Take a Punch?" (Ingress Mapping)

The ingress mapping layer lets external webhooks trigger disposable computers via user-defined Starlark transformation rules. If this layer is fragile, every integration built on it is fragile. So we test the full pipeline: rule CRUD, Starlark execution, and actual VM creation through the ingress endpoint.

### T13.1 — Rule CRUD Lifecycle

- Create a rule with valid Starlark. Does it return an `id` starting with `ir_` and an `ingress_url`?
- List rules. Is the newly created rule in the list?
- Get rule by ID. Does it include `starlark_source`?
- Update the rule's name. Does the change persist?
- Delete the rule. Does the ingress URL stop working (404)?

### T13.2 — Rule Validation

- Create a rule with invalid Starlark (syntax error). Does it return 400?
- Create a rule with valid Starlark but no `transform` function. 400?
- Update a rule with invalid Starlark. 400, and the old rule is unchanged?

### T13.3 — Dry-Run Test Endpoint

- Create a rule that transforms `body_json.message` into a fork action.
- Call the `/test` endpoint with a mock request containing `{"message": "hello"}`.
- Does it return the expected Starlark result with `action: fork`?
- Does `validation_errors` come back empty?
- Call `/test` with a mock request that triggers a Starlark runtime error. Does it return the error in `validation_errors`?

### T13.4 — Ingress Trigger (Async Fork)

- Create a computer, checkpoint it, destroy it.
- Create an ingress rule that forks from that checkpoint with `exec: "echo ingress-works"` and `self_destruct: true`.
- POST to the ingress URL with a JSON body. Does it return 202?
- Wait for the fork to complete (poll checkpoint list or use callback_url).
- Does a new checkpoint exist with the exec output containing "ingress-works"?

### T13.5 — Ingress Trigger (Sync Fork)

- Same setup as T13.4, but with `response_mode: "sync"`.
- POST to the ingress URL. Does it return 200 with `exec_stdout` containing "ingress-works"?
- Does the response include `computer_id` and `created_checkpoint_id`?

### T13.6 — Ingress Trigger (Create)

- Create an ingress rule that creates a computer with `exec: "echo hello"` and `self_destruct: true`, `response_mode: "sync"`.
- POST to the ingress URL. Does it return 200 with exec output?

### T13.7 — Starlark Transform Correctness

- Create a rule that extracts different fields from the body: `body_json`, `query_params`, `headers`.
- Verify the transform correctly maps each input field to the expected output.
- Verify that `body_json` is None when sending non-JSON content.

### T13.8 — Ingress Returns None (204)

- Create a rule where `transform` returns `None` for certain inputs.
- POST a request that triggers the None path. Does it return 204?

### T13.9 — Ingress Error Cases

- POST to a non-existent rule ID. 404?
- POST to a disabled rule. 404?
- POST with a body exceeding `max_body_bytes`. 413?
- Rule with Starlark that errors at runtime. 502?
- Rule that returns an invalid action. 502?

### T13.10 — Rate Limiting

- Create a rule with `rate_limit_rpm: 5`.
- Fire 10 requests in quick succession. Do the first ~5 succeed and the rest return 429?

### T13.11 — Rule Rotation

- Create a rule. Note the ingress URL.
- Rotate the rule ID. Does the old URL return 404?
- Does the new URL work?

### T13.12 — Ingress Logs

- Trigger a rule several times (success, error, None).
- Query `/logs`. Are all invocations recorded with correct statuses?

### T13.13 — Exclusive Restore via Ingress

- Create a rule with `exclusive: "defer_on_conflict"`.
- Fork from a labeled checkpoint. While the computer is running, trigger the ingress again.
- Does the second request get deferred (202)?
- After the first computer self-destructs, does the deferred request execute?
```

- [ ] **Step 2: Update Pass/Fail Criteria table**

Add row:
```
| Ingress (Phase 13) | Rule CRUD works, Starlark transforms execute correctly, ingress triggers fork/create, rate limiting enforced, logs recorded | Any CRUD failure, Starlark escape, or silent ingress failure |
```

- [ ] **Step 3: Commit**

```bash
git add docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md
git commit -m "docs: add Phase 13 (Ingress Mapping) to test plan"
```

### Task 8: E2E Tests

**Files:**
- Create: `tests/e2e/test_phase13_ingress.py`

- [ ] **Step 1: Write the E2E test file**

```python
# tests/e2e/test_phase13_ingress.py
"""Phase 13: Ingress Mapping E2E tests.

Run against a live server:
    MSHKN_API_URL=http://135.181.6.215:8000 .venv/bin/pytest tests/e2e/test_phase13_ingress.py -v
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from tests.e2e.conftest import (
    API_KEY,
    API_URL,
    HEADERS,
    checkpoint_computer,
    create_computer,
    destroy_computer,
    exec_command,
    managed_computer,
)

STARLARK_FORK = """
def transform(req):
    body = req["body_json"]
    if not body or "checkpoint_id" not in body:
        return None
    result = {"action": "fork", "checkpoint_id": body["checkpoint_id"]}
    if "exec" in body:
        result["exec"] = body["exec"]
    if "self_destruct" in body:
        result["self_destruct"] = body["self_destruct"]
    if "exclusive" in body:
        result["exclusive"] = body["exclusive"]
    return result
"""

STARLARK_CREATE = """
def transform(req):
    return {
        "action": "create",
        "exec": "echo hello-from-ingress",
        "self_destruct": True,
    }
"""

STARLARK_NONE = """
def transform(req):
    return None
"""

STARLARK_ERROR = """
def transform(req):
    return req["nonexistent"]["key"]
"""

STARLARK_INVALID_ACTION = """
def transform(req):
    return {"action": "invalid"}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def client() -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        base_url=API_URL, headers=HEADERS, timeout=60.0
    ) as c:
        yield c


@pytest.fixture
async def ingress_client() -> httpx.AsyncClient:
    """Client without auth headers for unauthenticated ingress calls."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=60.0) as c:
        yield c


async def create_rule(
    client: httpx.AsyncClient, name: str, source: str, **kwargs: object
) -> dict:
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
async def test_t13_1_rule_crud_lifecycle(client: httpx.AsyncClient) -> None:
    """T13.1: Full CRUD lifecycle for ingress rules."""
    # Create
    rule = await create_rule(client, "e2e-crud-test", STARLARK_FORK)
    rule_id = rule["id"]
    assert rule_id.startswith("ir_")
    assert "ingress_url" in rule

    try:
        # List
        resp = await client.get("/ingress_rules")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert rule_id in ids

        # Get
        resp = await client.get(f"/ingress_rules/{rule_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "e2e-crud-test"
        assert "starlark_source" in resp.json()

        # Update
        resp = await client.put(
            f"/ingress_rules/{rule_id}", json={"name": "updated-name"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated-name"

        # Delete
        resp = await client.delete(f"/ingress_rules/{rule_id}")
        assert resp.status_code == 204

        # Verify deleted
        resp = await client.get(f"/ingress_rules/{rule_id}")
        assert resp.status_code == 404
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.2 — Rule Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_2_invalid_starlark_rejected(client: httpx.AsyncClient) -> None:
    """T13.2: Invalid Starlark is rejected on create."""
    resp = await client.post(
        "/ingress_rules",
        json={"name": "bad", "starlark_source": "def other(): pass"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_t13_2_syntax_error_rejected(client: httpx.AsyncClient) -> None:
    """T13.2: Starlark syntax errors are rejected."""
    resp = await client.post(
        "/ingress_rules",
        json={"name": "bad", "starlark_source": "def transform(req):\n  {{{{"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# T13.3 — Dry-Run Test Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_3_test_endpoint(client: httpx.AsyncClient) -> None:
    """T13.3: Dry-run test endpoint returns expected transform result."""
    rule = await create_rule(client, "e2e-test-endpoint", STARLARK_FORK)
    rule_id = rule["id"]
    try:
        resp = await client.post(
            f"/ingress_rules/{rule_id}/test",
            json={
                "method": "POST",
                "body": json.dumps({"checkpoint_id": "cp_test", "exec": "echo hi"}),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["starlark_result"]["action"] == "fork"
        assert data["starlark_result"]["checkpoint_id"] == "cp_test"
        assert data["validation_errors"] == []
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.4 — Ingress Trigger (Async Fork)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_4_async_fork_via_ingress(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.4: Trigger an async fork via the ingress endpoint."""
    # Setup: create computer, checkpoint, destroy
    comp_id = await create_computer(client)
    ckpt_id = await checkpoint_computer(client, comp_id)
    await destroy_computer(client, comp_id)

    rule = await create_rule(client, "e2e-async-fork", STARLARK_FORK)
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={
                "checkpoint_id": ckpt_id,
                "exec": "echo ingress-async-works",
                "self_destruct": True,
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "request_id" in data

        # Wait for the fork to complete (self_destruct creates a new checkpoint)
        await asyncio.sleep(10)

        # Check that a new checkpoint was created
        resp = await client.get("/checkpoints")
        assert resp.status_code == 200
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.5 — Ingress Trigger (Sync Fork)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_5_sync_fork_via_ingress(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.5: Trigger a sync fork via the ingress endpoint."""
    comp_id = await create_computer(client)
    ckpt_id = await checkpoint_computer(client, comp_id)
    await destroy_computer(client, comp_id)

    rule = await create_rule(
        client, "e2e-sync-fork", STARLARK_FORK, response_mode="sync"
    )
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={
                "checkpoint_id": ckpt_id,
                "exec": "echo ingress-sync-works",
                "self_destruct": True,
            },
            timeout=120.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "ingress-sync-works" in data.get("exec_stdout", "")
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.8 — Returns None (204)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_8_returns_none(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.8: Starlark returning None gives 204."""
    rule = await create_rule(client, "e2e-none", STARLARK_NONE)
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(f"/ingress/{rule_id}", json={"data": "x"})
        assert resp.status_code == 204
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.9 — Error Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_9_nonexistent_rule(ingress_client: httpx.AsyncClient) -> None:
    """T13.9: Non-existent rule returns 404."""
    resp = await ingress_client.post("/ingress/ir_does_not_exist", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_t13_9_starlark_runtime_error(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.9: Starlark runtime error returns 502."""
    rule = await create_rule(client, "e2e-error", STARLARK_ERROR)
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(f"/ingress/{rule_id}", json={"data": "x"})
        assert resp.status_code == 502
    finally:
        await delete_rule(client, rule_id)


@pytest.mark.asyncio
async def test_t13_9_invalid_action(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.9: Invalid action returns 502."""
    rule = await create_rule(client, "e2e-invalid", STARLARK_INVALID_ACTION)
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(f"/ingress/{rule_id}", json={"data": "x"})
        assert resp.status_code == 502
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.10 — Rate Limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_10_rate_limiting(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.10: Per-rule rate limiting enforced."""
    rule = await create_rule(
        client, "e2e-ratelimit", STARLARK_NONE, rate_limit_rpm=5
    )
    rule_id = rule["id"]
    try:
        statuses = []
        for _ in range(10):
            resp = await ingress_client.post(f"/ingress/{rule_id}", json={})
            statuses.append(resp.status_code)
        assert 429 in statuses, f"Expected some 429s, got: {statuses}"
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.11 — Rule Rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_11_rule_rotation(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.11: Old ingress URL stops working after rotation, new one works."""
    rule = await create_rule(client, "e2e-rotate", STARLARK_NONE)
    old_id = rule["id"]
    try:
        # Old URL works
        resp = await ingress_client.post(f"/ingress/{old_id}", json={})
        assert resp.status_code == 204

        # Rotate
        resp = await client.post(f"/ingress_rules/{old_id}/rotate")
        assert resp.status_code == 200
        new_id = resp.json()["id"]

        # Old URL stops working
        resp = await ingress_client.post(f"/ingress/{old_id}", json={})
        assert resp.status_code == 404

        # New URL works
        resp = await ingress_client.post(f"/ingress/{new_id}", json={})
        assert resp.status_code == 204
    finally:
        await delete_rule(client, new_id)


# ---------------------------------------------------------------------------
# T13.12 — Ingress Logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_12_ingress_logs(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.12: Invocation logs are recorded with correct statuses."""
    rule = await create_rule(client, "e2e-logs", STARLARK_NONE)
    rule_id = rule["id"]
    try:
        # Trigger a few times
        await ingress_client.post(f"/ingress/{rule_id}", json={})
        await ingress_client.post(f"/ingress/{rule_id}", json={})

        # Check logs
        resp = await client.get(f"/ingress_rules/{rule_id}/logs")
        assert resp.status_code == 200
        logs = resp.json()
        assert len(logs) >= 2
    finally:
        await delete_rule(client, rule_id)
```

**Also add these missing E2E tests to the file:**

```python
# ---------------------------------------------------------------------------
# T13.6 — Ingress Trigger (Create)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_6_sync_create_via_ingress(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.6: Trigger a sync create via the ingress endpoint."""
    rule = await create_rule(
        client, "e2e-sync-create", STARLARK_CREATE, response_mode="sync"
    )
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={"trigger": True},
            timeout=120.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "hello-from-ingress" in data.get("exec_stdout", "")
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.7 — Starlark Transform Correctness
# ---------------------------------------------------------------------------

STARLARK_EXTRACT_FIELDS = """
def transform(req):
    parts = []
    if req["body_json"]:
        parts.append("json:" + str(req["body_json"].get("key", "")))
    if req["query_params"].get("q"):
        parts.append("query:" + req["query_params"]["q"])
    if req["headers"].get("x-custom"):
        parts.append("header:" + req["headers"]["x-custom"])
    return {
        "action": "create",
        "exec": "echo " + ",".join(parts),
        "self_destruct": True,
    }
"""


@pytest.mark.asyncio
async def test_t13_7_transform_extracts_fields(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.7: Starlark correctly extracts body_json, query_params, headers."""
    rule = await create_rule(
        client, "e2e-fields", STARLARK_EXTRACT_FIELDS, response_mode="sync"
    )
    rule_id = rule["id"]
    try:
        resp = await ingress_client.post(
            f"/ingress/{rule_id}?q=hello",
            json={"key": "world"},
            headers={"x-custom": "test123"},
            timeout=120.0,
        )
        assert resp.status_code == 200
        stdout = resp.json().get("exec_stdout", "")
        assert "json:world" in stdout
        assert "query:hello" in stdout
        assert "header:test123" in stdout
    finally:
        await delete_rule(client, rule_id)


# ---------------------------------------------------------------------------
# T13.13 — Exclusive Restore via Ingress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_13_exclusive_defer_via_ingress(
    client: httpx.AsyncClient, ingress_client: httpx.AsyncClient
) -> None:
    """T13.13: Exclusive defer_on_conflict works through ingress."""
    # Create a labeled checkpoint
    comp_id = await create_computer(client)
    ckpt_id = await checkpoint_computer(client, comp_id, label="ingress-excl-test")
    await destroy_computer(client, comp_id)

    source = """
def transform(req):
    body = req["body_json"]
    return {
        "action": "fork",
        "checkpoint_id": body["checkpoint_id"],
        "exec": body.get("exec", "echo ok"),
        "self_destruct": True,
        "exclusive": "defer_on_conflict",
    }
"""
    rule = await create_rule(client, "e2e-exclusive", source)
    rule_id = rule["id"]
    try:
        # First trigger - should succeed (202)
        resp1 = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={"checkpoint_id": ckpt_id, "exec": "sleep 5 && echo first"},
        )
        assert resp1.status_code == 202

        # Small delay to let the fork start
        await asyncio.sleep(2)

        # Second trigger while first is running - should be deferred (202)
        resp2 = await ingress_client.post(
            f"/ingress/{rule_id}",
            json={"checkpoint_id": ckpt_id, "exec": "echo second"},
        )
        assert resp2.status_code == 202

        # Wait for both to complete
        await asyncio.sleep(15)

        # Both should have produced checkpoints
        resp = await client.get("/checkpoints")
        assert resp.status_code == 200
    finally:
        await delete_rule(client, rule_id)
```

- [ ] **Step 2: Run unit tests locally**

Run: `.venv/bin/pytest tests/test_ingress.py -v`
Expected: All PASS

- [ ] **Step 3: Run ruff + mypy**

Run: `ruff check src/ && mypy src/`
Expected: Clean

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_phase13_ingress.py
git commit -m "test(ingress): add Phase 13 E2E tests for ingress mapping"
```

### Task 9: Full Test Suite Validation

- [ ] **Step 1: Run all unit tests**

Run: `.venv/bin/pytest tests/ --ignore=tests/e2e --ignore=tests/integration -v`
Expected: All PASS

- [ ] **Step 2: Run ruff + mypy**

Run: `ruff check src/ && mypy src/`
Expected: Clean

- [ ] **Step 3: Integrate log pruning into the reaper**

The existing reaper loop in `vm/manager.py` runs periodically. Add a call to `prune_old_ingress_logs` during each reaper cycle. Calculate the cutoff timestamp as `datetime.now(timezone.utc) - timedelta(hours=24)` and pass it to the prune function. This keeps the ingress_log table from growing unbounded.

- [ ] **Step 4: Deploy to server**

```bash
git push origin <branch>
ssh root@135.181.6.215 "cd /opt/mshkn && git pull && systemctl restart mshkn && systemctl restart litestream"
```

- [ ] **Step 5: Run E2E tests against live server**

Run: `MSHKN_API_URL=http://135.181.6.215:8000 .venv/bin/pytest tests/e2e/ -v --tb=short`
Expected: All existing tests still pass + new Phase 13 tests pass

- [ ] **Step 6: If regressions, fix them before proceeding**

- [ ] **Step 7: Final commit + PR**

```bash
git add -A
git commit -m "feat(ingress): complete ingress mapping implementation (closes #46)"
gh pr create --title "feat: ingress mapping with Starlark transforms (#46)" --body "Closes #46\n\n## What this does\nAdds ingress mapping: user-defined Starlark rules that transform arbitrary webhook payloads into mshkn API calls (fork/create).\n\n## Validation\n- Unit tests: X passing\n- E2E Phase 13: Y passing\n- All existing tests: no regressions"
```
