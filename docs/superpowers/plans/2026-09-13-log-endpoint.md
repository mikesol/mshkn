# A Readable Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every log record to the Elastic Common Schema and expose a bounded window of them at `GET /logs`, so T11.2 can assert on structured logs with nothing but an HTTP client.

**Architecture:** One function, `to_ecs(record) -> dict`, maps a `logging.LogRecord` to a flat ECS document. Two consumers call it: `ECSFormatter` serialises it to stdout, and `RingBufferHandler` appends it to a bounded `deque` that the `Runtime` owns beside `alerts`. `GET /logs` reads that deque behind `require_account_key` and returns only records whose `mshkn.account_id` matches the caller.

**Tech Stack:** Python 3.12, FastAPI, stdlib `logging` and `contextvars`, pytest. No new dependencies — ECS is a field-naming convention, not a library.

**Spec:** `docs/superpowers/specs/2026-09-13-log-endpoint-design.md`

---

## Before you start

**Run everything through the system interpreter.** The uv-managed CPython on this box lacks `os.pidfd_open` and fails five Firecracker tests on a clean `main`. Every command in this plan uses `uv run --python /usr/bin/python3`. Do not drop the flag.

**The gate**, run before any push:

```bash
uv lock --check && uv run --python /usr/bin/python3 ruff check . && uv run --python /usr/bin/python3 ruff format --check . && uv run --python /usr/bin/python3 mypy && uv run --python /usr/bin/python3 pytest --cov
```

Coverage floor is 98 %. E2E is deselected by default (`addopts = "-m 'not e2e' --strict-markers"`), so the gate never needs a live server.

**Line length is 100.** `ruff format` will not wrap a long string for you; write it wrapped.

**Do not merge anything.** Push the branch, open the PR, stop.

---

## File structure

| File | Responsibility |
|---|---|
| `src/mshkn/observability/logging.py` | modify — the two contextvars, `to_ecs`, `ECSFormatter`, `RingBufferHandler`, `install_log_buffer`. `RequestIdFilter` and `JSONFormatter` are deleted. |
| `src/mshkn/config.py` | modify — `log_buffer_size` |
| `src/mshkn/runtime.py` | modify — `Runtime.logs`, a bounded deque built in `Runtime.build` |
| `src/mshkn/api/deps.py` | modify — `require_principal` sets `account_id_var` |
| `src/mshkn/api/logs.py` | create — the `GET /logs` router. Its own module because `api/system.py` is the unauthenticated one. |
| `src/mshkn/app.py` | modify — include the router, install the buffer at both places the Runtime is attached |
| `docs/ARCHITECTURE.md` | modify — the route table and the auth paragraph; `tests/unit/test_docs.py` fails if a route is missing from it |
| `docs/plans/README.md` | modify — flip the section's status |
| `tests/unit/test_observability.py` | modify — the logging half is rewritten; the metrics half is untouched |
| `tests/flow/test_logs.py` | create — the endpoint over the real app: isolation, scoping, `limit`, `since` |
| `tests/e2e/test_phase11_observability.py` | modify — T11.2 stops being a placeholder |

---

## Task 1: `to_ecs` — the mapping

**Files:**
- Modify: `src/mshkn/observability/logging.py`
- Test: `tests/unit/test_observability.py`

- [ ] **Step 1: Write the failing tests**

Replace everything in `tests/unit/test_observability.py` from the imports down to (but not including) `def _sample(`. The metrics tests below `_sample` stay exactly as they are.

```python
from __future__ import annotations

import json
import logging
import sys

import pytest
from prometheus_client import generate_latest

from mshkn.errors import HostError, NotFound
from mshkn.host.shell import ShellError
from mshkn.observability.logging import (
    ECS_VERSION,
    ECSFormatter,
    account_id_var,
    request_id_var,
    to_ecs,
)
from mshkn.observability.metrics import (
    operation_duration_seconds,
    operation_errors_total,
    timed,
)


def _record(msg: str = "x", args: object = None, **attrs: object) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, msg, args, None)
    for key, value in attrs.items():
        setattr(record, key, value)
    return record


def test_the_ecs_envelope_is_present_on_every_record() -> None:
    entry = to_ecs(_record("hello %s", ("w",)))
    assert entry["message"] == "hello w"
    assert entry["log.level"] == "info"
    assert entry["log.logger"] == "t"
    assert entry["ecs.version"] == ECS_VERSION
    assert entry["@timestamp"].endswith("+00:00"), "ECS timestamps are UTC and offset-aware"


def test_trace_id_comes_from_the_context_and_is_absent_outside_a_request() -> None:
    assert "trace.id" not in to_ecs(_record()), "a reaper cycle has no trace"
    token = request_id_var.set("req-123")
    try:
        assert to_ecs(_record())["trace.id"] == "req-123"
    finally:
        request_id_var.reset(token)


def test_an_explicit_account_beats_the_context() -> None:
    """destroy logs the computer's account even when the reaper ran it."""
    token = account_id_var.set("acct-ctx")
    try:
        assert to_ecs(_record())["mshkn.account_id"] == "acct-ctx"
        assert to_ecs(_record(account_id="acct-row"))["mshkn.account_id"] == "acct-row"
    finally:
        account_id_var.reset(token)
    assert "mshkn.account_id" not in to_ecs(_record())


def test_extras_are_namespaced_not_lifted_to_the_top_level() -> None:
    entry = to_ecs(_record(computer_id="comp-1", op="create"))
    assert entry["mshkn.computer_id"] == "comp-1"
    assert entry["mshkn.op"] == "create"
    assert "computer_id" not in entry, "ECS reserves the top level for itself"


def test_an_exception_becomes_three_error_fields() -> None:
    try:
        raise NotFound("no such computer")
    except NotFound:
        record = logging.LogRecord(
            "t", logging.ERROR, "f.py", 1, "boom", None, sys.exc_info()
        )
    entry = to_ecs(record)
    assert entry["error.type"] == "NotFound"
    assert entry["error.message"] == "no such computer"
    assert "Traceback" in str(entry["error.stack_trace"])
    assert "exception" not in entry, "the single blob field is gone"


def test_an_oversized_field_is_truncated_and_marked() -> None:
    value = str(to_ecs(_record(blob="a" * 9000))["mshkn.blob"])
    assert len(value) < 9000 and value.endswith("[truncated]")


def test_scalars_keep_their_type() -> None:
    entry = to_ecs(_record(slot=7, ok=True, nothing=None))
    assert entry["mshkn.slot"] == 7, "an int stays an int, not a string"
    assert entry["mshkn.ok"] is True
    assert entry["mshkn.nothing"] is None


def test_the_formatter_emits_one_json_object_per_record() -> None:
    line = ECSFormatter().format(_record("hi"))
    assert "\n" not in line
    assert json.loads(line)["message"] == "hi"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/unit/test_observability.py -q
```

Expected: a collection error — `ImportError: cannot import name 'to_ecs' from 'mshkn.observability.logging'`.

- [ ] **Step 3: Write the implementation**

Replace the whole of `src/mshkn/observability/logging.py` above `configure_logging` — `request_id_var`, `RequestIdFilter` and `JSONFormatter` all go — with this. Leave `configure_logging` in place for now; Task 3 changes it.

```python
"""ECS log records, and the two places they go.

`to_ecs` maps a LogRecord to the Elastic Common Schema. `ECSFormatter`
serialises the result for stdout; `RingBufferHandler` (Task 3) keeps the dict in
a bounded deque the API reads. One mapping, two consumers, identical shape —
which is the whole point: what a collector scrapes and what `GET /logs` returns
are the same document.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("mshkn_request_id", default="-")
account_id_var: ContextVar[str | None] = ContextVar("mshkn_account_id", default=None)

ECS_VERSION = "8.11.0"

_CONFIGURED_MARKER = "_mshkn_configured"
_MAX_FIELD_CHARS = 4096
_TRUNCATED = "[truncated]"

_BUILTIN_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, None, None, None).__dict__.keys() | {"message", "asctime"}
)
# Read off the record into a field of their own; never namespaced as well.
_LIFTED = frozenset({"request_id", "account_id"})


def _cap(value: object) -> object:
    """Bound one field. JSON scalars pass through; anything else is stringified
    and truncated, so one oversized `extra=` cannot pin megabytes in the ring."""
    if value is None or isinstance(value, bool | int | float):
        return value
    text = value if isinstance(value, str) else str(value)
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[:_MAX_FIELD_CHARS] + _TRUNCATED


def to_ecs(record: logging.LogRecord) -> dict[str, object]:
    """One LogRecord as a flat, dotted ECS document.

    Dotted keys rather than nested objects: that is what Filebeat emits, it is
    what a flat dict produces for free, and it is what a test can assert on
    without walking a tree.
    """
    entry: dict[str, object] = {
        "@timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        "ecs.version": ECS_VERSION,
        "log.level": record.levelname.lower(),
        "log.logger": record.name,
        "message": _cap(record.getMessage()),
    }
    # An explicit attribute wins over the contextvar: destroy logs the computer's
    # own account, which is right even when the reaper is what ran it.
    request_id = getattr(record, "request_id", None) or request_id_var.get()
    if request_id != "-":
        entry["trace.id"] = request_id
    account_id = getattr(record, "account_id", None) or account_id_var.get()
    if account_id is not None:
        entry["mshkn.account_id"] = account_id
    for key, value in record.__dict__.items():
        if key not in _BUILTIN_ATTRS and key not in _LIFTED:
            entry[f"mshkn.{key}"] = _cap(value)
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        entry["error.type"] = type(exc).__name__
        entry["error.message"] = _cap(str(exc))
        entry["error.stack_trace"] = _cap(logging.Formatter().formatException(record.exc_info))
    return entry


class ECSFormatter(logging.Formatter):
    """Serialise `to_ecs` as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(to_ecs(record), default=str)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/unit/test_observability.py -q
```

Expected: PASS. The three metrics tests below `_sample` still pass unchanged.

- [ ] **Step 5: Fix the one remaining importer**

`configure_logging` still references `JSONFormatter` and `RequestIdFilter`, which no longer exist. Change its two lines to use the new formatter and drop the filter:

```python
    handler = logging.StreamHandler()
    handler.setFormatter(ECSFormatter())
    root.handlers = [handler]
```

The filter is gone because `to_ecs` reads the contextvars itself. A filter attached to one handler would have to run before the others to help them, and handler order on the root logger is not something to depend on.

- [ ] **Step 6: Run the whole non-e2e suite**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest -q
```

Expected: PASS. If anything else imported `JSONFormatter` or `RequestIdFilter`, it surfaces here — fix it rather than re-exporting the old names. This repo is pre-alpha and keeps no compatibility shims.

- [ ] **Step 7: Commit**

```bash
cd ~/work/mshkn-logs
git add src/mshkn/observability/logging.py tests/unit/test_observability.py
git commit -m "Log records are ECS documents, from one mapping (#65)

to_ecs replaces JSONFormatter's bespoke field names with the Elastic Common
Schema, so a stock collector ingests them untransformed. RequestIdFilter goes:
to_ecs reads the contextvar itself, which does not depend on handler order.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: The account contextvar

**Files:**
- Modify: `src/mshkn/api/deps.py:23-37`
- Test: `tests/flow/test_logs.py` (created here, extended in Task 5)

- [ ] **Step 1: Write the failing test**

Create `tests/flow/test_logs.py`:

```python
"""GET /logs over the real app: a record is attributed to the account that
caused it, and one account never sees another's."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.observability.logging import account_id_var

if TYPE_CHECKING:
    from .conftest import Flow


async def test_a_request_stamps_the_account_on_every_record_it_causes(flow: Flow) -> None:
    token = account_id_var.set(None)
    try:
        created = await flow.client.post("/computers", json={})
        assert created.status_code == 200
    finally:
        account_id_var.reset(token)
    stamped = [r for r in flow.runtime.logs if r.get("mshkn.account_id") == "acct-1"]
    assert stamped, "the create logged nothing under the calling account"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/flow/test_logs.py -q
```

Expected: FAIL with `AttributeError: 'Runtime' object has no attribute 'logs'`. That is the right failure for now — Tasks 3 and 4 build the buffer. This task only makes the contextvar exist and be set; the test goes green at the end of Task 4.

- [ ] **Step 3: Set the contextvar in `require_principal`**

In `src/mshkn/api/deps.py`, add the import:

```python
from mshkn.observability.logging import account_id_var
```

and set the variable at both points the account resolves:

```python
async def require_principal(request: Request) -> Principal:
    """The bearer as a Principal: the account key first, then a scoped key (#88).

    Sets account_id_var so every record this request causes carries the account.
    It cannot be done in the request-id middleware: authentication is a route
    dependency and runs after the middleware has handed control down. Nothing
    resets it — contextvars are task-local and Starlette gives each request its
    own task, the same property the request id already relies on.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    db = get_runtime(request).db
    secret = auth[7:]
    account = await get_account_by_key(db, secret)
    if account is not None:
        account_id_var.set(account.id)
        return Principal(account=account)
    key = await get_api_key_by_secret(db, secret)
    if key is not None:
        owner = await get_account_by_id(db, key.account_id)
        if owner is not None:
            account_id_var.set(owner.id)
            return Principal(account=owner, key=key)
    raise HTTPException(status_code=401, detail="Invalid API key")
```

- [ ] **Step 4: Run the gate's fast half**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 ruff check . && uv run --python /usr/bin/python3 mypy
```

Expected: both clean. `tests/flow/test_logs.py` still fails; that is expected and stated in Step 2.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-logs
git add src/mshkn/api/deps.py tests/flow/test_logs.py
git commit -m "Every record carries the account that caused it (#65)

require_principal sets account_id_var once the bearer resolves. The middleware
cannot do it: auth is a route dependency and runs after the middleware has
already handed control down.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: The ring buffer handler

**Files:**
- Modify: `src/mshkn/observability/logging.py`
- Test: `tests/unit/test_observability.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_observability.py`, immediately after `test_the_formatter_emits_one_json_object_per_record`.

The imports are inside the test bodies on purpose. Task 1's Step 4 requires this file to pass before `RingBufferHandler` exists, and a top-level import of a name that is not there yet fails collection for the whole module. Move them to the top only after Task 3 is committed, or leave them — either is fine, but do not move them earlier.

```python
def test_the_ring_holds_ecs_documents_and_evicts_the_oldest() -> None:
    from collections import deque

    from mshkn.observability.logging import RingBufferHandler

    buffer: deque[dict[str, object]] = deque(maxlen=2)
    handler = RingBufferHandler(buffer)
    for n in range(3):
        handler.emit(_record(f"msg-{n}"))
    assert [r["message"] for r in buffer] == ["msg-1", "msg-2"]
    assert buffer[0]["ecs.version"] == ECS_VERSION


def test_installing_the_buffer_twice_leaves_one_handler() -> None:
    from collections import deque

    from mshkn.observability.logging import (
        RingBufferHandler,
        configure_logging,
        install_log_buffer,
    )

    configure_logging()
    first: deque[dict[str, object]] = deque(maxlen=10)
    second: deque[dict[str, object]] = deque(maxlen=10)
    install_log_buffer(first)
    install_log_buffer(second)
    rings = [h for h in logging.root.handlers if isinstance(h, RingBufferHandler)]
    assert len(rings) == 1 and rings[0].buffer is second
    logging.getLogger("t").info("after")
    assert [r["message"] for r in second] == ["after"]
    assert not first, "the replaced buffer stops receiving"
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/unit/test_observability.py -q
```

Expected: FAIL with `ImportError: cannot import name 'RingBufferHandler'`.

- [ ] **Step 3: Write the implementation**

Append to `src/mshkn/observability/logging.py`, after `ECSFormatter` and before `configure_logging`:

```python
class RingBufferHandler(logging.Handler):
    """Append every record's ECS document to a bounded deque the API reads."""

    def __init__(self, buffer: deque[dict[str, object]]) -> None:
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append(to_ecs(record))


def install_log_buffer(buffer: deque[dict[str, object]]) -> RingBufferHandler:
    """Point the root logger at this buffer, replacing any earlier one.

    Not folded into configure_logging: that runs in create_app before a Runtime
    exists, and the buffer belongs to the Runtime because runtime.py keeps no
    module-level mutable state. Replacing rather than adding means a process
    that builds several apps — the flow tier does, once per test — ends with one
    handler pointed at the app it is actually serving.
    """
    root = logging.root
    root.handlers = [h for h in root.handlers if not isinstance(h, RingBufferHandler)]
    handler = RingBufferHandler(buffer)
    root.addHandler(handler)
    return handler
```

Add the import at the top of the file, under `import logging`:

```python
from collections import deque
```

`deque` is used in a runtime annotation on `__init__`, so it cannot go under `TYPE_CHECKING`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/unit/test_observability.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-logs
git add src/mshkn/observability/logging.py tests/unit/test_observability.py
git commit -m "A bounded ring of ECS documents on the root logger (#65)

install_log_buffer replaces its own handler rather than adding one, so a
process that builds several apps ends with the buffer of the app it serves.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Wiring — config, Runtime, app

**Files:**
- Modify: `src/mshkn/config.py`
- Modify: `src/mshkn/runtime.py`
- Modify: `src/mshkn/app.py`
- Test: `tests/flow/test_logs.py` (the test written in Task 2 goes green here)

- [ ] **Step 1: Add the config field**

In `src/mshkn/config.py`, after the `# Idle timeout and retention` block and before `# Relay (#110)`:

```python
    # Observability
    log_buffer_size: int = 1000  # ECS records kept in memory for the logs endpoint
```

Nothing else is needed: `Config.from_env` reads `MSHKN_<FIELD_UPPER>` for every field, so `MSHKN_LOG_BUFFER_SIZE` works with no extra code.

- [ ] **Step 2: Give the Runtime the deque**

In `src/mshkn/runtime.py`, add the field to the dataclass immediately after `alerts`:

```python
    alerts: deque[Alert]
    logs: deque[dict[str, object]]
    http: httpx.AsyncClient
```

and build it in `Runtime.build`, next to where `alerts` is built:

```python
        alerts: deque[Alert] = deque(maxlen=_ALERT_HISTORY_SIZE)
        logs: deque[dict[str, object]] = deque(maxlen=config.log_buffer_size)
```

and pass it in the `cls(...)` call, after `alerts=alerts`:

```python
            alerts=alerts,
            logs=logs,
            http=client,
```

- [ ] **Step 3: Install the buffer wherever the Runtime is attached**

In `src/mshkn/app.py`, extend the import on line 19:

```python
from mshkn.observability.logging import configure_logging, install_log_buffer, request_id_var
```

Then install the buffer at both points the app learns about a Runtime — the lifespan (production) and the constructor argument (tests). Both are needed: the flow tier passes a Runtime to `create_app` and never runs the lifespan.

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime if runtime is not None else await Runtime.from_env()
        app.state.runtime = rt
        install_log_buffer(rt.logs)
        try:
            await rt.start()
            yield
        finally:
            await rt.close()

    app = FastAPI(title="mshkn", version="0.1.0", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime
        install_log_buffer(runtime.logs)
```

- [ ] **Step 4: Run the test from Task 2**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/flow/test_logs.py -q
```

Expected: PASS. This is the first point at which a record written during a request is readable from the Runtime.

- [ ] **Step 5: Run the whole non-e2e suite**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest -q
```

Expected: PASS. Nothing in `src/` or `tests/` constructs a `Runtime` directly — every caller goes through `Runtime.build` or `Runtime.from_env` — so the new required field breaks no existing site.

- [ ] **Step 6: Commit**

```bash
cd ~/work/mshkn-logs
git add src/mshkn/config.py src/mshkn/runtime.py src/mshkn/app.py
git commit -m "The Runtime owns the log ring, sized by config (#65)

create_app installs the buffer at both places it learns about a Runtime: the
lifespan for production, and the constructor argument for the flow tier, which
passes one in and never runs the lifespan.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 5: `GET /logs`

**Files:**
- Create: `src/mshkn/api/logs.py`
- Modify: `src/mshkn/app.py`
- Test: `tests/flow/test_logs.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/flow/test_logs.py`. Add `import json` and `from mshkn.host import ExecResult` is **not** needed; the imports the new tests need are `json` and `datetime`:

```python
async def test_the_endpoint_returns_ndjson_the_caller_caused(flow: Flow) -> None:
    created = await flow.client.post("/computers", json={})
    computer_id = created.json()["computer_id"]

    resp = await flow.client.get("/logs")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in resp.text.splitlines()]
    assert lines, "the create logged nothing"
    for entry in lines:
        assert entry["ecs.version"] and entry["@timestamp"] and entry["log.level"]
        assert entry["mshkn.account_id"] == "acct-1"
    assert any(e.get("mshkn.computer_id") == computer_id for e in lines), (
        "a specific computer's life is not reconstructable from the response"
    )


async def test_one_account_never_sees_another(flow: Flow) -> None:
    """The isolation test. Look here first if the filter is ever touched."""
    mine = (await flow.client.post("/computers", json={})).json()["computer_id"]
    theirs = (await flow.other_client.post("/computers", json={})).json()["computer_id"]

    ours = (await flow.client.get("/logs")).text
    assert mine in ours and theirs not in ours

    others = (await flow.other_client.get("/logs")).text
    assert theirs in others and mine not in others


async def test_a_scoped_key_is_refused(flow: Flow) -> None:
    """A scoped key is a narrowing; the account's whole stream would widen it."""
    made = await flow.client.post(
        "/keys", json={"scopes": {"computers": {"create_from": "*"}, "labels": ["verb/"]}}
    )
    assert made.status_code == 200
    secret = made.json()["secret"]
    resp = await flow.client.get("/logs", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 403


async def test_limit_keeps_the_newest_and_since_drops_the_old(flow: Flow) -> None:
    async def read(query: str = "") -> list[dict[str, object]]:
        resp = await flow.client.get(f"/logs{query}")
        assert resp.status_code == 200, resp.text
        return [json.loads(line) for line in resp.text.splitlines()]

    await flow.client.post("/computers", json={})
    everything = await read()
    assert len(everything) >= 2, "this test needs at least two records to order"

    one = await read("?limit=1")
    assert len(one) == 1
    assert one[0]["@timestamp"] >= everything[-1]["@timestamp"], (
        "a truncated response drops the oldest end, not the newest"
    )

    # Assertions are on the boundary rather than on equality with `everything`:
    # a record can land between the two reads, and a test that forbids that is
    # testing the fixture's quietness, not the endpoint.
    cutoff = str(everything[0]["@timestamp"])
    after = await read(f"?since={cutoff}")
    assert all(str(e["@timestamp"]) > cutoff for e in after), "since is exclusive"
    assert len(after) >= len(everything) - 1

    assert (await flow.client.get("/logs?since=not-a-timestamp")).status_code == 422
```

Add `import json` to the module's imports.

- [ ] **Step 2: Run them to verify they fail**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/flow/test_logs.py -q
```

Expected: the four new tests FAIL on `assert 404 == 200` (and the scoped-key one on `404 != 403`). The Task 2 test still passes.

- [ ] **Step 3: Write the router**

Create `src/mshkn/api/logs.py`:

```python
"""GET /logs: the calling account's recent records, as ECS NDJSON.

Its own module rather than a route in api/system.py, because that file's
endpoints are unauthenticated by design — they carry host facts. A log record
carries a tenant's identifiers, so this one takes the account key and returns
nothing the account did not cause.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from mshkn.api.deps import get_runtime, require_account_key
from mshkn.errors import InvalidInput

if TYPE_CHECKING:
    from mshkn.models import Account

router = APIRouter(tags=["logs"])

NDJSON = "application/x-ndjson"

# The module-level Depends that api/keys.py uses. Same reason: a call in a
# default argument is evaluated once at import, and ruff's B008 flags it inline.
_require_account_key = Depends(require_account_key)


def _timestamp(value: str, *, field: str) -> datetime:
    """Parse an ISO 8601 timestamp, treating a naive one as UTC.

    Records are written offset-aware; comparing one to a naive datetime raises,
    so a caller who drops the offset gets UTC rather than a 500.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInput(f"{field} is not an ISO 8601 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@router.get("/logs")
async def read_logs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100_000)] = 100,
    since: Annotated[str | None, Query()] = None,
    account: Account = _require_account_key,
) -> Response:
    """The newest `limit` records this account caused, oldest first.

    `limit` is applied last and keeps the newest end: a truncated response drops
    the oldest records, never the most recent thing that happened.
    """
    # A snapshot. Every logging call appends to this deque, and iterating it
    # live raises "deque mutated during iteration".
    records = [
        r for r in list(get_runtime(request).logs) if r.get("mshkn.account_id") == account.id
    ]
    if since is not None:
        cutoff = _timestamp(since, field="since")
        records = [
            r for r in records if _timestamp(str(r["@timestamp"]), field="@timestamp") > cutoff
        ]
    body = "".join(json.dumps(r, default=str) + "\n" for r in records[-limit:])
    return Response(content=body, media_type=NDJSON)
```

- [ ] **Step 4: Include the router**

In `src/mshkn/app.py`, add the import beside the other routers:

```python
from mshkn.api.logs import router as logs_router
```

and the inclusion, after `keys_router`:

```python
    app.include_router(logs_router)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/flow/test_logs.py -q
```

Expected: PASS, five tests.

- [ ] **Step 6: Run the full gate**

```bash
cd ~/work/mshkn-logs && uv lock --check && uv run --python /usr/bin/python3 ruff check . && uv run --python /usr/bin/python3 ruff format --check . && uv run --python /usr/bin/python3 mypy && uv run --python /usr/bin/python3 pytest --cov
```

Expected: clean, coverage at or above 98 %.

- [ ] **Step 7: Commit**

```bash
cd ~/work/mshkn-logs
git add src/mshkn/api/logs.py src/mshkn/app.py tests/flow/test_logs.py
git commit -m "GET /logs returns the calling account's ECS records (#65)

Account key only: a scoped key is a narrowing, and handing it the account's
whole stream would widen it past every restriction the scopes exist to impose.
limit is applied last and keeps the newest end.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Docs

**Files:**
- Modify: `docs/ARCHITECTURE.md:20`, `docs/ARCHITECTURE.md:68-70`, `docs/ARCHITECTURE.md:252`
- Modify: `docs/plans/README.md`
- Test: `tests/unit/test_docs.py`

`tests/unit/test_docs.py` fails if a route exists in the app and is missing from `docs/ARCHITECTURE.md`. This task is not optional bookkeeping; the gate enforces it.

- [ ] **Step 1: Run the docs test to see it fail**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/unit/test_docs.py -q
```

Expected: FAIL in `test_architecture_lists_every_route_and_metric`, naming `GET /logs`.

- [ ] **Step 2: Add the route to the table**

In `docs/ARCHITECTURE.md`, after the `GET /alerts` row at line 70:

```markdown
| `GET /logs` | the calling account's recent log records, as ECS NDJSON |
```

- [ ] **Step 3: Correct the auth paragraph**

At `docs/ARCHITECTURE.md:20`, the sentence listing routes a scoped key may never call ends with the unauthenticated ones. Add `GET /logs` to the account-key set by replacing that sentence's second half:

```markdown
Routes a scoped key may never call depend on `mshkn.api.deps.require_account_key` instead and answer 403 to one; `GET /logs` is one of them, because a scoped key is a narrowing and the account's whole log stream would widen it. The unauthenticated routes are the ingress trigger and the three system endpoints (`GET /health`, `GET /metrics`, `GET /alerts`).
```

- [ ] **Step 4: Describe it where the other observability endpoints are described**

At `docs/ARCHITECTURE.md:252`, after the sentence describing `GET /alerts`, append:

```markdown
`GET /logs` returns the calling account's recent records as newline-delimited Elastic Common Schema documents — `@timestamp`, `ecs.version`, `log.level`, `log.logger`, `message`, `trace.id` when there was a request, `error.*` on a failure, and everything a call site passed as `extra=` under an `mshkn.` prefix. They come from a bounded in-memory ring on the runtime, sized by `MSHKN_LOG_BUFFER_SIZE` (1000 records by default), and the same documents are what the process writes to stdout. `limit` keeps the newest records and `since` is exclusive. Nothing survives a restart: stdout is the durable path, and a tenant never sees uvicorn's access lines because those are emitted before authentication has run and so belong to no account.
```

- [ ] **Step 5: Flip the plan index**

In `docs/plans/README.md`, in the `## A readable log (2026-09)` section, replace the final sentence:

```markdown
Status: **implemented**. Evidence: `src/mshkn/observability/logging.py`, `src/mshkn/api/logs.py`, `tests/unit/test_observability.py`, `tests/flow/test_logs.py`, `tests/e2e/test_phase11_observability.py`.
```

Every backticked path there now exists, so this is safe; before Task 5 it would have failed `test_every_path_exists`.

Also correct the roadmap table at `docs/plans/README.md:81`, whose P6 row already claims JSON logs are implemented. It is now true in a stronger sense — leave the row, but add the endpoint to its evidence:

```markdown
| P6 metrics, JSON logs, status enrichment, checkpoint DAG, alerts | implemented: `src/mshkn/observability/`, `GET /alerts`, `GET /logs`; Grafana dashboards are not automatable and were never built |
```

- [ ] **Step 6: Run the docs test to verify it passes**

```bash
cd ~/work/mshkn-logs && uv run --python /usr/bin/python3 pytest tests/unit/test_docs.py -q
```

Expected: PASS, 49 tests.

- [ ] **Step 7: Commit**

```bash
cd ~/work/mshkn-logs
git add docs/ARCHITECTURE.md docs/plans/README.md
git commit -m "Document the logs endpoint where the other observability ones live (#65)

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 7: T11.2 stops being a placeholder

**Files:**
- Modify: `tests/e2e/test_phase11_observability.py`

This is the acceptance criterion the whole plan exists to satisfy. It needs a live server.

- [ ] **Step 1: Replace the placeholder**

In `tests/e2e/test_phase11_observability.py`, replace the whole of `TestT112StructuredLogs`:

```python
class TestT112StructuredLogs:
    """Verify the server emits structured ECS logs and will hand them back."""

    async def test_logs_are_ecs_and_reconstruct_a_computer(
        self, client: httpx.AsyncClient
    ) -> None:
        """Every line is an ECS document, and one of them names the computer.

        The last clause is what makes this a test of mshkn rather than of
        json.loads: it proves a specific computer's life is reconstructable
        from the log alone, which is what the plan's T11.3 asked for.
        """
        async with managed_computer(client) as computer_id:
            resp = await client.get("/logs", params={"limit": 500})
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"].startswith("application/x-ndjson")

            lines = resp.text.splitlines()
            assert lines, "the create logged nothing"
            entries = []
            for line in lines:
                entry = json.loads(line)  # a non-JSON line fails here, which is the point
                for field in ("@timestamp", "ecs.version", "log.level", "log.logger", "message"):
                    assert field in entry, f"missing {field} in {entry}"
                entries.append(entry)

            assert any(e.get("mshkn.computer_id") == computer_id for e in entries), (
                f"no record names {computer_id}; the log cannot reconstruct its life"
            )
```

Add `import json` to the module's imports, under `from __future__ import annotations`.

- [ ] **Step 2: Correct the module docstring**

The docstring still says two tests are unimplemented. Replace it:

```python
"""Phase 11: Observability — "Metrics, Logs, and Status"

These tests run against a LIVE server with real Firecracker VMs. Nothing here
is skipped and nothing here is a placeholder.

T11.2 reads the server's own records back over `GET /logs` (see
docs/superpowers/specs/2026-09-13-log-endpoint-design.md). It used to fail as
`Not implemented` even though the formatter had existed since PR2, because this
tier holds an HTTP client and nothing else and had no route to stdout.

The audit-log placeholder left on 2026-09-13 and is now #180. It had no code
behind it at all — no audit table, no audit writer — so it was a feature wearing
a test's clothes. Its test comes back when the feature does.
"""
```

- [ ] **Step 3: Negative control — confirm the test fails without the feature**

A test that passes without the code is the failure mode this is guarding against, and T11.2 has been a placeholder long enough to earn the check.

`scripts/deploy.sh` runs `git checkout -B "$BRANCH" "origin/$BRANCH"` on the server, so it deploys **the pushed branch**, not the working tree. A `git stash` would never reach the server. Deploy `main` instead — it genuinely has no `/logs` — and run the new test from this worktree against it. No scratch branches, nothing to clean up.

```bash
# 1. Put the pre-change server up. ~/work/mshkn is the main worktree.
cd ~/work/mshkn && git checkout main && git pull
MSHKN_SERVER=root@<ip> scripts/deploy.sh

# 2. Run the new test from this branch against it. The test file is local;
#    pytest only needs the URL.
cd ~/work/mshkn-logs
MSHKN_API_URL=http://<ip>:8000 MSHKN_API_KEY=<key> \
  uv run --python /usr/bin/python3 pytest tests/e2e/test_phase11_observability.py -m e2e -k T112 -v
```

Expected: FAIL on `assert 404 == 200`, because `/logs` is not routed. **Stop if it passes.** Stop also if it fails for any other reason — a connection error, a 401, a KeyError — because a different failure means the test is not measuring what it claims, and the control proves nothing.

Record the actual output. It goes in the PR body verbatim.

- [ ] **Step 4: Deploy the branch and run the whole phase**

```bash
cd ~/work/mshkn-logs
MSHKN_SERVER=root@<ip> scripts/e2e.sh tests/e2e/test_phase11_observability.py -m e2e -v
```

`scripts/e2e.sh` pushes the current branch, deploys it, clears orphaned VM resources and ensures the test account before running pytest. Expected: every test in the file passes, T11.2 included.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-logs
git add tests/e2e/test_phase11_observability.py
git commit -m "T11.2 reads the server's own ECS records back (#65)

It asserts every line parses as an ECS document and that one of them names the
computer the test created — the clause that makes it a test of mshkn rather
than of json.loads.

Negative control: with src/ stashed, it fails on a 404 from /logs.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 8: The gate, the branch, the PR

- [ ] **Step 1: Run the full gate one more time**

```bash
cd ~/work/mshkn-logs && uv lock --check && uv run --python /usr/bin/python3 ruff check . && uv run --python /usr/bin/python3 ruff format --check . && uv run --python /usr/bin/python3 mypy && uv run --python /usr/bin/python3 pytest --cov
```

Expected: clean, coverage at or above 98 %.

- [ ] **Step 2: Confirm every file actually landed**

```bash
cd ~/work/mshkn-logs && git status --short && git diff --stat origin/main...HEAD
```

Expected: a clean working tree, and the diff lists all eleven files from the file-structure table. A clean `git status` proves nothing on its own — an edit that never landed leaves the tree clean too. Read the file list.

- [ ] **Step 3: Push**

```bash
cd ~/work/mshkn-logs && git push
```

- [ ] **Step 4: Open the PR**

The body needs four sections. `Substrate:` is mandatory on anything touching `src/`, and this does.

- **Substrate** — a tenant reading back the records of its own API calls, with no idea the embryo exists. ECS because a stock collector ingests it untransformed; the account key because a log record carries tenant identifiers.
- **What this does** — the three decisions from §3 of the spec.
- **Design alignment** — link the spec, name the sections the implementation follows.
- **Validation performed** — paste the gate's real output and the negative control's real output. Evidence, not claims. Never tick a box you did not run.

- [ ] **Step 5: Stop**

Do not merge. Report the PR number and the CI result, and say plainly that nothing is merged.

---

## What this plan does not do

Named here so no one adds them thinking they were forgotten:

- **No streaming or follow.** No SSE, no `?follow=1`, no long poll. A caller wanting a live tail polls with `since`.
- **No persistence.** The ring is memory and dies with the process. stdout is the durable path.
- **No audit log.** #180 is durable, complete and in SQLite. This is a bounded window over whatever happened to be logged.
- **No access lines for tenants.** `configure_logging` sets `propagate = False` on the three uvicorn loggers, so their records never reach the root logger's ring at all — and they are emitted before authentication anyway, so they belong to no account. They stay on stdout for the operator.
- **No multi-worker correctness.** One buffer per process. mshkn runs as a single process and `DEPLOY.md` does not change that.
