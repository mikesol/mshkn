# Asynchronous turns through a host-side relay (#110) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A turn of the embryo no longer keeps a computer alive while the model thinks: the brain hands the model call to a relay inside mshkn and is woken by a fork of `brain` when the answer arrives.

**Architecture:** mshkn gains `RelayService` (a job table, an upstream call in a background task with retry and SSE reassembly, an SSRF guard, delivery by fork-by-label) behind `POST /relay` and `GET /relay/{job_id}`, with a `relay` scope that pins a scoped key's targets and its one wake-up. The membrane's `say` posts the request and acknowledges; `membrane resume <job_id>` continues the turn; `state.json` carries the pending turn and a queue. The scripted model is served over the Messages API wire format by `membrane serve` so the flow and live tiers drive the real relay.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, httpx, Pydantic; the membrane's standard-library HTTP server; pytest with the fake host.

**Spec:** `docs/superpowers/specs/2026-09-09-async-turn-relay-design.md`. The plan argues from it; read both.

## Global Constraints

- Work in the worktree `/home/mikesol/Documents/GitHub/mshkn-async-turn-relay` on branch `async-turn-relay`. Every command below runs there.
- uv is the only package manager; every tool runs as `uv run <tool>`. No new dependencies: `uv lock --check` must stay green.
- The gate before every commit you would show anyone: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov` (coverage floor 98 %, zero warnings). While iterating on one file, `uv run pytest tests/unit/<file> -q` is enough; run `uv run ruff format .` before committing.
- `tests/unit/test_docs.py` fails when a document names a path, module, route, metric or variable that does not exist: fix the document, not the test. The architecture doc must list every route.
- No xfail, no weakened assertion, no fallback path, no version suffix. Pre-alpha: replace, do not keep the old thing beside the new one.
- Commit messages end with the two trailer lines:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
  ```
- Names fixed by the spec and used across tasks: config `relay_timeout_seconds` (3600) and `relay_body_bytes` (8 MiB); table `relay_jobs`; job ids `rj-<12 hex>`; job statuses `queued`, `in_progress`, `completed`, `failed`; delivery statuses `pending`, `delivered`, `failed`; scope section `relay` with `targets` and `deliver: {label, exec}`; membrane commands `membrane resume <job_id>` and `membrane serve [--port N]`; `.env` variable `ANTHROPIC_BASE_URL`.

## File structure

Host side (`src/mshkn`):

| File | Responsibility |
|---|---|
| `migrations/013_relay_jobs.sql` | The `relay_jobs` table. |
| `src/mshkn/config.py` | Two fields: `relay_timeout_seconds`, `relay_body_bytes`. |
| `src/mshkn/models.py` | `RelayStatus`, `DeliveryStatus`, `RelayDelivery`, `RetryPolicy`, `RelayJob`; `Scopes.relay_targets`, `Scopes.relay_deliver`, `may_relay_to`; `parse_scopes` and `to_document` for the `relay` section. |
| `src/mshkn/db/relay.py` | Column tuple, row mapper, `insert_relay_job`, `get_relay_job`, `update_relay_job`, `list_relay_jobs_by_status`, `delete_relay_jobs_before`. Exported from `src/mshkn/db/__init__.py`. |
| `src/mshkn/services/ssrf.py` | The guard: scheme, raw-address and resolved-address checks; `resolve_host`. Pure functions plus one async entry point. |
| `src/mshkn/services/sse.py` | Reassembly of a `text/event-stream` body into the final message; `StreamError`, `IncompleteStream`. |
| `src/mshkn/services/relay.py` | `RelayRequest`, `RelayService`: `submit`, `get_owned`, `run` (attempts, retry, headers cleared), `deliver`, `resume`, `expire`. |
| `src/mshkn/api/schemas.py` | `RelayRequestBody` and friends, `RelayJobResponse`, `relay_job_response`. |
| `src/mshkn/api/scopes.py` | `require_relay`, `require_relay_job_owner`. |
| `src/mshkn/api/relay.py` | The two routes. |
| `src/mshkn/runtime.py`, `src/mshkn/app.py`, `src/mshkn/services/reaper.py` | Wiring, router, restart re-run, expiry. |

Membrane (`embryo/membrane`):

| File | Responsibility |
|---|---|
| `config.py` | `Settings.anthropic_base_url`. |
| `mshkn.py` | `RelayJob`, `create_relay_job`, `get_relay_job` on the protocol and the client. |
| `model.py` | `compose_request`, `request_headers`, `parse_message`, `usage_from`; `AnthropicModel` and `build_model` go. |
| `state.py` | `Pending`, `Queued`, `Exchange.output` and `Exchange.audit`, `State.pending`, `State.queue`. |
| `loop.py` | `run_calls`: the tool calls of one completion against the handlers, the cap, the fork's clock. |
| `turn.py` | `Context`, `say`, `resume`, `settle`, `start_turn`, `finish`, `close_turn`, `build_tools`, `post_request`. |
| `commands.py` | `list_state` shows `pending`, `queue`, `window`; every root command settles first. |
| `cli.py` | `resume` and `serve`; `run` returns stdout and writes settle notes to stderr. |
| `serve.py` | `answer(model, body)` and the standard-library server for the scripted model. |
| `measure.py` | The doors wait on `list` for the turn. |
| `embryo/hatch.sh` | The `relay` scope on the brain's key; the scripted server in scripted mode; `ANTHROPIC_BASE_URL`. |

Tests: `tests/unit/test_relay_ssrf.py`, `test_relay_sse.py`, `test_relay_db.py`, `test_relay_service.py`, `test_relay_api.py`, additions to `test_scopes.py`, `test_scoped_routes.py`, `test_config.py`, `test_reaper.py`; `test_embryo_state.py`, `test_embryo_mshkn.py`, `test_embryo_model.py`, `test_embryo_loop.py`, `test_embryo_turn.py`, `test_embryo_commands.py`, `test_embryo_serve.py`, `test_embryo_measure.py`; `tests/support_embryo.py`; `tests/flow/conftest.py`, `tests/flow/test_relay.py`, `tests/flow/test_embryo_liturgy.py`; `tests/e2e/test_phase15_relay.py`, `tests/e2e/test_phase14_embryo.py`.

---

### Task 1: Config, migration, models and the `relay_jobs` table

**Files:**
- Create: `migrations/013_relay_jobs.sql`, `src/mshkn/db/relay.py`, `tests/unit/test_relay_db.py`
- Modify: `src/mshkn/config.py`, `src/mshkn/models.py`, `src/mshkn/db/__init__.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Config.relay_timeout_seconds: int = 3600`, `Config.relay_body_bytes: int = 8388608`; `mshkn.models.RelayStatus`, `DeliveryStatus`, `RelayDelivery(label, exec)`, `RetryPolicy(attempts, initial_delay_ms, max_delay_ms).delay(attempt) -> float`, `RelayJob` (fields below); `mshkn.db.insert_relay_job(db, job)`, `get_relay_job(db, job_id) -> RelayJob | None`, `update_relay_job(db, job)`, `list_relay_jobs_by_status(db, statuses) -> list[RelayJob]`, `delete_relay_jobs_before(db, cutoff) -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_relay_db.py`:

```python
"""relay_jobs rows round-trip; headers are a column of their own; expiry is by created_at."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import (
    delete_relay_jobs_before,
    get_relay_job,
    insert_account,
    insert_relay_job,
    list_relay_jobs_by_status,
    update_relay_job,
)
from mshkn.models import DeliveryStatus, RelayDelivery, RelayJob, RelayStatus, RetryPolicy
from tests.support import account_row

if TYPE_CHECKING:
    import aiosqlite


def job_row(
    id: str = "rj-000000000001",  # noqa: A002
    *,
    status: RelayStatus = RelayStatus.QUEUED,
    created_at: str = "2026-09-09T10:00:00+00:00",
    deliver: RelayDelivery | None = RelayDelivery(label="brain", exec="membrane resume"),
) -> RelayJob:
    return RelayJob(
        id=id,
        account_id="acct-1",
        api_key_id="key-1",
        status=status,
        target="https://api.anthropic.com/v1/messages",
        method="POST",
        forward_headers={"x-api-key": "sk-secret"},
        body={"model": "claude-opus-5", "stream": True},
        retry=RetryPolicy(),
        timeout_seconds=3600,
        deliver=deliver,
        attempts=0,
        error=None,
        response_status=None,
        response_headers=None,
        response_body=None,
        delivery_status=None if deliver is None else DeliveryStatus.PENDING,
        delivery_attempts=0,
        delivery_computer_id=None,
        delivery_deferred_id=None,
        delivery_error=None,
        created_at=created_at,
        updated_at=created_at,
    )


async def test_insert_get_and_update_round_trip(db: aiosqlite.Connection) -> None:
    await insert_account(db, account_row())
    job = job_row()
    await insert_relay_job(db, job)
    assert await get_relay_job(db, job.id) == job
    assert await get_relay_job(db, "rj-nope") is None
    job.status = RelayStatus.COMPLETED
    job.attempts = 2
    job.forward_headers = None
    job.response_status = 200
    job.response_headers = {"content-type": "application/json"}
    job.response_body = {"content": [{"type": "text", "text": "hi"}]}
    job.delivery_status = DeliveryStatus.DELIVERED
    job.delivery_attempts = 1
    job.delivery_computer_id = "comp-9"
    job.updated_at = "2026-09-09T10:05:00+00:00"
    await update_relay_job(db, job)
    assert await get_relay_job(db, job.id) == job


async def test_a_string_body_and_no_delivery_survive(db: aiosqlite.Connection) -> None:
    job = job_row(deliver=None)
    job.body = "raw text"
    job.response_body = "not json"
    await insert_relay_job(db, job)
    back = await get_relay_job(db, job.id)
    assert back is not None
    assert back.body == "raw text" and back.response_body == "not json"
    assert back.deliver is None and back.delivery_status is None


async def test_list_by_status_and_expiry(db: aiosqlite.Connection) -> None:
    await insert_relay_job(db, job_row("rj-1", status=RelayStatus.QUEUED))
    await insert_relay_job(db, job_row("rj-2", status=RelayStatus.IN_PROGRESS))
    await insert_relay_job(
        db, job_row("rj-3", status=RelayStatus.COMPLETED, created_at="2026-09-01T00:00:00+00:00")
    )
    unsettled = await list_relay_jobs_by_status(db, (RelayStatus.QUEUED, RelayStatus.IN_PROGRESS))
    assert [j.id for j in unsettled] == ["rj-1", "rj-2"]
    assert await delete_relay_jobs_before(db, "2026-09-05T00:00:00+00:00") == 1
    assert await get_relay_job(db, "rj-3") is None
    assert await get_relay_job(db, "rj-1") is not None
```

Add to `tests/unit/test_config.py`:

```python
def test_relay_fields_have_the_spec_defaults_and_read_their_variables() -> None:
    config = Config()
    assert config.relay_timeout_seconds == 3600 and config.relay_body_bytes == 8 * 1024 * 1024
    read = Config.from_env({"MSHKN_RELAY_TIMEOUT_SECONDS": "60", "MSHKN_RELAY_BODY_BYTES": "1024"})
    assert read.relay_timeout_seconds == 60 and read.relay_body_bytes == 1024
```

Add to `tests/unit/test_scopes.py` (the models half of this task, the parse half is Task 2) nothing yet.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_relay_db.py tests/unit/test_config.py -q`
Expected: ImportError on `RelayJob` / `insert_relay_job`, and `AttributeError: relay_timeout_seconds`.

- [ ] **Step 3: Write the migration**

`migrations/013_relay_jobs.sql`:

```sql
-- 013_relay_jobs.sql
-- The relay (#110): one row per job. The forwarded headers live in their own
-- column and are cleared the moment the upstream call settles; the response is
-- stored whole; the delivery (a fork by label) has its own status. Expired by
-- the reaper with exec_log_retention_seconds.
CREATE TABLE IF NOT EXISTS relay_jobs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    api_key_id TEXT REFERENCES api_keys(id),
    status TEXT NOT NULL,
    target TEXT NOT NULL,
    method TEXT NOT NULL,
    forward_headers_json TEXT,
    body_json TEXT,
    retry_json TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    deliver_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    response_status INTEGER,
    response_headers_json TEXT,
    response_body_json TEXT,
    delivery_status TEXT,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_computer_id TEXT,
    delivery_deferred_id TEXT,
    delivery_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relay_jobs_created ON relay_jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_relay_jobs_status ON relay_jobs(status);
```

- [ ] **Step 4: Add the config fields**

In `src/mshkn/config.py`, after `exec_log_retention_seconds`:

```python
    # Relay (#110)
    relay_timeout_seconds: int = 3600  # default and cap of a job's upstream timeout
    relay_body_bytes: int = 8 * 1024 * 1024  # a job's request body and an upstream response
```

- [ ] **Step 5: Add the models**

In `src/mshkn/models.py`, after `IngressLogStatus`:

```python
class RelayStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
```

Immediately **above** `class Scopes` (Task 2 extends `Scopes` and needs the type in scope there):

```python
@dataclass(frozen=True)
class RelayDelivery:
    """The wake-up a relay job causes: fork `label` with `exec <job_id>`."""

    label: str
    exec: str
```

At the end of the file:

```python


@dataclass(frozen=True)
class RetryPolicy:
    """Lampas's policy: exponential backoff, min(initial * 2^attempt, max)."""

    attempts: int = 3
    initial_delay_ms: int = 1000
    max_delay_ms: int = 30000

    def delay(self, attempt: int) -> float:
        """Seconds to wait after the zero-based `attempt` failed."""
        return min(self.initial_delay_ms * 2**attempt, self.max_delay_ms) / 1000

    def to_document(self) -> dict[str, int]:
        return {
            "attempts": self.attempts,
            "initial_delay_ms": self.initial_delay_ms,
            "max_delay_ms": self.max_delay_ms,
        }


@dataclass
class RelayJob:
    """One upstream HTTP call plus at most one delivery (#110)."""

    id: str
    account_id: str
    api_key_id: str | None
    status: RelayStatus
    target: str
    method: str
    forward_headers: dict[str, str] | None
    body: object
    retry: RetryPolicy
    timeout_seconds: int
    deliver: RelayDelivery | None
    attempts: int
    error: str | None
    response_status: int | None
    response_headers: dict[str, str] | None
    response_body: object
    delivery_status: DeliveryStatus | None
    delivery_attempts: int
    delivery_computer_id: str | None
    delivery_deferred_id: str | None
    delivery_error: str | None
    created_at: str
    updated_at: str
```

- [ ] **Step 6: Write the db module**

`src/mshkn/db/relay.py`:

```python
"""relay_jobs table: the relay's jobs (#110), headers in their own column."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mshkn.models import DeliveryStatus, RelayDelivery, RelayJob, RelayStatus, RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "api_key_id",
    "status",
    "target",
    "method",
    "forward_headers_json",
    "body_json",
    "retry_json",
    "timeout_seconds",
    "deliver_json",
    "attempts",
    "error",
    "response_status",
    "response_headers_json",
    "response_body_json",
    "delivery_status",
    "delivery_attempts",
    "delivery_computer_id",
    "delivery_deferred_id",
    "delivery_error",
    "created_at",
    "updated_at",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM relay_jobs"
# Everything but the id, written whole by update_relay_job.
_MUTABLE = COLUMNS[1:]


def _loads(value: object) -> object:
    return None if value is None else json.loads(str(value))


def _dumps(value: object) -> str | None:
    return None if value is None else json.dumps(value)


def _values(job: RelayJob) -> tuple[object, ...]:
    return (
        job.id,
        job.account_id,
        job.api_key_id,
        str(job.status),
        job.target,
        job.method,
        _dumps(job.forward_headers),
        _dumps(job.body),
        json.dumps(job.retry.to_document()),
        job.timeout_seconds,
        None if job.deliver is None else json.dumps({"label": job.deliver.label, "exec": job.deliver.exec}),
        job.attempts,
        job.error,
        job.response_status,
        _dumps(job.response_headers),
        _dumps(job.response_body),
        None if job.delivery_status is None else str(job.delivery_status),
        job.delivery_attempts,
        job.delivery_computer_id,
        job.delivery_deferred_id,
        job.delivery_error,
        job.created_at,
        job.updated_at,
    )


def _row_to_job(row: Sequence[object]) -> RelayJob:
    d = dict(zip(COLUMNS, row, strict=True))
    retry = json.loads(str(d["retry_json"]))
    deliver = _loads(d["deliver_json"])
    return RelayJob(
        id=str(d["id"]),
        account_id=str(d["account_id"]),
        api_key_id=None if d["api_key_id"] is None else str(d["api_key_id"]),
        status=RelayStatus(str(d["status"])),
        target=str(d["target"]),
        method=str(d["method"]),
        forward_headers=_loads(d["forward_headers_json"]),  # type: ignore[arg-type]
        body=_loads(d["body_json"]),
        retry=RetryPolicy(
            attempts=int(retry["attempts"]),
            initial_delay_ms=int(retry["initial_delay_ms"]),
            max_delay_ms=int(retry["max_delay_ms"]),
        ),
        timeout_seconds=int(d["timeout_seconds"]),  # type: ignore[call-overload]
        deliver=None
        if deliver is None
        else RelayDelivery(label=str(deliver["label"]), exec=str(deliver["exec"])),  # type: ignore[index]
        attempts=int(d["attempts"]),  # type: ignore[call-overload]
        error=None if d["error"] is None else str(d["error"]),
        response_status=None if d["response_status"] is None else int(d["response_status"]),  # type: ignore[call-overload]
        response_headers=_loads(d["response_headers_json"]),  # type: ignore[arg-type]
        response_body=_loads(d["response_body_json"]),
        delivery_status=None
        if d["delivery_status"] is None
        else DeliveryStatus(str(d["delivery_status"])),
        delivery_attempts=int(d["delivery_attempts"]),  # type: ignore[call-overload]
        delivery_computer_id=None
        if d["delivery_computer_id"] is None
        else str(d["delivery_computer_id"]),
        delivery_deferred_id=None
        if d["delivery_deferred_id"] is None
        else str(d["delivery_deferred_id"]),
        delivery_error=None if d["delivery_error"] is None else str(d["delivery_error"]),
        created_at=str(d["created_at"]),
        updated_at=str(d["updated_at"]),
    )


async def insert_relay_job(db: aiosqlite.Connection, job: RelayJob) -> None:
    await db.execute(
        "INSERT INTO relay_jobs (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        _values(job),
    )


async def update_relay_job(db: aiosqlite.Connection, job: RelayJob) -> None:
    """Write every column but the id from the dataclass: one statement, one shape."""
    await db.execute(
        "UPDATE relay_jobs SET " + ", ".join(f"{c} = ?" for c in _MUTABLE) + " WHERE id = ?",
        (*_values(job)[1:], job.id),
    )


async def get_relay_job(db: aiosqlite.Connection, job_id: str) -> RelayJob | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_job(row)


async def list_relay_jobs_by_status(
    db: aiosqlite.Connection, statuses: Iterable[RelayStatus]
) -> list[RelayJob]:
    wanted = [str(s) for s in statuses]
    cursor = await db.execute(
        _SELECT + " WHERE status IN (" + ", ".join("?" for _ in wanted) + ") ORDER BY created_at, id",
        wanted,
    )
    return [_row_to_job(r) for r in await cursor.fetchall()]


async def delete_relay_jobs_before(db: aiosqlite.Connection, cutoff: str) -> int:
    """Delete every job created before the ISO-8601 cutoff; return how many went."""
    cursor = await db.execute("DELETE FROM relay_jobs WHERE created_at < ?", (cutoff,))
    return cursor.rowcount
```

Export the five functions from `src/mshkn/db/__init__.py`: add `from mshkn.db.relay import (delete_relay_jobs_before, get_relay_job, insert_relay_job, list_relay_jobs_by_status, update_relay_job)` and the five names to `__all__` in alphabetical position. If mypy complains about a `# type: ignore` above being unused, delete that comment; if it complains about an untyped `int(...)` on `object`, add one.

- [ ] **Step 7: Run the tests and the type check**

Run: `uv run pytest tests/unit/test_relay_db.py tests/unit/test_config.py tests/unit/test_db_package.py -q && uv run mypy`
Expected: all pass, mypy clean. (`test_db_package.py` checks `__all__` against the modules; if it lists modules by hand, add `relay`.)

- [ ] **Step 8: Commit**

```bash
uv run ruff format . && uv run ruff check .
git add migrations/013_relay_jobs.sql src/mshkn/config.py src/mshkn/models.py src/mshkn/db/relay.py src/mshkn/db/__init__.py tests/unit/test_relay_db.py tests/unit/test_config.py
git commit -m "feat(relay): the relay_jobs table, its models and config (#110)"
```

---

### Task 2: The `relay` scope

**Files:**
- Modify: `src/mshkn/models.py` (`Scopes`, `parse_scopes`), `tests/unit/test_scopes.py`

**Interfaces:**
- Produces: `Scopes.relay_targets: tuple[str, ...]`, `Scopes.relay_deliver: RelayDelivery | None`, `Scopes.may_relay_to(target: str) -> bool`, `Scopes.has_relay` property; the document `{"relay": {"targets": [...], "deliver": {"label", "exec"}}}` parsed and emitted.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_scopes.py`:

```python
RELAY = {
    "relay": {
        "targets": ["https://api.anthropic.com/"],
        "deliver": {"label": "brain", "exec": "membrane resume"},
    }
}


def test_the_relay_section_pins_targets_and_one_delivery() -> None:
    scopes = parse_scopes({**BRAIN, **RELAY})
    assert scopes.relay_targets == ("https://api.anthropic.com/",)
    assert scopes.relay_deliver == RelayDelivery(label="brain", exec="membrane resume")
    assert scopes.has_relay
    assert scopes.may_relay_to("https://api.anthropic.com/v1/messages")
    assert not scopes.may_relay_to("https://api.anthropic.com.evil.example/v1/messages")
    assert not scopes.may_relay_to("http://api.anthropic.com/v1/messages")
    assert scopes.to_document() == {**BRAIN, **RELAY}


def test_without_the_section_a_key_may_relay_nowhere() -> None:
    scopes = parse_scopes(BRAIN)
    assert not scopes.has_relay and scopes.relay_deliver is None
    assert not scopes.may_relay_to("https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "section",
    [
        {},
        {"targets": []},
        {"targets": ["https://a/"]},
        {"deliver": {"label": "brain", "exec": "membrane resume"}},
        {"targets": "https://a/", "deliver": {"label": "brain", "exec": "x"}},
        {"targets": ["https://a/"], "deliver": {"label": "", "exec": "x"}},
        {"targets": ["https://a/"], "deliver": {"label": "brain", "exec": ""}},
        {"targets": ["https://a/"], "deliver": {"label": "brain"}},
        {"targets": ["https://a/"], "deliver": {"label": "brain", "exec": "x", "extra": 1}},
        {"targets": ["https://a/"], "deliver": "brain"},
        {"targets": [""], "deliver": {"label": "brain", "exec": "x"}},
    ],
)
def test_rejects_a_malformed_relay_section(section: dict[str, object]) -> None:
    with pytest.raises(InvalidInput):
        parse_scopes({"relay": section})
```

Add `RelayDelivery` to the `from mshkn.models import ...` line at the top of the test file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_scopes.py -q`
Expected: the three new tests fail (`InvalidInput: unknown fields ['relay']` and missing attributes).

- [ ] **Step 3: Extend `Scopes` and `parse_scopes`**

In `src/mshkn/models.py`, `Scopes` gains two fields and two helpers (keep the existing ones):

```python
@dataclass(frozen=True)
class Scopes:
    recipes_create: bool = False
    recipes_read: bool = False
    create_from: Literal["*"] | tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    # The relay (#110): URL prefixes the key may have called, and the one wake-up
    # it may cause. Both or neither; a key without them may relay nowhere.
    relay_targets: tuple[str, ...] = ()
    relay_deliver: RelayDelivery | None = None

    @property
    def has_relay(self) -> bool:
        return bool(self.relay_targets) and self.relay_deliver is not None

    def may_relay_to(self, target: str) -> bool:
        return any(target.startswith(prefix) for prefix in self.relay_targets)
```

`RelayDelivery` is already defined above `Scopes` (Task 1). In `to_document`, before `return doc`:

```python
        if self.has_relay:
            assert self.relay_deliver is not None
            doc["relay"] = {
                "targets": list(self.relay_targets),
                "deliver": {"label": self.relay_deliver.label, "exec": self.relay_deliver.exec},
            }
```

In `parse_scopes`, the unknown-field set becomes `{"recipes", "computers", "labels", "relay"}`, and before the `return Scopes(...)`:

```python
    relay_targets: tuple[str, ...] = ()
    relay_deliver: RelayDelivery | None = None
    if "relay" in document:
        relay = _section(document, "relay", {"targets", "deliver"})
        relay_targets = tuple(_strings(relay.get("targets"), "relay.targets"))
        if not relay_targets or any(not t for t in relay_targets):
            raise _reject("relay.targets must be a non-empty list of non-empty prefixes")
        deliver = relay.get("deliver")
        if not isinstance(deliver, dict) or set(deliver) != {"label", "exec"}:
            raise _reject("relay.deliver must be an object with label and exec")
        label, command = deliver["label"], deliver["exec"]
        if not isinstance(label, str) or not label or not isinstance(command, str) or not command:
            raise _reject("relay.deliver.label and relay.deliver.exec must be non-empty strings")
        relay_deliver = RelayDelivery(label=label, exec=command)
```

and pass `relay_targets=relay_targets, relay_deliver=relay_deliver` to `Scopes(...)`. Note `_strings(None, ...)` already raises for a missing `targets`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_scopes.py tests/unit/test_api_keys_db.py tests/unit/test_keys.py -q && uv run mypy`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add src/mshkn/models.py tests/unit/test_scopes.py
git commit -m "feat(scopes): the relay section pins a key's targets and its one wake-up (#110)"
```

---

### Task 3: The SSRF guard

**Files:**
- Create: `src/mshkn/services/ssrf.py`, `tests/unit/test_relay_ssrf.py`

**Interfaces:**
- Produces: `mshkn.services.ssrf.BLOCKED_RANGES`, `parse_address(text) -> IPv4Address | IPv6Address | None`, `blocked_reason(address) -> str | None`, `check_url(url) -> tuple[str, str | None]` (hostname, reason), `resolve_host(hostname) -> list[str]` (async), `guard(url, resolver) -> str | None` (async; the reason, None when allowed). `Resolver = Callable[[str], Awaitable[list[str]]]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_relay_ssrf.py`:

```python
"""The relay's SSRF guard, ported from lampas (#110): http and https only, no raw
private or reserved address, every resolved address checked, an unresolvable
host refused (lampas failed open; mshkn has no second guard behind it)."""

from __future__ import annotations

import pytest

from mshkn.services.ssrf import blocked_reason, check_url, guard, parse_address


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.1.2.3",
        "127.0.0.1",
        "127.255.255.254",
        "169.254.169.254",
        "172.16.254.1",
        "172.31.0.9",
        "192.168.1.1",
        "::1",
        "fc00::1",
        "fd12::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_private_and_reserved_addresses_are_blocked(address: str) -> None:
    parsed = parse_address(address)
    assert parsed is not None
    assert blocked_reason(parsed) is not None


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "65.21.22.161", "172.32.0.1", "2606:2800:220:1:248:1893:25c8:1946"]
)
def test_public_addresses_are_allowed(address: str) -> None:
    parsed = parse_address(address)
    assert parsed is not None
    assert blocked_reason(parsed) is None


def test_parse_address_returns_none_for_a_hostname() -> None:
    assert parse_address("api.anthropic.com") is None
    assert parse_address("") is None


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("ftp://example.com/x", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("example.com/no-scheme", "scheme"),
        ("http:///path", "host"),
        ("http://[::1", "invalid"),
        ("http://127.0.0.1:8000/health", "blocked"),
        ("http://[::1]/", "blocked"),
        ("http://[::ffff:10.0.0.1]/", "blocked"),
        ("http://172.16.254.1/", "blocked"),
    ],
)
def test_check_url_refuses_bad_schemes_and_raw_blocked_addresses(url: str, fragment: str) -> None:
    _, reason = check_url(url)
    assert reason is not None and fragment in reason


def test_check_url_passes_a_public_address_and_returns_a_hostname_to_resolve() -> None:
    assert check_url("https://93.184.216.34/v1") == ("93.184.216.34", None)
    assert check_url("HTTPS://api.anthropic.com:443/v1/messages") == ("api.anthropic.com", None)
    assert check_url("https://[2606:2800:220:1:248:1893:25c8:1946]/") == (
        "2606:2800:220:1:248:1893:25c8:1946",
        None,
    )


async def test_guard_resolves_hostnames_and_refuses_a_private_answer() -> None:
    table = {
        "api.anthropic.com": ["160.79.104.10"],
        "dual.example": ["93.184.216.34", "10.0.0.5"],
        "local.example": ["127.0.0.1"],
        "six.example": ["2606:2800:220:1:248:1893:25c8:1946"],
    }

    async def resolver(hostname: str) -> list[str]:
        if hostname not in table:
            raise OSError("no such host")
        return table[hostname]

    assert await guard("https://api.anthropic.com/v1/messages", resolver) is None
    assert await guard("https://six.example/", resolver) is None
    assert await guard("https://93.184.216.34/", resolver) is None  # raw: never resolved
    dual = await guard("https://dual.example/", resolver)
    assert dual is not None and "dual.example" in dual and "10.0.0.5" in dual
    local = await guard("http://local.example/", resolver)
    assert local is not None and "127.0.0.1" in local
    missing = await guard("https://nowhere.example/", resolver)
    assert missing is not None and "resolve" in missing
    scheme = await guard("ftp://api.anthropic.com/", resolver)
    assert scheme is not None and "scheme" in scheme


async def test_guard_refuses_a_host_that_resolves_to_nothing() -> None:
    async def resolver(hostname: str) -> list[str]:
        return []

    reason = await guard("https://empty.example/", resolver)
    assert reason is not None and "resolve" in reason
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_relay_ssrf.py -q`
Expected: `ModuleNotFoundError: mshkn.services.ssrf`.

- [ ] **Step 3: Write the guard**

`src/mshkn/services/ssrf.py`:

```python
"""The relay's SSRF guard (#110), ported from lampas: a target may be http or
https, may not be a raw private or reserved address, and every address its
hostname resolves to is checked. Two deviations from lampas, on purpose: a
hostname that does not resolve is refused (lampas failed open behind
Cloudflare's own fetch guard; mshkn has none), and there is no allowlist or off
switch (the flow tier injects a resolver instead)."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    Resolver = Callable[[str], Awaitable[list[str]]]

Address = ipaddress.IPv4Address | ipaddress.IPv6Address

ALLOWED_SCHEMES = frozenset({"http", "https"})
# Lampas's list. 172.16.0.0/12 is also the VM and Docker range on the host.
BLOCKED_RANGES: tuple[str, ...] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)
_NETWORKS = tuple(ipaddress.ip_network(r) for r in BLOCKED_RANGES)


def parse_address(text: str) -> Address | None:
    """The address `text` names, with an IPv4-mapped IPv6 address unwrapped; None for a name."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def blocked_reason(address: Address) -> str | None:
    for network in _NETWORKS:
        if address.version == network.version and address in network:
            return f"{address} is in the blocked range {network}"
    return None


def check_url(url: str) -> tuple[str, str | None]:
    """(hostname, reason): the scheme and a raw address are checked here; a name
    is returned for the caller to resolve. `reason` is None when nothing is wrong."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        return "", "invalid URL"
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return "", f"blocked scheme {parts.scheme or '(none)'!r}: only http and https"
    if not hostname:
        return "", "invalid URL: no host"
    address = parse_address(hostname)
    if address is not None:
        reason = blocked_reason(address)
        if reason is not None:
            return hostname, f"blocked address: {reason}"
    return hostname, None


async def resolve_host(hostname: str) -> list[str]:
    """Every address the process's resolver returns for the name, A and AAAA."""
    infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


async def guard(url: str, resolver: Resolver) -> str | None:
    """The reason `url` may not be called, or None. Resolves a hostname through `resolver`."""
    hostname, reason = check_url(url)
    if reason is not None:
        return reason
    if parse_address(hostname) is not None:
        return None
    try:
        addresses = await resolver(hostname)
    except OSError as exc:
        return f"{hostname} does not resolve: {exc}"
    if not addresses:
        return f"{hostname} does not resolve to any address"
    for text in addresses:
        address = parse_address(text)
        if address is None:
            return f"{hostname} resolves to an unparseable address {text!r}"
        blocked = blocked_reason(address)
        if blocked is not None:
            return f"{hostname} resolves to a blocked address: {blocked}"
    return None
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_relay_ssrf.py -q && uv run mypy`
Expected: pass. If `parts.hostname` on `http://[::1` raises `ValueError` only on `.port`, the `try` still covers it: `urlsplit` validates brackets on access to `hostname`; the test pins it.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add src/mshkn/services/ssrf.py tests/unit/test_relay_ssrf.py
git commit -m "feat(relay): the SSRF guard, ported from lampas (#110)"
```

---

### Task 4: Reassembling a streamed message

**Files:**
- Create: `src/mshkn/services/sse.py`, `tests/unit/test_relay_sse.py`

**Interfaces:**
- Produces: `mshkn.services.sse.parse_events(text) -> list[dict]`, `reassemble(text) -> dict` (the final message), `StreamError(type, message)` with `.retryable`, `IncompleteStream`; `RETRYABLE_STREAM_ERRORS = frozenset({"overloaded_error", "api_error"})`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_relay_sse.py`:

```python
"""A text/event-stream body becomes the final message with every block kept (#110):
text, tool_use with chunked partial JSON, thinking with its signature, cumulative usage."""

from __future__ import annotations

import json

import pytest

from mshkn.services.sse import IncompleteStream, StreamError, parse_events, reassemble


def _sse(*events: dict[str, object]) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 25, "output_tokens": 1},
    },
}


def test_text_tool_use_thinking_and_usage_are_reassembled() -> None:
    text = _sse(
        START,
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "see."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig=="}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "ping"},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "propose", "input": {}},
        },
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"kind": "ve'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": 'rb", "n": 1}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    )
    message = reassemble(text)
    assert message["content"] == [
        {"type": "thinking", "thinking": "Let me see.", "signature": "sig=="},
        {"type": "text", "text": "Hello"},
        {"type": "tool_use", "id": "toolu_1", "name": "propose", "input": {"kind": "verb", "n": 1}},
    ]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 25, "output_tokens": 42}
    assert message["id"] == "msg_1" and message["model"] == "claude-opus-5"


def test_an_empty_tool_input_is_an_empty_object_and_unknown_events_are_ignored() -> None:
    text = _sse(
        START,
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": "list", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "future_delta", "x": 1}},
        {"type": "content_block_stop", "index": 0},
        {"type": "future_event", "payload": {}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )
    message = reassemble(text)
    assert message["content"][0]["input"] == {}
    assert message["stop_reason"] == "tool_use"


def test_max_tokens_stop_reason_and_omitted_thinking_survive() -> None:
    text = _sse(
        START,
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "s"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 64000}},
        {"type": "message_stop"},
    )
    message = reassemble(text)
    assert message["content"] == [{"type": "thinking", "thinking": "", "signature": "s"}]
    assert message["stop_reason"] == "max_tokens" and message["usage"]["output_tokens"] == 64000


def test_an_error_event_raises_and_says_whether_it_is_retryable() -> None:
    overloaded = _sse(START, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    with pytest.raises(StreamError) as exc:
        reassemble(overloaded)
    assert exc.value.retryable and exc.value.type == "overloaded_error" and "Overloaded" in str(exc.value)
    invalid = _sse(START, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})
    with pytest.raises(StreamError) as exc:
        reassemble(invalid)
    assert not exc.value.retryable


def test_a_stream_cut_before_message_stop_is_incomplete() -> None:
    with pytest.raises(IncompleteStream):
        reassemble(_sse(START, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
    with pytest.raises(IncompleteStream):
        reassemble("")
    with pytest.raises(IncompleteStream):
        reassemble(_sse({"type": "message_stop"}))  # no message_start


def test_parse_events_joins_data_lines_and_skips_junk() -> None:
    text = 'event: message_stop\ndata: {"type":\ndata: "message_stop"}\n\n: comment\n\ndata: not json\n\n'
    assert parse_events(text) == [{"type": "message_stop"}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_relay_sse.py -q`
Expected: `ModuleNotFoundError: mshkn.services.sse`.

- [ ] **Step 3: Write the module**

`src/mshkn/services/sse.py`:

```python
"""Reassemble a Messages API event stream into the final message (#110). Every
block is kept: text, tool_use (partial JSON accumulated and parsed at the
block's stop), thinking with its signature; message_delta's usage is
cumulative and merged over message_start's. Unknown events and delta types are
ignored, as the API's versioning policy asks."""

from __future__ import annotations

import json
from typing import Any

RETRYABLE_STREAM_ERRORS = frozenset({"overloaded_error", "api_error"})


class StreamError(Exception):
    """The API sent an `error` event; `overloaded_error` is the streamed 529."""

    def __init__(self, type: str, message: str) -> None:  # noqa: A002
        super().__init__(f"{type}: {message}")
        self.type = type
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.type in RETRYABLE_STREAM_ERRORS


class IncompleteStream(Exception):
    """The stream ended before message_stop, or never began: a transport failure."""


def parse_events(text: str) -> list[dict[str, Any]]:
    """The JSON of every `data:` payload, in order; multi-line data joined by newlines."""
    events: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")]
        if not data:
            continue
        try:
            payload = json.loads("\n".join(data))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def reassemble(text: str) -> dict[str, Any]:
    message: dict[str, Any] | None = None
    partial: dict[int, list[str]] = {}
    stopped = False
    for event in parse_events(text):
        kind = event.get("type")
        if kind == "error":
            error = event.get("error") or {}
            raise StreamError(str(error.get("type", "error")), str(error.get("message", "")))
        if kind == "message_start":
            message = dict(event["message"])
            message["content"] = list(message.get("content") or [])
            continue
        if message is None:
            raise IncompleteStream("events before message_start")
        content: list[Any] = message["content"]
        if kind == "content_block_start":
            index = int(event["index"])
            while len(content) <= index:
                content.append(None)
            content[index] = dict(event["content_block"])
            if content[index].get("type") == "tool_use":
                partial[index] = []
        elif kind == "content_block_delta":
            index = int(event["index"])
            block = content[index]
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
            elif delta_type == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
            elif delta_type == "signature_delta":
                block["signature"] = delta["signature"]
            elif delta_type == "input_json_delta":
                partial.setdefault(index, []).append(delta["partial_json"])
        elif kind == "content_block_stop":
            index = int(event["index"])
            if index in partial:
                raw = "".join(partial.pop(index))
                content[index]["input"] = json.loads(raw) if raw.strip() else {}
        elif kind == "message_delta":
            message.update(event.get("delta") or {})
            usage = event.get("usage")
            if usage:
                message["usage"] = {**(message.get("usage") or {}), **usage}
        elif kind == "message_stop":
            stopped = True
            break
    if message is None or not stopped:
        raise IncompleteStream("stream ended before message_stop")
    return message
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_relay_sse.py -q && uv run mypy`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add src/mshkn/services/sse.py tests/unit/test_relay_sse.py
git commit -m "feat(relay): reassemble a streamed message with every block kept (#110)"
```

---

### Task 5: `RelayService`: submit, the upstream call, retry, headers cleared, restart

**Files:**
- Create: `src/mshkn/services/relay.py`, `tests/unit/test_relay_service.py`

**Interfaces:**
- Consumes: Task 1's models and db functions, Task 3's `guard`/`resolve_host`, Task 4's `reassemble`.
- Produces: `mshkn.services.relay.RelayRequest(target, method, forward_headers, body, retry, timeout_seconds, deliver)`; `RelayService(config, db, checkpoints, lifecycle, tasks, http, *, resolver=resolve_host, sleep=asyncio.sleep)` with `submit(account, request, *, api_key_id) -> RelayJob` (spawns the task under key `relay:<job id>`), `get_owned(account, job_id) -> RelayJob`, `run(job)`, `deliver(job)` (a stub in this task, filled in Task 6), `resume() -> int`; attributes `resolve` and `sleep` that tests replace.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_relay_service.py`:

```python
"""The relay's upstream half (#110): a job is accepted at once, called in the
background with lampas's retry policy, its headers deleted when the call settles,
its response stored whole, and re-run after a restart."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mshkn.config import Config
from mshkn.db import get_relay_job, insert_account, insert_relay_job
from mshkn.errors import InvalidInput, NotFound, PayloadTooLarge
from mshkn.host.fake import FakeHost
from mshkn.models import RelayStatus, RetryPolicy
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.recipes import RecipeService
from mshkn.services.relay import RelayRequest, RelayService
from tests.support import account_row
from tests.unit.test_relay_db import job_row

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import aiosqlite

ACCOUNT = account_row(api_key="k")
Handler = Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response]


async def _public(hostname: str) -> list[str]:
    return ["93.184.216.34"]


class Relay:
    """A RelayService over a MockTransport, with the sleeps it took recorded."""

    def __init__(self, db: aiosqlite.Connection, tmp_path: Path, handler: Handler, **config: Any) -> None:
        self.config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts", **config)
        host = FakeHost()
        allocator = SlotAllocator()
        self.tasks = BackgroundTasks()
        recipes = RecipeService(self.config, db, host.blocks, host.hypervisor, allocator, self.tasks)
        computers = ComputerService(self.config, db, host, allocator, recipes)
        checkpoints = CheckpointService(self.config, db, host, allocator, computers, self.tasks)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        lifecycle = Lifecycle(db, computers, checkpoints, self.tasks, self.http)
        self.slept: list[float] = []

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)

        self.service = RelayService(
            self.config, db, checkpoints, lifecycle, self.tasks, self.http, resolver=_public, sleep=sleep
        )

    async def submit(self, **fields: Any) -> str:
        request = RelayRequest(
            target=fields.pop("target", "https://model.example/v1/messages"),
            method=fields.pop("method", "POST"),
            forward_headers=fields.pop("forward_headers", {"x-api-key": "sk-secret"}),
            body=fields.pop("body", {"model": "m", "stream": True}),
            retry=fields.pop("retry", RetryPolicy()),
            timeout_seconds=fields.pop("timeout_seconds", None),
            deliver=fields.pop("deliver", None),
        )
        assert not fields, fields
        job = await self.service.submit(ACCOUNT, request, api_key_id="key-1")
        assert job.status == RelayStatus.QUEUED and job.id.startswith("rj-")
        return job.id

    async def settled(self, job_id: str) -> Any:
        await self.tasks.wait(f"relay:{job_id}")
        job = await self.service.get_owned(ACCOUNT, job_id)
        assert job.forward_headers is None, "headers are deleted whatever the outcome"
        return job


@pytest.fixture
async def account(db: aiosqlite.Connection) -> None:
    await insert_account(db, ACCOUNT)


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


MESSAGE_STREAM = _sse(
    {"type": "message_start", "message": {"id": "msg", "type": "message", "role": "assistant", "model": "m", "content": [], "usage": {"input_tokens": 3, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
    {"type": "message_stop"},
)


async def test_a_job_is_called_once_with_its_headers_and_body_and_the_stream_is_reassembled(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=MESSAGE_STREAM)

    relay = Relay(db, tmp_path, handler)
    job_id = await relay.submit()
    job = await relay.settled(job_id)
    assert job.status == RelayStatus.COMPLETED and job.attempts == 1 and job.error is None
    assert job.response_status == 200
    assert job.response_body["content"] == [{"type": "text", "text": "hi"}]
    assert job.response_body["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert job.response_headers["content-type"] == "text/event-stream"
    request = seen[0]
    assert request.method == "POST" and str(request.url) == "https://model.example/v1/messages"
    assert request.headers["x-api-key"] == "sk-secret"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"model": "m", "stream": True}
    assert relay.slept == []


async def test_json_and_text_bodies_are_stored_as_they_are_and_get_sends_none(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/json":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<title>Example Domain</title>")

    relay = Relay(db, tmp_path, handler)
    as_json = await relay.settled(await relay.submit(target="https://model.example/json", body="raw", forward_headers={}))
    assert as_json.response_body == {"ok": True}
    assert seen[0].content == b"raw" and "content-type" not in seen[0].headers
    as_text = await relay.settled(await relay.submit(target="https://model.example/page", method="GET", body=None))
    assert as_text.response_body == "<title>Example Domain</title>"
    assert seen[1].method == "GET" and seen[1].content == b""


async def test_5xx_429_and_transport_errors_are_retried_with_the_policy_then_fail(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("down")
        if calls == 2:
            return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error"}})
        return httpx.Response(429, json={})

    relay = Relay(db, tmp_path, handler)
    job = await relay.settled(await relay.submit(retry=RetryPolicy(attempts=3, initial_delay_ms=500, max_delay_ms=800)))
    assert job.status == RelayStatus.FAILED and job.attempts == 3
    assert job.error == "HTTP 429"
    assert job.response_status == 429, "the last response is kept beside the error"
    assert relay.slept == [0.5, 0.8]


async def test_a_retry_that_then_succeeds_completes(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else httpx.Response(200, json={"n": calls})

    relay = Relay(db, tmp_path, handler)
    job = await relay.settled(await relay.submit())
    assert job.status == RelayStatus.COMPLETED and job.attempts == 2 and job.error is None
    assert job.response_body == {"n": 2} and relay.slept == [1.0]


async def test_an_overloaded_stream_error_is_retried_and_another_is_final(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse({"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}))

    relay = Relay(db, tmp_path, handler)
    job = await relay.settled(await relay.submit())
    assert job.status == RelayStatus.FAILED and job.attempts == 2
    assert "invalid_request_error" in (job.error or "") and relay.slept == [1.0]


async def test_a_4xx_is_completed_with_its_status_and_a_cut_stream_is_retried(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    streams = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal streams
        if request.url.path == "/bad":
            return httpx.Response(400, json={"error": {"message": "max_tokens too large"}})
        streams += 1
        if streams == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=MESSAGE_STREAM[:80])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=MESSAGE_STREAM)

    relay = Relay(db, tmp_path, handler)
    bad = await relay.settled(await relay.submit(target="https://model.example/bad"))
    assert bad.status == RelayStatus.COMPLETED and bad.response_status == 400 and bad.attempts == 1
    assert bad.response_body == {"error": {"message": "max_tokens too large"}}
    cut = await relay.settled(await relay.submit())
    assert cut.status == RelayStatus.COMPLETED and cut.attempts == 2 and relay.slept == [1.0]


async def test_a_timeout_and_an_oversized_response_are_final(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slow":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 2048)

    relay = Relay(db, tmp_path, handler, relay_body_bytes=1024)
    slow = await relay.settled(await relay.submit(target="https://model.example/slow"))
    assert slow.status == RelayStatus.FAILED and slow.attempts == 1 and "timeout" in (slow.error or "")
    big = await relay.settled(await relay.submit(target="https://model.example/big", method="GET", body=None))
    assert big.status == RelayStatus.FAILED and big.attempts == 1 and "1024" in (big.error or "")
    assert relay.slept == []


async def test_submit_refuses_a_blocked_target_an_over_cap_timeout_and_a_big_body(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda request: httpx.Response(200), relay_body_bytes=64, relay_timeout_seconds=10)
    with pytest.raises(InvalidInput, match="blocked"):
        await relay.submit(target="http://127.0.0.1:8000/health")
    with pytest.raises(InvalidInput, match="timeout_seconds"):
        await relay.submit(timeout_seconds=11)
    with pytest.raises(PayloadTooLarge):
        await relay.submit(body={"pad": "x" * 100})
    job_id = await relay.submit(body={"ok": 1}, timeout_seconds=10)
    job = await relay.settled(job_id)
    assert job.timeout_seconds == 10
    default = await relay.settled(await relay.submit(body={"ok": 1}))
    assert default.timeout_seconds == 10


async def test_get_owned_is_scoped_to_the_account(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda request: httpx.Response(200, json={}))
    job_id = await relay.submit()
    await relay.settled(job_id)
    with pytest.raises(NotFound):
        await relay.service.get_owned(account_row("acct-2", api_key="o"), job_id)
    with pytest.raises(NotFound):
        await relay.service.get_owned(ACCOUNT, "rj-nope")


async def test_resume_re_runs_jobs_still_queued_or_in_progress(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    relay = Relay(db, tmp_path, handler)
    await insert_relay_job(db, job_row("rj-queued", status=RelayStatus.QUEUED, deliver=None))
    interrupted = job_row("rj-running", status=RelayStatus.IN_PROGRESS, deliver=None)
    interrupted.attempts = 1
    await insert_relay_job(db, interrupted)
    await insert_relay_job(db, job_row("rj-done", status=RelayStatus.COMPLETED, deliver=None))
    assert await relay.service.resume() == 2
    queued = await relay.settled("rj-queued")
    running = await relay.settled("rj-running")
    assert queued.status == RelayStatus.COMPLETED and queued.attempts == 1
    assert running.status == RelayStatus.COMPLETED and running.attempts == 2
    done = await get_relay_job(db, "rj-done")
    assert done is not None and done.forward_headers == {"x-api-key": "sk-secret"}, "untouched"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_relay_service.py -q`
Expected: `ModuleNotFoundError: mshkn.services.relay`.

- [ ] **Step 3: Write the service**

`src/mshkn/services/relay.py`:

```python
"""The relay (#110), lampas folded into mshkn: a job is one upstream HTTP call
plus at most one delivery. The call runs in a background task with lampas's
retry policy; the forwarded headers are deleted the moment it settles; the
response is stored whole; delivery is a fork by label (spec §3, §5)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from mshkn.db import (
    get_account_by_id,
    get_relay_job,
    insert_relay_job,
    list_relay_jobs_by_status,
    update_relay_job,
)
from mshkn.errors import InvalidInput, NotFound, PayloadTooLarge
from mshkn.models import DeliveryStatus, ExecSpec, RelayJob, RelayStatus
from mshkn.services.checkpoints import Deferred
from mshkn.services.sse import IncompleteStream, StreamError, reassemble
from mshkn.services.ssrf import guard, resolve_host

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Account, RelayDelivery, RetryPolicy
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.lifecycle import Lifecycle
    from mshkn.services.ssrf import Resolver

logger = logging.getLogger(__name__)

# What the SDK itself retries, plus any 5xx (529 included).
RETRY_STATUSES = frozenset({408, 409, 429})
BODYLESS_METHODS = frozenset({"GET", "HEAD"})


@dataclass(frozen=True)
class RelayRequest:
    """A validated `POST /relay` body; the router builds it, the service checks the rest."""

    target: str
    method: str
    forward_headers: dict[str, str]
    body: object
    retry: RetryPolicy
    timeout_seconds: int | None
    deliver: RelayDelivery | None


@dataclass(frozen=True)
class _Upstream:
    status: int
    headers: dict[str, str]
    body: object


class _Transient(Exception):
    """An attempt failed in a way the policy retries."""


class _Final(Exception):
    """An attempt failed for good."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _encode(body: object) -> bytes:
    return body.encode() if isinstance(body, str) else json.dumps(body).encode()


def task_key(job_id: str) -> str:
    return f"relay:{job_id}"


class RelayService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        checkpoints: CheckpointService,
        lifecycle: Lifecycle,
        tasks: BackgroundTasks,
        http: httpx.AsyncClient,
        *,
        resolver: Resolver = resolve_host,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.db = db
        self.checkpoints = checkpoints
        self.lifecycle = lifecycle
        self.tasks = tasks
        self.http = http
        self.resolve = resolver
        self.sleep = sleep

    # -- submit and read -----------------------------------------------------

    async def submit(
        self, account: Account, request: RelayRequest, *, api_key_id: str | None
    ) -> RelayJob:
        reason = await guard(request.target, self.resolve)
        if reason is not None:
            raise InvalidInput(f"target refused: {reason}")
        cap = self.config.relay_timeout_seconds
        timeout = cap if request.timeout_seconds is None else request.timeout_seconds
        if timeout > cap:
            raise InvalidInput(f"timeout_seconds must be at most {cap}")
        limit = self.config.relay_body_bytes
        if request.body is not None and len(_encode(request.body)) > limit:
            raise PayloadTooLarge(f"body exceeds {limit} bytes")
        now = _now()
        job = RelayJob(
            id=f"rj-{uuid.uuid4().hex[:12]}",
            account_id=account.id,
            api_key_id=api_key_id,
            status=RelayStatus.QUEUED,
            target=request.target,
            method=request.method,
            forward_headers=dict(request.forward_headers),
            body=request.body,
            retry=request.retry,
            timeout_seconds=timeout,
            deliver=request.deliver,
            attempts=0,
            error=None,
            response_status=None,
            response_headers=None,
            response_body=None,
            delivery_status=None if request.deliver is None else DeliveryStatus.PENDING,
            delivery_attempts=0,
            delivery_computer_id=None,
            delivery_deferred_id=None,
            delivery_error=None,
            created_at=now,
            updated_at=now,
        )
        await insert_relay_job(self.db, job)
        self._spawn(job)
        return job

    async def get_owned(self, account: Account, job_id: str) -> RelayJob:
        job = await get_relay_job(self.db, job_id)
        if job is None or job.account_id != account.id:
            raise NotFound("Relay job not found")
        return job

    async def resume(self) -> int:
        """Re-run every job a previous process left unsettled: its headers are
        still stored and the caller has seen nothing (spec §3)."""
        jobs = await list_relay_jobs_by_status(
            self.db, (RelayStatus.QUEUED, RelayStatus.IN_PROGRESS)
        )
        for job in jobs:
            self._spawn(job)
        return len(jobs)

    def _spawn(self, job: RelayJob) -> None:
        self.tasks.spawn(self.run(job), name=task_key(job.id), key=task_key(job.id))

    async def _save(self, job: RelayJob) -> None:
        job.updated_at = _now()
        await update_relay_job(self.db, job)

    # -- the upstream call ---------------------------------------------------

    async def run(self, job: RelayJob) -> None:
        try:
            await self._call(job)
        finally:
            # Deleted whatever happened, even a cancellation on shutdown.
            job.forward_headers = None
            await self._save(job)
        if job.deliver is not None:
            await self.deliver(job)

    async def _call(self, job: RelayJob) -> None:
        policy = job.retry
        for attempt in range(job.attempts, policy.attempts):
            job.attempts = attempt + 1
            job.status = RelayStatus.IN_PROGRESS
            await self._save(job)
            try:
                upstream = await self._attempt(job)
            except _Final as exc:
                job.status, job.error = RelayStatus.FAILED, str(exc)
                return
            except _Transient as exc:
                job.error = str(exc)
                logger.warning("relay %s attempt %d failed: %s", job.id, job.attempts, exc)
                if attempt + 1 < policy.attempts:
                    await self.sleep(policy.delay(attempt))
                    continue
                job.status = RelayStatus.FAILED
                return
            job.response_status = upstream.status
            job.response_headers = upstream.headers
            job.response_body = upstream.body
            if upstream.status in RETRY_STATUSES or upstream.status >= 500:
                job.error = f"HTTP {upstream.status}"
                logger.warning("relay %s attempt %d: %s", job.id, job.attempts, job.error)
                if attempt + 1 < policy.attempts:
                    await self.sleep(policy.delay(attempt))
                    continue
                job.status = RelayStatus.FAILED
                return
            job.status, job.error = RelayStatus.COMPLETED, None
            return
        # A restart found the job with its attempts already spent.
        job.status = RelayStatus.FAILED
        job.error = job.error or "attempts exhausted"

    async def _attempt(self, job: RelayJob) -> _Upstream:
        headers = dict(job.forward_headers or {})
        content: bytes | None = None
        if job.method not in BODYLESS_METHODS and job.body is not None:
            content = _encode(job.body)
            if not isinstance(job.body, str) and not any(
                k.lower() == "content-type" for k in headers
            ):
                headers["content-type"] = "application/json"
        limit = self.config.relay_body_bytes
        seconds = float(job.timeout_seconds)
        try:
            async with (
                asyncio.timeout(seconds),
                self.http.stream(
                    job.method,
                    job.target,
                    headers=headers,
                    content=content,
                    timeout=httpx.Timeout(seconds),
                ) as response,
            ):
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise _Final(f"upstream body exceeds {limit} bytes")
                    chunks.append(chunk)
        except TimeoutError as exc:
            raise _Final(f"timeout after {job.timeout_seconds} s") from exc
        except httpx.TimeoutException as exc:
            raise _Final(f"timeout after {job.timeout_seconds} s: {type(exc).__name__}") from exc
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            raise _Transient(f"{type(exc).__name__}: {exc}") from exc
        text = b"".join(chunks).decode(errors="replace")
        content_type = response.headers.get("content-type", "")
        body: object
        if "text/event-stream" in content_type:
            try:
                body = reassemble(text)
            except StreamError as exc:
                if exc.retryable:
                    raise _Transient(str(exc)) from exc
                raise _Final(str(exc)) from exc
            except IncompleteStream as exc:
                raise _Transient(str(exc)) from exc
        elif "json" in content_type:
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text
        else:
            body = text
        return _Upstream(response.status_code, dict(response.headers.items()), body)

    # -- delivery (Task 6) ---------------------------------------------------

    async def deliver(self, job: RelayJob) -> None:
        raise NotImplementedError("Task 6")
```

Note: `asyncio.timeout` raises `TimeoutError` (the builtin) on Python 3.12; `httpx.ReadTimeout` is an `httpx.TimeoutException`, which subclasses `httpx.HTTPError`, so its `except` clause must come first, as written.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_relay_service.py -q && uv run mypy`
Expected: pass. If `httpx.MockTransport` refuses a `raise` inside a sync handler for the streaming call, make the handler `async` (both shapes are accepted by `MockTransport`).

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add src/mshkn/services/relay.py tests/unit/test_relay_service.py
git commit -m "feat(relay): jobs run upstream in the background with retry and cleared headers (#110)"
```

---

### Task 6: Delivery by fork-by-label

**Files:**
- Modify: `src/mshkn/services/relay.py` (`deliver`), `tests/unit/test_relay_service.py`

**Interfaces:**
- Produces: `RelayService.deliver(job)`: forks `job.deliver.label` with `exec "<exec> <job_id>"`, `self_destruct`, `defer_on_conflict`, runs it through `Lifecycle.run_ephemeral`, retries a raising fork with the job's policy, records `delivery_status`, `delivery_computer_id` or `delivery_deferred_id`, `delivery_error`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_relay_service.py`:

```python
from mshkn.db import claim_deferred_by_label, get_exec_log
from mshkn.host import ExecResult
from mshkn.models import CheckpointTrigger, DeliveryStatus, RelayDelivery
from mshkn.resources import DEFAULT_RESOURCES


async def _chain(relay: Relay, db: aiosqlite.Connection, label: str) -> None:
    """A labelled head to fork: a computer checkpointed under `label` and destroyed."""
    computers = relay.service.lifecycle.computers
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await relay.service.checkpoints.create(base, label=label, trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)


async def test_a_settled_job_wakes_its_chain_with_the_job_id(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda request: httpx.Response(200, json={"answer": 42}))
    await _chain(relay, db, "brain")
    host_guest = relay.service.lifecycle.computers.host.guest
    job_id = await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume"))
    host_guest.script[f"membrane resume {job_id}"] = ExecResult(0, "resumed\n", "")
    job = await relay.settled(job_id)
    assert job.status == RelayStatus.COMPLETED
    assert job.delivery_status == DeliveryStatus.DELIVERED and job.delivery_attempts == 1
    assert job.delivery_computer_id is not None and job.delivery_deferred_id is None
    log = await get_exec_log(db, job.delivery_computer_id)
    assert log is not None and log.command == f"membrane resume {job_id}" and log.stdout == "resumed\n"
    assert log.label == "brain" and log.created_checkpoint_id is not None
    heads = await relay.service.checkpoints.list(ACCOUNT, label="brain")
    assert len(heads) == 2 and heads[0].id == log.created_checkpoint_id


async def test_a_failed_job_is_delivered_too(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    relay = Relay(db, tmp_path, handler)
    await _chain(relay, db, "brain")
    job = await relay.settled(await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume")))
    assert job.status == RelayStatus.FAILED and job.delivery_status == DeliveryStatus.DELIVERED
    assert job.delivery_computer_id is not None


async def test_a_busy_chain_defers_the_wake_up(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda request: httpx.Response(200, json={}))
    await _chain(relay, db, "brain")
    # a computer forked from the head is still running on the label
    head = await relay.service.checkpoints.latest_for_label(ACCOUNT, "brain")
    assert head is not None
    await relay.service.lifecycle.computers.fork(ACCOUNT, head, recipe_id=None, api_key_id=None)
    job = await relay.settled(await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume")))
    assert job.delivery_status == DeliveryStatus.DELIVERED
    assert job.delivery_computer_id is None and job.delivery_deferred_id is not None
    queued = await claim_deferred_by_label(db, "brain")
    assert len(queued) == 1 and json.loads(queued[0].request_payload)["exec"] == f"membrane resume {job.id}"


async def test_a_fork_that_raises_is_retried_then_the_delivery_fails(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda request: httpx.Response(200, json={}))
    # no chain: every fork is NotFound
    job = await relay.settled(await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume")))
    assert job.status == RelayStatus.COMPLETED, "a failed delivery never loses the result"
    assert job.delivery_status == DeliveryStatus.FAILED and job.delivery_attempts == 3
    assert job.delivery_error is not None and "NotFound" in job.delivery_error
    assert relay.slept == [1.0, 2.0]
```

`ComputerService.create(account, recipe_id=None, resources=DEFAULT_RESOURCES)`, `CheckpointService.create(computer, label=..., trigger=CheckpointTrigger.API)` and `ComputerService.fork(account, checkpoint, recipe_id=None, api_key_id=None)` are the same calls `tests/unit/test_ingress_service.py` makes to build a chain.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_relay_service.py -q -k "wakes or delivered_too or defers or retried_then"`
Expected: `NotImplementedError: Task 6`.

- [ ] **Step 3: Implement `deliver`**

Replace the stub in `src/mshkn/services/relay.py`:

```python
    async def deliver(self, job: RelayJob) -> None:
        """Wake the target chain: fork the label with the pinned exec plus the job
        id, self-destructing, deferred behind a running fork (spec §5). A fork that
        raises is retried with the job's policy; after that the delivery is failed
        and the job keeps its result: the brain's next command settles it itself."""
        assert job.deliver is not None
        account = await get_account_by_id(self.db, job.account_id)
        if account is None:
            job.delivery_status, job.delivery_error = DeliveryStatus.FAILED, "account not found"
            await self._save(job)
            return
        spec = ExecSpec(
            command=f"{job.deliver.exec} {job.id}",
            self_destruct=True,
            callback_url=None,
            label=None,
            meta_exec=None,
        )
        policy = job.retry
        for attempt in range(policy.attempts):
            job.delivery_attempts = attempt + 1
            try:
                head, forked = await self.checkpoints.fork_by_label(
                    account, job.deliver.label, spec, exclusive="defer_on_conflict", recipe_id=None
                )
                if isinstance(forked, Deferred):
                    job.delivery_deferred_id = forked.deferred_id
                else:
                    job.delivery_computer_id = forked.id
                job.delivery_status, job.delivery_error = DeliveryStatus.DELIVERED, None
                await self._save(job)
                if not isinstance(forked, Deferred):
                    await self.lifecycle.run_ephemeral(
                        account, forked, spec, source_checkpoint=head
                    )
                return
            except Exception as exc:
                job.delivery_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "relay %s delivery attempt %d failed: %s", job.id, job.delivery_attempts, exc
                )
                await self._save(job)
                if attempt + 1 < policy.attempts:
                    await self.sleep(policy.delay(attempt))
        job.delivery_status = DeliveryStatus.FAILED
        await self._save(job)
```

`Lifecycle` exposes `computers` (used by the test to reach the fake guest); `CheckpointService.list(account, label=...)` returns newest first.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_relay_service.py -q && uv run mypy`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add src/mshkn/services/relay.py tests/unit/test_relay_service.py
git commit -m "feat(relay): deliver a settled job by forking its chain by label (#110)"
```

---

### Task 7: Routes, scope checks, wiring, restart and expiry

**Files:**
- Create: `src/mshkn/api/relay.py`, `tests/unit/test_relay_api.py`
- Modify: `src/mshkn/api/schemas.py`, `src/mshkn/api/scopes.py`, `src/mshkn/app.py`, `src/mshkn/runtime.py`, `src/mshkn/services/reaper.py`, `docs/ARCHITECTURE.md` (§1a table only; the rest in Task 16), `tests/unit/test_scoped_routes.py`, `tests/unit/test_reaper.py`

**Interfaces:**
- Produces: `POST /relay` (202 `{job_id, status}`), `GET /relay/{job_id}` (`RelayJobResponse`); `mshkn.api.scopes.require_relay(principal, target, deliver_in_body)`, `require_relay_job_owner(principal, job)`; `Runtime.relay: RelayService`; `Runtime.start` re-runs unsettled jobs; `Reaper.expire_relay_jobs() -> int` in the cycle.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_relay_api.py`:

```python
"""The relay's two routes (#110): accepted at once, readable by its creator, and a
scoped key held to its pinned targets and wake-up."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from mshkn.config import Config
from mshkn.db import insert_account
from mshkn.host.fake import FakeHost
from tests.support import account_row
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import aiosqlite

    from mshkn.runtime import Runtime

ACCOUNT = {"Authorization": "Bearer test-key"}
RELAY_SCOPE = {
    "relay": {
        "targets": ["https://model.example/"],
        "deliver": {"label": "brain", "exec": "membrane resume"},
    }
}


async def _public(hostname: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
async def runtime(db: aiosqlite.Connection, tmp_path: Path) -> AsyncIterator[Runtime]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"echo": request.url.path})

    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    host = FakeHost()
    rt = make_runtime(db, config=config, host=host, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rt.relay.resolve = _public

    async def no_sleep(seconds: float) -> None:
        return None

    rt.relay.sleep = no_sleep  # the delivery's retries, without waiting
    try:
        yield rt
    finally:
        await rt.tasks.drain(timeout=2.0)
        await rt.http.aclose()
        host.close()


@pytest.fixture
async def client(db: aiosqlite.Connection, runtime: Runtime) -> AsyncIterator[AsyncClient]:
    await insert_account(db, account_row())
    async with AsyncClient(transport=ASGITransport(app=make_app(runtime)), base_url="http://test", headers=ACCOUNT) as c:
        yield c


async def _key(client: AsyncClient, scopes: dict[str, Any]) -> tuple[str, dict[str, str]]:
    resp = await client.post("/keys", json={"scopes": scopes, "label": "t"}, headers=ACCOUNT)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"], {"Authorization": f"Bearer {resp.json()['secret']}"}


async def test_the_account_key_submits_polls_and_gets_the_whole_record(client: AsyncClient, runtime: Runtime) -> None:
    resp = await client.post("/relay", json={"target": "https://model.example/v1/messages", "body": {"a": 1}, "forward_headers": {"x-api-key": "s"}})
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "queued"
    job_id = resp.json()["job_id"]
    await runtime.tasks.wait(f"relay:{job_id}")
    got = await client.get(f"/relay/{job_id}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["job_id"] == job_id and body["status"] == "completed" and body["attempts"] == 1
    assert body["response"]["status"] == 200 and body["response"]["body"] == {"echo": "/v1/messages"}
    assert body["response"]["headers"]["content-type"] == "application/json"  # httpx adds content-length too
    assert body["delivery"] is None and body["error"] is None
    assert "forward_headers" not in got.text and "x-api-key" not in got.text
    assert (await client.get("/relay/rj-nope")).status_code == 404


async def test_bad_requests_are_refused_before_a_job_exists(client: AsyncClient) -> None:
    blocked = await client.post("/relay", json={"target": "http://127.0.0.1:8000/health"})
    assert blocked.status_code == 422 and "blocked" in blocked.json()["detail"]
    scheme = await client.post("/relay", json={"target": "ftp://model.example/"})
    assert scheme.status_code == 422
    unknown = await client.post("/relay", json={"target": "https://model.example/", "timeout_ms": 5})
    assert unknown.status_code == 422
    too_long = await client.post("/relay", json={"target": "https://model.example/", "timeout_seconds": 999999})
    assert too_long.status_code == 422 and "timeout_seconds" in too_long.json()["detail"]
    method = await client.post("/relay", json={"target": "https://model.example/", "method": "TRACE"})
    assert method.status_code == 422


async def test_a_scoped_key_is_held_to_its_targets_and_its_pinned_wake_up(client: AsyncClient, runtime: Runtime) -> None:
    _, plain = await _key(client, {"recipes": {"read": True}})
    refused = await client.post("/relay", json={"target": "https://model.example/"}, headers=plain)
    assert refused.status_code == 403 and "relay" in refused.json()["detail"]
    key_id, scoped = await _key(client, RELAY_SCOPE)
    outside = await client.post("/relay", json={"target": "https://other.example/"}, headers=scoped)
    assert outside.status_code == 403 and "relay.targets" in outside.json()["detail"]
    with_deliver = await client.post("/relay", json={"target": "https://model.example/x", "deliver": {"label": "verb/x", "exec": "true"}}, headers=scoped)
    assert with_deliver.status_code == 422
    accepted = await client.post("/relay", json={"target": "https://model.example/x"}, headers=scoped)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    await runtime.tasks.wait(f"relay:{job_id}")
    mine = await client.get(f"/relay/{job_id}", headers=scoped)
    assert mine.status_code == 200
    assert mine.json()["delivery"]["label"] == "brain" and mine.json()["delivery"]["exec"] == "membrane resume"
    assert mine.json()["delivery"]["status"] == "failed", "no brain chain exists here"
    _, other = await _key(client, RELAY_SCOPE)
    assert (await client.get(f"/relay/{job_id}", headers=other)).status_code == 404
    assert (await client.get(f"/relay/{job_id}")).status_code == 200, "the account sees every job"
```

Append to `tests/unit/test_reaper.py`, which builds its reaper with the module's `_reaper(db, tmp_path)` helper (it inserts `ACCOUNT` itself, so call it once per test):

```python
async def test_the_cycle_expires_relay_jobs_with_the_exec_log_retention(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    from dataclasses import replace

    from mshkn.db import get_relay_job, insert_relay_job
    from tests.unit.test_relay_db import job_row

    reaper, _, _, _ = await _reaper(db, tmp_path)
    await insert_relay_job(db, job_row("rj-old", created_at="2026-01-01T00:00:00+00:00"))
    await insert_relay_job(db, job_row("rj-new", created_at=datetime.now(UTC).isoformat()))
    assert await reaper.expire_relay_jobs() == 1
    assert await get_relay_job(db, "rj-old") is None
    assert await get_relay_job(db, "rj-new") is not None
    reaper.config = replace(reaper.config, exec_log_retention_seconds=0)
    await insert_relay_job(db, job_row("rj-older", created_at="2025-01-01T00:00:00+00:00"))
    assert await reaper.expire_relay_jobs() == 0, "0 keeps every job"
    await reaper.cycle()  # the cycle runs it without raising
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_relay_api.py tests/unit/test_reaper.py -q`
Expected: 404 on `/relay` (no router) and `AttributeError: relay` on the runtime.

- [ ] **Step 3: Schemas**

Append to `src/mshkn/api/schemas.py` before the shared constructors:

```python
# --- relay -------------------------------------------------------------------


class RelayRetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: int = Field(default=3, ge=1)
    backoff: Literal["exponential"] = "exponential"
    initial_delay_ms: int = Field(default=1000, ge=0)
    max_delay_ms: int = Field(default=30000, ge=1)


class RelayDeliverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    exec: str = Field(min_length=1)


class RelayRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "POST"
    forward_headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    retry: RelayRetryBody = Field(default_factory=RelayRetryBody)
    timeout_seconds: int | None = Field(default=None, ge=1)
    deliver: RelayDeliverBody | None = None


class RelayAcceptedResponse(BaseModel):
    job_id: str
    status: str


class RelayResponseBody(BaseModel):
    status: int
    headers: dict[str, str]
    body: Any


class RelayDeliveryResponse(BaseModel):
    label: str
    exec: str
    status: str
    attempts: int
    computer_id: str | None = None
    deferred_id: str | None = None
    error: str | None = None


class RelayJobResponse(BaseModel):
    job_id: str
    status: str
    target: str
    method: str
    created_at: str
    updated_at: str
    attempts: int
    error: str | None = None
    response: RelayResponseBody | None = None
    delivery: RelayDeliveryResponse | None = None


def relay_job_response(job: RelayJob) -> RelayJobResponse:
    response = None
    if job.response_status is not None:
        response = RelayResponseBody(
            status=job.response_status, headers=job.response_headers or {}, body=job.response_body
        )
    delivery = None
    if job.deliver is not None:
        delivery = RelayDeliveryResponse(
            label=job.deliver.label,
            exec=job.deliver.exec,
            status=str(job.delivery_status),
            attempts=job.delivery_attempts,
            computer_id=job.delivery_computer_id,
            deferred_id=job.delivery_deferred_id,
            error=job.delivery_error,
        )
    return RelayJobResponse(
        job_id=job.id,
        status=str(job.status),
        target=job.target,
        method=job.method,
        created_at=job.created_at,
        updated_at=job.updated_at,
        attempts=job.attempts,
        error=job.error,
        response=response,
        delivery=delivery,
    )
```

Add `ConfigDict`, `Literal`, `Any` and `RelayJob` to the file's imports as needed (`from typing import Any, Literal`; `from pydantic import BaseModel, ConfigDict, Field`; `RelayJob` under `TYPE_CHECKING` from `mshkn.models`).

- [ ] **Step 4: Scope checks**

Append to `src/mshkn/api/scopes.py`:

```python
def require_relay(principal: Principal, target: str, *, deliver_in_body: bool) -> None:
    """`POST /relay`: a scoped key needs the relay section, a target under its
    prefixes, and no `deliver` of its own (the scope pins it, #110)."""
    scopes = principal.scopes
    if scopes is None:
        return
    if not scopes.has_relay:
        raise Forbidden("Scope relay is not granted to this key")
    if not scopes.may_relay_to(target):
        raise Forbidden(f"Scope relay.targets does not allow {target}")
    if deliver_in_body:
        raise InvalidInput("deliver is pinned by this key's scope and may not be given")


def require_relay_job_owner(principal: Principal, job: RelayJob) -> None:
    """`GET /relay/{job_id}`: a scoped key sees only the jobs it created."""
    if principal.key is not None and job.api_key_id != principal.key.id:
        raise NotFound("Relay job not found")
```

Import `InvalidInput` and `NotFound` from `mshkn.errors`, and `RelayJob` under `TYPE_CHECKING`.

- [ ] **Step 5: The router**

`src/mshkn/api/relay.py`:

```python
"""The relay's routes (#110): submit a job, read it back. The work is in
mshkn.services.relay; a scoped key is checked here first (§1a)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request

from mshkn.api import scopes
from mshkn.api.deps import get_runtime, require_principal
from mshkn.api.schemas import (
    RelayAcceptedResponse,
    RelayJobResponse,
    RelayRequestBody,
    relay_job_response,
)
from mshkn.models import RelayDelivery, RetryPolicy
from mshkn.services.relay import RelayRequest

if TYPE_CHECKING:
    from mshkn.models import Principal

router = APIRouter(prefix="/relay", tags=["relay"])

_require_principal = Depends(require_principal)


@router.post("", response_model=RelayAcceptedResponse, status_code=202)
async def submit_job(
    body: RelayRequestBody,
    request: Request,
    principal: Principal = _require_principal,
) -> RelayAcceptedResponse:
    scopes.require_relay(principal, body.target, deliver_in_body=body.deliver is not None)
    if principal.scopes is not None:
        deliver = principal.scopes.relay_deliver
    elif body.deliver is not None:
        deliver = RelayDelivery(label=body.deliver.label, exec=body.deliver.exec)
    else:
        deliver = None
    job = await get_runtime(request).relay.submit(
        principal.account,
        RelayRequest(
            target=body.target,
            method=body.method,
            forward_headers=body.forward_headers,
            body=body.body,
            retry=RetryPolicy(
                attempts=body.retry.attempts,
                initial_delay_ms=body.retry.initial_delay_ms,
                max_delay_ms=body.retry.max_delay_ms,
            ),
            timeout_seconds=body.timeout_seconds,
            deliver=deliver,
        ),
        api_key_id=scopes.key_id(principal),
    )
    return RelayAcceptedResponse(job_id=job.id, status=str(job.status))


@router.get("/{job_id}", response_model=RelayJobResponse)
async def get_job(
    job_id: str, request: Request, principal: Principal = _require_principal
) -> RelayJobResponse:
    job = await get_runtime(request).relay.get_owned(principal.account, job_id)
    scopes.require_relay_job_owner(principal, job)
    return relay_job_response(job)
```

- [ ] **Step 6: Wiring**

`src/mshkn/app.py`: import `from mshkn.api.relay import router as relay_router` and `app.include_router(relay_router)` after the keys router.

`src/mshkn/runtime.py`: import `RelayService`; add the field `relay: RelayService` after `keys`; in `build`, after `ingress = ...`: `relay = RelayService(config, db, checkpoints, lifecycle, tasks, client)` and pass `relay=relay`; in `start()`, after the reaped block:

```python
        resumed_jobs = await self.relay.resume()
        if resumed_jobs:
            logger.info("Startup: re-running %d unsettled relay job(s)", resumed_jobs)
```

`src/mshkn/services/reaper.py`: import `delete_relay_jobs_before` from `mshkn.db`; in `cycle()` after `expired = ...`: `expired_jobs = await self.expire_relay_jobs()`, include it in the condition and the log line (`"%d relay job(s) expired"`); and:

```python
    async def expire_relay_jobs(self) -> int:
        """Relay jobs, responses included, go with the exec-log retention (#110)."""
        retention = self.config.exec_log_retention_seconds
        if retention <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=retention)).isoformat()
        return await delete_relay_jobs_before(self.db, cutoff)
```

`docs/ARCHITECTURE.md` §1a route table: add, before the merge/ingress/keys row:

```
| `POST /relay` | always; `deliver` optional | the `relay` scope: `target` must start with one of `relay.targets`, and `deliver` must be absent (the scope pins it). The job records the key |
| `GET /relay/{job_id}` | any job on the account | only a job this key created |
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/unit/test_relay_api.py tests/unit/test_reaper.py tests/unit/test_scoped_routes.py tests/unit/test_openapi.py tests/unit/test_docs.py tests/unit/test_runtime.py -q && uv run mypy`
Expected: pass. `test_openapi.py` may snapshot the route list: update its expectation to include the two routes.

- [ ] **Step 8: Commit**

```bash
uv run ruff format . && git add src/mshkn/api src/mshkn/app.py src/mshkn/runtime.py src/mshkn/services/reaper.py docs/ARCHITECTURE.md tests/unit/test_relay_api.py tests/unit/test_reaper.py tests/unit/test_openapi.py
git commit -m "feat(relay): POST /relay and GET /relay/{job_id}, the relay scope checks, wiring, restart and expiry (#110)"
```

---

### Task 8: The relay over the fake host: flow tests

**Files:**
- Create: `tests/flow/test_relay.py`
- Modify: `tests/flow/conftest.py`

**Interfaces:**
- Produces: `Flow.targets: dict[str, httpx.AsyncBaseTransport]` (the hosts the runtime's HTTP client can reach; `receiver` pre-registered), `HostRouter` transport; `runtime.relay.resolve` set to a public resolver and `runtime.relay.sleep` to a no-op sleep in every flow.

- [ ] **Step 1: Extend the flow conftest**

In `tests/flow/conftest.py` add:

```python
class HostRouter(httpx.AsyncBaseTransport):
    """One in-process transport per hostname: the callback receiver, and whatever
    a test mounts (a relay target, the scripted model server)."""

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        self.routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self.routes.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"no route to {request.url.host}")
        return await transport.handle_async_request(request)


async def _public_resolver(hostname: str) -> list[str]:
    """Every in-process host resolves to a public address, so the guard lets it through."""
    return ["93.184.216.34"]


async def _no_sleep(seconds: float) -> None:
    return None
```

`Flow` gains `targets: dict[str, httpx.AsyncBaseTransport]`. In `_build_flow`: `targets = {"receiver": ASGITransport(app=_receiver(received))}`; `callbacks = AsyncClient(transport=HostRouter(targets), base_url="http://receiver")`; after `runtime = Runtime.build(...)`: `runtime.relay.resolve = _public_resolver` and `runtime.relay.sleep = _no_sleep`; pass `targets=targets` to `Flow(...)`. `import httpx` at the top (it is imported under `TYPE_CHECKING` today for the types; move it to a real import).

- [ ] **Step 2: Write the flow tests**

`tests/flow/test_relay.py`:

```python
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
    ckpt = (await flow.client.post(f"/computers/{base}/checkpoint", json={"label": label})).json()["checkpoint_id"]
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
        json={"target": "http://model/v1/messages", "body": {"q": 1}, "forward_headers": {"x-api-key": "s"}},
        headers=brain,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    job = await _job(flow, job_id, brain)
    assert job["status"] == "completed" and job["response"]["status"] == 200
    assert job["response"]["body"]["echo"] == {"q": 1} and job["response"]["body"]["content"][0]["text"] == "hi"
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
    running = await flow.client.post(f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"})
    assert running.status_code == 200
    resp = await flow.client.post("/relay", json={"target": "http://model/v1/messages", "deliver": {"label": "chain", "exec": "echo woke"}})
    job_id = resp.json()["job_id"]
    job = await _job(flow, job_id)
    assert job["delivery"]["status"] == "delivered" and job["delivery"]["deferred_id"]
    assert job["delivery"]["computer_id"] is None
    assert not any(cmd == f"echo woke {job_id}" for _, cmd in flow.host.guest.commands)
    assert (await flow.client.delete(f"/computers/{running.json()['computer_id']}")).status_code == 200
    await flow.runtime.tasks.drain(timeout=5.0)
    assert any(cmd == f"echo woke {job_id}" for _, cmd in flow.host.guest.commands)
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert len(chain) == 2


async def test_a_failed_upstream_is_still_delivered_and_says_why(flow: Flow) -> None:
    await _chain(flow, "chain")
    resp = await flow.client.post("/relay", json={"target": "http://down/v1", "deliver": {"label": "chain", "exec": "echo woke"}})
    assert resp.status_code == 202
    job = await _job(flow, resp.json()["job_id"])
    assert job["status"] == "failed" and job["attempts"] == 3 and "ConnectError" in job["error"]
    assert job["response"] is None and job["delivery"]["status"] == "delivered"


async def test_the_guard_and_the_scope_hold_over_http(flow: Flow) -> None:
    for target in ("http://127.0.0.1:8000/health", "http://172.16.254.1/", "http://[::1]/", "ftp://model/"):
        assert (await flow.client.post("/relay", json={"target": target})).status_code == 422, target
    brain = await _key(flow, SCOPE)
    assert (await flow.client.post("/relay", json={"target": "http://other/"}, headers=brain)).status_code == 403
    no_scope = await _key(flow, {"recipes": {"read": True}})
    assert (await flow.client.post("/relay", json={"target": "http://model/"}, headers=no_scope)).status_code == 403
```

- [ ] **Step 3: Run the flow tier**

Run: `uv run pytest tests/flow -q && uv run mypy`
Expected: pass, the existing flow tests included (the conftest change must not disturb the callback receiver: `test_exclusive.py` asserts `flow.received`).

- [ ] **Step 4: Commit**

```bash
uv run ruff format . && git add tests/flow/conftest.py tests/flow/test_relay.py
git commit -m "test(relay): the relay end to end over the fake host (#110)"
```

---

### Task 9: The membrane's state, settings and relay client

**Files:**
- Modify: `embryo/membrane/state.py`, `embryo/membrane/config.py`, `embryo/membrane/mshkn.py`, `tests/support_embryo.py`, `tests/unit/test_embryo_state.py`, `tests/unit/test_embryo_config.py`, `tests/unit/test_embryo_mshkn.py`

**Interfaces:**
- Produces: `membrane.state.Pending` (fields below) with `to_doc`/`from_doc`, `membrane.state.Queued(principal, door, message, payload)`, `Exchange.output: str` and `Exchange.audit: dict`, `State.pending: Pending | None`, `State.queue: list[Queued]`; `Settings.anthropic_base_url: str` (default `https://api.anthropic.com`, from `ANTHROPIC_BASE_URL`, trailing slash stripped); `membrane.mshkn.RelayJob(id, status, error, response_status, response_body)`, `MshknApi.create_relay_job(*, target, headers, body) -> str`, `MshknApi.get_relay_job(job_id) -> RelayJob`; in `tests/support_embryo.py`: `FakeMshkn.relay_answers`, `FakeMshkn.relay_jobs`, `message_of(completion, *, stop_reason=None, usage=None) -> dict`, `in_progress_job()`, `failed_job(error)`, `http_error_job(status, body)`; `StubModel` is deleted.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_embryo_state.py`:

```python
from membrane.state import Pending, Queued


def test_pending_queue_and_the_windows_audit_round_trip(tmp_path: Path) -> None:
    brain = Brain(tmp_path)
    state = State()
    state.pending = Pending(
        turn=3,
        principal="ssh:mike",
        door="ingress",
        message="Who am I?",
        payload='{"msg": "Who am I?", "sig": "s"}',
        messages=[{"role": "user", "content": "[turn 3 | principal ssh:mike | door ingress]\n..."}],
        offered=["remember"],
        job="rj-1",
        hooks=[{"name": "verify_ssh", "principal": "ssh:mike"}],
        calls=[{"name": "remember", "input": {"text": "x"}, "result": {"status": "remembered"}}],
        made=["p-1"],
        model_calls=2,
        usage={"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        forks=2,
        started_at="2026-09-09T10:00:00+00:00",
        memory_written=True,
    )
    state.queue.append(Queued(principal="root", door="api", message="next", payload="next"))
    state.window.append(
        Exchange(turn=2, principal="root", door="api", input="hi", reply="hello", output="hello\n", audit={"stopped": "done"})
    )
    brain.save(state)
    loaded = Brain(tmp_path).state()
    assert loaded == state
    assert loaded.pending is not None and loaded.pending.job == "rj-1"


def test_a_fresh_state_has_no_pending_turn_and_an_empty_queue() -> None:
    state = State()
    assert state.pending is None and state.queue == []
    assert State.from_doc(state.to_doc()) == state
```

Append to `tests/unit/test_embryo_config.py`:

```python
def test_the_model_base_url_defaults_to_anthropic_and_loses_its_trailing_slash(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n")
    assert load_settings(tmp_path).anthropic_base_url == "https://api.anthropic.com"
    (tmp_path / ".env").write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n"
        "ANTHROPIC_BASE_URL=https://8000-comp-1.mshkn.dev/\n"
    )
    assert load_settings(tmp_path).anthropic_base_url == "https://8000-comp-1.mshkn.dev"
```

Append to `tests/unit/test_embryo_mshkn.py`:

```python
async def test_create_relay_job_posts_the_target_headers_and_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"job_id": "rj-1", "status": "queued"})

    job_id = await _client(handler).create_relay_job(
        target="https://api.anthropic.com/v1/messages",
        headers={"x-api-key": "sk", "anthropic-version": "2023-06-01"},
        body={"model": "m", "messages": []},
    )
    assert job_id == "rj-1"
    assert seen[0].method == "POST" and seen[0].url.path == "/relay"
    assert json.loads(seen[0].content) == {
        "target": "https://api.anthropic.com/v1/messages",
        "method": "POST",
        "forward_headers": {"x-api-key": "sk", "anthropic-version": "2023-06-01"},
        "body": {"model": "m", "messages": []},
    }


async def test_get_relay_job_reads_the_status_the_response_and_the_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/relay/")
        if request.url.path.endswith("rj-done"):
            return httpx.Response(200, json={"job_id": "rj-done", "status": "completed", "error": None, "response": {"status": 200, "headers": {}, "body": {"content": []}}, "delivery": None})
        if request.url.path.endswith("rj-wait"):
            return httpx.Response(200, json={"job_id": "rj-wait", "status": "in_progress", "error": None, "response": None, "delivery": None})
        return httpx.Response(200, json={"job_id": "rj-bad", "status": "failed", "error": "HTTP 529", "response": {"status": 529, "headers": {}, "body": "Overloaded"}, "delivery": None})

    api = _client(handler)
    done = await api.get_relay_job("rj-done")
    assert (done.status, done.response_status, done.response_body, done.error) == ("completed", 200, {"content": []}, None)
    wait = await api.get_relay_job("rj-wait")
    assert wait.status == "in_progress" and wait.response_status is None and wait.response_body is None
    bad = await api.get_relay_job("rj-bad")
    assert bad.status == "failed" and bad.error == "HTTP 529" and bad.response_status == 529
```

The `Settings(...)` constructions in `test_embryo_mshkn.py` and `test_embryo_model.py` need no change: the new field has a default.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_embryo_state.py tests/unit/test_embryo_config.py tests/unit/test_embryo_mshkn.py -q`
Expected: ImportError on `Pending`, `AttributeError: anthropic_base_url`, `AttributeError: create_relay_job`.

- [ ] **Step 3: The state**

In `embryo/membrane/state.py` add `from dataclasses import asdict, dataclass, field` and `from membrane.model import zero_usage`, then after `Exchange`:

```python
@dataclass(frozen=True)
class Exchange:
    turn: int
    principal: str
    door: str
    input: str
    reply: str
    # What the door printed after the audit line (the reply with the proposals
    # appended) and the turn's closing audit fields, so `list` can show a turn
    # that closed in a fork nobody was watching (relay design §6).
    output: str = ""
    audit: dict[str, Any] = field(default_factory=dict)


@dataclass
class Pending:
    """The in-flight turn (relay design §6): what `say` began, the messages sent
    so far, the relay job in flight, and the turn's bookkeeping."""

    turn: int
    principal: str
    door: str
    message: str
    payload: str
    messages: list[dict[str, Any]]
    offered: list[str]
    job: str
    hooks: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    made: list[str] = field(default_factory=list)
    model_calls: int = 0
    usage: dict[str, int] = field(default_factory=zero_usage)
    forks: int = 1
    started_at: str = ""
    memory_written: bool = False

    def to_doc(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Pending:
        return cls(**doc)


@dataclass(frozen=True)
class Queued:
    """A message that arrived while a turn was pending; its hooks already ran."""

    principal: str
    door: str
    message: str
    payload: str
```

`State` gains `pending: Pending | None = None` and `queue: list[Queued] = field(default_factory=list)`. In `to_doc`, the window entries add `"output": e.output, "audit": e.audit`, and two keys: `"pending": None if self.pending is None else self.pending.to_doc()`, `"queue": [asdict(q) for q in self.queue]`. In `from_doc`: `pending=None if doc.get("pending") is None else Pending.from_doc(doc["pending"])`, `queue=[Queued(**q) for q in doc.get("queue", [])]`; `Exchange(**e)` already accepts the two new keys.

- [ ] **Step 4: The setting and the client**

`embryo/membrane/config.py`: `DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"`; `Settings` gains `anthropic_base_url: str = DEFAULT_ANTHROPIC_BASE_URL` after `effort`; `load_settings` passes `anthropic_base_url=env.get("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")`.

`embryo/membrane/mshkn.py`: the module docstring becomes "seven calls"; add

```python
@dataclass(frozen=True)
class RelayJob:
    id: str
    status: str
    error: str | None
    response_status: int | None
    response_body: Any
```

to the protocol:

```python
    async def create_relay_job(
        self, *, target: str, headers: dict[str, str], body: dict[str, Any]
    ) -> str: ...

    async def get_relay_job(self, job_id: str) -> RelayJob: ...
```

and to `Mshkn`:

```python
    async def create_relay_job(
        self, *, target: str, headers: dict[str, str], body: dict[str, Any]
    ) -> str:
        response = await self._request(
            "POST",
            "/relay",
            json={"target": target, "method": "POST", "forward_headers": headers, "body": body},
        )
        return str(response.json()["job_id"])

    async def get_relay_job(self, job_id: str) -> RelayJob:
        doc = (await self._request("GET", f"/relay/{job_id}")).json()
        response = doc.get("response") or {}
        return RelayJob(
            id=str(doc["job_id"]),
            status=str(doc["status"]),
            error=doc.get("error"),
            response_status=response.get("status"),
            response_body=response.get("body"),
        )
```

- [ ] **Step 5: The fakes**

In `tests/support_embryo.py`: keep `StubModel` for now (Task 11 deletes it, once the tests that import it are rewritten); import `RelayJob` from `membrane.mshkn` and `zero_usage` from `membrane.model`; add

```python
def message_of(
    completion: Completion, *, stop_reason: str | None = None, usage: dict[str, int] | None = None
) -> dict[str, Any]:
    """A Messages API message carrying the completion, as the relay stores one."""
    return {
        "id": "msg_scripted",
        "type": "message",
        "role": "assistant",
        "model": "scripted",
        "content": completion.content,
        "stop_reason": stop_reason or ("tool_use" if completion.calls else "end_turn"),
        "stop_sequence": None,
        "usage": usage or zero_usage(),
    }


def in_progress_job() -> RelayJob:
    return RelayJob(id="", status="in_progress", error=None, response_status=None, response_body=None)


def failed_job(error: str) -> RelayJob:
    return RelayJob(id="", status="failed", error=error, response_status=None, response_body=None)


def http_error_job(status: int, body: Any) -> RelayJob:
    return RelayJob(id="", status="completed", error=None, response_status=status, response_body=body)
```

and to `FakeMshkn` the fields and methods:

```python
    # The relay as the brain sees it: every posted job, and the canned answers
    # handed out in order, one per job, on its first read. An entry may be a
    # message dict (a completed job) or a RelayJob (in progress, failed, non-2xx).
    relay_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    relay_answers: list[dict[str, Any] | RelayJob] = field(default_factory=list)
    relay_results: dict[str, RelayJob] = field(default_factory=dict)

    async def create_relay_job(
        self, *, target: str, headers: dict[str, str], body: dict[str, Any]
    ) -> str:
        self.calls.append(("create_relay_job", {"target": target, "headers": headers, "body": body}))
        job_id = self._next("rj")
        self.relay_jobs[job_id] = {"target": target, "headers": headers, "body": body}
        return job_id

    async def get_relay_job(self, job_id: str) -> RelayJob:
        self.calls.append(("get_relay_job", {"job_id": job_id}))
        if job_id not in self.relay_jobs:
            raise MshknError(404, "Relay job not found")
        if job_id not in self.relay_results:
            answer: dict[str, Any] | RelayJob = (
                self.relay_answers.pop(0)
                if self.relay_answers
                else message_of(text_completion("(no script)"))
            )
            if isinstance(answer, RelayJob):
                self.relay_results[job_id] = RelayJob(
                    id=job_id,
                    status=answer.status,
                    error=answer.error,
                    response_status=answer.response_status,
                    response_body=answer.response_body,
                )
            else:
                self.relay_results[job_id] = RelayJob(
                    id=job_id, status="completed", error=None, response_status=200, response_body=answer
                )
        return self.relay_results[job_id]
```

`_next("rj")` yields `rj-1`, `rj-2`, ... alongside the other counters.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_embryo_state.py tests/unit/test_embryo_config.py tests/unit/test_embryo_mshkn.py -q && uv run mypy`
Expected: pass. **Tasks 9, 10 and 11 are one coupled unit for the gate** (controller ruling R1): Task 10 deletes `build_model`, which `cli.py` imports until Task 11 rewrites it, so `tests/unit/test_embryo_turn.py` and `tests/unit/test_embryo_commands.py` are red between Task 10's commit and Task 11's. Run only the test files each task names; the full `uv run pytest --cov` gate is required at the end of Task 11 and must be green there.

- [ ] **Step 7: Commit**

```bash
uv run ruff format . && git add embryo/membrane/state.py embryo/membrane/config.py embryo/membrane/mshkn.py tests/support_embryo.py tests/unit/test_embryo_state.py tests/unit/test_embryo_config.py tests/unit/test_embryo_mshkn.py
git commit -m "feat(membrane): the pending turn and the queue in state.json; the relay client (#110)"
```

---

### Task 10: The model module: compose a request, parse a message

**Files:**
- Modify: `embryo/membrane/model.py`, `tests/unit/test_embryo_model.py` (rewritten)

**Interfaces:**
- Produces: `membrane.model.compose_request(*, model_id, system, messages, tools, effort) -> dict`, `request_headers(api_key) -> dict[str, str]`, `parse_message(doc) -> Completion`, `usage_from(doc) -> dict[str, int]`, `ANTHROPIC_VERSION = "2023-06-01"`; keeps `MAX_TOKENS` (64000), `USAGE_KEYS`, `zero_usage`, `add_usage`, `ToolCall`, `Completion`, `Model`. Deletes `AnthropicModel`, `build_model`, `DEADLINE`, `usage_of`.

- [ ] **Step 1: Rewrite the tests**

Replace `tests/unit/test_embryo_model.py` with:

```python
"""The model as the membrane reaches it (relay design §6): a request body for the
Messages API, posted to the relay, and the final message parsed back."""

from __future__ import annotations

from membrane.model import (
    ANTHROPIC_VERSION,
    MAX_TOKENS,
    USAGE_KEYS,
    ToolCall,
    add_usage,
    compose_request,
    parse_message,
    request_headers,
    usage_from,
    zero_usage,
)

TOOLS = [{"name": "page_title", "description": "d", "input_schema": {"type": "object"}}]


def test_compose_request_streams_with_the_full_budget_and_only_what_is_set() -> None:
    body = compose_request(
        model_id="claude-opus-5",
        system="seed",
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS,
        effort=None,
    )
    assert body == {
        "model": "claude-opus-5",
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "system": "seed",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
    }
    assert MAX_TOKENS == 64000
    bare = compose_request(model_id="m", system="s", messages=[], tools=[], effort="medium")
    assert "tools" not in bare and bare["output_config"] == {"effort": "medium"}
    assert "thinking" not in bare


def test_request_headers_carry_the_version_and_the_key_when_there_is_one() -> None:
    assert request_headers("sk-test") == {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "x-api-key": "sk-test",
    }
    assert "x-api-key" not in request_headers(None)


def test_parse_message_maps_blocks_to_text_and_calls_and_keeps_content_whole() -> None:
    out = parse_message(
        {
            "content": [
                {"type": "thinking", "thinking": "", "signature": "s"},
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "tu_1", "name": "page_title", "input": {"url": "https://example.com"}, "parsed_output": None},
                {"type": "server_tool_use", "id": "x", "name": "y"},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 120, "output_tokens": 7, "cache_read_input_tokens": None},
        }
    )
    assert out.text == "Let me look."
    assert out.calls == (ToolCall("tu_1", "page_title", {"url": "https://example.com"}),)
    assert [b["type"] for b in out.content] == ["thinking", "text", "tool_use", "server_tool_use"]
    assert all("parsed_output" not in block for block in out.content)
    assert out.stop_reason == "tool_use"
    assert out.usage == {
        "input_tokens": 120,
        "output_tokens": 7,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def test_parse_message_joins_texts_and_counts_nothing_without_usage() -> None:
    out = parse_message({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]})
    assert out.text == "a\nb" and out.calls == () and out.usage == zero_usage()
    assert out.stop_reason is None
    assert parse_message({}).text == "" and parse_message({}).content == []


def test_usage_helpers() -> None:
    a = dict(zip(USAGE_KEYS, (1, 2, 0, 4), strict=True))
    b = dict(zip(USAGE_KEYS, (10, 20, 30, 40), strict=True))
    assert add_usage(a, b) == {
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 44,
    }
    assert tuple(zero_usage()) == USAGE_KEYS
    assert usage_from(None) == zero_usage() and usage_from({"output_tokens": 3})["output_tokens"] == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_embryo_model.py -q`
Expected: ImportError on `compose_request`.

- [ ] **Step 3: Rewrite the module**

`embryo/membrane/model.py`:

```python
"""The model as the membrane reaches it (spec §7, relay design §6): a request
body composed for the Messages API and handed to the relay, and the final
message parsed back. No SDK: the membrane never calls the model itself."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

# The output budget of one completion. Thinking counts against it (the model
# thinks by default). The relay reads the stream to its end, so nothing but the
# relay's patience bounds a completion (#110).
MAX_TOKENS = 64000
ANTHROPIC_VERSION = "2023-06-01"
# The token counts of one completion, as the Messages API reports them
# (`usage`); summed per turn and printed in the audit line so the cost of a
# run is read from mshkn's exec_log (#101).
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def zero_usage() -> dict[str, int]:
    return dict.fromkeys(USAGE_KEYS, 0)


def add_usage(a: Mapping[str, int], b: Mapping[str, int]) -> dict[str, int]:
    return {key: a.get(key, 0) + b.get(key, 0) for key in USAGE_KEYS}


def usage_from(doc: Mapping[str, Any] | None) -> dict[str, int]:
    """A message's `usage` as a plain dict; a missing object or field counts as 0."""
    usage = doc or {}
    return {key: int(usage.get(key) or 0) for key in USAGE_KEYS}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Completion:
    text: str
    calls: tuple[ToolCall, ...]
    content: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=zero_usage)
    stop_reason: str | None = None


class Model(Protocol):
    """What `membrane serve` and the unit tier answer with: the scripted model."""

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> Completion: ...


def compose_request(
    *,
    model_id: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    effort: str | None,
) -> dict[str, Any]:
    """The `POST /v1/messages` body: streamed, so a long answer produces bytes
    throughout and the relay reassembles it (relay design §12)."""
    body: dict[str, Any] = {
        "model": model_id,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "system": system,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
    if effort is not None:
        body["output_config"] = {"effort": effort}
    return body


def request_headers(api_key: str | None) -> dict[str, str]:
    """The headers the relay forwards; the key rides in them until #92."""
    headers = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def parse_message(doc: Mapping[str, Any]) -> Completion:
    """A stored message as the loop reads it: text, calls, the content echoed back
    without the SDK's `parsed_output` (live run 2026-09-09-run-7), usage, stop reason."""
    texts: list[str] = []
    calls: list[ToolCall] = []
    content: list[dict[str, Any]] = []
    for block in doc.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            calls.append(
                ToolCall(
                    id=str(block["id"]), name=str(block["name"]), input=dict(block.get("input") or {})
                )
            )
        content.append({k: v for k, v in block.items() if k != "parsed_output"})
    return Completion(
        text="\n".join(texts),
        calls=tuple(calls),
        content=content,
        usage=usage_from(doc.get("usage")),
        stop_reason=doc.get("stop_reason"),
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_embryo_model.py tests/unit/test_embryo_scripted.py -q`
Expected: pass (the scripted model imports `Completion` and `ToolCall`, unchanged). `test_embryo_turn.py` and `test_embryo_commands.py` are red until Task 11 (controller ruling R1): `cli.py` still imports the deleted `build_model`. Do not run the full suite here.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add embryo/membrane/model.py tests/unit/test_embryo_model.py
git commit -m "feat(membrane): compose the model request for the relay and parse the message back (#110)"
```

---

### Task 11: The turn: `say` acknowledges, `resume` continues, every command settles

**Files:**
- Modify: `embryo/membrane/loop.py`, `embryo/membrane/turn.py`, `embryo/membrane/commands.py`, `embryo/membrane/cli.py`, `tests/unit/test_embryo_loop.py`, `tests/unit/test_embryo_turn.py`, `tests/unit/test_embryo_commands.py`

**Interfaces:**
- Consumes: Task 9's `Pending`, `Queued`, `RelayJob`, the fakes; Task 10's `compose_request`, `parse_message`.
- Produces: `membrane.loop.run_calls(completion, tools, pending, *, deadline, now, cap=CAP) -> tuple[list[dict], Literal["ok", "cap"]]`, `Tool`, `CAP`, `CAP_REACHED`, `OUT_OF_TOKENS`, `OUT_OF_TIME`; `membrane.turn.Context` (fields: `brain, state, api, settings, deadline, memory=None, open_memory=None, now, sleep`; method `mem()`), `say(ctx, *, payload_b64, door) -> str`, `resume(ctx, job_id) -> str`, `settle(ctx) -> str`, `start_turn`, `finish`, `continue_turn`, `close_turn`, `build_tools`, `post_request`, `MODEL_FAILED`, `TURN_DEADLINE`; `membrane.commands.root(argv, ctx) -> tuple[str, int]`, `list_state(api, state)` with `pending`, `queue`, `window`; `membrane.cli.run(argv, *, brain_dir=None, api=None, memory=None, now=..., sleep=..., err=...) -> tuple[str, int]`, `ARITY` gains `resume`.

- [ ] **Step 1: Rewrite the loop test**

Replace `tests/unit/test_embryo_loop.py` with:

```python
"""The tool calls of one completion run against the handlers (relay design §6):
each gets a result, an unknown tool is an error, a call past the fork's clock
is an "out of time" result, and the cap ends the turn."""

from __future__ import annotations

from typing import Any

from membrane.loop import CAP, OUT_OF_TIME, Tool, run_calls
from membrane.state import Pending

from tests.support_embryo import text_completion, tool_call_completion


async def _echo(inp: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "echo": inp}


ECHO = Tool(
    definition={"name": "echo", "description": "d", "input_schema": {"type": "object"}},
    handler=_echo,
)


def _pending() -> Pending:
    return Pending(turn=1, principal="root", door="api", message="m", payload="m", messages=[], offered=["echo"], job="rj-1")


async def test_each_call_gets_a_tool_result_block_and_is_recorded() -> None:
    pending = _pending()
    completion = tool_call_completion("echo", x=1)
    results, outcome = await run_calls(completion, {"echo": ECHO}, pending, deadline=1e9, now=lambda: 0.0)
    assert outcome == "ok"
    assert results == [{"type": "tool_result", "tool_use_id": "tu_echo", "content": '{"status": "ok", "echo": {"x": 1}}'}]
    assert pending.calls == [{"name": "echo", "input": {"x": 1}, "result": {"status": "ok", "echo": {"x": 1}}}]


async def test_no_calls_is_no_results() -> None:
    results, outcome = await run_calls(text_completion("hi"), {"echo": ECHO}, _pending(), deadline=1e9, now=lambda: 0.0)
    assert results == [] and outcome == "ok"


async def test_unknown_tools_are_errors_not_exceptions() -> None:
    pending = _pending()
    results, _ = await run_calls(tool_call_completion("nope"), {"echo": ECHO}, pending, deadline=1e9, now=lambda: 0.0)
    assert pending.calls[0]["result"] == {"status": "error", "error": "no such tool nope"}
    assert results[0]["tool_use_id"] == "tu_nope"


async def test_a_call_past_the_forks_clock_is_an_out_of_time_result_not_the_turns_end() -> None:
    pending = _pending()
    results, outcome = await run_calls(tool_call_completion("echo", x=1), {"echo": ECHO}, pending, deadline=10.0, now=lambda: 11.0)
    assert outcome == "ok" and pending.calls[0]["result"] == {"status": "error", "error": OUT_OF_TIME}
    assert len(results) == 1


async def test_the_cap_counts_across_forks_and_ends_the_turn() -> None:
    pending = _pending()
    pending.calls = [{"name": "echo", "input": {}, "result": {}} for _ in range(CAP - 1)]
    results, outcome = await run_calls(tool_call_completion("echo", x=1), {"echo": ECHO}, pending, deadline=1e9, now=lambda: 0.0)
    assert outcome == "ok" and len(pending.calls) == CAP and len(results) == 1
    results, outcome = await run_calls(tool_call_completion("echo", x=2), {"echo": ECHO}, pending, deadline=1e9, now=lambda: 0.0)
    assert outcome == "cap" and results == [] and len(pending.calls) == CAP
```

- [ ] **Step 2: Write the loop**

Replace `embryo/membrane/loop.py`:

```python
"""One completion's tool calls, run against the handlers (relay design §6). The
loop of a turn is a chain of forks: the relay posts the answer, `resume` runs
its calls here, posts the next request and exits. Every call gets a result (a
call past the fork's clock gets "out of time", so the model can retry); the cap
counts every call of the turn, across forks, and ends it."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from membrane.model import Completion
    from membrane.state import Pending

type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

CAP = 20
CAP_REACHED = "I reached the tool-call cap for this turn."
OUT_OF_TOKENS = "I ran out of output tokens before finishing this turn."
OUT_OF_TIME = "out of time"


@dataclass(frozen=True)
class Tool:
    definition: dict[str, Any]
    handler: Handler


async def run_calls(
    completion: Completion,
    tools: dict[str, Tool],
    pending: Pending,
    *,
    deadline: float,
    now: Callable[[], float],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], Literal["ok", "cap"]]:
    """The tool_result blocks for the completion's calls, appended to
    `pending.calls` as they run. "cap" when a call would exceed the turn's cap:
    the caller ends the turn, so the results so far are not sent."""
    results: list[dict[str, Any]] = []
    for call in completion.calls:
        if len(pending.calls) >= cap:
            return results, "cap"
        tool = tools.get(call.name)
        if tool is None:
            result: dict[str, Any] = {"status": "error", "error": f"no such tool {call.name}"}
        elif now() >= deadline:
            result = {"status": "error", "error": OUT_OF_TIME}
        else:
            result = await tool.handler(call.input)
        pending.calls.append({"name": call.name, "input": call.input, "result": result})
        results.append(
            {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}
        )
    return results, "ok"
```

Run: `uv run pytest tests/unit/test_embryo_loop.py -q`. Expected: pass.

- [ ] **Step 3: Rewrite the turn test**

Replace `tests/unit/test_embryo_turn.py` with the file below. It keeps the tests of the old file that do not depend on the loop (`decode_payload`, `compose_input`, `history_from`, the hook tests) with their call shape changed to a `Context`, and adds the asynchronous turn's own. For the kept hook tests (`test_the_hook_names_the_principal_and_anonymous_gets_nothing`, `test_hooks_fail_closed_when_not_ready_or_malformed`, `test_anonymous_turn_polls_but_leaves_the_inbox_for_a_later_authenticated_turn`, `test_authenticated_without_propose_rights_gets_remember_only`, `test_propose_tool_reports_a_declaration_error_as_invalid`, `test_a_ready_verb_is_a_tool_and_builds_are_polled_first`): copy each from the old file, build a `Context` with `_ctx(...)`, replace every `say(brain=..., state=..., api=..., model=StubModel([...]), memory=..., payload_b64=..., door=..., deadline=...)` by `_turn(ctx, payload, door, answers=[message_of(...)])` (defined below: a `say` followed by `resume` of the posted job), and read the closing audit and reply from its return. An assertion on `model.calls[0]` becomes one on `ctx.api.relay_jobs["rj-1"]["body"]` (`system`, `tools`, `messages`). Everything else in those tests stands.

```python
"""A say turn is a chain of forks (relay design §6): say posts the request and
acknowledges; resume appends the answer, runs its calls, posts the next request
or closes; every command settles a pending turn first; a say while one is
pending is queued and runs next."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from membrane.config import Settings
from membrane.loop import CAP_REACHED, OUT_OF_TOKENS
from membrane.memory import Provenance
from membrane.model import zero_usage
from membrane.principals import ROOT
from membrane.proposals import approve, propose
from membrane.state import Brain, Exchange, InboxItem, State
from membrane.turn import (
    BAD_PAYLOAD,
    DOOR_CLOSED,
    MODEL_FAILED,
    Context,
    compose_input,
    decode_payload,
    history_from,
    resume,
    say,
    settle,
)

from tests.support_embryo import (
    FakeMshkn,
    ListMemory,
    b64,
    failed_job,
    http_error_job,
    in_progress_job,
    message_of,
    split_output,
    text_completion,
    tool_call_completion,
)
from tests.unit.test_embryo_declarations import VERB
from tests.unit.test_embryo_proposals import CLOSED, HOOK

if TYPE_CHECKING:
    from pathlib import Path

OPEN = {
    "principals": {"ssh:mike": {"invoke": "*", "propose": True}},
    "hooks": ["verify_ssh"],
    "door": "open",
}


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "brain": tmp_path,
        "api_url": "http://api",
        "api_key": "mk-scoped",
        "model": "scripted",
        "model_id": "claude-opus-5",
        "anthropic_api_key": None,
        "openai_api_key": None,
    }
    fields.update(overrides)
    return Settings(**fields)


def _ctx(
    tmp_path: Path,
    *,
    policy: dict[str, Any] = CLOSED,
    api: FakeMshkn | None = None,
    memory: ListMemory | None = None,
    answers: list[Any] | None = None,
    **settings: Any,
) -> Context:
    (tmp_path / "policy.json").write_text(json.dumps(policy))
    (tmp_path / "seed.md").write_text("SEED")
    brain = Brain(tmp_path)
    api = api or FakeMshkn()
    if answers:
        api.relay_answers.extend(answers)
    return Context(
        brain=brain,
        state=brain.state(),
        api=api,
        settings=_settings(tmp_path, **settings),
        deadline=1e9,
        memory=memory or ListMemory(),
        now=lambda: 0.0,
    )


def _ack(out: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A say's output: its start audit line and the acknowledgement."""
    audit, rest = split_output(out)
    return audit, dict(json.loads(rest))


async def _turn(ctx: Context, payload: Any, door: str = "api", *, answers: list[Any] | None = None) -> str:
    """A whole turn in one call: say, then resume until the turn closes. Returns
    the output of the fork that closed it (the closing audit line and the reply)."""
    if answers:
        ctx.api.relay_answers.extend(answers)  # type: ignore[attr-defined]
    out = await say(ctx, payload_b64=b64(payload), door=door)  # type: ignore[arg-type]
    audit, _ = split_output(out)
    if "job" not in audit:
        return out
    while ctx.state.pending is not None:
        out = await resume(ctx, ctx.state.pending.job)
    return out


def test_decode_payload_and_compose_input() -> None:
    assert decode_payload(b64("hi")) == ("hi", "hi")
    assert decode_payload(b64({"msg": "Who am I?", "sig": "s"})) == ("Who am I?", json.dumps({"msg": "Who am I?", "sig": "s"}))
    assert decode_payload("not base64!") is None
    other = json.dumps({"sig": "s"})
    assert decode_payload(b64({"sig": "s"})) == (other, other)
    text = compose_input(turn=3, principal="ssh:mike", door="ingress", inbox=[InboxItem("build", "verb x is ready")], recalled=["mike likes tea"], message="hello")
    assert text.startswith("[turn 3 | principal ssh:mike | door ingress]\n")
    assert "- verb x is ready" in text and "- mike likes tea" in text and text.endswith("message:\nhello")


def test_history_is_the_last_ten_exchanges() -> None:
    window = [Exchange(turn=i, principal="root", door="api", input=f"in{i}", reply=f"out{i}") for i in range(12)]
    history = history_from(window)
    assert len(history) == 20 and history[0]["content"] == "[root via api] in2" and history[-1]["content"] == "out11"


async def test_a_say_posts_the_request_and_acknowledges_without_a_model_call(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, anthropic_api_key="sk-test", model="anthropic", openai_api_key="o", effort="medium")
    out = await say(ctx, payload_b64=b64("Hello. I am the one who hatched you."), door="api")
    audit, ack = _ack(out)
    assert audit["started"] is True and audit["turn"] == 1 and audit["principal"] == "root" and audit["door"] == "api"
    assert audit["offered"] == ["propose", "remember", "try"] and audit["job"] == "rj-1"
    assert ack == {"turn": 1, "job": "rj-1"}
    posted = ctx.api.relay_jobs["rj-1"]  # type: ignore[attr-defined]
    assert posted["target"] == "https://api.anthropic.com/v1/messages"
    assert posted["headers"]["x-api-key"] == "sk-test" and posted["headers"]["anthropic-version"] == "2023-06-01"
    body = posted["body"]
    assert body["system"] == "SEED" and body["stream"] is True and body["output_config"] == {"effort": "medium"}
    assert [t["name"] for t in body["tools"]] == ["remember", "try", "propose"]  # insertion order
    assert body["messages"][-1]["role"] == "user" and "hatched you" in body["messages"][-1]["content"]
    pending = ctx.state.pending
    assert pending is not None and pending.job == "rj-1" and pending.turn == 1 and pending.forks == 1
    assert pending.model_calls == 1 and pending.memory_written is True and pending.started_at
    assert ctx.state.window == [] and ctx.state.turn == 1
    assert not any(name == "get_relay_job" for name, _ in ctx.api.calls)  # type: ignore[attr-defined]


async def test_a_text_answer_closes_the_turn_with_the_full_audit_and_the_reply(tmp_path: Path) -> None:
    memory = ListMemory()
    ctx = _ctx(tmp_path, memory=memory, answers=[message_of(text_completion("I am an embryo."))])
    await say(ctx, payload_b64=b64("Hello. I am the one who hatched you."), door="api")
    out = await resume(ctx, "rj-1")
    audit, reply = split_output(out)
    assert reply == "I am an embryo.\n"
    assert audit["turn"] == 1 and audit["stopped"] == "done" and audit["model_calls"] == 1
    assert audit["usage"] == zero_usage() and audit["forks"] == 1 and audit["job"] == "rj-1"
    assert audit["tools"] == [] and audit["proposals"] == [] and audit["memory_written"] is True
    assert ctx.state.pending is None
    entry = ctx.state.window[-1]
    assert entry.reply == "I am an embryo." and entry.output == "I am an embryo.\n" and entry.audit["stopped"] == "done"
    assert memory.entries[0][1] == Provenance(principal=ROOT, door="api", turn=1)
    assert "hatched" in memory.entries[0][0] and "I am an embryo." in memory.entries[0][0]


async def test_tool_calls_run_and_the_next_request_is_posted_in_a_new_fork(tmp_path: Path) -> None:
    memory = ListMemory()
    proposal = {"kind": "verb", "title": "page_title", "rationale": "r", "verb": VERB}
    ctx = _ctx(tmp_path, memory=memory, answers=[
        message_of(tool_call_completion("remember", text="mike hatched me")),
        message_of(tool_call_completion("propose", **proposal)),
        message_of(text_completion("Proposed p-1.")),
    ])
    await say(ctx, payload_b64=b64("go"), door="api")
    first = await resume(ctx, "rj-1")
    audit, rest = split_output(first)
    assert rest == "" and audit["continued"] is True and audit["job"] == "rj-1" and audit["next_job"] == "rj-2"
    assert audit["calls"] == [{"name": "remember", "status": "remembered"}]
    assert memory.entries[0][0] == "mike hatched me"
    pending = ctx.state.pending
    assert pending is not None and pending.job == "rj-2" and pending.forks == 2 and pending.model_calls == 2
    assert pending.messages[-2]["role"] == "assistant" and pending.messages[-2]["content"][0]["type"] == "tool_use"
    assert pending.messages[-1] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_remember", "content": '{"status": "remembered"}'}]}
    assert ctx.api.relay_jobs["rj-2"]["body"]["messages"] == pending.messages  # type: ignore[attr-defined]
    await resume(ctx, "rj-2")
    out = await resume(ctx, "rj-3")
    audit, reply = split_output(out)
    assert reply.startswith("Proposed p-1.\nproposal p-1\n")
    assert json.loads(reply.split("proposal p-1\n", 1)[1])["verb"]["name"] == "page_title"
    assert [c["name"] for c in audit["tools"]] == ["remember", "propose"]
    assert audit["proposals"][0]["id"] == "p-1" and len(audit["proposals"][0]["sha256"]) == 64
    assert audit["forks"] == 3 and audit["model_calls"] == 3 and ctx.state.proposals["p-1"].status == "pending"
    # the audit sink: the last exec's stdout carries the whole turn's audit line
    assert ctx.state.window[-1].audit["tools"] == audit["tools"]


async def test_a_forged_or_stale_job_id_does_nothing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert await resume(ctx, "rj-77") == "no pending turn for job rj-77\n"
    await say(ctx, payload_b64=b64("go"), door="api")
    assert await resume(ctx, "rj-77") == "no pending turn for job rj-77\n"
    assert ctx.state.pending is not None and ctx.state.pending.job == "rj-1"
    assert not any(name == "get_relay_job" for name, _ in ctx.api.calls)  # type: ignore[attr-defined]


async def test_resume_and_settle_leave_a_job_still_in_progress(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, answers=[in_progress_job(), message_of(text_completion("later"))])
    await say(ctx, payload_b64=b64("go"), door="api")
    assert await settle(ctx) == "" and ctx.state.pending is not None
    # the fake answers every read of a job the same way until its results are cleared
    assert await resume(ctx, "rj-1") == "job rj-1 is in_progress\n"
    ctx.api.relay_results.clear()  # type: ignore[attr-defined]
    audit, reply = split_output(await resume(ctx, "rj-1"))
    assert reply == "later\n" and audit["stopped"] == "done" and ctx.state.pending is None


async def test_a_failed_job_and_a_non_2xx_answer_end_the_turn_with_the_error(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, answers=[failed_job("HTTP 529 after 3 attempts")])
    await say(ctx, payload_b64=b64("go"), door="api")
    audit, reply = split_output(await settle(ctx))
    assert audit["stopped"] == "error" and reply.startswith(MODEL_FAILED) and "529" in reply
    assert ctx.state.pending is None and ctx.state.window[-1].audit["stopped"] == "error"
    ctx.api.relay_answers.append(http_error_job(400, {"error": {"message": "max_tokens too large"}}))  # type: ignore[attr-defined]
    await say(ctx, payload_b64=b64("again"), door="api")
    audit, reply = split_output(await resume(ctx, "rj-2"))
    assert audit["stopped"] == "error" and "HTTP 400" in reply and "max_tokens too large" in reply


async def test_max_tokens_and_the_cap_end_the_turn_honestly(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, answers=[message_of(tool_call_completion("remember", text="never"), stop_reason="max_tokens")])
    memory = ctx.memory
    assert isinstance(memory, ListMemory)
    await say(ctx, payload_b64=b64("go"), door="api")
    audit, reply = split_output(await resume(ctx, "rj-1"))
    assert audit["stopped"] == "max_tokens" and reply.startswith(OUT_OF_TOKENS) and audit["tools"] == []
    assert not any(text == "never" for text, _ in memory.entries), "calls of a truncated answer never run"
    # the cap: 20 calls over as many forks, then the 21st ends the turn
    ctx.api.relay_answers.extend([message_of(tool_call_completion("remember", text=f"fact {i}")) for i in range(21)])  # type: ignore[attr-defined]
    await say(ctx, payload_b64=b64("count"), door="api")
    out = ""
    while ctx.state.pending is not None:
        out = await resume(ctx, ctx.state.pending.job)
    audit, reply = split_output(out)
    assert audit["stopped"] == "cap" and reply.startswith(CAP_REACHED) and len(audit["tools"]) == 20
    assert audit["forks"] == 21


async def test_a_say_while_a_turn_is_pending_is_queued_and_runs_next(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, answers=[message_of(text_completion("first reply")), message_of(text_completion("second reply"))])
    await say(ctx, payload_b64=b64("first"), door="api")
    audit, ack = _ack(await say(ctx, payload_b64=b64("second"), door="api"))
    assert ack == {"queued": 1} and audit["queued"] == 1 and audit["principal"] == "root"
    assert len(ctx.state.queue) == 1 and ctx.state.turn == 1
    out = await resume(ctx, "rj-1")
    lines = out.splitlines()
    assert lines[0].startswith("audit ") and lines[1] == "first reply"
    started = json.loads(lines[2][len("audit "):])
    assert started["started"] is True and started["turn"] == 2 and started["job"] == "rj-2"
    assert json.loads(lines[3]) == {"turn": 2, "job": "rj-2"}
    assert ctx.state.queue == [] and ctx.state.pending is not None and ctx.state.pending.message == "second"
    audit, reply = split_output(await resume(ctx, "rj-2"))
    assert reply == "second reply\n" and [e.reply for e in ctx.state.window] == ["first reply", "second reply"]
    assert ctx.api.relay_jobs["rj-2"]["body"]["messages"][0]["content"] == "[root via api] first"  # type: ignore[attr-defined]


async def test_tools_are_rebuilt_from_the_current_catalog_on_every_fork(tmp_path: Path) -> None:
    from membrane.declarations import parse_verb, render_command
    from membrane.verbs import poll_builds

    api = FakeMshkn()
    ctx = _ctx(tmp_path, api=api)
    await say(ctx, payload_b64=b64("go"), door="api")
    assert ctx.state.pending is not None and "page_title" not in ctx.state.pending.offered
    # root approves a verb while the model thinks, and its build is polled by root's next command
    p = propose(ctx.state, {"kind": "verb", "title": "t", "rationale": "r", "verb": VERB})
    await approve(api, ctx.state, p.id)
    ctx.state.inbox.extend(await poll_builds(api, ctx.state))
    assert ctx.state.catalog["page_title"].status == "ready"
    verb = parse_verb(VERB)
    api.outputs[render_command(verb, {"url": "https://example.com"})] = (0, "Example Domain\n", "")
    api.relay_answers.extend([message_of(tool_call_completion("page_title", url="https://example.com")), message_of(text_completion("Example Domain"))])
    await resume(ctx, "rj-1")
    assert ctx.state.pending is not None and ctx.state.pending.calls[0]["result"]["stdout"] == "Example Domain\n"
    assert [t["name"] for t in api.relay_jobs["rj-2"]["body"]["tools"]] == ["remember", "try", "propose", "page_title"]


async def test_closed_door_and_bad_payload_answer_one_line_and_post_nothing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = await say(ctx, payload_b64=b64("hi"), door="ingress")
    audit, reply = split_output(out)
    assert audit["closed"] is True and audit["principal"] is None and reply == DOOR_CLOSED + "\n"
    out = await say(ctx, payload_b64="not base64!", door="api")
    audit, reply = split_output(out)
    assert audit["error"] == "bad payload" and reply == BAD_PAYLOAD + "\n"
    assert ctx.state.pending is None and ctx.state.turn == 0 and ctx.api.relay_jobs == {}  # type: ignore[attr-defined]


# --- kept from the synchronous turn, call shape changed --------------------------
# (copy the six hook, inbox and tool tests named in the plan here, each built on
# `_ctx(...)` and driven by `_turn(...)`)
```

- [ ] **Step 4: Run the turn test to verify it fails**

Run: `uv run pytest tests/unit/test_embryo_turn.py -q`
Expected: ImportError on `Context`.

- [ ] **Step 5: Write the turn**

Replace `embryo/membrane/turn.py`:

```python
"""A turn (relay design §6). `say` runs the principal, the builds, the input and
the tools as before, then posts the model request to the relay and acknowledges.
`resume <job_id>` is the wake-up: it appends the answer, runs its calls, and
posts the next request or closes the turn. Every command settles a pending turn
first. A `say` while one is pending is queued and runs next."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from membrane.declarations import DeclarationError
from membrane.hooks import principal_for
from membrane.invariants import door_is_open, may_invoke, may_propose
from membrane.loop import CAP_REACHED, OUT_OF_TOKENS, Tool, run_calls
from membrane.memory import Provenance
from membrane.model import add_usage, compose_request, parse_message, request_headers
from membrane.mshkn import MshknError
from membrane.principals import ROOT, is_authenticated, namespace_of
from membrane.proposals import propose
from membrane.state import WINDOW, Exchange, Pending, Queued
from membrane.trials import poll_trials, try_verb
from membrane.verbs import invoke, poll_builds

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from membrane.config import Settings
    from membrane.memory import MemoryStore
    from membrane.mshkn import MshknApi, RelayJob
    from membrane.state import Brain, CatalogEntry, InboxItem, State

Door = Literal["api", "ingress"]
# One fork's clock: bounds the tool runs of that fork, not the model (#110).
TURN_DEADLINE = 240.0
DOOR_CLOSED = "The public door is closed."
BAD_PAYLOAD = "The payload is not base64."
MODEL_FAILED = "The model service failed this turn."

REMEMBER_TOOL = {...}  # unchanged
TRY_TOOL = {...}  # unchanged
PROPOSE_TOOL = {...}  # unchanged


@dataclass
class Context:
    """What every command runs with. Memory opens on first use: a `list` that
    settles nothing never pays for mem0."""

    brain: Brain
    state: State
    api: MshknApi
    settings: Settings
    deadline: float
    memory: MemoryStore | None = None
    open_memory: Callable[[], MemoryStore] | None = None
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def mem(self) -> MemoryStore:
        if self.memory is None:
            assert self.open_memory is not None, "no memory store to open"
            self.memory = self.open_memory()
        return self.memory


def decode_payload(b64: str) -> tuple[str, str] | None: ...  # unchanged
def compose_input(...) -> str: ...  # unchanged
def history_from(window: list[Exchange]) -> list[dict[str, Any]]: ...  # unchanged
def audit_line(**fields: Any) -> str: ...  # unchanged
def _tool_summary(call: dict[str, Any]) -> dict[str, Any]: ...  # unchanged


def _excerpt(body: object, limit: int = 300) -> str:
    text = body if isinstance(body, str) else json.dumps(body)
    return text[:limit]


def build_tools(ctx: Context, pending: Pending) -> dict[str, Tool]:
    """The tools of this fork, from the current policy and catalog (§6 step 4):
    a verb approved while the model thought is usable on the next request."""
    state, policy, principal = ctx.state, ctx.state.policy, pending.principal
    provenance = Provenance(principal=principal, door=pending.door, turn=pending.turn)
    tools: dict[str, Tool] = {}

    async def remember(inp: dict[str, Any]) -> dict[str, Any]:
        ctx.mem().add(str(inp.get("text", "")), provenance)
        return {"status": "remembered"}

    async def do_try(inp: dict[str, Any]) -> dict[str, Any]:
        params = inp.get("params") or {}
        return await try_verb(
            ctx.api, state, inp.get("verb"), dict(params), until=ctx.deadline, now=ctx.now, sleep=ctx.sleep
        )

    async def do_propose(inp: dict[str, Any]) -> dict[str, Any]:
        try:
            proposal = propose(state, inp)
        except DeclarationError as exc:
            return {"status": "invalid", "error": str(exc)}
        pending.made.append(proposal.id)
        return proposal.to_doc()

    if is_authenticated(principal):
        tools["remember"] = Tool(REMEMBER_TOOL, remember)
        if may_propose(principal, policy):
            tools["try"] = Tool(TRY_TOOL, do_try)
            tools["propose"] = Tool(PROPOSE_TOOL, do_propose)
    for name, entry in state.catalog.items():
        if entry.status != "ready" or entry.recipe_id is None or not may_invoke(principal, entry.verb, policy):
            continue

        def make(verb_entry: CatalogEntry) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
            recipe_id = verb_entry.recipe_id
            assert recipe_id is not None

            async def handler(inp: dict[str, Any]) -> dict[str, Any]:
                return await invoke(ctx.api, verb_entry.verb, inp, recipe_id=recipe_id, remaining=ctx.deadline - ctx.now())

            return handler

        tools[name] = Tool(entry.verb.tool(), make(entry))
    return tools


async def post_request(ctx: Context, pending: Pending, tools: dict[str, Tool]) -> None:
    """Compose the request and hand it to the relay; the job id is the turn's pointer."""
    settings = ctx.settings
    system = ctx.brain.seed()
    if ctx.state.self_description:
        system = f"{system}\n\n{ctx.state.self_description}"
    body = compose_request(
        model_id=settings.model_id,
        system=system,
        messages=pending.messages,
        tools=[t.definition for t in tools.values()],
        effort=settings.effort,
    )
    pending.job = await ctx.api.create_relay_job(
        target=f"{settings.anthropic_base_url}/v1/messages",
        headers=request_headers(settings.anthropic_api_key),
        body=body,
    )
    pending.model_calls += 1


async def start_turn(
    ctx: Context, *, principal: str, door: str, message: str, payload_text: str, hook_runs: list[dict[str, Any]]
) -> str:
    """Steps 2 to 4 of §6, then the first request. Returns the start audit line
    and the acknowledgement."""
    state = ctx.state
    if namespace_of(principal) is not None:
        state.principals.add(principal)
    state.turn += 1
    turn = state.turn
    state.inbox.extend(await poll_builds(ctx.api, state))
    state.inbox.extend(await poll_trials(ctx.api, state, remaining=ctx.deadline - ctx.now()))
    # Ruling P2 stands: only an authenticated principal drains the inbox.
    if is_authenticated(principal):
        inbox, state.inbox = state.inbox, []
    else:
        inbox = []
    recalled = ctx.mem().recall(message, principal=principal) if is_authenticated(principal) else []
    user = compose_input(turn=turn, principal=principal, door=door, inbox=inbox, recalled=recalled, message=message)
    pending = Pending(
        turn=turn,
        principal=principal,
        door=door,
        message=message,
        payload=payload_text,
        messages=[*history_from(state.window), {"role": "user", "content": user}],
        offered=[],
        job="",
        hooks=hook_runs,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        memory_written=is_authenticated(principal),
    )
    tools = build_tools(ctx, pending)
    pending.offered = sorted(tools)
    state.pending = pending
    await post_request(ctx, pending, tools)
    ack = json.dumps({"turn": turn, "job": pending.job})
    return (
        audit_line(turn=turn, principal=principal, door=door, hooks=hook_runs, offered=pending.offered, job=pending.job, started=True)
        + "\n" + ack + "\n"
    )


async def say(ctx: Context, *, payload_b64: str, door: Door) -> str:
    decoded = decode_payload(payload_b64)
    if decoded is None:
        return audit_line(door=door, principal=None, error="bad payload") + "\n" + BAD_PAYLOAD + "\n"
    message, payload_text = decoded
    hook_runs: list[dict[str, Any]] = []
    if door == "api":
        principal = ROOT
    else:
        if not door_is_open(ctx.state.policy):
            return audit_line(door=door, principal=None, closed=True) + "\n" + DOOR_CLOSED + "\n"
        principal = await principal_for(ctx.api, ctx.state, ctx.state.policy, payload_text, remaining=ctx.deadline - ctx.now(), runs=hook_runs)
    if ctx.state.pending is not None:
        ctx.state.queue.append(Queued(principal=principal, door=door, message=message, payload=payload_text))
        position = len(ctx.state.queue)
        return audit_line(door=door, principal=principal, hooks=hook_runs, queued=position) + "\n" + json.dumps({"queued": position}) + "\n"
    return await start_turn(ctx, principal=principal, door=door, message=message, payload_text=payload_text, hook_runs=hook_runs)


async def resume(ctx: Context, job_id: str) -> str:
    """The wake-up. A forged or stale id does nothing (§10.2)."""
    pending = ctx.state.pending
    if pending is None or pending.job != job_id:
        return f"no pending turn for job {job_id}\n"
    try:
        job = await ctx.api.get_relay_job(job_id)
    except MshknError as exc:
        if exc.status == 404:
            return await close_turn(ctx, text=f"{MODEL_FAILED} job {job_id} is gone", stopped="error")
        raise
    return await finish(ctx, job) or f"job {job_id} is {job.status}\n"


async def settle(ctx: Context) -> str:
    """What every command does first: a pending job that has settled is finished
    now, whether or not its wake-up arrived. Nothing to do is an empty string."""
    pending = ctx.state.pending
    if pending is None:
        return ""
    try:
        job = await ctx.api.get_relay_job(pending.job)
    except MshknError as exc:
        if exc.status == 404:
            return await close_turn(ctx, text=f"{MODEL_FAILED} job {pending.job} is gone", stopped="error")
        return ""
    return await finish(ctx, job)


async def finish(ctx: Context, job: RelayJob) -> str:
    if job.status in ("queued", "in_progress"):
        return ""
    if job.status != "completed":
        return await close_turn(ctx, text=f"{MODEL_FAILED} {job.error}", stopped="error")
    if job.response_status is None or not 200 <= job.response_status < 300:
        return await close_turn(ctx, text=f"{MODEL_FAILED} HTTP {job.response_status}: {_excerpt(job.response_body)}", stopped="error")
    if not isinstance(job.response_body, dict):
        return await close_turn(ctx, text=f"{MODEL_FAILED} the response is not a message", stopped="error")
    return await continue_turn(ctx, job.response_body)


async def continue_turn(ctx: Context, message: dict[str, Any]) -> str:
    pending = ctx.state.pending
    assert pending is not None
    completion = parse_message(message)
    pending.usage = add_usage(pending.usage, completion.usage)
    if completion.stop_reason == "max_tokens":
        # The budget ran out mid-response: whatever calls arrived are not run.
        return await close_turn(ctx, text=f"{OUT_OF_TOKENS} {completion.text}".strip(), stopped="max_tokens")
    if not completion.calls:
        return await close_turn(ctx, text=completion.text, stopped="done")
    pending.messages.append({"role": "assistant", "content": completion.content})
    tools = build_tools(ctx, pending)
    before = len(pending.calls)
    results, outcome = await run_calls(completion, tools, pending, deadline=ctx.deadline, now=ctx.now)
    if outcome == "cap":
        return await close_turn(ctx, text=f"{CAP_REACHED} {completion.text}".strip(), stopped="cap")
    pending.messages.append({"role": "user", "content": results})
    previous = pending.job
    pending.forks += 1
    await post_request(ctx, pending, tools)
    summaries = [_tool_summary(c) for c in pending.calls[before:]]
    return audit_line(turn=pending.turn, job=previous, calls=summaries, next_job=pending.job, continued=True) + "\n"


async def close_turn(ctx: Context, *, text: str, stopped: str) -> str:
    """Step 6 of §6: memory, the window, the audit line, the reply and the
    proposals. Then the queue's head starts the next turn in this same fork."""
    state = ctx.state
    pending = state.pending
    assert pending is not None
    proposals_made = [
        {"id": pid, "sha256": hashlib.sha256(json.dumps(state.proposals[pid].to_doc(), sort_keys=True).encode()).hexdigest()}
        for pid in pending.made
    ]
    if pending.memory_written:
        ctx.mem().add(f"{pending.principal}: {pending.message}\nembryo: {text}", Provenance(principal=pending.principal, door=pending.door, turn=pending.turn))
    audit: dict[str, Any] = {
        "turn": pending.turn,
        "principal": pending.principal,
        "door": pending.door,
        "hooks": pending.hooks,
        "offered": pending.offered,
        "tools": [_tool_summary(c) for c in pending.calls],
        "proposals": proposals_made,
        "memory_written": pending.memory_written,
        "stopped": stopped,
        "model_calls": pending.model_calls,
        "usage": pending.usage,
        "forks": pending.forks,
        "started_at": pending.started_at,
        "job": pending.job,
    }
    lines = [text]
    for pid in pending.made:
        lines.append(f"proposal {pid}")
        lines.append(json.dumps(state.proposals[pid].to_doc(), indent=1))
    output = "\n".join(lines) + "\n"
    state.window.append(Exchange(turn=pending.turn, principal=pending.principal, door=pending.door, input=pending.message, reply=text, output=output, audit=audit))
    del state.window[:-WINDOW]
    state.pending = None
    result = audit_line(**audit) + "\n" + output
    if state.queue:
        head = state.queue.pop(0)
        result += await start_turn(ctx, principal=head.principal, door=head.door, message=head.message, payload_text=head.payload, hook_runs=[])
    return result
```

Where a body says `# unchanged`, copy it from the current file verbatim.

- [ ] **Step 6: Commands and the CLI**

`embryo/membrane/commands.py`: `root(argv, ctx)` takes a `Context` instead of the eight keywords; `say` becomes `await say(ctx, payload_b64=args[0], door="api")`; the `poll_builds`/`poll_trials` calls use `ctx.api`, `ctx.state`, `ctx.deadline - ctx.now()`; `approve(ctx.api, ctx.state, ...)`. `USAGE` gains a line `"       membrane resume <job_id>\n"` and `"       membrane serve [--port N]\n"`. `list_state` adds, in the listing dict:

```python
        "pending": None
        if state.pending is None
        else {
            "turn": state.pending.turn,
            "principal": state.pending.principal,
            "door": state.pending.door,
            "job": state.pending.job,
            "started_at": state.pending.started_at,
            "forks": state.pending.forks,
            "model_calls": state.pending.model_calls,
            "usage": state.pending.usage,
        },
        "queue": [{"principal": q.principal, "door": q.door, "message": q.message} for q in state.queue],
        "window": [
            {"turn": e.turn, "principal": e.principal, "door": e.door, "input": e.input, "reply": e.reply, "output": e.output, "audit": e.audit}
            for e in state.window
        ],
```

`embryo/membrane/cli.py`:

```python
"""The console script. `membrane say <b64>` is the public door; `membrane root
<command>` the authenticated one; `membrane resume <job_id>` the relay's
wake-up; `membrane serve` the scripted model's server. One command per process;
state is loaded from /brain and saved before exit. Every command but resume
settles a pending turn first; what that prints goes to stderr, so a command's
stdout is its own."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING

from membrane.commands import USAGE as USAGE
from membrane.commands import root
from membrane.config import load_settings
from membrane.state import Brain
from membrane.turn import TURN_DEADLINE, Context, resume, say, settle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from membrane.memory import MemoryStore
    from membrane.mshkn import MshknApi

ARITY = {"say": 1, "list": 0, "approve": 1, "reject": 2, "disable": 1, "revert": 1}


def _valid(argv: list[str]) -> bool:
    if len(argv) == 2 and argv[0] in ("say", "resume"):
        return True
    if len(argv) >= 2 and argv[0] == "root" and argv[1] in ARITY:
        return len(argv) == 2 + ARITY[argv[1]]
    return False


def _stderr(text: str) -> None:
    print(text, end="", file=sys.stderr)


async def run(
    argv: list[str],
    *,
    brain_dir: Path | None = None,
    api: MshknApi | None = None,
    memory: MemoryStore | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    err: Callable[[str], None] = _stderr,
) -> tuple[str, int]:
    if not _valid(argv):
        return USAGE, 2
    settings = load_settings(brain_dir)
    brain = Brain(settings.brain)
    state = brain.state()
    owned_api = api is None
    if api is None:
        from membrane.mshkn import Mshkn

        api = Mshkn.connect(settings)

    def open_memory() -> MemoryStore:
        from membrane.memory import Mem0Store

        return Mem0Store.open(settings, brain.memory_dir)

    ctx = Context(
        brain=brain,
        state=state,
        api=api,
        settings=settings,
        deadline=now() + TURN_DEADLINE,
        memory=memory,
        open_memory=open_memory,
        now=now,
        sleep=sleep,
    )
    try:
        if argv[0] != "resume":
            notes = await settle(ctx)
            if notes:
                err(notes)
        if argv[0] == "say":
            out, code = await say(ctx, payload_b64=argv[1], door="ingress"), 0
        elif argv[0] == "resume":
            out, code = await resume(ctx, argv[1]), 0
        else:
            out, code = await root(argv[1:], ctx)
        # P14: state is durable only when the command actually succeeded; a
        # crashed turn leaves state.json exactly as it was. Never move this into `finally`.
        brain.save(state)
        return out, code
    finally:
        if memory is None and ctx.memory is not None:
            ctx.memory.close()
        if owned_api:
            from membrane.mshkn import Mshkn

            if isinstance(api, Mshkn):
                await api.aclose()


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["serve"]:
        from membrane.serve import main as serve_main

        serve_main(argv[1:])
        return
    out, code = asyncio.run(run(argv))
    print(out, end="")
    sys.exit(code)
```

`tests/support_embryo.py`: delete `StubModel` now — nothing imports it after this task (controller ruling R1).

`tests/unit/test_embryo_commands.py`: `_run` drops the `model` parameter (`run(argv, brain_dir=brain_dir, api=api, memory=ListMemory())`), `test_usage` adds `["resume"]` and `["serve", "x"]` to the invalid list (`serve` is not `run`'s to handle), and `test_public_say_and_root_say_differ_by_door` becomes:

```python
async def test_public_say_and_root_say_differ_by_door(brain_dir: Path) -> None:
    api = FakeMshkn(relay_answers=[message_of(text_completion("hello"))])
    out, code = await _run(brain_dir, ["say", b64("hi")], api)
    assert code == 0 and "The public door is closed." in out
    out, code = await _run(brain_dir, ["root", "say", b64("hi")], api)
    assert code == 0 and json.loads(out.splitlines()[1]) == {"turn": 1, "job": "rj-1"}
    assert '"principal": "root"' in out.splitlines()[0]
    assert Brain(brain_dir).state().pending is not None  # state was saved
    out, code = await _run(brain_dir, ["resume", "rj-1"], api)
    assert code == 0 and out.endswith("hello\n")
    assert Brain(brain_dir).state().pending is None and Brain(brain_dir).state().turn == 1


async def test_every_root_command_settles_a_pending_turn_to_stderr_first(brain_dir: Path) -> None:
    api = FakeMshkn(relay_answers=[message_of(text_completion("settled"))])
    await _run(brain_dir, ["root", "say", b64("hi")], api)
    notes: list[str] = []
    out, code = await run(["root", "list"], brain_dir=brain_dir, api=api, memory=ListMemory(), err=notes.append)
    assert code == 0
    listing = json.loads(out)
    assert listing["pending"] is None and listing["window"][0]["reply"] == "settled"
    assert listing["window"][0]["audit"]["stopped"] == "done" and listing["queue"] == []
    assert len(notes) == 1 and notes[0].startswith("audit ") and notes[0].endswith("settled\n")
```

In `test_run_builds_and_closes_the_real_clients`, the mock transport answers `POST /relay` with `httpx.Response(202, json={"job_id": "rj-1", "status": "queued"})` and everything else with `200 {}`; run `["root", "say", b64("hi")]` instead of the public say (the closed door opens no memory now) and assert `closed == ["memory", "api"]`; add a second call `run(["say", b64("hi")], ...)` asserting `closed == ["memory", "api", "api"]` (a closed door opens no memory). `test_main_prints_and_exits` stands. `test_list_state_reports_chain_heads` and the others that call `list_state(api, state)` stand.

- [ ] **Step 7: Run the membrane's unit tests**

Run: `uv run pytest tests/unit/test_embryo_*.py -q && uv run mypy`
Expected: every membrane test passes except `tests/unit/test_embryo_measure.py`, which Task 14 rewrites. R1's coupled unit ends here: `uv run ruff check . && uv run ruff format --check . && uv run mypy` must be clean, and `uv run pytest -q --deselect tests/unit/test_embryo_measure.py` must be green.

- [ ] **Step 8: Commit**

```bash
uv run ruff format . && git add embryo/membrane tests/unit/test_embryo_loop.py tests/unit/test_embryo_turn.py tests/unit/test_embryo_commands.py
git commit -m "feat(membrane): the asynchronous turn: say acknowledges, resume continues, every command settles (#110)"
```

---

### Task 12: `membrane serve`: the scripted model on the wire

**Files:**
- Create: `embryo/membrane/serve.py`, `tests/unit/test_embryo_serve.py`
- Modify: `tests/support_embryo.py` (an ASGI wrapper for the flow tier)

**Interfaces:**
- Produces: `membrane.serve.answer(model, body) -> dict` (a Messages API message), `membrane.serve.serve(model, port) -> None` (blocks), `membrane.serve.main(argv)`; `tests.support_embryo.scripted_asgi(model) -> ASGI app` answering `POST /v1/messages`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_embryo_serve.py`:

```python
"""`membrane serve` answers the Messages API wire format from the scripted model
(relay design §7), so the flow and live tiers drive the real relay."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

import pytest
from membrane.liturgy import LITURGY
from membrane.model import zero_usage
from membrane.scripted import ScriptedModel
from membrane.serve import answer, build_server, main

if TYPE_CHECKING:
    from pathlib import Path


def _request(system: str, message: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "model": "scripted",
        "max_tokens": 64000,
        "stream": True,
        "system": system,
        "messages": [{"role": "user", "content": f"[turn 1 | principal root | door api]\ninbox:\n\nrecall:\n\nmessage:\n{message}"}],
        "tools": tools or [],
    }


def test_answer_is_a_message_with_end_turn_for_text_and_tool_use_for_calls() -> None:
    model = ScriptedModel()
    text = answer(model, _request("seed", LITURGY[1]))
    assert text["type"] == "message" and text["role"] == "assistant" and text["model"] == "scripted"
    assert text["content"][0]["type"] == "text" and "embryo" in text["content"][0]["text"]
    assert text["stop_reason"] == "end_turn" and text["usage"] == zero_usage()
    tools = [{"name": n, "description": "d", "input_schema": {"type": "object"}} for n in ("remember", "try", "propose")]
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIServeTestKeyServeTestKeyServeTestKeyServeT"
    calls = answer(model, _request("seed", LITURGY[2].format(key=key), tools))
    assert calls["stop_reason"] == "tool_use"
    assert [b["type"] for b in calls["content"]] == ["tool_use", "tool_use", "tool_use"]
    assert [b["name"] for b in calls["content"]] == ["try", "propose", "propose"]


def test_the_server_answers_post_v1_messages_and_nothing_else() -> None:
    server = build_server(ScriptedModel(), 0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/messages",
            data=json.dumps(_request("seed", LITURGY[1])).encode(),
            headers={"content-type": "application/json", "x-api-key": "ignored"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200 and resp.headers["content-type"] == "application/json"
            body = json.loads(resp.read())
        assert body["stop_reason"] == "end_turn" and "embryo" in body["content"][0]["text"]
        bad = urllib.request.Request(f"http://127.0.0.1:{port}/other", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad)
        assert exc.value.code == 404
        junk = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=b"{not json", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(junk)
        assert exc.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_main_refuses_to_serve_a_real_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=anthropic\nANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n")
    monkeypatch.setenv("MEMBRANE_BRAIN", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["--port", "0"])
    assert exc.value.code == 2


def test_main_serves_the_scripted_model_until_shut_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import membrane.serve as serve_module

    (tmp_path / ".env").write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n")
    monkeypatch.setenv("MEMBRANE_BRAIN", str(tmp_path))
    served: list[tuple[Any, int]] = []

    def fake_serve(model: Any, port: int) -> None:
        served.append((model, port))

    monkeypatch.setattr(serve_module, "serve", fake_serve)
    main(["--port", "8123"])
    assert len(served) == 1 and isinstance(served[0][0], ScriptedModel) and served[0][1] == 8123
```

Append to `tests/unit/test_embryo_commands.py` (the CLI's serve branch):

```python
def test_main_hands_serve_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import membrane.cli as cli
    import membrane.serve as serve_module

    served: list[list[str]] = []
    monkeypatch.setattr(serve_module, "main", served.append)
    monkeypatch.setattr("sys.argv", ["membrane", "serve", "--port", "8001"])
    cli.main()
    assert served == [["--port", "8001"]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_embryo_serve.py -q`
Expected: `ModuleNotFoundError: membrane.serve`.

- [ ] **Step 3: Write the server**

`embryo/membrane/serve.py`:

```python
"""`membrane serve`: the scripted model behind `POST /v1/messages` on the
Messages API wire format (relay design §7), so the deterministic tiers drive the
real relay. Standard library only; `stream` is ignored and a plain JSON message
is answered, which the relay stores verbatim. Refuses to serve a real model."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from membrane.config import load_settings
from membrane.model import Model, zero_usage
from membrane.scripted import ScriptedModel

DEFAULT_PORT = 8000
_counter = 0


async def answer_async(model: Model, body: dict[str, Any]) -> dict[str, Any]:
    """The message the scripted model gives for a request body. The async half, so
    an ASGI caller inside a running loop can await it (the flow tier does)."""
    global _counter
    _counter += 1
    completion = await model.complete(
        system=str(body.get("system", "")),
        messages=list(body.get("messages") or []),
        tools=list(body.get("tools") or []),
    )
    return {
        "id": f"msg_scripted_{_counter}",
        "type": "message",
        "role": "assistant",
        "model": str(body.get("model", "scripted")),
        "content": completion.content,
        "stop_reason": "tool_use" if completion.calls else "end_turn",
        "stop_sequence": None,
        "usage": zero_usage(),
    }


def answer(model: Model, body: dict[str, Any]) -> dict[str, Any]:
    """`answer_async` for the blocking server, which has no loop of its own."""
    return asyncio.run(answer_async(model, body))


class Handler(BaseHTTPRequestHandler):
    """The server object carries the model as `server.model`."""

    def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
        if self.path != "/v1/messages":
            self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}})
            return
        model: Model = self.server.model  # type: ignore[attr-defined]
        self._json(200, answer(model, body))

    def _json(self, status: int, doc: dict[str, Any]) -> None:
        data = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's name
        sys.stderr.write("serve: " + format % args + "\n")


def build_server(model: Model, port: int, host: str = "0.0.0.0") -> HTTPServer:  # noqa: S104 — the VM's own network
    server = HTTPServer((host, port), Handler)
    server.model = model  # type: ignore[attr-defined]
    return server


def serve(model: Model, port: int) -> None:
    sys.stderr.write(f"membrane serve: scripted model on port {port}\n")
    build_server(model, port).serve_forever()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="membrane serve")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    settings = load_settings()
    if settings.model != "scripted":
        sys.stderr.write("membrane serve: only MEMBRANE_MODEL=scripted can be served\n")
        sys.exit(2)
    serve(ScriptedModel(), args.port)
```

Append to `tests/support_embryo.py`:

```python
def scripted_asgi(model: Model) -> Callable[..., Awaitable[None]]:
    """`membrane serve` as an ASGI app, for the flow tier's in-process relay target."""
    from membrane.serve import answer_async

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        if scope["method"] != "POST" or scope["path"] != "/v1/messages":
            status, doc = 404, {"type": "error", "error": {"type": "not_found_error", "message": scope["path"]}}
        else:
            status, doc = 200, await answer_async(model, json.loads(body or b"{}"))
        data = json.dumps(doc).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
        await send({"type": "http.response.body", "body": data})

    return app
```

with `from collections.abc import Awaitable, Callable` and `from membrane.model import Model` imported. The wrapper imports and awaits `answer_async` (not `answer`, whose `asyncio.run` cannot run inside a running loop).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_embryo_serve.py -q && uv run mypy`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add embryo/membrane/serve.py tests/unit/test_embryo_serve.py tests/support_embryo.py
git commit -m "feat(membrane): serve the scripted model on the Messages API wire format (#110)"
```

---

### Task 13: The liturgy through the real relay (flow tier)

**Files:**
- Modify: `tests/flow/test_embryo_liturgy.py`

**Interfaces:**
- Consumes: Task 8's `Flow.targets`, Task 12's `scripted_asgi`, Task 11's `run`.
- Produces: the `Embryo` helper whose doors wait for a turn through `list`, and two new tests.

- [ ] **Step 1: Rewrite the `Embryo` helper and its fixture**

In `tests/flow/test_embryo_liturgy.py`, the brain's key scope becomes:

```python
BRAIN_SCOPES = {
    "recipes": {"create": True, "read": True},
    "computers": {"create_from": "*"},
    "labels": ["verb/"],
    "relay": {"targets": ["http://model/"], "deliver": {"label": "brain", "exec": "membrane resume"}},
}
```

The fixture mounts the scripted server and gives the chain a head to wake, before yielding:

```python
    from httpx import ASGITransport
    from membrane.scripted import ScriptedModel
    from tests.support_embryo import scripted_asgi

    flow.targets["model"] = ASGITransport(app=scripted_asgi(ScriptedModel()))
    # the wake-up needs a `brain` head: on the fake host a bare computer, checkpointed and gone
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    base = (await flow.client.post("/computers", json={})).json()["computer_id"]
    await flow.client.post(f"/computers/{base}/checkpoint", json={"label": "brain"})
    await flow.client.delete(f"/computers/{base}")
```

and the `.env` gains `ANTHROPIC_BASE_URL=http://model\n`. The `Embryo` class:

```python
class Embryo:
    """The two doors, in process. A say acknowledges; the relay's wake-up forks
    `brain` on the fake host, which runs nothing, so the helper plays the
    wake-up itself: it drains the relay's task and settles through `list`."""

    def __init__(self, flow: Flow, brain: Path, api: Mshkn) -> None:
        self.flow, self.brain, self.api = flow, brain, api
        self.notes: list[str] = []

    async def _run(self, argv: list[str]) -> str:
        out, code = await run(argv, brain_dir=self.brain, api=self.api, sleep=_fast_sleep, err=self.notes.append)
        assert code == 0, out
        await self.flow.runtime.tasks.drain(timeout=5.0)
        return out

    async def listing(self) -> dict[str, Any]:
        return dict(json.loads(await self._run(["root", "list"])))

    async def _await_turn(self, turn: int) -> tuple[dict[str, Any], str]:
        """`list` until the turn is in the window: each `list` settles a job that
        has answered, which may post the next; the relay's task runs in between."""
        for _ in range(60):
            listing = await self.listing()
            for entry in listing["window"]:
                if entry["turn"] == turn:
                    return dict(entry["audit"]), str(entry["output"])
            assert listing["pending"] is not None, f"turn {turn} neither pending nor in the window"
        raise AssertionError(f"turn {turn} never closed")

    async def _door(self, argv: list[str]) -> tuple[dict[str, Any], str]:
        audit, rest = split_output(await self._run(argv))
        if "job" not in audit:
            return audit, rest  # a closed door or a bad payload answers at once
        return await self._await_turn(int(audit["turn"]))

    async def root_say(self, text: str) -> tuple[dict[str, Any], str]:
        return await self._door(["root", "say", b64(text)])

    async def public_say(self, payload: Any) -> tuple[dict[str, Any], str]:
        return await self._door(["say", b64(payload)])

    async def root(self, *argv: str) -> str:
        return await self._run(["root", *argv])

    # script_output and memory_texts stand as they are
```

- [ ] **Step 2: Adjust `test_the_liturgy`**

The assertions stand, with these changes: the audit read after each door call is now the closing audit (it has the same fields as before plus `forks`, `started_at`, `job`); where the old test asserted on the reply of an anonymous knock (`"will not act or remember" in reply`), it still holds because the window's `output` is the reply. Add, after turn 8's assertions, a check that the turn was a chain of forks on the real chain:

```python
    # the turn was a chain of forks: the relay woke `brain` once per model call, and
    # every wake-up ran `membrane resume <job>` on the chain's head (relay design §5)
    assert audit["forks"] >= 1 and audit["job"].startswith("rj-")
    wake_ups = [cmd for _, cmd in flow.host.guest.commands if cmd.startswith("membrane resume ")]
    assert f"membrane resume {audit['job']}" in wake_ups
    job = await flow.client.get(f"/relay/{audit['job']}")
    assert job.status_code == 200 and job.json()["delivery"]["status"] == "delivered"
    assert job.json()["response"]["body"]["stop_reason"] == "end_turn"
```

- [ ] **Step 3: Add the queue test**

```python
async def test_a_say_while_a_turn_is_pending_is_queued_and_runs_next(embryo: Embryo, flow: Flow) -> None:
    first = split_output(await embryo._run(["root", "say", b64(LITURGY[1])]))[0]
    assert first["started"] is True and first["turn"] == 1
    queued = split_output(await embryo._run(["root", "say", b64("And what is your public door?")]))[0]
    assert queued["queued"] == 1
    assert len(Brain(embryo.brain).state().queue) == 1
    audit1, reply1 = await embryo._await_turn(1)
    audit2, reply2 = await embryo._await_turn(2)
    assert "embryo" in reply1 and audit1["stopped"] == "done"
    assert audit2["stopped"] == "done" and audit2["principal"] == "root"
    window = (await embryo.listing())["window"]
    assert [e["turn"] for e in window] == [1, 2] and (await embryo.listing())["queue"] == []
    assert not embryo.notes or all(n.startswith("audit ") for n in embryo.notes)
```

Import `Brain` from `membrane.state` for the queue check.

- [ ] **Step 4: Run the flow tier**

Run: `uv run pytest tests/flow/test_embryo_liturgy.py -q -x`
Expected: pass. If `test_the_liturgy` stalls in `_await_turn`, print `listing["pending"]` and the relay job (`GET /relay/{job}`) to see which side is waiting: a `failed` job with `ConnectError` means the `model` target is not mounted; a job stuck `queued` means the relay's task was not drained.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add tests/flow/test_embryo_liturgy.py
git commit -m "test(embryo): the liturgy end to end through the real relay (#110)"
```

---

### Task 14: Hatching with the relay scope and the scripted server; the measure waits on `list`

**Files:**
- Modify: `embryo/hatch.sh`, `embryo/membrane/measure.py`, `tests/unit/test_embryo_measure.py`

**Interfaces:**
- Produces: `hatch.sh` output gains `server_id` (null unless scripted); `Hatched.server_id: str | None`; `Doors.root_say`/`public_say` return the closing audit and the output after waiting on `list`; `Doors.await_turn(turn)`; `TURN_WAIT = 3600.0`; teardown destroys the server.

- [ ] **Step 1: `hatch.sh`**

After the recipe is ready and before "creating the brain", add:

```bash
ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"
SERVER_ID=null
if [ "$MEMBRANE_MODEL" = scripted ]; then
  echo "starting the scripted model server" >&2
  CREATED="$(api POST /computers "$(jq -cn --arg r "$RECIPE_ID" '{recipe_id: $r}')")"
  SID="$(jq -r .computer_id <<<"$CREATED")"
  upload "$SID" "/tmp/$WHEEL_NAME" "$WHEEL"
  printf 'MSHKN_API_URL=%s\nMSHKN_API_KEY=unused\nMEMBRANE_MODEL=scripted\n' "$BRAIN_API_URL" > "$TMP/server-env"
  upload "$SID" /brain/.env "$TMP/server-env"
  run "$SID" "/brain/venv/bin/pip install --no-deps -q /tmp/$WHEEL_NAME && ln -sf /brain/venv/bin/membrane /usr/local/bin/membrane"
  api POST "/computers/$SID/exec/bg" '{"command": "membrane serve --port 8000"}' > /dev/null
  # the computer's route, port-prefixed: https://8000-<id>.<domain>
  ANTHROPIC_BASE_URL="$(jq -r .url <<<"$CREATED" | sed 's#^https://#https://8000-#')"
  SERVER_ID="\"$SID\""
fi
```

Move "minting the brain's scoped key" below that block and build the scope with the base URL:

```bash
SCOPES="$(jq -cn --arg t "$ANTHROPIC_BASE_URL/" '{recipes: {create: true, read: true}, computers: {create_from: "*"}, labels: ["verb/"], relay: {targets: [$t], deliver: {label: "brain", exec: "membrane resume"}}}')"
```

(delete the old `SCOPES=` line near the top). In the `.env` block add `echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"`. The final `jq -cn` gains `--argjson server "$SERVER_ID"` and `server_id: $server` in its object. Update the header comment: `ANTHROPIC_BASE_URL` (optional, the model's base URL; in scripted mode the server's route). Run `bash -n embryo/hatch.sh`.

- [ ] **Step 2: The measure's doors**

In `embryo/membrane/measure.py`: `TURN_WAIT = 3600.0` beside `TURN_TIMEOUT`; `Hatched` gains `server_id: str | None = None`; `Doors` gains:

```python
    async def await_turn(self, turn: int) -> tuple[dict[str, Any], str]:
        """`list` until the turn is in the window (relay design §7): the reply and the
        closing audit line live there, written by the fork that closed the turn."""
        deadline = self.now() + TURN_WAIT
        while True:
            listing = await self.listing()
            for entry in listing["window"]:
                if entry["turn"] == turn:
                    return dict(entry["audit"]), str(entry["output"])
            if self.now() >= deadline:
                raise RuntimeError(f"turn {turn} did not close within {TURN_WAIT:.0f} s")
            await self.sleep(BUILD_INTERVAL)

    async def _spoken(self, out: str) -> tuple[dict[str, Any], str]:
        audit, rest = split_output(out)
        if "job" not in audit:
            if "queued" in audit:
                raise RuntimeError("a turn was still pending when the next was spoken")
            return audit, rest
        return await self.await_turn(int(audit["turn"]))
```

`root_say` becomes `return await self._spoken(await self.root("say", b64(text)))` and `public_say` returns `await self._spoken(await self._turn("ingress", "say", payload, send))`. `teardown` drops `/computers/{hatched.server_id}` first when it is set. `hatch()` passes `server_id` through from the JSON line (`Hatched(**body)` already does if the key is present).

- [ ] **Step 3: The measure's tests**

In `tests/unit/test_embryo_measure.py`, `FakeApi._stdout` for a `say` now answers the start audit line and the acknowledgement, and `list` answers a listing whose `window` carries the turn: extend `FakeApi` with `turns: dict[int, tuple[dict[str, Any], str]]` and `next_turn: int`; when a `membrane root say …` or `membrane say …` command has canned output, the fake answers `_out(_audit(started=True, turn=n, job=f"rj-{n}"), json.dumps({"turn": n, "job": f"rj-{n}"}))` and stores the canned `(audit, reply)` under `turns[n]`; `membrane root list` answers `{"catalog": {}, "proposals": [], "policy": {"principals": {}}, "pending": None, "queue": [], "window": [{"turn": n, "principal": ..., "door": ..., "input": "", "reply": reply, "output": reply, "audit": audit} for each stored turn]}`. Keep the canned-output shape the tests already use (`_out(_audit(...), "hello")`) as what ends up in the window. `FakeDoors` (the state machine for `speak_liturgy`) is unchanged: it never spoke HTTP. Add:

```python
async def test_root_say_waits_for_the_turn_in_the_window(tmp_path: Path) -> None:
    api = FakeApi(outputs={f"membrane root say {b64('hi')}": [_out(_audit(stopped="done"), "hello")]})
    doors = _doors(api, tmp_path)
    audit, reply = await doors.root_say("hi")
    assert audit["stopped"] == "done" and reply == "hello\n"
    assert [p for m, p, _ in api.requests] == ["/checkpoints/fork", "/checkpoints/fork"], "the say, then one list"


async def test_a_closed_door_answers_at_once_and_a_queued_say_is_an_error(tmp_path: Path) -> None:
    closed = _out({"door": "ingress", "principal": None, "closed": True}, "The public door is closed.")
    api = FakeApi(outputs={f"membrane say {b64('hi')}": [closed]})
    doors = _doors(api, tmp_path)
    audit, reply = await doors.public_say("hi")
    assert audit["principal"] is None and reply.startswith("The public door is closed.")
    api.outputs[f"membrane say {b64('again')}"] = [_out({"door": "ingress", "principal": "root", "queued": 1}, '{"queued": 1}')]
    with pytest.raises(RuntimeError, match="pending"):
        await doors.public_say("again")
```

Adjust the existing measure tests that counted requests or read `exec_stdout` of a say for their audit (`test_root_retries_a_409_and_records_the_command`, `test_public_say_carries_no_credential_and_records_the_principal`, `test_a_failed_exec_is_an_error_with_the_output`, `test_an_aborted_run_writes_what_it_had_and_tears_down`, `test_run_once_hatches_speaks_judges_records_and_tears_down`) to the new sequence: a say records two commands (the say and the list). Run the file and fix each failing assertion against the new shape; the tests' intent does not change.

- [ ] **Step 4: Run the tests**

Run: `bash -n embryo/hatch.sh && uv run pytest tests/unit/test_embryo_measure.py tests/unit/test_embryo_priors.py -q && uv run mypy`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add embryo/hatch.sh embryo/membrane/measure.py tests/unit/test_embryo_measure.py
git commit -m "feat(embryo): hatch with the relay scope and the scripted server; the measure waits on list (#110)"
```

---

### Task 15: The live tier: Phase 15 and Phase 14 on the asynchronous turn

**Files:**
- Create: `tests/e2e/test_phase15_relay.py`
- Modify: `tests/e2e/test_phase14_embryo.py`, `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md`

**Interfaces:**
- Produces: four Phase 15 tests; Phase 14's `Doors` waiting through `list`, touching the scripted server between tests, and destroying it at teardown; the test plan's Phase 15 section and Phase 14 restated.

- [ ] **Step 1: Phase 15**

`tests/e2e/test_phase15_relay.py`:

```python
"""Phase 15: the relay on the live host (#110). A job to a public target is called
and delivered by a fork of a labelled chain; the guard refuses the host; a scoped
key is held to its scope; a delivery waits behind a running fork."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx
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
    return job["status"] in ("completed", "failed") and (job["delivery"] or {}).get("status") in ("delivered", "failed")


async def _wait_exec_log(client: httpx.AsyncClient, computer_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_TIMEOUT
    while time.monotonic() < deadline:
        resp = await client.get(f"/computers/{computer_id}/exec_log")
        if resp.status_code == 200:
            return dict(resp.json())
        await asyncio.sleep(2)
    raise TimeoutError(f"no exec log for {computer_id}")


class TestPhase15Relay:
    async def test_t15_1_a_job_to_a_public_target_is_delivered_to_a_chain(self, long_client: httpx.AsyncClient, chain: str) -> None:
        resp = await long_client.post("/relay", json={"target": "https://example.com/", "method": "GET", "deliver": {"label": chain, "exec": "echo woke"}})
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        job = await _wait(long_client, job_id, _delivered)
        assert job["status"] == "completed" and job["attempts"] == 1, job
        assert job["response"]["status"] == 200 and "Example Domain" in job["response"]["body"]
        assert job["delivery"]["status"] == "delivered" and job["delivery"]["computer_id"], job["delivery"]
        log = await _wait_exec_log(long_client, job["delivery"]["computer_id"])
        assert log["command"] == f"echo woke {job_id}" and job_id in log["stdout"]
        assert log["label"] == chain and log["created_checkpoint_id"]
        heads = (await long_client.get("/checkpoints", params={"label": chain})).json()
        assert len(heads) == 2
        assert "forward_headers" not in resp.text and "forward_headers" not in str(job)

    async def test_t15_2_the_guard_refuses_the_host(self, client: httpx.AsyncClient) -> None:
        for target in ("http://127.0.0.1:8000/health", "http://172.16.254.1/", "http://localhost/", "http://[::1]/", "ftp://example.com/"):
            resp = await client.post("/relay", json={"target": target})
            assert resp.status_code == 422, (target, resp.text)

    async def test_t15_3_a_scoped_key_is_held_to_its_scope(self, long_client: httpx.AsyncClient, chain: str) -> None:
        minted = await long_client.post("/keys", json={"scopes": {"recipes": {"read": True}}, "label": "t15"})
        assert minted.status_code == 200, minted.text
        plain = {"Authorization": f"Bearer {minted.json()['secret']}"}
        scope = {"relay": {"targets": ["https://example.com/"], "deliver": {"label": chain, "exec": "echo pinned"}}}
        minted_relay = await long_client.post("/keys", json={"scopes": scope, "label": "t15-relay"})
        assert minted_relay.status_code == 200, minted_relay.text
        scoped = {"Authorization": f"Bearer {minted_relay.json()['secret']}"}
        other_minted = await long_client.post("/keys", json={"scopes": scope, "label": "t15-other"})
        other = {"Authorization": f"Bearer {other_minted.json()['secret']}"}
        try:
            assert (await long_client.post("/relay", json={"target": "https://example.com/", "method": "GET"}, headers=plain)).status_code == 403
            assert (await long_client.post("/relay", json={"target": "https://www.iana.org/", "method": "GET"}, headers=scoped)).status_code == 403
            assert (await long_client.post("/relay", json={"target": "https://example.com/", "method": "GET", "deliver": {"label": chain, "exec": "echo mine"}}, headers=scoped)).status_code == 422
            resp = await long_client.post("/relay", json={"target": "https://example.com/", "method": "GET"}, headers=scoped)
            assert resp.status_code == 202, resp.text
            job_id = resp.json()["job_id"]
            job = await _wait(long_client, job_id, _delivered)
            assert job["delivery"]["exec"] == "echo pinned" and job["delivery"]["status"] == "delivered"
            assert (await long_client.get(f"/relay/{job_id}", headers=scoped)).status_code == 200
            assert (await long_client.get(f"/relay/{job_id}", headers=other)).status_code == 404
            assert (await long_client.get(f"/relay/{job_id}")).status_code == 200
            log = await _wait_exec_log(long_client, job["delivery"]["computer_id"])
            assert log["command"] == f"echo pinned {job_id}"
        finally:
            for key in (minted, minted_relay, other_minted):
                await long_client.delete(f"/keys/{key.json()['id']}")

    async def test_t15_4_a_delivery_waits_behind_a_running_fork(self, long_client: httpx.AsyncClient, chain: str) -> None:
        sleeper = asyncio.create_task(
            long_client.post("/checkpoints/fork", json={"label": chain, "exec": "sleep 40", "self_destruct": True, "exclusive": "error_on_conflict"}, timeout=120.0)
        )
        await asyncio.sleep(10)  # the sleeper's fork is admitted and running
        resp = await long_client.post("/relay", json={"target": "https://example.com/", "method": "GET", "deliver": {"label": chain, "exec": "echo woke"}})
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        job = await _wait(long_client, job_id, _delivered)
        assert job["delivery"]["status"] == "delivered" and job["delivery"]["deferred_id"], job["delivery"]
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
```

`pytestmark`: follow `test_phase12_pistachio.py` (`@pytest.mark.asyncio` per test or a module mark, whichever the E2E conftest expects; copy its style).

- [ ] **Step 2: Phase 14**

In `tests/e2e/test_phase14_embryo.py`: `TURN_WAIT = 3600.0`; `Hatched` gains `server_id: str | None = None`; `Doors` gains `await_turn` and `_spoken` exactly as the measure's (Task 14 Step 2), with `asyncio.sleep(3)` between polls; `root_say` and `public_say` go through `_spoken`; `public_say` records `audit["principal"]` from the start audit line it gets first (the ack), before waiting; a new method:

```python
    async def touch(self) -> None:
        """The scripted server is a computer with only a background process; the idle
        reaper does not count that as activity (relay design §12), so a command keeps it."""
        if self.hatched.server_id:
            resp = await self.client.post(f"/computers/{self.hatched.server_id}/exec", json={"command": "true"}, timeout=60.0)
            assert resp.status_code == 200, resp.text
```

called first in every `test_t14_*`. The teardown adds `await drop(f"/computers/{hatched.server_id}")` when set, before the key is dropped. In T14.7 add, after the exec-log check:

```python
        # the turn was a chain of forks: the ingress log's computer holds the start audit
        # line and the acknowledgement; the relay job it names was delivered to `brain`
        first = log.json()["stdout"].splitlines()
        assert first[0].startswith("audit ") and json.loads(first[1])["job"].startswith("rj-")
        job = await doors.client.get(f"/relay/{json.loads(first[1])['job']}")
        assert job.status_code == 200 and job.json()["delivery"]["label"] == "brain"
        assert job.json()["delivery"]["status"] == "delivered"
```

- [ ] **Step 3: The test plan**

In `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md`, Phase 14's intro gains one sentence: "Since #110 a turn is a chain of forks: a `say` answers with an acknowledgement, and the reply is read from `membrane root list` once the relay has woken the brain." T14.1, T14.3, T14.5 and T14.6 say "the reply, read from `list`," where they say "replies". After Phase 14 add:

```markdown
## Phase 15: "Call Me Back" (The Relay)

The relay (`docs/superpowers/specs/2026-09-09-async-turn-relay-design.md`): a job is one upstream HTTP call plus one delivery, a fork of a labelled chain with the job id as the exec's last argument. Folded in from lampas; the SSRF guard and the retry policy are its.

### T15.1 — A Job to a Public Target Is Delivered to a Chain

- `POST /relay` with `GET https://example.com/` and a `deliver` naming a labelled chain answers 202 with a job id at once.
- `GET /relay/{job_id}` reaches `completed` with the page in `response.body`, and `delivery.status` `delivered` with the computer that ran the wake-up.
- That computer's exec log holds `<exec> <job_id>` and the chain is one checkpoint longer. No response ever carries `forward_headers`.

### T15.2 — The Guard Refuses the Host

- `http://127.0.0.1:8000/health`, `http://172.16.254.1/`, `http://localhost/`, `http://[::1]/` and an `ftp://` target are 422 before any job exists.

### T15.3 — A Scoped Key Is Held to Its Scope

- A key without the `relay` section is 403; with it, a target outside its prefixes is 403 and a `deliver` in the body is 422.
- Its job is delivered through the exec the scope pins, is readable by that key and by the account, and is 404 to another key.

### T15.4 — A Delivery Waits Behind a Running Fork

- With a fork running on the chain, the job's delivery is `delivered` with a deferred id and no computer.
- When the fork self-destructs, the drain runs the wake-up: the chain is two checkpoints longer, in order, and the newest one's exec log holds the job id.
```

and the pass/fail table gains `| The relay (Phase 15) | A public target is called and its chain woken; the host is refused; a scoped key is held to its scope; a busy chain defers | Any job that never settles, any wake-up the guard or the scope should have stopped |`.

- [ ] **Step 4: Check and commit**

Run: `uv run ruff check tests/e2e && uv run ruff format --check tests/e2e && uv run mypy && uv run pytest tests/unit/test_docs.py -q`
Expected: clean. The live run is Task 17.

```bash
git add tests/e2e/test_phase15_relay.py tests/e2e/test_phase14_embryo.py docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md
git commit -m "test(e2e): Phase 15, the relay; Phase 14 on the asynchronous turn (#110)"
```

---

### Task 16: The documents

**Files:**
- Modify: `docs/superpowers/specs/2026-09-08-embryo-design.md`, `docs/ARCHITECTURE.md`, `README.md`, `CLAUDE.md`, `docs/plans/README.md`, `embryo/README.md`, `docs/embryo/README.md`

- [ ] **Step 1: The embryo spec**

In `docs/superpowers/specs/2026-09-08-embryo-design.md`: replace the body of §6 from "### A `say` turn, in order" to the end of the section with:

```markdown
### A `say` turn

Rewritten by `docs/superpowers/specs/2026-09-09-async-turn-relay-design.md` §6 (#110): a turn is a chain of forks. `say` runs the principal, the builds, the input and the tools, posts the model request to the host's relay with its own wake-up as the delivery, records the pending turn in `state.json`, and answers with an acknowledgement. `membrane resume <job_id>` continues it. Every command settles a pending turn first; a `say` while one is pending is queued and runs next. The reply and the closing audit line are read from `list`.

What the brain cannot do in a turn, by construction: read or write a file, run a command, reach the network, see a key, change its tools, act with any authority beyond its scoped key, make the host call anything but the prefixes its key names, or make the host run anything on `brain` but its own `resume`.
```

Add `membrane resume <job_id>` to the root commands table ("no", "The relay's wake-up; §6 of the relay design"). In §4, the `timeout_seconds` row becomes "Bounded by a fork's 300 s exec budget minus the membrane's clock; since #110 that bounds a tool run, not the turn." In §15, the first fact becomes "A fork's exec runs with `ComputerService.exec`'s 300 s default and the membrane's clock is 240 s; since #110 these bound the tool runs of one fork, not the model." In §13 add "- The reply callback: a URL root names in policy, delivered through the relay at the end of a turn (relay design §11)."

- [ ] **Step 2: `docs/ARCHITECTURE.md`**

- §4's table: `| \`mshkn.services.relay.RelayService\` | Relay jobs (#110): \`submit\` (the SSRF guard, the caps, a row, a background task), \`run\` (the upstream call with lampas's retry policy, headers cleared when it settles, a streamed body reassembled by \`mshkn.services.sse\`), \`deliver\` (a fork by label with the job id, deferred behind a running fork), \`get_owned\`, \`resume\` (re-run after a restart). \`mshkn.services.ssrf\` is the guard. |`
- §2: `RelayService` in the build order after `IngressService`; `Runtime.start` re-runs unsettled relay jobs.
- §5: `relay_jobs` in the table list, with "forwarded headers in their own column, cleared when the call settles".
- New section after §9, "## 9a. The relay": four paragraphs from the relay spec §3 to §5: the job (fields, statuses, the retry set), the guard (the ranges, fail-closed DNS), the response (verbatim or reassembled), delivery (fork by label, `defer_on_conflict`, the account's computer, retry, `delivered` with a computer or a deferred id, `failed`), restart and expiry.
- §13's table: `| \`relay_timeout_seconds\` | \`3600\` | \`MSHKN_RELAY_TIMEOUT_SECONDS\` |` and `| \`relay_body_bytes\` | \`8388608\` | \`MSHKN_RELAY_BODY_BYTES\` |`.
- §11: a bullet "**Relay jobs survive a restart.** Unsettled jobs are re-run at start; their headers are still stored and the brain has seen nothing."

- [ ] **Step 3: README, CLAUDE.md, the indexes**

`README.md` "What exists": after **Ingress**, `- **Relay.** \`POST /relay\` makes an HTTP call on a caller's behalf in the background (\`forward_headers\` sent upstream and deleted when the call settles, lampas's retry policy, a streamed Messages API response reassembled whole) and wakes a labelled chain by forking it with the job id; \`GET /relay/{job_id}\` is the record. A scoped key's \`relay\` scope pins the URL prefixes it may call and the one wake-up it may cause (\`docs/ARCHITECTURE.md\` §9a).` In **The embryo**: "a turn is a chain of forks through the relay: the brain never keeps a computer alive while the model thinks." The counts: "Four of the 180 end-to-end tests" and "It currently reports 170 passed, 6 skipped and 4 failed".

`CLAUDE.md`: "(180 E2E tests)" and "**170 passed, 6 skipped, 4 failed**".

`docs/plans/README.md`: a section "## Asynchronous turns through the relay (2026-09)" naming the spec, this plan, the PR, and status **implemented** once merged; and the embryo section's last sentence updated: "#110 landed as PR #<N>; #111 resumes the measure."

`embryo/README.md`: the hatching paragraph names `server_id` and `ANTHROPIC_BASE_URL`; "Speaking through each door" says a `say` answers with `{"turn", "job"}` and the reply is read with `membrane root list` (its `window`), and shows `membrane resume <job_id>` as the relay's command; the brain-disk table's `state.json` row adds "the pending turn and the queue".

`docs/embryo/README.md`: one paragraph under the finding: "#110 landed on <date>: a turn is a chain of forks through the host's relay; the measure resumes as #111."

- [ ] **Step 4: Run the docs test and the gate**

Run: `uv run pytest tests/unit/test_docs.py -q && uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov -q`
Expected: all green, coverage at or above 98 %, zero warnings. If coverage falls short, the report names the lines; the likely ones are error branches in `relay.py` (`account not found`, a cancelled task) and `serve.py`'s `log_message`: add the unit test that reaches each rather than a pragma.

- [ ] **Step 5: Commit**

```bash
git add docs README.md CLAUDE.md embryo/README.md
git commit -m "docs: the relay and the asynchronous turn in the spec, the architecture, the README and the indexes (#110)"
```

---

### Task 17: The live gate and the pull request

**Files:** none new.

- [ ] **Step 1: Push and run the live E2E, detached**

```bash
git push -u origin async-turn-relay
export MSHKN_SERVER=mshkn
setsid nohup scripts/e2e.sh > /tmp/e2e-async-turn-relay.log 2>&1 < /dev/null &
```

Read the log's "running E2E against" line first (`feedback_e2e_alias_url`): it must name `https://api.mshkn.dev`, not the ssh alias. A full run takes about 25 minutes now (Phase 14 grows by the scripted server, Phase 15 by four tests).

- [ ] **Step 2: Read the result**

Expected line: `170 passed, 6 skipped, 4 failed`, the four being exactly the `Not implemented` tests in #65. Anything else is a regression: fix it in this branch or stop and report it. Then:

```bash
ssh $MSHKN_SERVER journalctl -u mshkn --since '40 min ago' --no-pager | grep -ci traceback
```

Expected: `0`. A traceback in the relay's task shows as `background task relay:… failed`; read it.

- [ ] **Step 3: Open the PR**

```bash
gh pr create --base main --title "feat: asynchronous turns through a host-side relay (#110)" --body-file - <<'BODY'
Closes #110

**What this does**

Folds lampas into mshkn as `RelayService` (`POST /relay`, `GET /relay/{job_id}`, the `relay` scope, `relay_jobs`): an upstream HTTP call in the background with lampas's retry policy, the SSRF guard, a streamed Messages API response reassembled whole, and delivery by forking a labelled chain with the job id. The embryo's turn becomes a chain of forks: `say` posts the model request and acknowledges, `membrane resume <job_id>` continues the turn, every command settles a pending turn first, a `say` while one is pending is queued. `membrane serve` puts the scripted model on the wire so the flow and live tiers drive the real relay.

**Design alignment**

- Spec `docs/superpowers/specs/2026-09-09-async-turn-relay-design.md` §2 to §7: each decision is implemented as written; the deviations from lampas are §10's.
- `docs/ARCHITECTURE.md` §1a (tenancy): the `relay` scope pins targets and one wake-up, checked in the router before any service runs; a leaked brain key can cause `membrane resume <id>` on `brain` and nothing else (embryo spec §10.2).
- §5 (state ownership): one table, headers in their own column and cleared on settle; jobs expire with the exec-log retention; unsettled jobs re-run at start.
- §6/§7 (lifecycle): delivery goes through `fork_by_label` and `run_ephemeral` like every other fork; a busy chain defers.

**Validation performed**

- Gate: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov` → <paste the last lines: N passed, coverage %>.
- CI: <link>.
- Live E2E (`scripts/e2e.sh` against https://api.mshkn.dev): `170 passed, 6 skipped, 4 failed`; the four are #65's `Not implemented` tests (<names>). Journal tracebacks in the window: 0.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
BODY
gh pr checks --watch
```

Fill every `<…>` with the real output. Do not merge: merging needs Mike's word.

- [ ] **Step 4: Triage bot reviews**

Reply to every CodeRabbit and Copilot comment with a rationale, fix only what is wrong, resolve every thread (the API calls are in `CLAUDE.md`, "How to handle PR reviews").

---

## Self-review

**Spec coverage.** §3 the job, statuses, retry set, SSE reassembly, guard, bounds, restart, retention: Tasks 1, 3, 4, 5, 7. §4 routes and scope: Tasks 2, 7. §5 delivery: Task 6 (Task 8 proves it over HTTP). §6 the membrane's turn: Tasks 9 to 11. §7 serve, hatch, tiers: Tasks 12 to 15. §8 proof: unit in each task, flow in 8 and 13, live in 15, the gate line in 16. §9 documents: Task 16. §10 deviations: stated in the code's docstrings (Tasks 3, 5). §11 the after-list: nothing to build. §12 facts: the idle reaper's treatment of the server is handled by `touch` (Task 15) and `hatch.sh`'s comment (Task 14).

**Placeholder scan.** The kept hook tests in Task 11 are named and their transformation is stated exactly (one call shape, one assertion swap); the measure's adjusted tests in Task 14 are named with the one change they need (a say is two commands). The ARCHITECTURE section in Task 16 is described by its content, which is the spec's §3 to §5.

**Type consistency.** `RelayJob` field names match between `models.py` (Task 1), `db/relay.py` (Task 1), `relay.py` (Tasks 5, 6), `schemas.py` (Task 7) and the membrane's own `RelayJob` in `mshkn.py` (Task 9, a different, five-field class on the client side, named the same on purpose: the wire record as the brain reads it). `Pending` field names match between `state.py` (Task 9), `loop.py` and `turn.py` (Task 11) and `list_state` (Task 11). `Flow.targets` (Task 8) is what Task 13 mounts into. `scripted_asgi` and `answer_async` (Task 12) are what Task 13 uses.
