# PR 2: Foundations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the shared foundations in place that the host boundary (PR 3) and the service layer (PR 4) build on: typed domain errors mapped to HTTP, a validated `Resources` type, status enums, a `db/` package with pragmas and single row mappers, a generic env-driven `Config`, an `observability/` package with request-id logging and operation metrics, a `BackgroundTasks` registry, and a `Runtime` object plus `create_app(runtime)` factory that removes every module-level mutable global from `src/`.

**Architecture:** Behavior-preserving except where the spec names a change (unknown recipe → 404 instead of 500, bad `needs` → 422 instead of silently defaulting). Routers keep their shape but obtain everything through one `Runtime` reached via `get_runtime(request)`; module globals (`_background_tasks`, `_upload_tasks`, `_rule_rate_limiters`, `_build_locks`, the global `rate_limiter`, `VMManager._bg_tasks`) move onto the `Runtime`. Tests stop mutating a shared `app.state` and instead build a `Runtime` with a temp SQLite database and a mocked `VMManager`, then call `create_app(runtime)`.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, prometheus-client, pytest 9 / pytest-asyncio 1.3, uv, ruff 0.15, mypy 1.19 strict.

**Spec:** `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` §5 (runtime), §7 (data layer), §8 (config and resources), §9 (API contract, error mapping only), §10 (observability), §14 step 2. One spec amendment is made in Task 9 (SQLite `foreign_keys` stays off; see Task 5 for why).

## Global Constraints

- Python `>=3.12`; uv only; every command runs as `uv run <tool>` inside the worktree.
- Local validation, identical to CI: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`. Must be green at the end of every task.
- Runtime behavior is unchanged except: unknown recipe on create → 404; recipe not ready → 409; invalid `needs` → 422; every response keeps FastAPI's `{"detail": ...}` error shape.
- No module-level mutable state remains under `src/mshkn/` when this PR is done (module-level constants, loggers, routers, and Prometheus metric objects are fine; sets/dicts/locks/limiter instances are not).
- Dependency direction: `api → services/vm → db`; nothing under `db/`, `vm/`, `observability/` imports `api`. (The pre-existing `vm/manager.py → api/computers._process_deferred` import is removed in PR 4, not here.)
- Live E2E gate before merge: `MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000 scripts/e2e.sh`, run detached, must report 151 passed, 6 skipped, 0 failed (the PR 1 baseline).
- Commit messages end with the trailer block (Co-Authored-By and Claude-Session lines). Never merge; open the PR and request authorization.

---

## File Structure

**Created**
- `src/mshkn/errors.py` — domain exception hierarchy.
- `src/mshkn/api/errors.py` — exception handlers mapping domain errors to HTTP.
- `src/mshkn/resources.py` — `Resources`, `DEFAULT_RESOURCES`, `Resources.from_needs`.
- `src/mshkn/db/__init__.py` — `connect`, `run_migrations`, re-exports.
- `src/mshkn/db/accounts.py`, `computers.py`, `checkpoints.py`, `recipes.py`, `deferred.py`, `templates.py`, `ingress.py` — one table each, one `COLUMNS` tuple, one `_row_to_*`.
- `migrations/010_indexes.sql`.
- `src/mshkn/observability/__init__.py`, `logging.py`, `metrics.py`.
- `src/mshkn/api/system.py` — `/health`, `/metrics`, `/alerts`.
- `src/mshkn/runtime.py` — `BackgroundTasks`, `Runtime`.
- `src/mshkn/app.py` — `create_app`.
- `src/mshkn/api/deps.py` — `get_runtime`, `require_account`.
- `tests/unit/conftest.py` — `db` fixture, `make_runtime`, `make_app`.
- `tests/unit/test_errors.py`, `test_resources.py`, `test_db_package.py`, `test_config.py`, `test_observability.py`, `test_runtime.py`.

**Modified**
- `src/mshkn/models.py` — `ComputerStatus`, `RecipeStatus`, `DeferredRequest`; status fields typed.
- `src/mshkn/ingress/models.py` — `IngressLogStatus`; `IngressLog.status` typed.
- `src/mshkn/config.py` — generic env mapping.
- `src/mshkn/main.py` — becomes `app = create_app()`.
- `src/mshkn/vm/manager.py` — typed errors, `Resources`, enums, `tasks: BackgroundTasks`.
- `src/mshkn/vm/staging.py` — no change to signatures; receives `mem_size_mib`/`vcpu_count` as before.
- `src/mshkn/recipe/builder.py` — `RecipeStatus`.
- `src/mshkn/api/computers.py`, `checkpoints.py`, `ingress.py`, `recipes.py`, `auth.py` — read state from `Runtime`; globals removed; `timed()` around the main operations.
- `src/mshkn/api/ratelimit.py` — global instance removed.
- `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` — §7 amended (foreign keys).
- Tests listed in Task 4 and Task 8.

**Deleted**
- `src/mshkn/db.py` (replaced by the package), `src/mshkn/ingress/db.py` (moved to `db/ingress.py`), `src/mshkn/logging.py` (moved), `src/mshkn/api/metrics.py` (moved to `observability/metrics.py` + `api/system.py`).

---

### Task 1: Worktree and baseline

**Files:** none.

- [ ] **Step 1: Create the worktree**

Use `superpowers:using-git-worktrees`; the worktree must be `../mshkn-pr2` on branch `pr2-foundations` from `main` (41bcead or later). Then:

```bash
cd ../mshkn-pr2 && uv sync && uv run pytest -q 2>&1 | tail -1
```

Expected: `109 passed, 157 deselected`.

- [ ] **Step 2: Record the baseline**

```bash
{ echo "Baseline before PR 2 (main @ $(git rev-parse --short HEAD))"; uv run ruff check . | tail -1; uv run ruff format --check . | tail -1; uv run mypy | tail -1; uv run pytest -q 2>&1 | tail -1; uv run pytest --cov -q 2>&1 | grep TOTAL; } | tee docs/superpowers/plans/2026-09-04-pr2-baseline.txt
git add docs/superpowers/plans/2026-09-04-pr2-baseline.txt && git commit -m "chore: record pre-PR2 baseline"
```

Expected: all clean, 109 passed, coverage TOTAL 39%.

---

### Task 2: Domain errors and HTTP mapping

**Files:**
- Create: `src/mshkn/errors.py`, `src/mshkn/api/errors.py`, `tests/unit/test_errors.py`
- Modify: `src/mshkn/main.py` (register handlers), `src/mshkn/vm/manager.py` (raise typed errors)

**Interfaces:**
- Produces: `MshknError(message)`, `NotFound`, `Conflict`, `InvalidInput`, `LimitExceeded`, `HostError`, `ConfigError`; `install_error_handlers(app: FastAPI) -> None`. Mapping: NotFound 404, Conflict 409, InvalidInput 422, LimitExceeded 429, HostError 502 (generic detail), anything else in the hierarchy 500.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_errors.py`:

```python
from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mshkn.api.errors import install_error_handlers
from mshkn.errors import (
    ConfigError,
    Conflict,
    HostError,
    InvalidInput,
    LimitExceeded,
    MshknError,
    NotFound,
)


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/raise/{kind}")
    async def _raise(kind: str) -> dict[str, str]:
        errors: dict[str, MshknError] = {
            "not_found": NotFound("recipe rcp-1 not found"),
            "conflict": Conflict("recipe rcp-1 is not ready"),
            "invalid": InvalidInput("ram must end with MB or GB"),
            "limit": LimitExceeded("VM limit reached"),
            "host": HostError("dmsetup create failed: device busy"),
            "config": ConfigError("MSHKN_PORT: invalid literal for int()"),
        }
        raise errors[kind]

    return app


async def _get(kind: str) -> tuple[int, dict[str, str]]:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.get(f"/raise/{kind}")
    return resp.status_code, resp.json()


async def test_not_found_maps_to_404_with_message() -> None:
    assert await _get("not_found") == (404, {"detail": "recipe rcp-1 not found"})


async def test_conflict_maps_to_409() -> None:
    assert await _get("conflict") == (409, {"detail": "recipe rcp-1 is not ready"})


async def test_invalid_input_maps_to_422() -> None:
    assert await _get("invalid") == (422, {"detail": "ram must end with MB or GB"})


async def test_limit_exceeded_maps_to_429() -> None:
    assert await _get("limit") == (429, {"detail": "VM limit reached"})


async def test_host_error_maps_to_502_without_leaking_detail() -> None:
    status, body = await _get("host")
    assert status == 502
    assert body == {"detail": "host operation failed"}


async def test_unmapped_domain_error_is_500() -> None:
    status, body = await _get("config")
    assert status == 500
    assert body == {"detail": "internal error"}


def test_message_attribute_and_str() -> None:
    err = NotFound("x")
    assert err.message == "x"
    assert str(err) == "x"
    assert isinstance(err, MshknError)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_errors.py -q`
Expected: ImportError on `mshkn.errors`.

- [ ] **Step 3: Implement**

`src/mshkn/errors.py`:

```python
"""Domain errors. The API layer maps these to HTTP responses (see api/errors.py)."""

from __future__ import annotations


class MshknError(Exception):
    """Base class for errors that carry a user-facing message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(MshknError):
    """A referenced resource does not exist (or is not visible to the caller)."""


class Conflict(MshknError):
    """The operation is valid but the resource is in the wrong state for it."""


class InvalidInput(MshknError):
    """The request is well-formed but its values are not acceptable."""


class LimitExceeded(MshknError):
    """A per-account or per-key limit was hit."""


class HostError(MshknError):
    """A host-side operation (dm-thin, tap, Firecracker, SSH, rclone) failed."""


class ConfigError(MshknError):
    """Startup configuration is invalid."""
```

`src/mshkn/api/errors.py`:

```python
"""Map domain errors to HTTP responses, keeping FastAPI's {"detail": ...} shape."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mshkn.errors import (
    Conflict,
    HostError,
    InvalidInput,
    LimitExceeded,
    MshknError,
    NotFound,
)

logger = logging.getLogger(__name__)

_STATUS_BY_TYPE: tuple[tuple[type[MshknError], int], ...] = (
    (NotFound, 404),
    (Conflict, 409),
    (InvalidInput, 422),
    (LimitExceeded, 429),
    (HostError, 502),
)


def _status_for(exc: MshknError) -> int:
    for cls, status in _STATUS_BY_TYPE:
        if isinstance(exc, cls):
            return status
    return 500


async def _handle_domain_error(request: Request, exc: MshknError) -> JSONResponse:
    status = _status_for(exc)
    if isinstance(exc, HostError):
        logger.error("host operation failed: %s", exc.message, extra={"path": request.url.path})
        return JSONResponse(status_code=status, content={"detail": "host operation failed"})
    if status == 500:
        logger.error("unmapped domain error: %s", exc.message, extra={"path": request.url.path})
        return JSONResponse(status_code=500, content={"detail": "internal error"})
    return JSONResponse(status_code=status, content={"detail": exc.message})


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(MshknError, _handle_domain_error)  # type: ignore[arg-type]
```

(The `type: ignore[arg-type]` is needed because Starlette types the handler as taking `Exception`; keep it, with this comment.)

In `src/mshkn/main.py`, after the routers are included, add:

```python
from mshkn.api.errors import install_error_handlers
...
install_error_handlers(app)
```

In `src/mshkn/vm/manager.py`, add `from mshkn.errors import Conflict, NotFound` and replace:
- `raise ValueError(f"Recipe {recipe_id} not found")` → `raise NotFound(f"Recipe {recipe_id} not found")`
- `raise ValueError(f"Recipe {recipe_id} is not ready (status={recipe.status})")` → `raise Conflict(...)` (same message)
- `raise ValueError(f"Recipe {recipe_id} has no base volume")` → `raise Conflict(...)`
- in `fork_from_checkpoint`, `raise ValueError(msg)` (checkpoint has no disk snapshot) → `raise Conflict(msg)`
- in `_download_checkpoint_snapshot`, `raise ValueError(f"Checkpoint {checkpoint.id} has no R2 prefix")` → `raise Conflict(...)`
- in `destroy`, `raise ValueError(f"Computer {computer_id} not found")` → `raise NotFound(...)`

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/unit/test_errors.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
```

Expected: 7 passed; clean; `116 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/errors.py src/mshkn/api/errors.py src/mshkn/main.py src/mshkn/vm/manager.py tests/unit/test_errors.py
git commit -m "feat: typed domain errors with HTTP mapping

Unknown recipe on create is now 404 and a not-ready recipe 409 instead
of an unhandled ValueError (500)."
```

---

### Task 3: Resources

**Files:**
- Create: `src/mshkn/resources.py`, `tests/unit/test_resources.py`
- Modify: `src/mshkn/vm/manager.py` (`create(..., resources: Resources)`), `src/mshkn/api/computers.py` (build `Resources` from `body.needs`), `src/mshkn/api/ingress.py` (`_do_create` passes default)

**Interfaces:**
- Produces: `Resources(mem_mib: int = 256, vcpus: int = 2)` frozen; `DEFAULT_RESOURCES`; `Resources.is_default`; `Resources.from_needs(needs: Mapping[str, object] | None) -> Resources` raising `InvalidInput`.
- `VMManager.create(self, account_id: str, recipe_id: str | None = None, resources: Resources = DEFAULT_RESOURCES) -> Computer`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_resources.py`:

```python
from __future__ import annotations

import pytest

from mshkn.errors import InvalidInput
from mshkn.resources import DEFAULT_RESOURCES, Resources


def test_defaults() -> None:
    assert DEFAULT_RESOURCES == Resources(mem_mib=256, vcpus=2)
    assert DEFAULT_RESOURCES.is_default
    assert Resources.from_needs(None) is DEFAULT_RESOURCES
    assert Resources.from_needs({}) is DEFAULT_RESOURCES


@pytest.mark.parametrize(
    ("needs", "expected"),
    [
        ({"ram": "8GB"}, Resources(mem_mib=8192, vcpus=2)),
        ({"ram": "512MB"}, Resources(mem_mib=512, vcpus=2)),
        ({"ram": " 1.5gb "}, Resources(mem_mib=1536, vcpus=2)),
        ({"cores": 4}, Resources(mem_mib=256, vcpus=4)),
        ({"cores": "3"}, Resources(mem_mib=256, vcpus=3)),
        ({"ram": "2GB", "cores": 8}, Resources(mem_mib=2048, vcpus=8)),
    ],
)
def test_from_needs_parses(needs: dict[str, object], expected: Resources) -> None:
    got = Resources.from_needs(needs)
    assert got == expected
    assert not got.is_default


@pytest.mark.parametrize(
    "needs",
    [
        {"ram": "8"},
        {"ram": "8TB"},
        {"ram": 8},
        {"ram": "lots"},
        {"ram": "64MB"},
        {"ram": "33GB"},
        {"cores": 0},
        {"cores": 17},
        {"cores": True},
        {"cores": "two"},
        {"cores": 2.5},
        {"gpu": 1},
    ],
)
def test_from_needs_rejects(needs: dict[str, object]) -> None:
    with pytest.raises(InvalidInput):
        Resources.from_needs(needs)


def test_error_names_the_field() -> None:
    with pytest.raises(InvalidInput, match="ram"):
        Resources.from_needs({"ram": "8TB"})
    with pytest.raises(InvalidInput, match="cores"):
        Resources.from_needs({"cores": 0})
    with pytest.raises(InvalidInput, match="gpu"):
        Resources.from_needs({"gpu": 1})
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_resources.py -q`
Expected: ImportError on `mshkn.resources`.

- [ ] **Step 3: Implement**

`src/mshkn/resources.py`:

```python
"""VM resource requests: parsing and bounds for the API's `needs` field."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mshkn.errors import InvalidInput

if TYPE_CHECKING:
    from collections.abc import Mapping

MIN_MEM_MIB = 128
MAX_MEM_MIB = 32 * 1024
MIN_VCPUS = 1
MAX_VCPUS = 16
_KNOWN_KEYS = frozenset({"ram", "cores"})


@dataclass(frozen=True)
class Resources:
    mem_mib: int = 256
    vcpus: int = 2

    @property
    def is_default(self) -> bool:
        return self == DEFAULT_RESOURCES

    @classmethod
    def from_needs(cls, needs: Mapping[str, object] | None) -> Resources:
        """Parse the API's `needs` dict. Missing or empty means the defaults."""
        if not needs:
            return DEFAULT_RESOURCES
        unknown = sorted(set(needs) - _KNOWN_KEYS)
        if unknown:
            raise InvalidInput(f"unknown needs field(s): {', '.join(unknown)}")
        mem_mib = _parse_ram(needs["ram"]) if "ram" in needs else DEFAULT_RESOURCES.mem_mib
        vcpus = _parse_cores(needs["cores"]) if "cores" in needs else DEFAULT_RESOURCES.vcpus
        if not MIN_MEM_MIB <= mem_mib <= MAX_MEM_MIB:
            raise InvalidInput(f"ram must be between {MIN_MEM_MIB}MB and {MAX_MEM_MIB // 1024}GB")
        if not MIN_VCPUS <= vcpus <= MAX_VCPUS:
            raise InvalidInput(f"cores must be between {MIN_VCPUS} and {MAX_VCPUS}")
        return cls(mem_mib=mem_mib, vcpus=vcpus)


DEFAULT_RESOURCES = Resources()


def _parse_ram(value: object) -> int:
    if not isinstance(value, str):
        raise InvalidInput("ram must be a string like '512MB' or '8GB'")
    raw = value.strip().upper()
    if raw.endswith("GB"):
        number, scale = raw[:-2], 1024
    elif raw.endswith("MB"):
        number, scale = raw[:-2], 1
    else:
        raise InvalidInput("ram must end with MB or GB")
    try:
        amount = float(number)
    except ValueError:
        raise InvalidInput(f"ram value {value!r} is not a number") from None
    if amount <= 0:
        raise InvalidInput("ram must be positive")
    return int(amount * scale)


def _parse_cores(value: object) -> int:
    if isinstance(value, bool):
        raise InvalidInput("cores must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise InvalidInput("cores must be an integer")
```

`src/mshkn/vm/manager.py`: delete `parse_needs`, `_DEFAULT_MEM_MIB`, `_DEFAULT_VCPU`. Change the signature and the first lines of `create`:

```python
    async def create(
        self,
        account_id: str,
        recipe_id: str | None = None,
        resources: Resources = DEFAULT_RESOURCES,
    ) -> Computer:
        custom_resources = not resources.is_default
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
```

and in the cold-boot branch pass `mem_size_mib=resources.mem_mib, vcpu_count=resources.vcpus` (the log line uses the same two values). Import: `from mshkn.resources import DEFAULT_RESOURCES, Resources` (runtime import, not `TYPE_CHECKING`, because the default argument needs the object).

`src/mshkn/api/computers.py`, in `create_computer`, replace `computer = await vm_mgr.create(account.id, recipe_id=body.recipe_id, needs=body.needs)` with:

```python
    resources = Resources.from_needs(body.needs)
    computer = await vm_mgr.create(account.id, recipe_id=body.recipe_id, resources=resources)
```

and import `from mshkn.resources import Resources`. `CreateRequest.needs` stays `dict[str, object] | None`.

`src/mshkn/api/ingress.py`: `_do_create` calls `vm_manager.create(account_id)`; leave it (the default applies).

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/unit/test_resources.py tests/unit/test_vm_manager.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
grep -rn "parse_needs\|_DEFAULT_MEM_MIB\|needs=" src/ || echo "no leftovers"
```

Expected: tests pass; clean; `137 passed`; `no leftovers`.

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/resources.py src/mshkn/vm/manager.py src/mshkn/api/computers.py tests/unit/test_resources.py
git commit -m "feat: Resources type with parsing and bounds; bad needs is 422"
```

---

### Task 4: Status enums and DeferredRequest

**Files:**
- Modify: `src/mshkn/models.py`, `src/mshkn/ingress/models.py`, `src/mshkn/vm/manager.py`, `src/mshkn/api/computers.py`, `src/mshkn/api/ingress.py`, `src/mshkn/api/recipes.py`, `src/mshkn/recipe/builder.py`, `src/mshkn/db.py` (mapper conversions only), `tests/unit/test_models.py`, and every test that constructs `Computer(...)`, `Recipe(...)`, or `IngressLog(...)` with a string status.

**Interfaces:**
- Produces: `ComputerStatus(StrEnum)`: `CREATING="creating"`, `RUNNING="running"`, `DESTROYED="destroyed"`. `RecipeStatus(StrEnum)`: `PENDING`, `BUILDING`, `EXPORTING`, `INJECTING`, `READY`, `FAILED` (lowercase values). `IngressLogStatus(StrEnum)`: `ACCEPTED`, `COMPLETED`, `FAILED`. `DeferredRequest` frozen dataclass: `id, label, account_id, request_payload, created_at` (all `str`). `Computer.status: ComputerStatus`, `Recipe.status: RecipeStatus`, `IngressLog.status: IngressLogStatus`.
- Consumed by Task 5 (mappers construct enums) and Task 8.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_models.py`:

```python
from mshkn.models import ComputerStatus, DeferredRequest, RecipeStatus


def test_status_enums_are_strings() -> None:
    assert ComputerStatus.RUNNING == "running"
    assert str(ComputerStatus.DESTROYED) == "destroyed"
    assert ComputerStatus("running") is ComputerStatus.RUNNING
    assert {s.value for s in RecipeStatus} == {
        "pending", "building", "exporting", "injecting", "ready", "failed",
    }


def test_deferred_request_is_frozen() -> None:
    d = DeferredRequest(
        id="def-1", label="l", account_id="a", request_payload="{}", created_at="t",
    )
    with pytest.raises(FrozenInstanceError):
        d.label = "other"  # type: ignore[misc]
```

with `import pytest` and `from dataclasses import FrozenInstanceError` at the top of the file if not already present.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_models.py -q`
Expected: ImportError on `ComputerStatus`.

- [ ] **Step 3: Implement**

`src/mshkn/models.py` (full file):

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ComputerStatus(StrEnum):
    CREATING = "creating"
    RUNNING = "running"
    DESTROYED = "destroyed"


class RecipeStatus(StrEnum):
    PENDING = "pending"
    BUILDING = "building"
    EXPORTING = "exporting"
    INJECTING = "injecting"
    READY = "ready"
    FAILED = "failed"


@dataclass
class Account:
    id: str
    api_key: str
    vm_limit: int
    created_at: str


@dataclass
class Recipe:
    id: str
    account_id: str
    dockerfile: str
    content_hash: str
    status: RecipeStatus
    build_log: str | None
    base_volume_id: int | None
    template_vmstate: str | None
    template_memory: str | None
    created_at: str
    built_at: str | None


@dataclass
class Computer:
    id: str
    account_id: str
    thin_volume_id: int
    tap_device: str
    vm_ip: str
    socket_path: str
    firecracker_pid: int | None
    manifest_hash: str
    manifest_json: str
    status: ComputerStatus
    created_at: str
    last_exec_at: str | None
    source_checkpoint_id: str | None = None
    recipe_id: str | None = None


@dataclass
class Checkpoint:
    id: str
    account_id: str
    parent_id: str | None
    computer_id: str | None
    thin_volume_id: int | None
    manifest_hash: str
    manifest_json: str
    r2_prefix: str
    disk_delta_size_bytes: int | None
    memory_size_bytes: int | None
    label: str | None
    pinned: bool
    created_at: str
    recipe_id: str | None = None


@dataclass(frozen=True)
class DeferredRequest:
    id: str
    label: str
    account_id: str
    request_payload: str
    created_at: str
```

`src/mshkn/ingress/models.py`: add `from enum import StrEnum` and

```python
class IngressLogStatus(StrEnum):
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"
```

and change `IngressLog.status: str  # "accepted" | ...` to `status: IngressLogStatus`.

Adoption in `src/` (every literal, none may remain; `grep -rnE '"(running|destroyed|creating|pending|building|exporting|injecting|ready|failed|accepted|completed)"' src/mshkn --include='*.py'` must return only SQL strings inside `db.py` queries and the `Literal[...]`/`Field` definitions in `ingress/models.py`):
- `vm/manager.py`: `c.status == "running"` → `c.status == ComputerStatus.RUNNING` (three sites: `initialize`, `reap_dead_vms`, `reap_idle_vms`); `computer.status == "destroyed"` in `destroy` → `ComputerStatus.DESTROYED`; `status="running"` in both `Computer(...)` constructions → `status=ComputerStatus.RUNNING`; `update_computer_status(self.db, computer_id, "destroyed")` (two sites) → `ComputerStatus.DESTROYED`; `recipe.status != "ready"` → `RecipeStatus.READY`.
- `api/computers.py`: `computer.status != "running"` → `ComputerStatus.RUNNING`; `computer.status == "running"` in `computer_status` → same; `computer.status == "destroyed"` (two sites) → `ComputerStatus.DESTROYED`.
- `api/recipes.py`: `status="pending"` → `RecipeStatus.PENDING`.
- `recipe/builder.py`: `update_recipe_status(db, recipe_id, "building")` etc. → `RecipeStatus.BUILDING`, `EXPORTING`, `INJECTING`; `status="ready"` → `RecipeStatus.READY`; `status="failed"` → `RecipeStatus.FAILED`. Change the parameter types of `update_recipe_status(..., status: RecipeStatus)` and `update_recipe_build_result(..., *, status: RecipeStatus, ...)` in `db.py`.
- `api/ingress.py`: the string literals passed to `_log_invocation` (`"failed"`, `"completed"`, `"accepted"`) → `IngressLogStatus.FAILED` etc.; `_log_invocation(..., status: IngressLogStatus, ...)`.
- `db.py` mappers: `status=row[9]` in the four `Computer(...)` constructions → `status=ComputerStatus(row[9])`; `status=row[4]` in the three `Recipe(...)` constructions → `status=RecipeStatus(row[4])`; `update_computer_status(..., status: ComputerStatus)`. `ingress/db.py`: `status=r[2]`/`row[2]` in `IngressLog(...)` → `IngressLogStatus(...)`. `list_deferred_by_label` now returns `list[DeferredRequest]` (construct `DeferredRequest(id=r[0], label=r[1], account_id=r[2], request_payload=r[3], created_at=r[4])`); update `_process_deferred` in `api/computers.py` to use `d.request_payload` instead of `d["request_payload"]`, and its parameter type to `list[DeferredRequest]`.

Tests: every `status="running"` / `status="destroyed"` / `status: str = "running"` in `tests/unit/*.py` becomes the enum (`ComputerStatus.RUNNING` etc.; parameter annotations become `status: ComputerStatus = ComputerStatus.RUNNING`). `Recipe(... status="ready")` → `RecipeStatus.READY`. `IngressLog(... status="accepted")` → `IngressLogStatus.ACCEPTED`. Assertions like `assert result.status == "destroyed"` may stay (StrEnum equality with str holds) but prefer the enum. Run mypy to find every site.

- [ ] **Step 4: Verify**

```bash
uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
grep -rnE '"(running|destroyed|creating|pending|building|exporting|injecting|ready|failed|accepted|completed)"' src/mshkn --include='*.py' | grep -vE "db\.py:.*(SELECT|WHERE|status !=|status =)|ingress/models\.py"
```

Expected: clean; `139 passed`; the grep prints nothing.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "refactor: StrEnum statuses and a DeferredRequest dataclass"
```

---

### Task 5: `db/` package, pragmas, migration runner, indexes

**Files:**
- Create: `src/mshkn/db/__init__.py`, `accounts.py`, `computers.py`, `checkpoints.py`, `recipes.py`, `deferred.py`, `templates.py`, `ingress.py`, `migrations/010_indexes.sql`, `tests/unit/test_db_package.py`
- Delete: `src/mshkn/db.py`, `src/mshkn/ingress/db.py`
- Modify: `src/mshkn/api/ingress.py` and `tests/unit/test_ingress.py` (import path `mshkn.db.ingress`), `tests/unit/test_db.py` (add index assertion)

**Interfaces:**
- Produces: `mshkn.db.connect(path: Path | str) -> aiosqlite.Connection` (WAL, `synchronous=NORMAL`, `busy_timeout=5000`); `mshkn.db.run_migrations(db, migrations_dir)` using `executescript`; every function that existed in `mshkn.db` and `mshkn.ingress.db` keeps its name and signature and is importable from `mshkn.db` (and from its table module). Each table module exposes `COLUMNS: tuple[str, ...]` and `_row_to_<x>(row: Sequence[object]) -> X`.
- **Spec deviation, decided here:** `PRAGMA foreign_keys=ON` is NOT enabled. `computers.source_checkpoint_id REFERENCES checkpoints(id)` and `computers.recipe_id REFERENCES recipes(id)` have no `ON DELETE` action, and destroyed computer rows are never deleted, so enforcement would make `delete_checkpoint`, `prune_checkpoints`, and recipe deletion fail with FK violations. Fixing that needs table rebuilds, which the additive-migration rule forbids. Task 9 amends the spec.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_db_package.py`:

```python
from __future__ import annotations

from pathlib import Path

import aiosqlite

from mshkn.db import connect, run_migrations
from mshkn.db.computers import COLUMNS as COMPUTER_COLUMNS
from mshkn.db.computers import _row_to_computer
from mshkn.models import ComputerStatus


async def _pragma(db: aiosqlite.Connection, name: str) -> object:
    cursor = await db.execute(f"PRAGMA {name}")
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def test_connect_sets_pragmas(tmp_path: Path) -> None:
    db = await connect(tmp_path / "t.db")
    try:
        assert await _pragma(db, "journal_mode") == "wal"
        assert await _pragma(db, "synchronous") == 1  # NORMAL
        assert await _pragma(db, "busy_timeout") == 5000
        assert await _pragma(db, "foreign_keys") == 0  # deliberately off, see plan Task 5
    finally:
        await db.close()


async def test_migrations_create_indexes(tmp_path: Path) -> None:
    db = await connect(tmp_path / "t.db")
    try:
        await run_migrations(db, Path("migrations"))
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        names = {row[0] for row in await cursor.fetchall()}
    finally:
        await db.close()
    assert {
        "idx_computers_account_status",
        "idx_checkpoints_account_created",
        "idx_checkpoints_computer_created",
        "idx_checkpoints_label",
        "idx_deferred_queue_label_created",
    } <= names


async def test_migrations_record_each_file_once(tmp_path: Path) -> None:
    db = await connect(tmp_path / "t.db")
    try:
        await run_migrations(db, Path("migrations"))
        await run_migrations(db, Path("migrations"))
        cursor = await db.execute("SELECT filename FROM _migrations ORDER BY filename")
        applied = [row[0] for row in await cursor.fetchall()]
    finally:
        await db.close()
    expected = sorted(p.name for p in Path("migrations").glob("*.sql"))
    assert applied == expected


def test_row_mapper_uses_column_order() -> None:
    row: list[object] = [None] * len(COMPUTER_COLUMNS)
    row[COMPUTER_COLUMNS.index("id")] = "comp-1"
    row[COMPUTER_COLUMNS.index("account_id")] = "acct-1"
    row[COMPUTER_COLUMNS.index("thin_volume_id")] = 7
    row[COMPUTER_COLUMNS.index("tap_device")] = "tap1"
    row[COMPUTER_COLUMNS.index("vm_ip")] = "172.16.1.2"
    row[COMPUTER_COLUMNS.index("socket_path")] = "/tmp/s"
    row[COMPUTER_COLUMNS.index("manifest_hash")] = "none"
    row[COMPUTER_COLUMNS.index("manifest_json")] = "{}"
    row[COMPUTER_COLUMNS.index("status")] = "running"
    row[COMPUTER_COLUMNS.index("created_at")] = "t"
    computer = _row_to_computer(row)
    assert computer.id == "comp-1"
    assert computer.thin_volume_id == 7
    assert computer.status is ComputerStatus.RUNNING
    assert computer.recipe_id is None
```

Also add to `tests/unit/test_db.py::test_migrations_apply`: `assert "deferred_queue" in tables`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_db_package.py -q`
Expected: ImportError (`connect` does not exist).

- [ ] **Step 3: Implement**

`migrations/010_indexes.sql`:

```sql
-- 010_indexes.sql
-- Indexes for the hot queries: computers by account+status, checkpoints by
-- account/computer/label ordered by created_at, deferred queue by label.
CREATE INDEX IF NOT EXISTS idx_computers_account_status ON computers(account_id, status);
CREATE INDEX IF NOT EXISTS idx_checkpoints_account_created ON checkpoints(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checkpoints_computer_created ON checkpoints(computer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checkpoints_label ON checkpoints(label);
CREATE INDEX IF NOT EXISTS idx_deferred_queue_label_created ON deferred_queue(label, created_at);
```

`src/mshkn/db/__init__.py`:

```python
"""SQLite access: connection setup, migrations, and one module per table.

Every query function is importable from this package so callers do not need
to know which table module owns it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from mshkn.db.accounts import get_account_by_id, get_account_by_key, insert_account
from mshkn.db.checkpoints import (
    delete_checkpoint,
    get_checkpoint,
    get_latest_checkpoint_for_computer,
    get_max_checkpoint_volume_id,
    insert_checkpoint,
    list_account_ids_with_checkpoints,
    list_checkpoints_by_account,
    list_prunable_checkpoints,
)
from mshkn.db.computers import (
    count_active_computers_by_account,
    get_active_computer_for_label,
    get_computer,
    insert_computer,
    list_all_computers,
    update_computer_status,
    update_last_exec_at,
)
from mshkn.db.deferred import delete_deferred_by_label, insert_deferred, list_deferred_by_label
from mshkn.db.ingress import (
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
from mshkn.db.recipes import (
    count_recipe_references,
    delete_failed_recipes_by_hash,
    delete_recipe,
    get_max_recipe_volume_id,
    get_recipe,
    get_recipe_by_content_hash,
    insert_recipe,
    list_recipes_by_account,
    update_recipe_build_result,
    update_recipe_status,
    update_recipe_template,
)
from mshkn.db.templates import cache_bare_template, get_bare_template

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "cache_bare_template",
    "connect",
    "count_active_computers_by_account",
    "count_recipe_references",
    "delete_checkpoint",
    "delete_deferred_by_label",
    "delete_failed_recipes_by_hash",
    "delete_ingress_rule",
    "delete_recipe",
    "get_account_by_id",
    "get_account_by_key",
    "get_active_computer_for_label",
    "get_bare_template",
    "get_checkpoint",
    "get_computer",
    "get_ingress_rule_by_id",
    "get_latest_checkpoint_for_computer",
    "get_max_checkpoint_volume_id",
    "get_max_recipe_volume_id",
    "get_recipe",
    "get_recipe_by_content_hash",
    "insert_account",
    "insert_checkpoint",
    "insert_computer",
    "insert_deferred",
    "insert_ingress_log",
    "insert_ingress_rule",
    "insert_recipe",
    "list_account_ids_with_checkpoints",
    "list_all_computers",
    "list_checkpoints_by_account",
    "list_deferred_by_label",
    "list_ingress_logs",
    "list_ingress_rules_by_account",
    "list_prunable_checkpoints",
    "list_recipes_by_account",
    "prune_old_ingress_logs",
    "rotate_ingress_rule_id",
    "run_migrations",
    "update_computer_status",
    "update_ingress_rule",
    "update_last_exec_at",
    "update_recipe_build_result",
    "update_recipe_status",
    "update_recipe_template",
]


async def connect(path: Path | str) -> aiosqlite.Connection:
    """Open the database with the pragmas the service relies on.

    WAL for concurrent readers and Litestream; NORMAL sync is durable enough
    under WAL and much faster; a busy timeout so concurrent writers wait
    instead of failing. Foreign keys stay OFF on purpose: the schema has
    REFERENCES without ON DELETE actions and destroyed rows are retained, so
    enforcement would break checkpoint deletion and pruning.
    """
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=5000")
    return db


async def run_migrations(db: aiosqlite.Connection, migrations_dir: Path) -> None:
    """Apply every *.sql file in name order that is not yet recorded in _migrations."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations "
        "(id INTEGER PRIMARY KEY, filename TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    await db.commit()
    cursor = await db.execute("SELECT filename FROM _migrations")
    applied = {row[0] for row in await cursor.fetchall()}
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        if sql_file.name in applied:
            continue
        # 001 creates _migrations itself; we already did, so make it idempotent.
        sql = sql_file.read_text().replace(
            "CREATE TABLE _migrations", "CREATE TABLE IF NOT EXISTS _migrations"
        )
        await db.executescript(sql)
        await db.execute("INSERT INTO _migrations (filename) VALUES (?)", (sql_file.name,))
        await db.commit()
```

Table modules. Each has this shape; the example is `src/mshkn/db/computers.py` in full, and the other modules follow it with their own columns and functions (the function bodies are the existing ones from `db.py`, with the hand-written `Computer(id=row[0], ...)` constructions replaced by the mapper):

```python
"""computers table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import Computer, ComputerStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "thin_volume_id",
    "tap_device",
    "vm_ip",
    "socket_path",
    "firecracker_pid",
    "manifest_hash",
    "manifest_json",
    "status",
    "created_at",
    "last_exec_at",
    "source_checkpoint_id",
    "recipe_id",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM computers"


def _row_to_computer(row: Sequence[object]) -> Computer:
    d = dict(zip(COLUMNS, row, strict=True))
    return Computer(
        id=str(d["id"]),
        account_id=str(d["account_id"]),
        thin_volume_id=int(d["thin_volume_id"]),  # type: ignore[call-overload]
        tap_device=str(d["tap_device"]),
        vm_ip=str(d["vm_ip"]),
        socket_path=str(d["socket_path"]),
        firecracker_pid=None if d["firecracker_pid"] is None else int(d["firecracker_pid"]),  # type: ignore[call-overload]
        manifest_hash=str(d["manifest_hash"]),
        manifest_json=str(d["manifest_json"]),
        status=ComputerStatus(str(d["status"])),
        created_at=str(d["created_at"]),
        last_exec_at=None if d["last_exec_at"] is None else str(d["last_exec_at"]),
        source_checkpoint_id=(
            None if d["source_checkpoint_id"] is None else str(d["source_checkpoint_id"])
        ),
        recipe_id=None if d["recipe_id"] is None else str(d["recipe_id"]),
    )


async def insert_computer(db: aiosqlite.Connection, computer: Computer) -> None:
    await db.execute(
        "INSERT INTO computers (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        (
            computer.id,
            computer.account_id,
            computer.thin_volume_id,
            computer.tap_device,
            computer.vm_ip,
            computer.socket_path,
            computer.firecracker_pid,
            computer.manifest_hash,
            computer.manifest_json,
            computer.status,
            computer.created_at,
            computer.last_exec_at,
            computer.source_checkpoint_id,
            computer.recipe_id,
        ),
    )
    await db.commit()


async def get_computer(db: aiosqlite.Connection, computer_id: str) -> Computer | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (computer_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_computer(row)


async def list_all_computers(db: aiosqlite.Connection) -> list[Computer]:
    """Return all non-destroyed computers across all accounts."""
    cursor = await db.execute(_SELECT + " WHERE status != 'destroyed'")
    return [_row_to_computer(r) for r in await cursor.fetchall()]


async def count_active_computers_by_account(db: aiosqlite.Connection, account_id: str) -> int:
    """Count non-destroyed computers for the given account."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM computers WHERE account_id = ? AND status != 'destroyed'",
        (account_id,),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def update_computer_status(
    db: aiosqlite.Connection, computer_id: str, status: ComputerStatus
) -> None:
    await db.execute("UPDATE computers SET status = ? WHERE id = ?", (status, computer_id))
    await db.commit()


async def update_last_exec_at(db: aiosqlite.Connection, computer_id: str, timestamp: str) -> None:
    await db.execute(
        "UPDATE computers SET last_exec_at = ? WHERE id = ?", (timestamp, computer_id)
    )
    await db.commit()


async def get_active_computer_for_label(
    db: aiosqlite.Connection, account_id: str, label: str
) -> Computer | None:
    """Return a running computer whose source checkpoint has the given label, or None."""
    cols = ", ".join("c." + c for c in COLUMNS)
    cursor = await db.execute(
        f"SELECT {cols} FROM computers c "
        "INNER JOIN checkpoints ck ON c.source_checkpoint_id = ck.id "
        "WHERE c.account_id = ? AND c.status = 'running' AND ck.label = ? LIMIT 1",
        (account_id, label),
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_computer(row)
```

If mypy rejects the `int(d[...])` calls differently from the `type: ignore` codes shown, use `typing.cast` instead: `int(cast(int, d["thin_volume_id"]))`; either way, no bare `Any` and no untyped functions.

The other modules:
- `accounts.py`: `COLUMNS = ("id", "api_key", "vm_limit", "created_at")`, `_row_to_account`, `insert_account`, `get_account_by_id`, `get_account_by_key`.
- `checkpoints.py`: `COLUMNS = ("id", "account_id", "parent_id", "computer_id", "thin_volume_id", "manifest_hash", "manifest_json", "r2_prefix", "disk_delta_size_bytes", "memory_size_bytes", "label", "pinned", "created_at", "recipe_id")`, `_row_to_checkpoint` (`pinned=bool(d["pinned"])`), `insert_checkpoint`, `get_checkpoint`, `list_checkpoints_by_account`, `get_latest_checkpoint_for_computer`, `get_max_checkpoint_volume_id`, `delete_checkpoint`, `list_prunable_checkpoints`, `list_account_ids_with_checkpoints`.
- `recipes.py`: `COLUMNS = ("id", "account_id", "dockerfile", "content_hash", "status", "build_log", "base_volume_id", "template_vmstate", "template_memory", "created_at", "built_at")`, `_row_to_recipe` (`status=RecipeStatus(...)`), `insert_recipe`, `get_recipe`, `get_recipe_by_content_hash`, `list_recipes_by_account`, `update_recipe_status`, `update_recipe_build_result`, `update_recipe_template`, `delete_recipe`, `delete_failed_recipes_by_hash`, `get_max_recipe_volume_id`, `count_recipe_references`.
- `deferred.py`: `COLUMNS = ("id", "label", "account_id", "request_payload", "created_at")`, `_row_to_deferred`, `insert_deferred`, `list_deferred_by_label -> list[DeferredRequest]`, `delete_deferred_by_label`.
- `templates.py`: `get_bare_template`, `cache_bare_template` (no dataclass; keep the tuple return).
- `ingress.py`: the whole of the old `src/mshkn/ingress/db.py` with `COLUMNS` for `ingress_rules` (`"internal_id", "id", "account_id", "name", "starlark_source", "response_mode", "max_body_bytes", "rate_limit_rpm", "enabled", "created_at", "updated_at"`) and `LOG_COLUMNS` for `ingress_log` (`"id", "rule_internal_id", "status", "starlark_result", "error_message", "created_at"`), `_row_to_rule` (`enabled=bool(...)`), `_row_to_log` (`status=IngressLogStatus(...)`).

Delete `src/mshkn/db.py` and `src/mshkn/ingress/db.py`. Update `from mshkn.ingress.db import ...` → `from mshkn.db.ingress import ...` in `src/mshkn/api/ingress.py` and `tests/unit/test_ingress.py`. `src/mshkn/vm/manager.py` has several function-local `from mshkn.db import ...`; they keep working (leave them; PR 4 removes them).

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/unit/test_db_package.py tests/unit/test_db.py tests/unit/test_recipe_db.py tests/unit/test_ingress.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
test ! -e src/mshkn/db.py && test ! -e src/mshkn/ingress/db.py && echo "old modules gone"
```

Expected: pass; clean; `143 passed`; `old modules gone`.

- [ ] **Step 5: Commit**

```bash
git add -A src migrations tests
git commit -m "refactor: db package with connect() pragmas, executescript migrations, one mapper per table

Adds migration 010 (indexes for the hot queries). foreign_keys stays off:
the schema's REFERENCES have no ON DELETE actions and destroyed rows are
retained, so enforcement would break checkpoint deletion and pruning."
```

---

### Task 6: Generic env-driven Config

**Files:**
- Modify: `src/mshkn/config.py`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Config.from_env(environ: Mapping[str, str] | None = None) -> Config`. Every field maps from `MSHKN_<FIELD_UPPER>`; aliases `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `MSHKN_IDLE_TIMEOUT` (→ `idle_timeout_seconds`), `MSHKN_CHECKPOINT_RETENTION` (→ `checkpoint_retention_count`) keep working and win over the generic name. Parsing by annotated type: `int`, `str`, `Path`, `bool` (`1/true/yes/on` vs `0/false/no/off`, case-insensitive). Bad values raise `ConfigError` naming the variable.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_config.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mshkn.config import Config
from mshkn.errors import ConfigError


def test_defaults_when_env_is_empty() -> None:
    cfg = Config.from_env({})
    assert cfg == Config()


def test_generic_names_map_every_field() -> None:
    cfg = Config.from_env(
        {
            "MSHKN_PORT": "9000",
            "MSHKN_DB_PATH": "/var/lib/mshkn/x.db",
            "MSHKN_THIN_POOL_NAME": "pool2",
            "MSHKN_THIN_VOLUME_SECTORS": "1234",
            "MSHKN_SSH_KEY_PATH": "/root/.ssh/other",
            "MSHKN_DOMAIN": "example.test",
        }
    )
    assert cfg.port == 9000
    assert cfg.db_path == Path("/var/lib/mshkn/x.db")
    assert cfg.thin_pool_name == "pool2"
    assert cfg.thin_volume_sectors == 1234
    assert cfg.ssh_key_path == Path("/root/.ssh/other")
    assert cfg.domain == "example.test"


def test_aliases_keep_working_and_win() -> None:
    cfg = Config.from_env(
        {
            "R2_ENDPOINT": "https://r2.example",
            "R2_ACCESS_KEY_ID": "k",
            "R2_SECRET_ACCESS_KEY": "s",
            "R2_BUCKET": "b",
            "MSHKN_IDLE_TIMEOUT": "120",
            "MSHKN_IDLE_TIMEOUT_SECONDS": "999",
            "MSHKN_CHECKPOINT_RETENTION": "5",
        }
    )
    assert cfg.r2_endpoint == "https://r2.example"
    assert cfg.r2_access_key_id == "k"
    assert cfg.r2_secret_access_key == "s"
    assert cfg.r2_bucket == "b"
    assert cfg.idle_timeout_seconds == 120
    assert cfg.checkpoint_retention_count == 5


@pytest.mark.parametrize(
    ("var", "value"),
    [("MSHKN_PORT", "eighty"), ("MSHKN_IDLE_TIMEOUT", "1.5"), ("MSHKN_THIN_VOLUME_SECTORS", "")],
)
def test_bad_values_raise_config_error_naming_the_variable(var: str, value: str) -> None:
    with pytest.raises(ConfigError, match=var):
        Config.from_env({var: value})


def test_unknown_mshkn_variables_are_ignored() -> None:
    assert Config.from_env({"MSHKN_NOT_A_FIELD": "1"}) == Config()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: `test_generic_names_map_every_field` and the alias/bad-value tests fail (`from_env` takes no argument today).

- [ ] **Step 3: Implement**

Replace `Config.from_env` and add helpers in `src/mshkn/config.py` (keep the dataclass fields exactly as they are):

```python
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, get_type_hints

from mshkn.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

# Variables that predate the generic MSHKN_<FIELD> rule. They win over the generic name.
_ALIASES: dict[str, str] = {
    "R2_ENDPOINT": "r2_endpoint",
    "R2_ACCESS_KEY_ID": "r2_access_key_id",
    "R2_SECRET_ACCESS_KEY": "r2_secret_access_key",
    "R2_BUCKET": "r2_bucket",
    "MSHKN_IDLE_TIMEOUT": "idle_timeout_seconds",
    "MSHKN_CHECKPOINT_RETENTION": "checkpoint_retention_count",
}
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
```

and, inside the class, replacing the old `from_env`:

```python
    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        """Build a Config from environment variables.

        Every field reads MSHKN_<FIELD_UPPER>; the aliases in _ALIASES are
        honored too and take precedence. Values are parsed by the field's
        annotated type; failures raise ConfigError naming the variable.
        """
        env = os.environ if environ is None else environ
        hints = get_type_hints(cls)
        kwargs: dict[str, object] = {}
        for f in fields(cls):
            var = f"MSHKN_{f.name.upper()}"
            raw = env.get(var)
            if raw is not None:
                kwargs[f.name] = _parse(var, raw, hints[f.name])
        for var, name in _ALIASES.items():
            raw = env.get(var)
            if raw is not None:
                kwargs[name] = _parse(var, raw, hints[name])
        return cls(**kwargs)  # type: ignore[arg-type]


def _parse(var: str, raw: str, kind: object) -> object:
    try:
        if kind is int:
            return int(raw)
        if kind is bool:
            lowered = raw.strip().lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError(f"expected a boolean, got {raw!r}")
        if kind is Path:
            if not raw:
                raise ValueError("empty path")
            return Path(raw)
        if kind is str:
            return raw
    except ValueError as exc:
        raise ConfigError(f"{var}: {exc}") from None
    raise ConfigError(f"{var}: unsupported field type {kind!r}")
```

(`get_type_hints` resolves the string annotations produced by `from __future__ import annotations`. The `type: ignore[arg-type]` on `cls(**kwargs)` is because mypy cannot see that the dict's values match the fields; keep it with this comment.)

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/unit/test_config.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
```

Expected: 6 passed; clean; `149 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/config.py tests/unit/test_config.py
git commit -m "feat: generic MSHKN_<FIELD> config mapping with typed parsing and ConfigError"
```

---

### Task 7: Observability package and system router

**Files:**
- Create: `src/mshkn/observability/__init__.py` (empty docstring), `src/mshkn/observability/logging.py`, `src/mshkn/observability/metrics.py`, `src/mshkn/api/system.py`, `tests/unit/test_observability.py`
- Delete: `src/mshkn/logging.py`, `src/mshkn/api/metrics.py`
- Modify: `src/mshkn/main.py` (use the new modules; middleware sets the request-id contextvar; `/health` and `/alerts` move to `api/system.py`), `src/mshkn/api/computers.py` and `checkpoints.py` (import metrics from the new module; wrap operations in `timed`), `tests/unit/test_metrics.py` (new names), `tests/unit/test_health.py` (unchanged behavior)

**Interfaces:**
- Produces: `observability.logging`: `JSONFormatter`, `request_id_var: ContextVar[str]` (default `"-"`), `RequestIdFilter`, `configure_logging(level: int = logging.INFO) -> None` (idempotent). `observability.metrics`: existing `computers_active`, `computers_created_total`, `checkpoints_total`, `exec_duration_seconds` unchanged; new `operation_duration_seconds` (Histogram, label `op`), `operation_errors_total` (Counter, labels `op`, `kind` ∈ `domain|host|unexpected`), `thin_pool_used_ratio` (Gauge, label `kind` ∈ `data|metadata`), `host_ram_used_ratio` (Gauge); `timed(op: str)` async context manager. `api/system.py`: `router` with `GET /health`, `GET /metrics`, `GET /alerts`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_observability.py`:

```python
from __future__ import annotations

import json
import logging

import pytest
from prometheus_client import generate_latest

from mshkn.errors import HostError, NotFound
from mshkn.observability.logging import JSONFormatter, RequestIdFilter, request_id_var
from mshkn.observability.metrics import (
    operation_duration_seconds,
    operation_errors_total,
    timed,
)


def _format(record: logging.LogRecord) -> dict[str, object]:
    RequestIdFilter().filter(record)
    result: dict[str, object] = json.loads(JSONFormatter().format(record))
    return result


def test_json_formatter_includes_request_id_from_context() -> None:
    token = request_id_var.set("req-123")
    try:
        record = logging.LogRecord("t", logging.INFO, "f.py", 1, "hello %s", ("w",), None)
        entry = _format(record)
    finally:
        request_id_var.reset(token)
    assert entry["msg"] == "hello w"
    assert entry["request_id"] == "req-123"
    assert entry["level"] == "info"


def test_request_id_defaults_to_dash_outside_a_request() -> None:
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "x", None, None)
    assert _format(record)["request_id"] == "-"


def test_extra_fields_are_emitted() -> None:
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "x", None, None)
    record.computer_id = "comp-1"
    assert _format(record)["computer_id"] == "comp-1"


def _sample(metric_text: str, name: str, labels: str) -> float:
    for line in metric_text.splitlines():
        if line.startswith(f"{name}{{{labels}}}"):
            return float(line.split()[-1])
    return 0.0


async def test_timed_observes_duration_and_counts_domain_errors() -> None:
    before = _sample(generate_latest().decode(), "mshkn_operation_duration_seconds_count", 'op="unit_test"')
    async with timed("unit_test"):
        pass
    with pytest.raises(NotFound):
        async with timed("unit_test"):
            raise NotFound("x")
    with pytest.raises(HostError):
        async with timed("unit_test"):
            raise HostError("y")
    with pytest.raises(RuntimeError):
        async with timed("unit_test"):
            raise RuntimeError("z")
    text = generate_latest().decode()
    assert _sample(text, "mshkn_operation_duration_seconds_count", 'op="unit_test"') == before + 4
    assert _sample(text, "mshkn_operation_errors_total", 'kind="domain",op="unit_test"') == 1
    assert _sample(text, "mshkn_operation_errors_total", 'kind="host",op="unit_test"') == 1
    assert _sample(text, "mshkn_operation_errors_total", 'kind="unexpected",op="unit_test"') == 1
    assert operation_duration_seconds is not None and operation_errors_total is not None
```

Update `tests/unit/test_metrics.py::test_metrics_contains_expected_names` to also assert `"mshkn_operation_duration_seconds" in text`, `"mshkn_operation_errors_total" in text`, `"mshkn_thin_pool_used_ratio" in text`, and `"mshkn_host_ram_used_ratio" in text`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_observability.py -q`
Expected: ImportError on `mshkn.observability`.

- [ ] **Step 3: Implement**

`src/mshkn/observability/__init__.py`:

```python
"""Structured logging and Prometheus metrics."""
```

`src/mshkn/observability/logging.py`:

```python
"""JSON log formatting with a per-request id carried in a contextvar."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("mshkn_request_id", default="-")

_CONFIGURED_MARKER = "_mshkn_configured"


class RequestIdFilter(logging.Filter):
    """Stamp every record with the current request id (or "-")."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    _BUILTIN_ATTRS = frozenset(
        logging.LogRecord("", 0, "", 0, None, None, None).__dict__.keys() | {"message", "asctime"}
    )

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS and key != "request_id":
                entry[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Route the root and uvicorn loggers through the JSON formatter. Idempotent."""
    root = logging.root
    if getattr(root, _CONFIGURED_MARKER, False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    handler.addFilter(RequestIdFilter())
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False
    setattr(root, _CONFIGURED_MARKER, True)  # noqa: B010
```

`src/mshkn/observability/metrics.py`:

```python
"""Prometheus metrics and the timed() helper that feeds them."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram

from mshkn.errors import HostError, MshknError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

computers_active = Gauge("mshkn_computers_active", "Number of currently running VMs")
computers_created_total = Counter(
    "mshkn_computers_created_total", "Total number of computers created"
)
checkpoints_total = Counter("mshkn_checkpoints_total", "Total number of checkpoints created")
exec_duration_seconds = Histogram(
    "mshkn_exec_duration_seconds", "Duration of exec commands in seconds"
)
operation_duration_seconds = Histogram(
    "mshkn_operation_duration_seconds",
    "Duration of orchestrator operations in seconds",
    ["op"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
operation_errors_total = Counter(
    "mshkn_operation_errors_total",
    "Operations that raised, by kind (domain, host, unexpected)",
    ["op", "kind"],
)
thin_pool_used_ratio = Gauge(
    "mshkn_thin_pool_used_ratio", "dm-thin pool usage as a ratio, by kind (data, metadata)", ["kind"]
)
host_ram_used_ratio = Gauge("mshkn_host_ram_used_ratio", "Host RAM in use as a ratio")


@asynccontextmanager
async def timed(op: str) -> AsyncIterator[None]:
    """Observe the duration of an operation and count failures by kind."""
    start = time.monotonic()
    try:
        yield
    except HostError:
        operation_errors_total.labels(op=op, kind="host").inc()
        raise
    except MshknError:
        operation_errors_total.labels(op=op, kind="domain").inc()
        raise
    except Exception:
        operation_errors_total.labels(op=op, kind="unexpected").inc()
        raise
    finally:
        operation_duration_seconds.labels(op=op).observe(time.monotonic() - start)
```

`src/mshkn/api/system.py`:

```python
"""Unauthenticated system endpoints: health, metrics, alerts."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

if TYPE_CHECKING:
    from mshkn.vm.manager import VMManager

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/alerts")
async def alerts(request: Request) -> list[dict[str, object]]:
    """Return recent resource alerts."""
    vm_manager: VMManager = request.app.state.vm_manager
    return [asdict(a) for a in vm_manager.alerts]
```

(Task 8 changes `alerts` to read the manager from the `Runtime`.)

`src/mshkn/main.py`: remove `_configure_logging` and the `/health` and `/alerts` handlers; import `configure_logging` and `request_id_var` from `mshkn.observability.logging`; call `configure_logging()` at module level (Task 8 moves it into `create_app`); include `system_router` instead of `metrics_router`; make the middleware set the contextvar:

```python
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Attach a request id to the response and to every log line during the request."""
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-Id"] = request_id
    return response
```

Metric imports in `api/computers.py`: `from mshkn.observability.metrics import checkpoints_total, computers_active, computers_created_total, exec_duration_seconds, timed`. Wrap: in `create_computer`, `async with timed("create"): computer = await vm_mgr.create(...)`; in `checkpoint_computer`, wrap from the `sync` exec through `insert_checkpoint` in `async with timed("checkpoint"):`; in `destroy_computer`, `async with timed("destroy"): await vm_mgr.destroy(computer_id)`. In `api/checkpoints.py`: `async with timed("fork"): computer = await vm_mgr.fork_from_checkpoint(...)`; in `merge_checkpoints`, wrap the block from `create_snapshot` through the `finally` unmount in `async with timed("merge"):`. In `api/recipes.py`, wrap `await build_recipe(...)` inside `_run_build` in `async with timed("recipe_build"):`. Delete `src/mshkn/logging.py` and `src/mshkn/api/metrics.py`.

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/unit/test_observability.py tests/unit/test_metrics.py tests/unit/test_health.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
test ! -e src/mshkn/logging.py && test ! -e src/mshkn/api/metrics.py && echo "old modules gone"
```

Expected: pass; clean; `153 passed`; `old modules gone`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat: observability package (request-id JSON logging, operation metrics, timed()) and system router"
```

---

### Task 8: BackgroundTasks, Runtime, create_app, and the end of module globals

**Files:**
- Create: `src/mshkn/runtime.py`, `src/mshkn/app.py`, `src/mshkn/api/deps.py`, `tests/unit/conftest.py`, `tests/unit/test_runtime.py`
- Modify: `src/mshkn/main.py`, `src/mshkn/api/auth.py` (delete; folded into `deps.py`), `src/mshkn/api/ratelimit.py` (remove the global instance), `src/mshkn/api/computers.py`, `checkpoints.py`, `ingress.py`, `recipes.py`, `system.py`, `src/mshkn/vm/manager.py`, and tests: `test_auth.py`, `test_health.py`, `test_metrics.py`, `test_exec_on_create.py`, `test_ingress.py`, `test_self_destruct.py`, `test_vm_limit.py`, `test_vm_manager.py`.

**Interfaces:**
- Produces:
  - `BackgroundTasks`: `spawn(coro, *, name: str, key: str | None = None) -> asyncio.Task[Any]`, `async cancel(key: str) -> None`, `async wait(key: str) -> None`, `async drain(timeout: float) -> None`, `__len__`.
  - `Runtime` dataclass: `config: Config`, `db: aiosqlite.Connection`, `vm_manager: VMManager`, `caddy: CaddyClient | None`, `ssh_pool: SSHPool | None`, `tasks: BackgroundTasks`, `rate_limiter: RateLimiter`, `rule_limiters: dict[str, RateLimiter]`, `build_locks: dict[str, asyncio.Lock]`; `@classmethod async from_env() -> Runtime`; `async start()`; `async close()`; `build_lock(account_id) -> asyncio.Lock`; `rule_limiter(rule_id, rpm) -> RateLimiter`.
  - `create_app(runtime: Runtime | None = None) -> FastAPI`. With a runtime given, `app.state.runtime` is set immediately (tests need no lifespan) and the lifespan only calls `start()`/`close()`; with none, the lifespan builds it via `Runtime.from_env()`.
  - `api/deps.py`: `get_runtime(request) -> Runtime`, `require_account(request) -> Account`.
  - `VMManager.__init__(self, config, db, *, caddy=None, ssh_pool=None, tasks: BackgroundTasks | None = None)`; `self.tasks` replaces `self._bg_tasks`.
  - `tests/unit/conftest.py`: `db` fixture (temp DB, migrated, closed after), `make_runtime(db, *, vm_manager: Any = None, config: Config | None = None, ssh_pool: Any = None) -> Runtime`, `make_app(runtime) -> FastAPI`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/conftest.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from mshkn.api.ratelimit import RateLimiter
from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.runtime import BackgroundTasks, Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite
    from fastapi import FastAPI


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(tmp_path / "test.db")
    await run_migrations(conn, Path("migrations"))
    try:
        yield conn
    finally:
        await conn.close()


def make_runtime(
    db: aiosqlite.Connection,
    *,
    vm_manager: Any = None,
    config: Config | None = None,
    ssh_pool: Any = None,
) -> Runtime:
    """A Runtime for API tests: real DB, mocked VMManager, no Caddy, no reaper."""
    return Runtime(
        config=config if config is not None else Config(domain="test.dev"),
        db=db,
        vm_manager=vm_manager if vm_manager is not None else AsyncMock(),
        caddy=None,
        ssh_pool=ssh_pool,
        tasks=BackgroundTasks(),
        rate_limiter=RateLimiter(max_requests=80, window_seconds=10.0),
    )


def make_app(runtime: Runtime) -> FastAPI:
    return create_app(runtime)
```

`tests/unit/test_runtime.py`:

```python
from __future__ import annotations

import asyncio

import pytest

from mshkn.runtime import BackgroundTasks


async def _sleep_then(result: list[str], tag: str, seconds: float) -> None:
    await asyncio.sleep(seconds)
    result.append(tag)


async def test_spawn_tracks_and_forgets_tasks() -> None:
    tasks = BackgroundTasks()
    out: list[str] = []
    tasks.spawn(_sleep_then(out, "a", 0.01), name="a")
    assert len(tasks) == 1
    await asyncio.sleep(0.05)
    assert out == ["a"]
    assert len(tasks) == 0


async def test_cancel_by_key_stops_the_task() -> None:
    tasks = BackgroundTasks()
    out: list[str] = []
    tasks.spawn(_sleep_then(out, "slow", 10), name="slow", key="upload:ckpt-1")
    await tasks.cancel("upload:ckpt-1")
    assert out == []
    assert len(tasks) == 0


async def test_wait_by_key_returns_after_completion() -> None:
    tasks = BackgroundTasks()
    out: list[str] = []
    tasks.spawn(_sleep_then(out, "x", 0.01), name="x", key="k")
    await tasks.wait("k")
    assert out == ["x"]
    await tasks.wait("missing")  # no-op


async def test_drain_waits_then_cancels_stragglers() -> None:
    tasks = BackgroundTasks()
    out: list[str] = []
    tasks.spawn(_sleep_then(out, "fast", 0.01), name="fast")
    tasks.spawn(_sleep_then(out, "slow", 10), name="slow")
    await tasks.drain(timeout=0.1)
    assert out == ["fast"]
    assert len(tasks) == 0


async def test_failed_task_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    async def boom() -> None:
        raise RuntimeError("kaboom")

    tasks = BackgroundTasks()
    tasks.spawn(boom(), name="boom")
    await asyncio.sleep(0.01)
    assert len(tasks) == 0
    assert any("boom" in r.getMessage() for r in caplog.records)
```

Convert the seven app-level test files from the shared `app` to `make_runtime`/`make_app`. The pattern, shown for `tests/unit/test_auth.py` (rewrite in full):

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account
from mshkn.models import Account
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite


async def _account(db: aiosqlite.Connection) -> None:
    await insert_account(
        db,
        Account(id="acct-1", api_key="test-key-123", vm_limit=10, created_at="2026-03-08T00:00:00"),
    )


async def test_no_auth_returns_401(db: aiosqlite.Connection) -> None:
    await _account(db)
    app = make_app(make_runtime(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/computers", json={})
    assert resp.status_code == 401


async def test_bad_key_returns_401(db: aiosqlite.Connection) -> None:
    await _account(db)
    app = make_app(make_runtime(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/computers", json={}, headers={"Authorization": "Bearer wrong-key"}
        )
    assert resp.status_code == 401


async def test_health_no_auth_required(db: aiosqlite.Connection) -> None:
    app = make_app(make_runtime(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
```

`tests/unit/test_health.py` and `test_metrics.py`: same shape (`app = make_app(make_runtime(db))`). In `test_vm_limit.py`, `test_exec_on_create.py`, `test_self_destruct.py`, `test_ingress.py`: every block of the form

```python
    app.state.db = db
    app.state.config = MagicMock(domain="test.dev")   # or _FakeConfig()
    app.state.vm_manager = vm_mgr
    transport = ASGITransport(app=app)
```

becomes

```python
    app = make_app(make_runtime(db, vm_manager=vm_mgr))
    transport = ASGITransport(app=app)
```

with `db` coming from the `db` fixture (drop the file-local `_setup`/`_setup_app_db` helpers that opened connections; keep whatever they inserted, as a helper that takes `db`). Where a test set `app.state.ssh_pool`, pass `ssh_pool=` to `make_runtime`. `from mshkn.main import app` disappears from every test. `MagicMock(domain=...)` and `_FakeConfig` disappear (a real `Config(domain="test.dev")` is used; tests asserting a URL use `https://comp-1.test.dev`). In `test_ingress.py`, the tests that constructed `_FakeConfig(domain="mshkn.dev")` expect `ingress_url` under `mshkn.dev`: pass `config=Config()` (its default domain is `mshkn.dev`).

`tests/unit/test_vm_manager.py`: add `manager.tasks = BackgroundTasks()` after the other attribute assignments in both tests (import from `mshkn.runtime`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_runtime.py -q`
Expected: ImportError on `mshkn.runtime`.

- [ ] **Step 3: Implement**

`src/mshkn/runtime.py`:

```python
"""Process-wide state: the Runtime object and the BackgroundTasks registry.

There are no module-level mutable globals in mshkn; everything that used to
be one lives here, is built once in the app lifespan (or by a test), and is
reached through api.deps.get_runtime().
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from mshkn.api.ratelimit import RateLimiter
from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.proxy.caddy import CaddyClient
from mshkn.vm.manager import VMManager
from mshkn.vm.ssh import SSHPool

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import aiosqlite

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_SECONDS = 30.0


class BackgroundTasks:
    """Owns background asyncio tasks: keeps strong references, logs failures,
    lets callers cancel or await a task by key, and drains on shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._keyed: dict[str, asyncio.Task[Any]] = {}

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
        key: str | None = None,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        if key is not None:
            self._keyed[key] = task
        task.add_done_callback(partial(self._on_done, key))
        return task

    def _on_done(self, key: str | None, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if key is not None and self._keyed.get(key) is task:
            del self._keyed[key]
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "background task %s failed: %s", task.get_name(), exc, exc_info=exc
            )

    async def cancel(self, key: str) -> None:
        """Cancel the task registered under key (if any) and wait for it to finish."""
        task = self._keyed.pop(key, None)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def wait(self, key: str) -> None:
        """Wait for the task registered under key (if any) to finish."""
        task = self._keyed.get(key)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def drain(self, timeout: float) -> None:
        """Wait up to timeout for outstanding tasks, then cancel whatever is left."""
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return
        _done, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    def __len__(self) -> int:
        return len(self._tasks)


@dataclass
class Runtime:
    config: Config
    db: aiosqlite.Connection
    vm_manager: VMManager
    caddy: CaddyClient | None
    ssh_pool: SSHPool | None
    tasks: BackgroundTasks
    rate_limiter: RateLimiter
    rule_limiters: dict[str, RateLimiter] = field(default_factory=dict)
    build_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    @classmethod
    async def from_env(cls) -> Runtime:
        config = Config.from_env()
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await connect(config.db_path)
        await run_migrations(db, config.migrations_dir)
        caddy = CaddyClient(admin_url=config.caddy_admin_url, domain=config.domain)
        ssh_pool = SSHPool(config.ssh_key_path)
        tasks = BackgroundTasks()
        vm_manager = VMManager(config, db, caddy=caddy, ssh_pool=ssh_pool, tasks=tasks)
        return cls(
            config=config,
            db=db,
            vm_manager=vm_manager,
            caddy=caddy,
            ssh_pool=ssh_pool,
            tasks=tasks,
            rate_limiter=RateLimiter(max_requests=80, window_seconds=10.0),
        )

    async def start(self) -> None:
        """Recover host state and start the reaper. Called from the app lifespan."""
        await self.vm_manager.initialize()
        reaped = await self.vm_manager.reap_dead_vms()
        if reaped:
            logger.info("Startup: reaped %d dead VM(s)", reaped)
        self.tasks.spawn(self.vm_manager.run_reaper_loop(), name="reaper", key="reaper")

    async def close(self) -> None:
        await self.tasks.cancel("reaper")
        await self.tasks.drain(_DRAIN_TIMEOUT_SECONDS)
        if self.ssh_pool is not None:
            await self.ssh_pool.close_all()
        if self.caddy is not None:
            await self.caddy.close()
        await self.db.close()

    def build_lock(self, account_id: str) -> asyncio.Lock:
        """Per-account lock serializing recipe builds."""
        return self.build_locks.setdefault(account_id, asyncio.Lock())

    def rule_limiter(self, rule_id: str, rate_limit_rpm: int) -> RateLimiter:
        """Per-ingress-rule limiter, rebuilt when the rule's rpm changes."""
        limiter = self.rule_limiters.get(rule_id)
        if limiter is None or limiter.max_requests != rate_limit_rpm:
            limiter = RateLimiter(max_requests=rate_limit_rpm, window_seconds=60.0)
            self.rule_limiters[rule_id] = limiter
        return limiter
```

`src/mshkn/api/deps.py`:

```python
"""FastAPI dependencies: the Runtime and the authenticated account."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from mshkn.db import get_account_by_key

if TYPE_CHECKING:
    from mshkn.models import Account
    from mshkn.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


async def require_account(request: Request) -> Account:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    account = await get_account_by_key(get_runtime(request).db, auth[7:])
    if account is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return account
```

Delete `src/mshkn/api/auth.py`; every `from mshkn.api.auth import require_account` becomes `from mshkn.api.deps import get_runtime, require_account`.

`src/mshkn/app.py`:

```python
"""Application factory."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request

from mshkn.api.checkpoints import router as checkpoints_router
from mshkn.api.computers import router as computers_router
from mshkn.api.errors import install_error_handlers
from mshkn.api.ingress import router as ingress_router
from mshkn.api.recipes import router as recipes_router
from mshkn.api.system import router as system_router
from mshkn.observability.logging import configure_logging, request_id_var
from mshkn.runtime import Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Build the FastAPI app.

    With a Runtime given (tests), it is attached immediately so requests work
    without running the lifespan. Without one (production), the lifespan
    builds it from the environment. Either way the lifespan starts and closes it.
    """
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime if runtime is not None else await Runtime.from_env()
        app.state.runtime = rt
        await rt.start()
        try:
            yield
        finally:
            await rt.close()

    app = FastAPI(title="mshkn", version="0.1.0", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime
    install_error_handlers(app)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach a request id to the response and to every log line during the request."""
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response

    app.include_router(computers_router)
    app.include_router(checkpoints_router)
    app.include_router(ingress_router)
    app.include_router(recipes_router)
    app.include_router(system_router)
    return app
```

`src/mshkn/main.py` (full file):

```python
"""ASGI entry point: `uvicorn mshkn.main:app`."""

from __future__ import annotations

from mshkn.app import create_app

app = create_app()
```

`src/mshkn/api/ratelimit.py`: delete the two trailing lines (`# Global rate limiter instance` and `rate_limiter = RateLimiter(...)`).

`src/mshkn/api/system.py::alerts`: `return [asdict(a) for a in get_runtime(request).vm_manager.alerts]` (import `get_runtime` from `mshkn.api.deps`; drop the `VMManager` type import).

Router adaptation (mechanical; every endpoint):
- Replace `db: aiosqlite.Connection = request.app.state.db`, `config: Config = request.app.state.config`, `vm_mgr: VMManager = request.app.state.vm_manager` (and the `vm_manager`/`config = request.app.state.config` variants) with `rt = get_runtime(request)` followed by `db = rt.db`, `config = rt.config`, `vm_mgr = rt.vm_manager` as needed.
- `_get_pool(request)` in `computers.py` → delete the function; use `rt.ssh_pool`. `checkpoints.py` imports `_get_pool` from computers: use `rt.ssh_pool` there too.
- `computers.py`: delete `_background_tasks`, `_upload_tasks`, `_start_upload_task`, `cancel_upload_task`. Replace `_start_upload_task(checkpoint_id, snapshot_dir, r2_prefix, config.r2_bucket)` with `tasks.spawn(upload_checkpoint(snapshot_dir, r2_prefix, config.r2_bucket), name=f"upload:{checkpoint_id}", key=f"upload:{checkpoint_id}")`. `_self_destruct` and `_process_deferred` gain a `tasks: BackgroundTasks` keyword parameter (used for the upload, the callback `tasks.spawn(deliver_callback(...), name=f"callback:{computer.id}")`, and the deferred drain `tasks.spawn(_process_deferred(...), name=f"deferred:{label}")`); callers pass `rt.tasks` (endpoints) or `self.tasks` (`VMManager._auto_checkpoint_and_destroy`). `_check_rate_limit(request)` → `_check_rate_limit(rt, request)` using `rt.rate_limiter`.
- `checkpoints.py::delete_checkpoint`: `await cancel_upload_task(checkpoint_id)` → `await rt.tasks.cancel(f"upload:{checkpoint_id}")`. `merge_checkpoints` keeps using `vm_mgr._alloc_lock`/`_allocate_volume_id` (PR 4 replaces them).
- `ingress.py`: delete `_background_tasks`, `_rule_rate_limiters`, `_get_rule_rate_limiter`; `handle_ingress` uses `rt.rule_limiter(rule_id, rule.rate_limit_rpm)`; `delete_rule_endpoint` does `rt.rule_limiters.pop(rule_id, None)`; `rotate_rule` moves the entry in `rt.rule_limiters`; the async fire-and-forget uses `rt.tasks.spawn(_execute_action_and_log(...), name=f"ingress:{rule.id}")`. `_do_create`/`_do_fork` gain `tasks: BackgroundTasks` and pass it to `_self_destruct`; `_execute_action` and `_execute_action_and_log` gain `tasks` and pass it through.
- `recipes.py`: delete `_build_locks`/`_get_build_lock`; `build_lock = rt.build_lock(account.id)`; `rt.tasks.spawn(_run_build(), name=f"recipe_build:{recipe_id}")` instead of `vm_mgr._bg_tasks.add(...)`.
- `vm/manager.py`: `__init__(..., tasks: BackgroundTasks | None = None)` storing `self.tasks = tasks if tasks is not None else BackgroundTasks()` (import from `mshkn.runtime` under `TYPE_CHECKING` for the annotation and at runtime inside `__init__` to avoid the import cycle: `from mshkn.runtime import BackgroundTasks` placed in the function body with the comment `# runtime imports vm.manager; import here to avoid a cycle until PR 4`); replace every `task = asyncio.create_task(...); self._bg_tasks.add(task); task.add_done_callback(self._bg_tasks.discard)` with `self.tasks.spawn(..., name=...)` (names: `f"upload:{checkpoint_id}"` with the same key, `f"deferred:{effective_label}"`).

- [ ] **Step 4: Verify**

```bash
uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
grep -rnE "^_[a-z_]+: (set|dict)\[|^rate_limiter = |request\.app\.state\.(db|config|vm_manager|ssh_pool)" src/mshkn || echo "no module globals or app.state reads left"
grep -rn "from mshkn.main import app" tests || echo "no shared app in tests"
```

Expected: clean; `158 passed`; both greps print their "no ..." line.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "refactor: Runtime + create_app factory; module-level mutable globals removed

BackgroundTasks owns every background task (keyed cancel/wait, drain on
shutdown). Routers reach db/config/vm_manager/pool/limiters through
get_runtime(). Tests build a Runtime with a temp DB and a mocked
VMManager instead of mutating a shared app.state."
```

---

### Task 9: Spec amendment, final verification, PR

**Files:**
- Modify: `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` §7

- [ ] **Step 1: Amend the spec**

In §7, replace the bullet beginning `- \`db.connect(path)\` sets` with:

```markdown
- `db.connect(path)` sets `journal_mode=WAL`, `synchronous=NORMAL`, and `busy_timeout=5000`. `foreign_keys` is deliberately left OFF (amended 2026-09-04 during PR 2): the schema's `REFERENCES` clauses have no `ON DELETE` actions and destroyed computer rows are retained, so enforcement would make checkpoint deletion, pruning, and recipe deletion fail; changing that needs table rebuilds, which the additive-migration rule forbids.
```

Commit: `git commit -am "docs: spec §7 — foreign_keys stays off (reason recorded)"`.

- [ ] **Step 2: Full local validation**

Use `superpowers:verification-before-completion`:

```bash
uv sync --frozen && uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov 2>&1 | grep -E "passed|TOTAL"
git status --short
```

Expected: clean; `158 passed`; coverage TOTAL above 39%; clean tree.

- [ ] **Step 3: Push, open the PR, watch CI**

```bash
git push -u origin pr2-foundations
gh pr create --title "PR 2: foundations (errors, resources, enums, db package, config, observability, runtime)" --body-file - <<'EOF'
Part 2 of 6 of the quality overhaul (spec: docs/superpowers/specs/2026-09-04-quality-overhaul-design.md, plan: docs/superpowers/plans/2026-09-04-pr2-foundations.md).

**What this does**
Adds the shared foundations for the host boundary (PR 3) and service layer (PR 4): typed domain errors mapped to HTTP, a validated `Resources` type, StrEnum statuses, a `db/` package with WAL/busy-timeout pragmas, executescript migrations, one row mapper per table and migration 010 (indexes), a generic `MSHKN_<FIELD>` config mapping with `ConfigError`, an `observability/` package (request-id JSON logging, operation duration/error metrics, pool and RAM gauges, `timed()`), a `BackgroundTasks` registry, and a `Runtime` object with a `create_app(runtime)` factory. Every module-level mutable global under `src/` is gone; tests build a Runtime with a temp DB instead of mutating a shared `app.state`.

**Design alignment**
- §5 Runtime and state ownership: implemented (Runtime, BackgroundTasks, get_runtime); host boundary fields arrive in PR 3.
- §7 Data layer: implemented, with one recorded amendment: `foreign_keys` stays OFF because the schema's REFERENCES lack ON DELETE actions and destroyed rows are retained (spec §7 updated in this PR).
- §8 Config and resources: implemented.
- §9 API contract: error mapping only (unknown recipe 404, not-ready 409, bad `needs` 422); response models and field removals are PR 4.
- §10 Observability: logging and metrics helpers implemented; per-lifecycle `extra` fields, pool/RAM gauge updates, and health subsystems are wired in PR 4.

**Validation performed**
- Baseline before: <paste docs/superpowers/plans/2026-09-04-pr2-baseline.txt>
- After: ruff/format/mypy clean; `uv run pytest` 158 passed; coverage TOTAL <n>%.
- CI: <link>
- Live E2E (`scripts/e2e.sh` against 65.21.22.161 at <sha>): <N passed, M skipped, 0 failed>; PR 1 baseline was 151/6/0.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01CPKyFZiT4pPi4v5gkph5KZ
EOF
gh pr checks --watch
```

Fill the `<...>` fields before submitting the final body (`gh pr edit --body-file`).

- [ ] **Step 4: Live E2E**

From the worktree, detached so the tool timeout cannot kill it:

```bash
LOG=/tmp/e2e-pr2.log; setsid nohup env MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000 scripts/e2e.sh -p no:cacheprovider > "$LOG" 2>&1 < /dev/null & disown
```

Poll `tail -1 "$LOG"` until the summary line appears (about 16 minutes). Expected: `151 passed, 6 skipped`. Any failure that passed in PR 1's baseline blocks the PR; investigate with `ssh mshkn journalctl -u mshkn --since '30 min ago' --no-pager`.

- [ ] **Step 5: Bot reviews and merge request**

Triage per CLAUDE.md "How to handle PR reviews". Report to the owner with the CI link and the E2E summary. Do not merge.

---

## Self-review

**Spec coverage:** §5 (Runtime, BackgroundTasks, get_runtime, shutdown order) → Task 8. §7 (connect pragmas, executescript runner, migration 010, single mappers, DeferredRequest, enums) → Tasks 4, 5, with the foreign-keys amendment in Task 9. §8 (Config generic mapping, Resources with bounds) → Tasks 6, 3. §9 error mapping → Task 2. §10 logging filter/contextvar, metrics series, `timed()` → Task 7; the `extra` fields on lifecycle events and gauge updates from the reaper are PR 4 (they belong to the services). §14 step 2 lists exactly these items.

**Placeholder scan:** the only `<...>` fields are in the PR body template, to be filled before submission. Task 5 describes the non-example table modules by column tuple and function list rather than repeating each body; the bodies are the existing `db.py` functions, which the implementer moves verbatim with the mapper substitution shown in the full `computers.py` example.

**Type consistency:** `Resources` (Task 3) is what `VMManager.create` takes in Task 3 and Task 8 leaves unchanged. `ComputerStatus`/`RecipeStatus`/`IngressLogStatus` (Task 4) are what the Task 5 mappers construct. `BackgroundTasks.spawn(coro, *, name, key)` (Task 8) matches every call site named in Task 8's router adaptation and the `VMManager` change. `get_runtime` / `Runtime` field names used in Task 8's router bullets match the dataclass. `connect`/`run_migrations` (Task 5) are what `Runtime.from_env` and the tests' `db` fixture call.
