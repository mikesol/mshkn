# PR 4: Services — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `VMManager` and the fat routers with a service layer (`SlotAllocator`, `RecipeService`, `ComputerService`, `CheckpointService`, `Lifecycle`, `IngressService`, `Reaper`) so that every lifecycle flow (create, fork, checkpoint, self-destruct, deferred drain, ingress create/fork, idle/dead reaping, prune, merge) has exactly one implementation, routers only translate HTTP to service calls, and the known correctness bugs (leaked volume and slot on boot failure, double deferred drain, prune racing an upload, missing-file download 500, unlabeled checkpoint metrics, fake `/health`) are fixed with tests that pin them.

**Architecture:** `src/mshkn/services/` holds one class per concern, each constructed once by `Runtime.build(config, db, host)` and reachable through `Runtime`. Dependency direction is strict: `api → services → host, db`; `models`, `errors`, `config`, `resources`, `observability` are leaves; nothing in `services` imports `api` or `runtime`. `Lifecycle.run_ephemeral` is the single implementation behind REST create, REST fork, ingress create, ingress fork, and the deferred drain. Deferred items are claimed with one `DELETE … RETURNING`. The active-computer gauge is set from the database after every state change. `vm/`, `checkpoint/`, `recipe/`, `ingress/`, and `callback.py` are deleted; their logic moves into `services/` unchanged except for the fixes named in Global Constraints. A `python -m mshkn` CLI creates accounts and runs migrations, replacing the `sqlite3` insert in `scripts/e2e.sh` and `DEPLOY.md`.

**Tech Stack:** Python 3.12, asyncio, FastAPI, Pydantic 2, httpx, aiosqlite (SQLite ≥ 3.35 for `RETURNING`; local 3.47, server 3.45), prometheus_client, starlark_go, pytest 9 / pytest-asyncio 1.3, uv, ruff 0.15, mypy 1.19 strict.

**Spec:** `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` §3 (layout), §5 (Runtime), §6 (services), §7 (`CheckpointTrigger`, manifest columns), §9 (API contract), §10 (metrics, alerts), §11 (flow tier), §14 step 4. Deviations decided here, each for a reason:

1. **`Lifecycle` is a class, not `run_ephemeral(rt, …)`.** A free function taking `Runtime` would make `services` import `runtime`, which imports `services`. `Lifecycle(db, computers, checkpoints, tasks, http)` carries exactly what §6.4 says the function needs; `Runtime.lifecycle` holds it.
2. **A `BadRequest` (400) domain error exists alongside `InvalidInput` (422).** The E2E suite (T7 `test_merge_checkpoint_with_itself`) pins 400 for merge validation, and "computer is not running" has always been 400. §9 lists only the changes that correct an inconsistency; these are not among them, so they keep their codes.
3. **`PayloadTooLarge` (413) and `TransformError` (502) domain errors.** The ingress trigger already returns 413 for oversized bodies and 502 with a structured detail for a failing or invalid Starlark result (E2E T13 pins both). They become typed errors with a `detail` payload so routers do not raise `HTTPException` for domain outcomes.
4. **`SlotAllocator.acquire_volume_id()`** in addition to `acquire()`. Checkpoints, merges, and recipes need a volume id without a slot.
5. **`RecipeService.create` returns `(recipe, created)`** so the router can keep answering 200 for a deduplicated recipe and 202 for a new build, as the E2E suite expects.
6. **Placement of small types.** `ExecSpec`, `EphemeralResult`, `Alert`, `CheckpointTrigger`, `IngressRule`, `IngressLog`, `IngressLogStatus` live in `models.py`; the ingress Pydantic request/response models move to `api/schemas.py`; the Starlark sandbox moves to `services/starlark.py`. `ingress/` is deleted.
7. **`deliver_callback(client, url, payload, *, max_retries, sleep)`** in `services/callback.py`, taking the shared `httpx.AsyncClient` from `Runtime.http` (spec §5) instead of building one per attempt.
8. **Checkpoint `sync` is bounded uniformly.** The REST checkpoint path ran the guest `sync` with a 10 s exec timeout and no outer bound; the reaper path wrapped it in `wait_for(…, 15.0)`. The single implementation uses the reaper's bound everywhere.
9. **`exec`, `stream`, and `exec_bg` all touch `last_exec_at`** (§6.2), where before only `stream` and `exec_bg` did.
10. **`Runtime.close` closes the shared HTTP client.** The shutdown order of §5 is reaper → drain → guest → proxy → db; the branch adds `await self.http.aclose()` between the drain and the guest, because `Runtime.http` (§5) is created here and nothing else owns it. Callbacks in flight are drained first, so nothing is cut short.
11. **`boot` and `restore` are observed inside `_bring_up`.** §10 lists both among the `op` values, but the only timers were on the outer operations. `timed("restore")`/`timed("boot")` now wrap the two hypervisor calls, nested inside `timed("create")`/`timed("fork")`: different labels, so a host failure counts once per op.

## Global Constraints

- Python `>=3.12`; uv only; every command runs as `uv run <tool>` inside the worktree.
- Local validation, identical to CI: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`. Green at the end of every task. Implementers report the **actual** test count; the numbers in this plan are expectations, not targets.
- Dependency direction: `api → services → host, db`. `services/` never imports `api` or `runtime`. `host/` never imports `services`, `api`, `db`, or `runtime`. No function-local imports anywhere under `src/` (the ones in `vm/manager.py` and the routers exist only to dodge cycles that this PR removes).
- No module-level mutable state under `src/mshkn/`.
- Behavior on the live server is unchanged except these deliberate fixes, each named in the PR body:
  1. `create`/`fork` that fail after the disk snap remove the volume, release the slot, kill a VM that booted, and raise `HostError` (502). Before, the volume and slot leaked and the client got a 500.
  2. The deferred queue is claimed atomically (`DELETE … RETURNING`), so a destroy and an idle reap racing on the same label cannot both process a batch.
  3. `DELETE /checkpoints/{id}` and prune cancel the checkpoint's in-flight upload before removing its directory (prune did not, so rclone failed on a vanished directory; three such failures appear in every E2E run's journal).
  4. `GET /computers/{id}/download` of a missing file is 404, not 500.
  5. `POST /computers` with `recipe_id` unknown is 404 and not-ready is 409 (already true since PR 2), and ingress `create` actions accept `recipe_id` and `needs` and reject `capabilities` and `uses`.
  6. `GET /computers/{id}/status` drops `manifest_hash` and adds `recipe_id`; `GET /checkpoints` drops `manifest_hash` and adds `recipe_id`. The two E2E assertions on `manifest_hash` change to `recipe_id` in this PR.
  7. `GET /health` reports `subsystems` and `"degraded"` when one fails (HTTP 200 either way). `GET /alerts` gains thin-pool data/metadata alerts.
  8. `mshkn_checkpoints_total` carries a `trigger` label; `mshkn_computers_created_total` carries a `source` label; `mshkn_computers_active` is set from the database count, never incremented or decremented.
  9. Two concurrent first creates for the same recipe (or bare) build the template once.
  10. Firecracker and SSH failures surface as `HostError` (502) instead of raw `httpx`/`asyncssh` exceptions (500). `CaddyProxy.healthy()` returns `False` after `close()` instead of raising.
- Live E2E gate: `MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000 scripts/e2e.sh`, detached, must report 151 passed, 6 skipped, 0 failed (PR 3 baseline at c91e7c0). The journal must show no `rclone … directory not found` upload failures during the run.
- Commit messages end with the trailer block (Co-Authored-By and Claude-Session lines). Never merge; open the PR and request authorization.

---

## File Structure

**Created**
- `src/mshkn/services/__init__.py` — empty.
- `src/mshkn/services/allocator.py` — `SlotAllocator`.
- `src/mshkn/services/recipes.py` — `RecipeService`, `dockerfile_content_hash`, `docker_build_image`, `_post_process_rootfs` (moved from `recipe/builder.py`).
- `src/mshkn/services/computers.py` — `ComputerService`.
- `src/mshkn/services/checkpoints.py` — `CheckpointService`, `MergeOutcome`, `Deferred`.
- `src/mshkn/services/merge.py` — `three_way_merge` (moved from `checkpoint/merge.py`, unchanged).
- `src/mshkn/services/callback.py` — `deliver_callback` (moved from `callback.py`, client injected).
- `src/mshkn/services/lifecycle.py` — `Lifecycle`.
- `src/mshkn/services/starlark.py` — `validate_starlark`, `execute_transform`, `StarlarkError` (moved from `ingress/starlark.py`).
- `src/mshkn/services/ingress.py` — `IngressService`, `ForkAction`, `CreateAction`, `validate_transform_result`, `TriggerOutcome`.
- `src/mshkn/services/reaper.py` — `Reaper`.
- `src/mshkn/api/schemas.py` — every request/response model.
- `src/mshkn/cli.py`, `src/mshkn/__main__.py` — `accounts create|list`, `migrate`.
- `tests/unit/test_allocator.py`, `test_recipe_service.py`, `test_computer_service.py`, `test_checkpoint_service.py`, `test_lifecycle.py`, `test_callback.py`, `test_ingress_service.py`, `test_reaper.py`, `test_cli.py`, `test_host_errors.py`, `test_deferred_claim.py`.
- `tests/flow/test_exclusive.py`, `test_self_destruct.py`, `test_reaper.py`, `test_failures.py`, `test_ingress.py`, `test_recipes.py`, `test_system.py`.

**Modified**
- `src/mshkn/models.py` — manifest fields removed; `CheckpointTrigger`, `ExecSpec`, `EphemeralResult`, `Alert`, ingress dataclasses added.
- `src/mshkn/errors.py` — `detail` on `MshknError`; `BadRequest`, `PayloadTooLarge`, `TransformError`.
- `src/mshkn/api/errors.py` — the three new mappings; `detail` payloads.
- `src/mshkn/observability/metrics.py` — labels on `checkpoints_total` and `computers_created_total`.
- `src/mshkn/db/computers.py`, `checkpoints.py`, `deferred.py`, `accounts.py`, `__init__.py` — manifest constants on insert, `count_active_computers`, `claim_deferred_by_label`, `list_accounts`.
- `src/mshkn/host/firecracker.py`, `host/ssh.py`, `host/caddy.py` — error wrapping, `healthy()` after close.
- `src/mshkn/runtime.py` — services, `alerts`, `http`; `build()`; shutdown order.
- `src/mshkn/api/computers.py`, `checkpoints.py`, `recipes.py`, `ingress.py`, `system.py` — thin.
- `pyproject.toml` — `[project.scripts] mshkn = "mshkn.cli:main"`.
- `scripts/e2e.sh`, `DEPLOY.md` — test account via the CLI.
- `tests/e2e/test_phase6_durability.py:63`, `tests/e2e/test_phase7_api.py:161` — `manifest_hash` → `recipe_id`.
- `tests/unit/conftest.py` — `make_runtime(db, *, config=None, host=None, http=None)` builds real services.
- Every unit test that constructs `Computer`/`Checkpoint` or mocks `vm_manager` (listed in Tasks 2 and 11).

**Deleted**
- `src/mshkn/vm/` (package), `src/mshkn/checkpoint/` (package), `src/mshkn/recipe/` (package), `src/mshkn/ingress/` (package), `src/mshkn/callback.py`.
- `tests/unit/test_vm_manager.py`, `tests/unit/test_recipe_builder.py` (their surviving tests move to the new service test files).

---

### Task 1: Worktree and baseline

- [ ] **Step 1:** Use `superpowers:using-git-worktrees` to create `../mshkn-pr4` on branch `pr4-services` from `main` (61064c6 or later). `cd ../mshkn-pr4 && uv sync`.
- [ ] **Step 2:** Record the baseline:

```bash
{ echo "Baseline before PR 4 (main @ $(git rev-parse --short HEAD), $(date -I))"; uv run ruff check . | tail -1; uv run ruff format --check . | tail -1; uv run mypy | tail -1; uv run pytest -q -p no:cacheprovider 2>&1 | tail -1; uv run pytest --cov -q -p no:cacheprovider 2>&1 | grep TOTAL; } | tee docs/superpowers/plans/2026-09-06-pr4-baseline.txt
git add docs/superpowers/plans/2026-09-06-pr4-baseline.txt && git commit -m "chore: record pre-PR4 baseline"
```

Expected: clean; `218 passed`; coverage TOTAL 66%.

---

### Task 2: Models, errors, metrics, and data-layer additions

**Files:**
- Modify: `src/mshkn/models.py`, `src/mshkn/errors.py`, `src/mshkn/api/errors.py`, `src/mshkn/observability/metrics.py`, `src/mshkn/db/computers.py`, `src/mshkn/db/checkpoints.py`, `src/mshkn/db/deferred.py`, `src/mshkn/db/accounts.py`, `src/mshkn/db/__init__.py`
- Modify (call sites of the removed fields and functions): `src/mshkn/vm/manager.py`, `src/mshkn/api/computers.py`, `src/mshkn/api/checkpoints.py`, `src/mshkn/ingress/models.py` (dataclasses move out; keep the Pydantic models there until Task 10)
- Modify (tests constructing `Computer`/`Checkpoint` with manifest fields): `tests/unit/test_models.py`, `test_db.py`, `test_db_package.py`, `test_checkpoint_parent.py`, `test_checkpoint_label_filter.py`, `test_exclusive_restore.py`, `test_self_destruct.py`, `test_exec_on_create.py`, `test_vm_limit.py`, `test_vm_manager.py`, `test_recipe_db.py`, `test_metrics.py`, `test_errors.py`
- Create: `tests/unit/test_deferred_claim.py`

**Interfaces:**
- Produces (models): `CheckpointTrigger` (`API="api"`, `SELF_DESTRUCT="self_destruct"`, `IDLE="idle"`); `ExclusiveMode = Literal["error_on_conflict", "defer_on_conflict"]`; `ExecSpec(command, self_destruct, callback_url, label, meta_exec)` frozen; `EphemeralResult(computer_id, exec_exit_code, exec_stdout, exec_stderr, created_checkpoint_id)` frozen; `Alert(level, source, message, value, threshold, timestamp)`; `IngressLogStatus`, `IngressRule`, `IngressLog` (moved verbatim from `ingress/models.py`). `Computer` and `Checkpoint` lose `manifest_hash` and `manifest_json`.
- Produces (errors): `MshknError(message, *, detail=None)` with `.detail`; `BadRequest` (400), `PayloadTooLarge` (413), `TransformError` (502, detail preserved).
- Produces (metrics): `checkpoints_total` labelled `["trigger"]`; `computers_created_total` labelled `["source"]`.
- Produces (db): `count_active_computers(db) -> int`; `list_accounts(db) -> list[Account]`; `claim_deferred_by_label(db, label) -> list[DeferredRequest]` (atomic, oldest first); `list_deferred_by_label` and `delete_deferred_by_label` are removed.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_deferred_claim.py`:

```python
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mshkn.db import claim_deferred_by_label, insert_deferred

if TYPE_CHECKING:
    import aiosqlite


async def _queue(db: aiosqlite.Connection, label: str, n: int) -> None:
    for i in range(n):
        await insert_deferred(db, f"def-{label}-{i}", label, "acct-1", "{}", f"2026-09-06T00:00:0{i}")


async def test_claim_returns_items_oldest_first_and_empties_the_label(
    db: aiosqlite.Connection,
) -> None:
    await _queue(db, "chain", 3)
    await _queue(db, "other", 1)
    items = await claim_deferred_by_label(db, "chain")
    assert [i.id for i in items] == ["def-chain-0", "def-chain-1", "def-chain-2"]
    assert await claim_deferred_by_label(db, "chain") == []
    assert [i.id for i in await claim_deferred_by_label(db, "other")] == ["def-other-0"]


async def test_concurrent_claims_hand_out_each_item_exactly_once(
    db: aiosqlite.Connection,
) -> None:
    await _queue(db, "chain", 5)
    a, b = await asyncio.gather(
        claim_deferred_by_label(db, "chain"), claim_deferred_by_label(db, "chain")
    )
    ids = sorted(i.id for i in a + b)
    assert ids == [f"def-chain-{i}" for i in range(5)]
    assert not (a and b), "one claimer must get everything, the other nothing"
```

Add to `tests/unit/test_errors.py`:

```python
async def test_bad_request_maps_to_400() -> None:
    assert await _status(BadRequest("nope")) == (400, {"detail": "nope"})


async def test_payload_too_large_maps_to_413() -> None:
    assert await _status(PayloadTooLarge("too big")) == (413, {"detail": "too big"})


async def test_transform_error_keeps_its_structured_detail() -> None:
    exc = TransformError("bad transform", detail={"errors": ["x"], "starlark_result": {"a": 1}})
    assert await _status(exc) == (502, {"detail": {"errors": ["x"], "starlark_result": {"a": 1}}})


def test_detail_defaults_to_none() -> None:
    assert NotFound("x").detail is None
```

(`_status` is the existing helper in that file that runs `_handle_domain_error` and returns `(status_code, json)`; if it is named differently, use the existing one.)

Add to `tests/unit/test_models.py`:

```python
def test_computer_and_checkpoint_have_no_manifest_fields() -> None:
    assert "manifest_hash" not in Computer.__dataclass_fields__
    assert "manifest_json" not in Checkpoint.__dataclass_fields__


def test_checkpoint_trigger_values() -> None:
    assert [t.value for t in CheckpointTrigger] == ["api", "self_destruct", "idle"]
    assert CheckpointTrigger.IDLE == "idle"


def test_exec_spec_is_frozen() -> None:
    spec = ExecSpec(command="ls", self_destruct=False, callback_url=None, label=None, meta_exec=None)
    with pytest.raises(FrozenInstanceError):
        spec.command = "rm"  # type: ignore[misc]
```

Add to `tests/unit/test_db.py` (next to the existing computer insert/get tests):

```python
async def test_insert_writes_manifest_placeholders(db: aiosqlite.Connection) -> None:
    await insert_account(db, Account(id="acct-1", api_key="k", vm_limit=1, created_at="t"))
    await insert_computer(db, _computer("comp-1"))
    cur = await db.execute("SELECT manifest_hash, manifest_json FROM computers WHERE id = 'comp-1'")
    assert await cur.fetchone() == ("", "{}")
    await insert_checkpoint(db, _checkpoint("ckpt-1"))
    cur = await db.execute("SELECT manifest_hash, manifest_json FROM checkpoints WHERE id = 'ckpt-1'")
    assert await cur.fetchone() == ("", "{}")


async def test_count_active_computers_spans_accounts(db: aiosqlite.Connection) -> None:
    await insert_account(db, Account(id="acct-1", api_key="k1", vm_limit=5, created_at="t"))
    await insert_account(db, Account(id="acct-2", api_key="k2", vm_limit=5, created_at="t"))
    await insert_computer(db, _computer("c1", account_id="acct-1"))
    await insert_computer(db, _computer("c2", account_id="acct-2"))
    await insert_computer(db, _computer("c3", account_id="acct-2", status=ComputerStatus.DESTROYED))
    assert await count_active_computers(db) == 2
    assert [a.id for a in await list_accounts(db)] == ["acct-1", "acct-2"]
```

(`_computer`/`_checkpoint` are that file's helpers; extend them with `account_id` and `status` keyword arguments if they lack them.)

Add to `tests/unit/test_metrics.py`:

```python
async def test_labelled_counters_render_after_first_increment(db: aiosqlite.Connection) -> None:
    checkpoints_total.labels(trigger="api").inc()
    computers_created_total.labels(source="fork").inc()
    app = make_app(make_runtime(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        text = (await client.get("/metrics")).text
    assert 'mshkn_checkpoints_total{trigger="api"}' in text
    assert 'mshkn_computers_created_total{source="fork"}' in text
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/test_deferred_claim.py tests/unit/test_errors.py tests/unit/test_models.py tests/unit/test_db.py tests/unit/test_metrics.py -q`
Expected: ImportError on `claim_deferred_by_label`, `BadRequest`, `CheckpointTrigger`, `ExecSpec`, `count_active_computers`, `list_accounts`; the manifest test fails on `__dataclass_fields__`.

- [ ] **Step 3: Implement**

`src/mshkn/models.py` — replace the file:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


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


class CheckpointTrigger(StrEnum):
    API = "api"
    SELF_DESTRUCT = "self_destruct"
    IDLE = "idle"


class IngressLogStatus(StrEnum):
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"


ExclusiveMode = Literal["error_on_conflict", "defer_on_conflict"]


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
    status: ComputerStatus
    created_at: str
    last_exec_at: str | None
    source_checkpoint_id: str | None = None
    recipe_id: str | None = None

    @property
    def slot(self) -> int:
        return int(self.tap_device.removeprefix("tap"))

    @property
    def volume_name(self) -> str:
        return f"mshkn-{self.id}"


@dataclass
class Checkpoint:
    id: str
    account_id: str
    parent_id: str | None
    computer_id: str | None
    thin_volume_id: int | None
    r2_prefix: str
    disk_delta_size_bytes: int | None
    memory_size_bytes: int | None
    label: str | None
    pinned: bool
    created_at: str
    recipe_id: str | None = None

    @property
    def volume_name(self) -> str:
        return f"mshkn-ckpt-{self.id}"


@dataclass(frozen=True)
class DeferredRequest:
    id: str
    label: str
    account_id: str
    request_payload: str
    created_at: str


@dataclass(frozen=True)
class ExecSpec:
    """What to do with a freshly created or forked computer (spec §6.4)."""

    command: str | None
    self_destruct: bool
    callback_url: str | None
    label: str | None
    meta_exec: str | None


@dataclass(frozen=True)
class EphemeralResult:
    computer_id: str
    exec_exit_code: int | None
    exec_stdout: str | None
    exec_stderr: str | None
    created_checkpoint_id: str | None


@dataclass
class Alert:
    level: str  # "warning" or "critical"
    source: str  # "nvme", "ram", "thin_pool_data", "thin_pool_metadata"
    message: str
    value: float
    threshold: float
    timestamp: str  # ISO 8601


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
    status: IngressLogStatus
    starlark_result: str | None
    error_message: str | None
    created_at: str
```

`src/mshkn/errors.py`:

```python
"""Domain errors. The API layer maps these to HTTP responses (see api/errors.py)."""

from __future__ import annotations


class MshknError(Exception):
    """Base class for errors that carry a user-facing message.

    ``detail`` is an optional structured payload the API returns verbatim
    under ``{"detail": ...}`` instead of the message (ingress validation
    errors carry a list, for example).
    """

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFound(MshknError):  # noqa: N818 -- name is part of the public API contract
    """A referenced resource does not exist (or is not visible to the caller)."""


class Conflict(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The operation is valid but the resource is in the wrong state for it."""


class BadRequest(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The request cannot be carried out as stated (legacy 400 contract: merge
    validation, operations on a computer that is not running)."""


class InvalidInput(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The request is well-formed but its values are not acceptable."""


class PayloadTooLarge(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The request body exceeds the configured limit."""


class LimitExceeded(MshknError):  # noqa: N818 -- name is part of the public API contract
    """A per-account or per-key limit was hit."""


class TransformError(MshknError):
    """An ingress rule's Starlark failed or returned an invalid action (502)."""


class HostError(MshknError):
    """A host-side operation failed.

    Raised by the dm-thin, tap and rclone paths (as ``ShellError``), by the
    Firecracker and SSH wrappers, by ``CaddyProxy``, and by the fake host.
    """


class ConfigError(MshknError):
    """Startup configuration is invalid."""
```

`src/mshkn/api/errors.py` — replace `_STATUS_BY_TYPE` and `_handle_domain_error`:

```python
_STATUS_BY_TYPE: tuple[tuple[type[MshknError], int], ...] = (
    (NotFound, 404),
    (Conflict, 409),
    (BadRequest, 400),
    (InvalidInput, 422),
    (PayloadTooLarge, 413),
    (LimitExceeded, 429),
    (TransformError, 502),
    (HostError, 502),
)


async def _handle_domain_error(request: Request, exc: MshknError) -> JSONResponse:
    status = _status_for(exc)
    if isinstance(exc, HostError):
        logger.error("host operation failed: %s", exc.message, extra={"path": request.url.path})
        return JSONResponse(status_code=status, content={"detail": "host operation failed"})
    if status == 500:
        logger.error("unmapped domain error: %s", exc.message, extra={"path": request.url.path})
        return JSONResponse(status_code=500, content={"detail": "internal error"})
    detail = exc.detail if exc.detail is not None else exc.message
    return JSONResponse(status_code=status, content={"detail": detail})
```

(import the three new classes.)

`src/mshkn/observability/metrics.py` — change the two counters:

```python
computers_created_total = Counter(
    "mshkn_computers_created_total", "Computers created, by source", ["source"]
)
checkpoints_total = Counter("mshkn_checkpoints_total", "Checkpoints created, by trigger", ["trigger"])
```

`src/mshkn/db/computers.py` — `COLUMNS` loses `manifest_hash` and `manifest_json`; `_row_to_computer` loses the two lines; `insert_computer` writes the placeholders as SQL constants; add `count_active_computers`:

```python
async def insert_computer(db: aiosqlite.Connection, computer: Computer) -> None:
    await db.execute(
        "INSERT INTO computers (" + ", ".join(COLUMNS) + ", manifest_hash, manifest_json) "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ", '', '{}')",
        (
            computer.id,
            computer.account_id,
            computer.thin_volume_id,
            computer.tap_device,
            computer.vm_ip,
            computer.socket_path,
            computer.firecracker_pid,
            computer.status,
            computer.created_at,
            computer.last_exec_at,
            computer.source_checkpoint_id,
            computer.recipe_id,
        ),
    )
    await db.commit()


async def count_active_computers(db: aiosqlite.Connection) -> int:
    """Count non-destroyed computers across every account (feeds the gauge)."""
    cursor = await db.execute("SELECT COUNT(*) FROM computers WHERE status != 'destroyed'")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0
```

`src/mshkn/db/checkpoints.py` — same treatment: `COLUMNS` without the manifest pair, `_row_to_checkpoint` without the two lines, and:

```python
async def insert_checkpoint(db: aiosqlite.Connection, checkpoint: Checkpoint) -> None:
    await db.execute(
        "INSERT INTO checkpoints (" + ", ".join(COLUMNS) + ", manifest_hash, manifest_json) "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ", '', '{}')",
        (
            checkpoint.id,
            checkpoint.account_id,
            checkpoint.parent_id,
            checkpoint.computer_id,
            checkpoint.thin_volume_id,
            checkpoint.r2_prefix,
            checkpoint.disk_delta_size_bytes,
            checkpoint.memory_size_bytes,
            checkpoint.label,
            int(checkpoint.pinned),
            checkpoint.created_at,
            checkpoint.recipe_id,
        ),
    )
    await db.commit()
```

`src/mshkn/db/deferred.py` — replace `list_deferred_by_label` and `delete_deferred_by_label` with:

```python
async def claim_deferred_by_label(db: aiosqlite.Connection, label: str) -> list[DeferredRequest]:
    """Atomically take every queued request for a label, oldest first.

    One statement, so two drains racing on the same label (a destroy and an
    idle reap, say) cannot both receive the batch: SQLite serialises writers
    and the second DELETE finds nothing.
    """
    cursor = await db.execute(
        "DELETE FROM deferred_queue WHERE label = ? RETURNING " + ", ".join(COLUMNS),
        (label,),
    )
    rows = await cursor.fetchall()
    await db.commit()
    items = [_row_to_deferred(r) for r in rows]
    items.sort(key=lambda d: (d.created_at, d.id))
    return items
```

`src/mshkn/db/accounts.py` — add:

```python
async def list_accounts(db: aiosqlite.Connection) -> list[Account]:
    cursor = await db.execute(_SELECT + " ORDER BY id")
    return [_row_to_account(r) for r in await cursor.fetchall()]
```

`src/mshkn/db/__init__.py` — export `claim_deferred_by_label`, `count_active_computers`, `list_accounts`; drop the two removed names from the imports and `__all__` (keep `__all__` sorted).

`src/mshkn/ingress/models.py` — delete the three dataclasses and the `IngressLogStatus` enum and re-export them from `mshkn.models` for now (`from mshkn.models import IngressLog, IngressLogStatus, IngressRule  # noqa: F401`), so `db/ingress.py` and `api/ingress.py` keep working until Task 10 deletes the package. `db/ingress.py` imports them from `mshkn.models` directly.

Call sites: `vm/manager.py` (two `Computer(...)` constructions and one `Checkpoint(...)`) and `api/computers.py` / `api/checkpoints.py` (three `Checkpoint(...)`) drop the `manifest_hash=`/`manifest_json=` arguments; `api/computers.py` `computer_status` replaces `"manifest_hash": computer.manifest_hash` with `"recipe_id": computer.recipe_id`; `api/checkpoints.py` `list_checkpoints` replaces the `manifest_hash` entry with `"recipe_id": c.recipe_id`. `checkpoints_total.inc()` becomes `checkpoints_total.labels(trigger="api").inc()` in `checkpoint_computer` and `labels(trigger="self_destruct")` in `_self_destruct`; `vm/manager.py` `_auto_checkpoint_and_destroy` gets `labels(trigger="idle").inc()` after its insert; `computers_created_total.inc()` becomes `.labels(source="create").inc()` in `create_computer`, and `fork_checkpoint` gains `computers_created_total.labels(source="fork").inc()` after the fork. Replace the `list_deferred_by_label` + `delete_deferred_by_label` pairs in `api/computers.py` (`_self_destruct`, `destroy_computer`) and `vm/manager.py` (`_auto_checkpoint_and_destroy`) with a single `deferred = await claim_deferred_by_label(db, label)`; keep the `if deferred:` spawn.

Tests constructing `Computer`/`Checkpoint`: delete the `manifest_hash=` and `manifest_json=` lines everywhere (`grep -rn "manifest" tests/unit` must come back empty). `tests/unit/test_exclusive_restore.py`: the three deferred-queue tests become claim tests: replace `list_deferred_by_label` reads with `claim_deferred_by_label` and drop `test_deferred_queue_delete_by_label` (the claim test file covers it). `tests/e2e/test_phase6_durability.py:63` and `tests/e2e/test_phase7_api.py:161`: `assert "recipe_id" in body`.

- [ ] **Step 4: Verify**

```bash
grep -rn "manifest" src/mshkn --include='*.py' | grep -v "^src/mshkn/db/" ; grep -rn "manifest" tests/unit
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1
```

Expected: the first grep prints only the `'', '{}'` insert lines in `db/`; the second prints nothing; gate clean; test count = baseline + 9 new − 1 deleted (report the actual number).

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "refactor: drop manifest fields from models, add CheckpointTrigger and typed 400/413/502 errors, atomic deferred claim"
```

---

### Task 3: Firecracker and SSH failures are HostErrors; Caddy health after close

**Files:**
- Modify: `src/mshkn/host/firecracker.py`, `src/mshkn/host/ssh.py`, `src/mshkn/host/caddy.py`
- Create: `tests/unit/test_host_errors.py`
- Modify: `tests/unit/test_caddy.py` (the after-close assertion)

**Interfaces:**
- `FirecrackerHypervisor.boot/restore/snapshot/build_template` raise `HostError` (with the original exception chained) for any `httpx.HTTPError`, `TimeoutError`, `OSError`, or `asyncssh.Error`; `ShellError` already is one.
- `SshGuest.exec/stream/exec_bg/upload/download/metrics/warm` raise `HostError` for `asyncssh.Error`, `OSError`, and `TimeoutError`; `download` keeps raising `FileNotFoundError` for a missing remote file (the service maps it to `NotFound`).
- `CaddyProxy.healthy()` returns `False` after `close()`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_host_errors.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncssh
import pytest

from mshkn.config import Config
from mshkn.errors import HostError
from mshkn.host.firecracker import FirecrackerHypervisor
from mshkn.host.ssh import SshGuest


async def test_snapshot_on_a_missing_socket_is_a_host_error(tmp_path: Path) -> None:
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    with pytest.raises(HostError) as info:
        await hv.snapshot(str(tmp_path / "no-such.socket"), tmp_path / "snap")
    assert info.value.__cause__ is not None


async def test_ssh_connect_failure_is_a_host_error() -> None:
    async def connect(host: str, **kwargs: Any) -> Any:
        raise asyncssh.PermissionDenied("nope")

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    with pytest.raises(HostError) as info:
        await guest.exec("172.16.1.2", "true")
    assert isinstance(info.value.__cause__, asyncssh.PermissionDenied)


async def test_ssh_os_error_is_a_host_error() -> None:
    async def connect(host: str, **kwargs: Any) -> Any:
        raise OSError(113, "No route to host")

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    with pytest.raises(HostError):
        await guest.warm("172.16.1.2")


async def test_ssh_stream_connect_failure_is_a_host_error() -> None:
    async def connect(host: str, **kwargs: Any) -> Any:
        raise asyncssh.ConnectionLost("gone")

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    with pytest.raises(HostError):
        async for _ in guest.stream("172.16.1.2", "true"):
            pass
```

In `tests/unit/test_caddy.py`, the after-close test asserts `await proxy.healthy() is False` (replace the current `pytest.raises(RuntimeError)` assertion, if that is what it asserts; the PR 3 final review recorded that `healthy()` raised after close).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_host_errors.py tests/unit/test_caddy.py -q`
Expected: the first raises `httpx.ConnectError`, the SSH ones raise the asyncssh/OS error, the Caddy one raises `RuntimeError`.

- [ ] **Step 3: Implement**

`src/mshkn/host/firecracker.py` — add one helper and use it in the four public coroutines:

```python
_WRAPPED = (httpx.HTTPError, TimeoutError, OSError, asyncssh.Error)


@contextlib.asynccontextmanager
async def _host_errors(what: str) -> AsyncIterator[None]:
    """Turn transport-level failures into HostError; leave HostError alone."""
    try:
        yield
    except HostError:
        raise
    except _WRAPPED as exc:
        raise HostError(f"{what}: {type(exc).__name__}: {exc}") from exc
```

Wrap the body of `boot`, `restore`, `snapshot`, and `build_template` in `async with _host_errors("boot")` (and "restore", "snapshot", "build_template"). `kill`, `is_alive`, and `teardown_slot` are unchanged (they already never raise transport errors). `AsyncIterator` under `TYPE_CHECKING`; `HostError` from `mshkn.errors`.

`src/mshkn/host/ssh.py` — the same helper (`_WRAPPED = (asyncssh.Error, OSError, TimeoutError)`), applied to `exec`, `exec_bg`, `upload`, `metrics`, `warm`, and inside `stream` around the connection acquisition and the pump (a `HostError` raised from within the generator is what the SSE endpoint turns into `error` + `exit 255`). `download`: wrap everything except the `SFTPNoSuchFile` branch, which still raises `FileNotFoundError`:

```python
    async def download(self, vm_ip: str, remote_path: str) -> bytes:
        async with _host_errors("download"):
            conn = await self._pooled(vm_ip)
            try:
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open(remote_path, "rb") as f:
                        data: bytes = await f.read()
                        return data
            except asyncssh.SFTPNoSuchFile:
                raise FileNotFoundError(f"File not found: {remote_path}") from None
```

(Keep the exact SFTP calls the module already makes; only the wrapping changes. Note `FileNotFoundError` is an `OSError`, so it must be raised outside `_host_errors` or excluded: implement `_host_errors` in `ssh.py` with `except FileNotFoundError: raise` before the `_WRAPPED` clause.)

`src/mshkn/host/caddy.py`:

```python
    async def healthy(self) -> bool:
        if self._client.is_closed:
            return False
        try:
            resp = await self._client.get("/config/")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200
```

(Keep whatever URL `healthy()` already probes; only add the `is_closed` guard.)

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_host_errors.py tests/unit/test_caddy.py tests/unit/test_ssh_guest.py tests/unit/test_firecracker_hypervisor.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1`
Expected: pass; clean; previous count + 4.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "fix(host): Firecracker and SSH failures are HostErrors; Caddy healthy() is False after close"
```

---

### Task 4: SlotAllocator

**Files:**
- Create: `src/mshkn/services/__init__.py` (empty), `src/mshkn/services/allocator.py`, `tests/unit/test_allocator.py`
- (`vm/manager.py` keeps its own allocation until Task 10.)

**Interfaces:**
- Produces: `SlotAllocator()` with `async initialize(db, blocks: BlockStore) -> None`, `async acquire() -> tuple[int, int]` (slot, volume id), `async acquire_volume_id() -> int`, `async release_slot(slot: int) -> None`, read-only properties `next_slot`, `next_volume_id`, `free_slots: frozenset[int]`. Slot 254 (`STAGING_SLOT`) is never handed out. Exhaustion raises `LimitExceeded("No free VM slots")`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_allocator.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mshkn.db import insert_account, insert_checkpoint, insert_computer, insert_recipe
from mshkn.errors import LimitExceeded
from mshkn.host.fake import FakeHost
from mshkn.models import Account, Checkpoint, Computer, ComputerStatus, Recipe, RecipeStatus
from mshkn.services.allocator import SlotAllocator

if TYPE_CHECKING:
    import aiosqlite


async def test_fresh_allocator_starts_at_slot_1_and_volume_100() -> None:
    alloc = SlotAllocator()
    assert await alloc.acquire() == (1, 100)
    assert await alloc.acquire() == (2, 101)
    assert await alloc.acquire_volume_id() == 102


async def test_released_slots_are_reused_before_new_ones() -> None:
    alloc = SlotAllocator()
    await alloc.acquire()
    await alloc.acquire()
    await alloc.release_slot(1)
    assert (await alloc.acquire())[0] == 1
    assert (await alloc.acquire())[0] == 3


async def test_staging_slot_is_skipped_and_never_recycled() -> None:
    alloc = SlotAllocator()
    for _ in range(253):
        await alloc.acquire()
    assert (await alloc.acquire())[0] == 255
    await alloc.release_slot(254)  # a bug elsewhere must not put 254 in circulation
    with pytest.raises(LimitExceeded):
        await alloc.acquire()


async def test_initialize_derives_state_from_db_and_pool(db: aiosqlite.Connection) -> None:
    await insert_account(db, Account(id="acct-1", api_key="k", vm_limit=10, created_at="t"))
    await insert_computer(
        db,
        Computer(
            id="comp-a", account_id="acct-1", thin_volume_id=120, tap_device="tap3",
            vm_ip="172.16.3.2", socket_path="/tmp/fc-mshkn-comp-a.socket",
            firecracker_pid=1, status=ComputerStatus.RUNNING, created_at="t", last_exec_at=None,
        ),
    )
    await insert_checkpoint(
        db,
        Checkpoint(
            id="ckpt-a", account_id="acct-1", parent_id=None, computer_id="comp-a",
            thin_volume_id=150, r2_prefix="acct-1/ckpt-a", disk_delta_size_bytes=None,
            memory_size_bytes=None, label=None, pinned=False, created_at="t",
        ),
    )
    await insert_recipe(
        db,
        Recipe(
            id="rcp-a", account_id="acct-1", dockerfile="FROM x", content_hash="h",
            status=RecipeStatus.READY, build_log=None, base_volume_id=160,
            template_vmstate=None, template_memory=None, created_at="t", built_at="t",
        ),
    )
    host = FakeHost()
    host.blocks.volumes[170] = 0  # an orphan the DB does not know about
    alloc = SlotAllocator()
    await alloc.initialize(db, host.blocks)
    assert alloc.next_volume_id == 171
    assert alloc.free_slots == frozenset({1, 2})  # gaps below the highest running slot
    assert (await alloc.acquire())[0] in {1, 2}
    await alloc.release_slot(1)
    await alloc.release_slot(2)
    await alloc.acquire()
    await alloc.acquire()
    assert (await alloc.acquire())[0] == 4  # next after tap3
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_allocator.py -q`
Expected: `ModuleNotFoundError: No module named 'mshkn.services'`.

- [ ] **Step 3: Implement**

`src/mshkn/services/allocator.py`:

```python
"""Slot and volume-id allocation (spec §6.1).

State is derived at startup from the database (running computers, highest
checkpoint and recipe volume) and from the pool itself, so orphaned volumes
the database never heard of cannot be reused.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from mshkn.db import (
    get_max_checkpoint_volume_id,
    get_max_recipe_volume_id,
    list_all_computers,
)
from mshkn.errors import LimitExceeded
from mshkn.host.firecracker import STAGING_SLOT
from mshkn.models import ComputerStatus

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.host import BlockStore

logger = logging.getLogger(__name__)

_FIRST_VOLUME_ID = 100  # volume 0 is the base image; leave room below
_LAST_SLOT = 255


class SlotAllocator:
    def __init__(self) -> None:
        self._next_slot = 1
        self._free_slots: set[int] = set()
        self._next_volume_id = _FIRST_VOLUME_ID
        self._lock = asyncio.Lock()

    @property
    def next_slot(self) -> int:
        return self._next_slot

    @property
    def next_volume_id(self) -> int:
        return self._next_volume_id

    @property
    def free_slots(self) -> frozenset[int]:
        return frozenset(self._free_slots)

    async def initialize(self, db: aiosqlite.Connection, blocks: BlockStore) -> None:
        computers = await list_all_computers(db)
        max_vol = _FIRST_VOLUME_ID - 1
        if computers:
            max_vol = max(max_vol, max(c.thin_volume_id for c in computers))
        running = [c for c in computers if c.status == ComputerStatus.RUNNING]
        if running:
            active = {c.slot for c in running}
            self._next_slot = min(max(active) + 1, _LAST_SLOT + 1)
            self._free_slots = {s for s in range(1, self._next_slot) if s not in active}
        else:
            self._next_slot = 1
            self._free_slots = set()
        self._free_slots.discard(STAGING_SLOT)
        for candidate in (
            await get_max_checkpoint_volume_id(db),
            await get_max_recipe_volume_id(db),
            await blocks.max_volume_id(),
        ):
            if candidate is not None:
                max_vol = max(max_vol, candidate)
        self._next_volume_id = max_vol + 1
        logger.info(
            "allocator initialized: next_slot=%d free=%d next_volume_id=%d",
            self._next_slot,
            len(self._free_slots),
            self._next_volume_id,
        )

    async def acquire(self) -> tuple[int, int]:
        async with self._lock:
            return self._take_slot(), self._take_volume_id()

    async def acquire_volume_id(self) -> int:
        async with self._lock:
            return self._take_volume_id()

    async def release_slot(self, slot: int) -> None:
        async with self._lock:
            if slot != STAGING_SLOT:
                self._free_slots.add(slot)

    def _take_slot(self) -> int:
        self._free_slots.discard(STAGING_SLOT)
        if self._free_slots:
            return self._free_slots.pop()
        slot = self._next_slot
        if slot == STAGING_SLOT:
            slot = STAGING_SLOT + 1
        if slot > _LAST_SLOT:
            raise LimitExceeded("No free VM slots")
        self._next_slot = slot + 1
        return slot

    def _take_volume_id(self) -> int:
        volume_id = self._next_volume_id
        self._next_volume_id += 1
        return volume_id
```

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_allocator.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1`
Expected: `4 passed`; clean; previous + 4.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(services): SlotAllocator"
```

---

### Task 5: RecipeService

**Files:**
- Create: `src/mshkn/services/recipes.py`, `tests/unit/test_recipe_service.py`
- Delete: `src/mshkn/recipe/` (package), `tests/unit/test_recipe_builder.py` (its one test moves into the new file)
- Modify: `src/mshkn/api/recipes.py` (import `dockerfile_content_hash` and `build_recipe`'s replacement from the service module; the router itself is rewritten in Task 10, so here only change the import and the `_run_build` body to call `RecipeService.build` on a temporary instance — see Step 3), `src/mshkn/vm/manager.py` (no change: it keeps its own `_ensure_template` until Task 10)

**Interfaces:**
- Produces: `dockerfile_content_hash(dockerfile) -> str`; `BuildImageFn = Callable[[str], Awaitable[str]]`; `docker_build_image(cmd) -> str` (module function, 600 s timeout, raises `RuntimeError` on non-zero exit or timeout); `RecipeService(config, db, blocks, hypervisor, allocator, tasks, *, run: RunFn = shell.run, build_image: BuildImageFn = docker_build_image)` with:
  - `async create(account, dockerfile) -> tuple[Recipe, bool]` — `(existing, False)` on a hash hit, else inserts a pending recipe, allocates a volume id, spawns the build under the per-account lock, returns `(recipe, True)`.
  - `async get(account, recipe_id) -> Recipe` (`NotFound`), `async list(account) -> list[Recipe]`.
  - `async delete(account, recipe_id) -> None` (`NotFound`; `Conflict` when referenced; removes `mshkn-recipe-<id>` when `base_volume_id` is set).
  - `async resolve(recipe_id) -> Recipe` — `NotFound` if unknown, `Conflict` if not ready or without a base volume (what `ComputerService.create` needs).
  - `async ensure_template(recipe: Recipe | None) -> SnapshotFiles | None` — cached paths, else build under a per-key lock (`recipe.id` or `"bare"`), cache, return; `None` on build failure (logged).
  - `async build(recipe_id, dockerfile, content_hash, volume_id) -> None` — the pipeline, moved from `recipe/builder.py`.
  - `build_task_name(recipe_id) -> str` = `f"recipe_build:{recipe_id}"`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_recipe_service.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import get_recipe, insert_account, insert_computer
from mshkn.errors import Conflict, NotFound
from mshkn.host.fake import FakeHost
from mshkn.models import Account, Computer, ComputerStatus, RecipeStatus
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.recipes import RecipeService, dockerfile_content_hash

if TYPE_CHECKING:
    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")


class FakeShell:
    """Records commands; can be told to fail one of them."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            raise RuntimeError(f"failed: {cmd}")
        return ""


def _service(
    db: aiosqlite.Connection, tmp_path: Path, *, shell: FakeShell | None = None, build_ok: bool = True
) -> tuple[RecipeService, FakeHost, FakeShell]:
    host = FakeHost()
    shell = shell or FakeShell()

    async def build_image(cmd: str) -> str:
        if not build_ok:
            raise RuntimeError("docker build failed (rc=1):\nboom")
        return "Successfully built"

    config = Config(ssh_key_path=tmp_path / "id_ed25519", checkpoint_local_dir=tmp_path / "ckpts")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    service = RecipeService(
        config, db, host.blocks, host.hypervisor, SlotAllocator(), BackgroundTasks(),
        run=shell, build_image=build_image,
    )
    return service, host, shell


def test_dockerfile_content_hash() -> None:
    assert dockerfile_content_hash("FROM a") == dockerfile_content_hash("FROM a")
    assert dockerfile_content_hash("FROM a") != dockerfile_content_hash("FROM b")
    assert len(dockerfile_content_hash("x")) == 64


async def test_create_builds_through_the_state_machine_to_ready(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, shell = _service(db, tmp_path)
    recipe, created = await service.create(ACCOUNT, "FROM mshkn-base\nRUN true")
    assert created and recipe.status is RecipeStatus.PENDING
    await service.tasks.wait(service.build_task_name(recipe.id))
    stored = await get_recipe(db, recipe.id)
    assert stored is not None and stored.status is RecipeStatus.READY
    assert stored.base_volume_id == 100 and stored.built_at is not None
    assert host.blocks.volumes[100] == 0
    assert f"mshkn-recipe-{recipe.id}" not in host.blocks.active  # deactivated after inject
    assert ("mkfs", f"mshkn-recipe-{recipe.id}") in host.blocks.calls
    assert any(c.startswith("docker create --name tmp-") for c in shell.calls)
    assert any(c.startswith("docker export -o ") for c in shell.calls)
    assert any(c.startswith("tar xf ") and "-C " in c for c in shell.calls)


async def test_failed_build_records_the_log_and_leaves_no_device(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, _ = _service(db, tmp_path, build_ok=False)
    recipe, _ = await service.create(ACCOUNT, "FROM nope")
    await service.tasks.wait(service.build_task_name(recipe.id))
    stored = await get_recipe(db, recipe.id)
    assert stored is not None and stored.status is RecipeStatus.FAILED
    assert stored.build_log is not None and "boom" in stored.build_log
    assert stored.base_volume_id is None
    assert host.blocks.active == {}


async def test_inject_failure_after_activate_deactivates_the_device(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, _ = _service(db, tmp_path, shell=FakeShell(fail_on="tar xf"))
    recipe, _ = await service.create(ACCOUNT, "FROM x")
    await service.tasks.wait(service.build_task_name(recipe.id))
    stored = await get_recipe(db, recipe.id)
    assert stored is not None and stored.status is RecipeStatus.FAILED
    assert host.blocks.active == {}


async def test_create_dedupes_by_content_hash(db: aiosqlite.Connection, tmp_path: Path) -> None:
    await insert_account(db, ACCOUNT)
    service, _, _ = _service(db, tmp_path)
    first, created1 = await service.create(ACCOUNT, "FROM same")
    again, created2 = await service.create(ACCOUNT, "FROM same")
    assert created1 and not created2 and again.id == first.id


async def test_failed_recipe_is_replaced_on_retry(db: aiosqlite.Connection, tmp_path: Path) -> None:
    await insert_account(db, ACCOUNT)
    failing, _, _ = _service(db, tmp_path, build_ok=False)
    first, _ = await failing.create(ACCOUNT, "FROM retry")
    await failing.tasks.wait(failing.build_task_name(first.id))
    ok, _, _ = _service(db, tmp_path)
    second, created = await ok.create(ACCOUNT, "FROM retry")
    assert created and second.id != first.id
    assert await get_recipe(db, first.id) is None


async def test_resolve_rejects_unknown_and_not_ready(db: aiosqlite.Connection, tmp_path: Path) -> None:
    await insert_account(db, ACCOUNT)
    service, _, _ = _service(db, tmp_path, build_ok=False)
    with pytest.raises(NotFound):
        await service.resolve("rcp-nope")
    recipe, _ = await service.create(ACCOUNT, "FROM x")
    with pytest.raises(Conflict):
        await service.resolve(recipe.id)  # still pending


async def test_delete_refuses_referenced_recipes_and_removes_the_volume(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, _ = _service(db, tmp_path)
    recipe, _ = await service.create(ACCOUNT, "FROM x")
    await service.tasks.wait(service.build_task_name(recipe.id))
    await insert_computer(
        db,
        Computer(
            id="comp-1", account_id="acct-1", thin_volume_id=101, tap_device="tap1",
            vm_ip="172.16.1.2", socket_path="/tmp/s", firecracker_pid=1,
            status=ComputerStatus.RUNNING, created_at="t", last_exec_at=None, recipe_id=recipe.id,
        ),
    )
    with pytest.raises(Conflict):
        await service.delete(ACCOUNT, recipe.id)
    await db.execute("UPDATE computers SET status = 'destroyed'")
    await db.commit()
    await service.delete(ACCOUNT, recipe.id)
    assert ("remove", (100, f"mshkn-recipe-{recipe.id}")) in host.blocks.calls
    with pytest.raises(NotFound):
        await service.get(ACCOUNT, recipe.id)


async def test_ensure_template_builds_once_under_concurrency(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host, _ = _service(db, tmp_path)
    results = await asyncio.gather(*(service.ensure_template(None) for _ in range(3)))
    assert len(host.hypervisor.snapshots) == 1
    assert all(r is not None and r.vmstate.exists() for r in results)
    assert await service.ensure_template(None) == results[0]  # cached in the DB now


async def test_ensure_template_returns_none_when_the_build_fails(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host, _ = _service(db, tmp_path)
    host.hypervisor.fail_next("build_template")
    assert await service.ensure_template(None) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_recipe_service.py -q`
Expected: `ModuleNotFoundError: No module named 'mshkn.services.recipes'`.

- [ ] **Step 3: Implement**

`src/mshkn/services/recipes.py` — the pipeline is `recipe/builder.py`'s `build_recipe` and `_post_process_rootfs` moved verbatim into the class and module, with the docker build extracted so tests can inject it:

```python
"""Recipes: Docker build → export → dm-thin inject, and L3 template caching (spec §6.6)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import shutil
import subprocess
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.db import (
    cache_bare_template,
    count_recipe_references,
    delete_failed_recipes_by_hash,
    delete_recipe,
    get_bare_template,
    get_recipe,
    get_recipe_by_content_hash,
    insert_recipe,
    list_recipes_by_account,
    update_recipe_build_result,
    update_recipe_status,
    update_recipe_template,
)
from mshkn.errors import Conflict, NotFound
from mshkn.host import SnapshotFiles
from mshkn.host.shell import run as shell_run
from mshkn.models import Recipe, RecipeStatus
from mshkn.observability.metrics import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import BlockStore, Hypervisor
    from mshkn.host.shell import RunFn
    from mshkn.models import Account
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.allocator import SlotAllocator

    BuildImageFn = Callable[[str], Awaitable[str]]

logger = logging.getLogger(__name__)

_DOCKER_BUILD_TIMEOUT_SECONDS = 600


def dockerfile_content_hash(dockerfile: str) -> str:
    """SHA-256 hex digest of the Dockerfile text."""
    return hashlib.sha256(dockerfile.encode()).hexdigest()


async def docker_build_image(cmd: str) -> str:
    """Run `docker build …`, returning its combined output; raise on failure or timeout."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            ),
            timeout=_DOCKER_BUILD_TIMEOUT_SECONDS,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_BUILD_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise RuntimeError("docker build timed out after 10 minutes") from exc
    output = stdout.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"docker build failed (rc={proc.returncode}):\n{output}")
    return output


class RecipeService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        blocks: BlockStore,
        hypervisor: Hypervisor,
        allocator: SlotAllocator,
        tasks: BackgroundTasks,
        *,
        run: RunFn = shell_run,
        build_image: BuildImageFn = docker_build_image,
    ) -> None:
        self.config = config
        self.db = db
        self.blocks = blocks
        self.hypervisor = hypervisor
        self.allocator = allocator
        self.tasks = tasks
        self._run = run
        self._build_image = build_image
        self._build_locks: dict[str, asyncio.Lock] = {}  # per account
        self._template_locks: dict[str, asyncio.Lock] = {}  # per recipe id / "bare"

    @staticmethod
    def build_task_name(recipe_id: str) -> str:
        return f"recipe_build:{recipe_id}"

    # -- CRUD ----------------------------------------------------------------

    async def create(self, account: Account, dockerfile: str) -> tuple[Recipe, bool]:
        content_hash = dockerfile_content_hash(dockerfile)
        existing = await get_recipe_by_content_hash(self.db, account.id, content_hash)
        if existing is not None:
            return existing, False
        await delete_failed_recipes_by_hash(self.db, account.id, content_hash)
        recipe = Recipe(
            id=f"rcp-{uuid.uuid4().hex[:12]}",
            account_id=account.id,
            dockerfile=dockerfile,
            content_hash=content_hash,
            status=RecipeStatus.PENDING,
            build_log=None,
            base_volume_id=None,
            template_vmstate=None,
            template_memory=None,
            created_at=datetime.now(UTC).isoformat(),
            built_at=None,
        )
        await insert_recipe(self.db, recipe)
        volume_id = await self.allocator.acquire_volume_id()
        lock = self._build_locks.setdefault(account.id, asyncio.Lock())

        async def _run_build() -> None:
            async with lock, timed("recipe_build"):
                await self.build(recipe.id, dockerfile, content_hash, volume_id)

        self.tasks.spawn(_run_build(), name=self.build_task_name(recipe.id))
        return recipe, True

    async def get(self, account: Account, recipe_id: str) -> Recipe:
        recipe = await get_recipe(self.db, recipe_id)
        if recipe is None or recipe.account_id != account.id:
            raise NotFound("Recipe not found")
        return recipe

    async def list(self, account: Account) -> list[Recipe]:
        return await list_recipes_by_account(self.db, account.id)

    async def delete(self, account: Account, recipe_id: str) -> None:
        recipe = await self.get(account, recipe_id)
        refs = await count_recipe_references(self.db, recipe_id)
        if refs > 0:
            raise Conflict(f"Recipe is referenced by {refs} computer(s)/checkpoint(s)")
        if recipe.base_volume_id is not None:
            await self.blocks.remove(
                volume_id=recipe.base_volume_id, name=f"mshkn-recipe-{recipe.id}"
            )
        await delete_recipe(self.db, recipe_id)

    async def resolve(self, recipe_id: str) -> Recipe:
        """The recipe a computer can be created from, or the reason it cannot."""
        recipe = await get_recipe(self.db, recipe_id)
        if recipe is None:
            raise NotFound(f"Recipe {recipe_id} not found")
        if recipe.status != RecipeStatus.READY:
            raise Conflict(f"Recipe {recipe_id} is not ready (status={recipe.status})")
        if recipe.base_volume_id is None:
            raise Conflict(f"Recipe {recipe_id} has no base volume")
        return recipe

    # -- L3 templates --------------------------------------------------------

    async def ensure_template(self, recipe: Recipe | None) -> SnapshotFiles | None:
        """The template snapshot for a recipe (or the bare base), built at most once.

        Concurrent first callers share one build through a per-key lock; a
        build failure logs a warning and returns None so the caller cold-boots.
        """
        key = recipe.id if recipe is not None else "bare"
        lock = self._template_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await self._cached_template(recipe)
            if cached is not None:
                return cached
            if recipe is not None:
                source_volume_id = recipe.base_volume_id or 0
            else:
                source_volume_id = 0
            dest_dir = self.config.checkpoint_local_dir / "templates" / key
            try:
                files = await self.hypervisor.build_template(
                    disk_volume_id=source_volume_id, dest_dir=dest_dir
                )
            except Exception:
                logger.warning("L3 template build failed for %s, will cold-boot", key)
                return None
            if recipe is not None:
                await update_recipe_template(
                    self.db, recipe.id, str(files.vmstate), str(files.memory)
                )
            else:
                await cache_bare_template(self.db, str(files.vmstate), str(files.memory))
            logger.info("Built L3 template for %s", key)
            return files

    async def _cached_template(self, recipe: Recipe | None) -> SnapshotFiles | None:
        if recipe is not None:
            fresh = await get_recipe(self.db, recipe.id)  # another caller may have cached it
            if fresh is not None and fresh.template_vmstate and fresh.template_memory:
                return SnapshotFiles(
                    vmstate=Path(fresh.template_vmstate), memory=Path(fresh.template_memory)
                )
            return None
        bare = await get_bare_template(self.db)
        if bare is not None:
            return SnapshotFiles(vmstate=Path(bare[0]), memory=Path(bare[1]))
        return None

    # -- build pipeline ------------------------------------------------------

    async def build(
        self, recipe_id: str, dockerfile: str, content_hash: str, volume_id: int
    ) -> None:
        """Docker build → export → inject into dm-thin → ready; failed with a log otherwise."""
        build_dir = Path(f"/tmp/mshkn-build-{content_hash}")
        tar_path = build_dir / "rootfs.tar"
        container_name = f"tmp-{recipe_id}"
        volume_name = f"mshkn-recipe-{recipe_id}"
        image_tag = f"mshkn-recipe-img-{recipe_id}"
        device_active = False
        build_log_lines: list[str] = []
        try:
            await update_recipe_status(self.db, recipe_id, RecipeStatus.BUILDING)
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "Dockerfile").write_text(dockerfile)
            pub_key_path = self.config.ssh_key_path.with_suffix(".pub")
            if pub_key_path.exists():
                shutil.copy(pub_key_path, build_dir / "mshkn_key.pub")
            else:
                (build_dir / "mshkn_key.pub").write_text("")
            build_cmd = f"docker build --memory=4g --cpuset-cpus=0-1 -t {image_tag} {build_dir}"
            build_log_lines.append(await self._build_image(build_cmd))

            await update_recipe_status(self.db, recipe_id, RecipeStatus.EXPORTING)
            await self._run(f"docker create --name {container_name} {image_tag}")
            await self._run(f"docker export -o {tar_path} {container_name}")
            await self._run(f"docker rm {container_name}")

            await update_recipe_status(self.db, recipe_id, RecipeStatus.INJECTING)
            await self.blocks.snap(source_volume_id=0, new_volume_id=volume_id)
            await self.blocks.activate(volume_id=volume_id, name=volume_name)
            device_active = True
            await self.blocks.mkfs(volume_name)
            async with self.blocks.mounted(volume_name) as mount_point:
                await self._run(f"tar xf {tar_path} -C {mount_point}")
                _post_process_rootfs(mount_point, self.config)
            await self.blocks.deactivate(volume_name)
            device_active = False

            await update_recipe_build_result(
                self.db,
                recipe_id,
                status=RecipeStatus.READY,
                build_log="\n".join(build_log_lines),
                base_volume_id=volume_id,
                built_at=datetime.now(UTC).isoformat(),
            )
            logger.info("recipe %s: ready (vol %d)", recipe_id, volume_id)
        except Exception as exc:
            build_log_lines.append(f"\n--- BUILD FAILED ---\n{traceback.format_exc()}")
            logger.error("recipe %s: build failed: %s", recipe_id, exc)
            await update_recipe_build_result(
                self.db, recipe_id, status=RecipeStatus.FAILED, build_log="\n".join(build_log_lines)
            )
        finally:
            if device_active:
                with contextlib.suppress(Exception):
                    await self.blocks.deactivate(volume_name)
            shutil.rmtree(build_dir, ignore_errors=True)
            with contextlib.suppress(Exception):
                await self._run(f"docker rm {container_name}", check=False)
            with contextlib.suppress(Exception):
                await self._run(f"docker rmi {image_tag}", check=False)


def _post_process_rootfs(mount_point: Path, config: Config) -> None:
    """Force-write the Firecracker-required config into an exported rootfs."""
    ...  # the body of recipe/builder.py::_post_process_rootfs, verbatim, with
    ...  # `mp = mount_point` and the two inner `import re` lines removed (re is
    ...  # imported at the top). It stays synchronous; the caller runs it inline
    ...  # exactly as the old builder did.
```

Notes for the implementer: the two `...` lines above are not placeholders to invent; they mark a verbatim move of the existing `_post_process_rootfs` body (from `subprocess.run(["ssh-keygen", …])` through the `fcnet.service` symlink). The old builder's `subprocess.run(["docker", "rm", …])` cleanups become `self._run(…, check=False)` so tests can observe them and no synchronous subprocess call remains in an async path. The `tar_path.unlink` cleanup is subsumed by `rmtree(build_dir)`.

`src/mshkn/api/recipes.py` (interim, until Task 10): import `dockerfile_content_hash` from `mshkn.services.recipes`; in `create_recipe` replace the `_run_build` body with a temporary `RecipeService(config, db, rt.host.blocks, rt.host.hypervisor, SlotAllocator(), rt.tasks)` whose `build` is called with the same arguments. This keeps the tree green with one build implementation; Task 10 replaces it with `rt.recipes.create`.

Delete `src/mshkn/recipe/` and `tests/unit/test_recipe_builder.py`.

- [ ] **Step 4: Verify**

```bash
grep -rn "mshkn.recipe\b\|recipe.builder" src tests || echo "recipe package gone"
uv run pytest tests/unit/test_recipe_service.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1
```

Expected: gone; `10 passed`; clean; previous + 10 − 1.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(services): RecipeService with a deduplicated template build; recipe/ package removed"
```

---

### Task 6: ComputerService

**Files:**
- Create: `src/mshkn/services/computers.py`, `tests/unit/test_computer_service.py`
- (`vm/manager.py` and the routers are untouched until Task 10.)

**Interfaces:**
- Produces: `ComputerService(config, db, host, allocator, recipes: RecipeService)` with:
  - `async create(account, *, recipe_id: str | None, resources: Resources) -> Computer` — `LimitExceeded` at the VM limit; `NotFound`/`Conflict` from `recipes.resolve`; leak-free on failure (below); `computers_created_total{source="create"}`; `timed("create")`.
  - `async fork(account, checkpoint, *, recipe_id: str | None) -> Computer` — `Conflict` when the checkpoint has no disk snapshot; downloads snapshot files on a local miss; cold-boots a merge checkpoint; `computers_created_total{source="fork"}`; `timed("fork")`.
  - `async destroy(computer_id) -> None` — `NotFound` if unknown; no-op if already destroyed; `timed("destroy")`.
  - `async cleanup_dead(computer) -> None` — the reaper's path (no kill; every step best-effort).
  - `async get_owned(account, computer_id) -> Computer` — `NotFound` if missing, another account's, or destroyed.
  - `async get_running(account, computer_id) -> Computer` — `NotFound` as above for missing/foreign; `BadRequest(f"Computer is {status}")` when not running.
  - `async exec(computer, command, *, timeout=300.0) -> ExecResult`; `stream(computer, command) -> AsyncIterator[OutputLine]`; `async exec_bg(computer, command) -> int`; `async exec_logs(computer, pid) -> list[str]`; `async exec_kill(computer, pid) -> ExecResult`; `async upload(computer, path, data)`; `async download(computer, path) -> bytes` (`NotFound` on a missing file); `async metrics(computer) -> VmMetrics | None` (bounded by `STATUS_METRICS_TIMEOUT_SECONDS = 15.0`, `None` on timeout or failure, logged).
  - `async active_count(account_id) -> int`; `async active_count_total() -> int`; `async refresh_active_gauge() -> int`.
- Failure contract for `create`/`fork`: after the disk snap succeeds, any exception (a) kills the VM if one came up, (b) removes the proxy route if it was added, (c) removes the volume, (d) tears down the slot's tap, (e) releases the slot, (f) re-raises `HostError` (chaining the original) unless the original already is a `MshknError`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_computer_service.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import get_computer, insert_account, insert_checkpoint
from mshkn.errors import BadRequest, HostError, LimitExceeded, NotFound
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import Account, Checkpoint, ComputerStatus
from mshkn.observability.metrics import computers_active
from mshkn.resources import DEFAULT_RESOURCES, Resources
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.computers import ComputerService
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=2, created_at="t")


async def _service(db: aiosqlite.Connection, tmp_path: Path) -> tuple[ComputerService, FakeHost]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    allocator = SlotAllocator()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, BackgroundTasks())
    return ComputerService(config, db, host, allocator, recipes), host


async def test_create_default_resources_restores_from_the_template(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    assert computer.status is ComputerStatus.RUNNING and computer.slot == 1
    assert host.blocks.volumes[computer.thin_volume_id] == 0
    assert host.hypervisor.restored[0][0] == computer.thin_volume_id and host.hypervisor.booted == []
    assert host.guest.warmed == [computer.vm_ip]
    assert host.proxy.routes == {computer.id: computer.vm_ip}
    assert await service.active_count_total() == 1
    assert computers_active._value.get() == 1  # gauge set from the DB


async def test_create_custom_resources_cold_boots(db: aiosqlite.Connection, tmp_path: Path) -> None:
    service, host = await _service(db, tmp_path)
    big = Resources(mem_mib=1024, vcpus=4)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=big)
    assert host.hypervisor.booted == [(computer.thin_volume_id, big)]
    assert host.hypervisor.restored == []


async def test_create_enforces_the_vm_limit(db: aiosqlite.Connection, tmp_path: Path) -> None:
    service, _ = await _service(db, tmp_path)
    await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    with pytest.raises(LimitExceeded):
        await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)


async def test_create_unknown_recipe_leaves_no_host_state(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    with pytest.raises(NotFound):
        await service.create(ACCOUNT, recipe_id="rcp-nope", resources=DEFAULT_RESOURCES)
    assert host.blocks.volumes == {0: None} and service.allocator.free_slots == frozenset()


async def test_boot_failure_after_snap_releases_volume_and_slot(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    host.hypervisor.fail_next("boot")
    with pytest.raises(HostError):
        await service.create(ACCOUNT, recipe_id=None, resources=Resources(mem_mib=512, vcpus=1))
    assert host.blocks.volumes == {0: None}, "the snapped volume must be removed"
    assert host.hypervisor.torn_down == [1]
    assert service.allocator.free_slots == frozenset({1})
    assert await service.active_count_total() == 0
    # the slot is reusable and the next create works
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    assert computer.slot == 1


async def test_route_failure_after_boot_kills_the_vm_and_cleans_up(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    host.proxy.fail_next("add_route")
    with pytest.raises(HostError):
        await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    assert host.hypervisor.alive == {}
    assert host.blocks.volumes == {0: None}
    assert host.proxy.routes == {}
    assert await service.active_count_total() == 0


async def test_destroy_releases_everything_and_is_idempotent(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await service.destroy(computer.id)
    assert host.hypervisor.alive == {} and host.proxy.routes == {}
    assert computer.thin_volume_id not in host.blocks.volumes
    assert host.hypervisor.torn_down == [computer.slot]
    assert host.guest.evicted == [computer.vm_ip]
    assert service.allocator.free_slots == frozenset({computer.slot})
    await service.destroy(computer.id)  # no error, nothing repeated
    assert host.hypervisor.torn_down == [computer.slot]
    with pytest.raises(NotFound):
        await service.destroy("comp-nope")
    assert computers_active._value.get() == 0


async def test_fork_restores_from_checkpoint_files_or_downloads_them(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    await host.blocks.snap(source_volume_id=0, new_volume_id=50)
    ckpt = Checkpoint(
        id="ckpt-1", account_id="acct-1", parent_id=None, computer_id=None, thin_volume_id=50,
        r2_prefix="acct-1/ckpt-1", disk_delta_size_bytes=None, memory_size_bytes=None,
        label=None, pinned=False, created_at="t",
    )
    await insert_checkpoint(db, ckpt)
    host.objects.prefixes["acct-1/ckpt-1"] = {"vmstate": b"v", "memory": b"m"}
    computer = await service.fork(ACCOUNT, ckpt, recipe_id=None)
    assert computer.source_checkpoint_id == "ckpt-1"
    assert host.hypervisor.restored[-1][0] == computer.thin_volume_id
    assert (tmp_path / "ckpts" / "ckpt-1" / "vmstate").read_bytes() == b"v"


async def test_fork_of_a_merge_checkpoint_cold_boots(db: aiosqlite.Connection, tmp_path: Path) -> None:
    service, host = await _service(db, tmp_path)
    await host.blocks.snap(source_volume_id=0, new_volume_id=50)
    ckpt = Checkpoint(
        id="ckpt-m", account_id="acct-1", parent_id=None, computer_id=None, thin_volume_id=50,
        r2_prefix="acct-1/ckpt-m", disk_delta_size_bytes=None, memory_size_bytes=None,
        label="merge", pinned=False, created_at="t",
    )
    await insert_checkpoint(db, ckpt)
    computer = await service.fork(ACCOUNT, ckpt, recipe_id=None)
    assert host.hypervisor.booted == [(computer.thin_volume_id, DEFAULT_RESOURCES)]


async def test_guest_operations_touch_last_exec_at_and_map_errors(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["true"] = ExecResult(0, "", "")
    assert (await service.exec(computer, "true")).exit_code == 0
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.last_exec_at is not None
    with pytest.raises(NotFound):
        await service.download(computer, "/nope")
    await service.destroy(computer.id)
    with pytest.raises(BadRequest):
        await service.get_running(ACCOUNT, computer.id)
    with pytest.raises(NotFound):
        await service.get_owned(ACCOUNT, computer.id)
    with pytest.raises(NotFound):
        await service.get_running(Account(id="other", api_key="o", vm_limit=1, created_at="t"), computer.id)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_computer_service.py -q`
Expected: `ModuleNotFoundError: No module named 'mshkn.services.computers'`.

- [ ] **Step 3: Implement**

`src/mshkn/services/computers.py`:

```python
"""Computers: create, fork, destroy, and guest operations (spec §6.2)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mshkn.db import (
    count_active_computers,
    count_active_computers_by_account,
    get_computer,
    insert_computer,
    update_computer_status,
    update_last_exec_at,
)
from mshkn.errors import BadRequest, Conflict, HostError, LimitExceeded, MshknError, NotFound
from mshkn.host import SnapshotFiles
from mshkn.models import Computer, ComputerStatus
from mshkn.observability.metrics import computers_active, computers_created_total, timed
from mshkn.resources import DEFAULT_RESOURCES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import ExecResult, Host, OutputLine, RunningVM, VmMetrics
    from mshkn.models import Account, Checkpoint, Recipe
    from mshkn.resources import Resources
    from mshkn.services.allocator import SlotAllocator
    from mshkn.services.recipes import RecipeService

logger = logging.getLogger(__name__)

STATUS_METRICS_TIMEOUT_SECONDS = 15.0


class ComputerService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        host: Host,
        allocator: SlotAllocator,
        recipes: RecipeService,
    ) -> None:
        self.config = config
        self.db = db
        self.host = host
        self.allocator = allocator
        self.recipes = recipes

    # -- lookups -------------------------------------------------------------

    async def get_owned(self, account: Account, computer_id: str) -> Computer:
        computer = await get_computer(self.db, computer_id)
        if (
            computer is None
            or computer.account_id != account.id
            or computer.status == ComputerStatus.DESTROYED
        ):
            raise NotFound("Computer not found")
        return computer

    async def get_running(self, account: Account, computer_id: str) -> Computer:
        computer = await get_computer(self.db, computer_id)
        if computer is None or computer.account_id != account.id:
            raise NotFound("Computer not found")
        if computer.status != ComputerStatus.RUNNING:
            raise BadRequest(f"Computer is {computer.status}")
        return computer

    async def active_count(self, account_id: str) -> int:
        return await count_active_computers_by_account(self.db, account_id)

    async def active_count_total(self) -> int:
        return await count_active_computers(self.db)

    async def refresh_active_gauge(self) -> int:
        total = await self.active_count_total()
        computers_active.set(total)
        return total

    # -- create / fork -------------------------------------------------------

    async def create(
        self, account: Account, *, recipe_id: str | None, resources: Resources
    ) -> Computer:
        if await self.active_count(account.id) >= account.vm_limit:
            raise LimitExceeded("VM limit reached")
        recipe: Recipe | None = None
        if recipe_id is not None:
            recipe = await self.recipes.resolve(recipe_id)
        source_volume_id = recipe.base_volume_id if recipe is not None else 0
        assert source_volume_id is not None  # resolve() guarantees a base volume
        async with timed("create"):
            computer = await self._bring_up(
                account,
                source_volume_id=source_volume_id,
                recipe_id=recipe_id,
                source_checkpoint=None,
                resources=resources,
                files_for=lambda: self._template_for(recipe, resources),
            )
        computers_created_total.labels(source="create").inc()
        logger.info("Created computer %s (slot=%d, ip=%s)", computer.id, computer.slot, computer.vm_ip)
        return computer

    async def fork(
        self, account: Account, checkpoint: Checkpoint, *, recipe_id: str | None
    ) -> Computer:
        if checkpoint.thin_volume_id is None:
            raise Conflict(f"Checkpoint {checkpoint.id} has no disk snapshot")
        effective_recipe_id = recipe_id if recipe_id is not None else checkpoint.recipe_id
        async with timed("fork"):
            computer = await self._bring_up(
                account,
                source_volume_id=checkpoint.thin_volume_id,
                recipe_id=effective_recipe_id,
                source_checkpoint=checkpoint,
                resources=DEFAULT_RESOURCES,
                files_for=lambda: self._snapshot_files_for(checkpoint),
            )
        computers_created_total.labels(source="fork").inc()
        logger.info(
            "Forked computer %s from checkpoint %s (slot=%d, ip=%s)",
            computer.id, checkpoint.id, computer.slot, computer.vm_ip,
        )
        return computer

    async def _template_for(
        self, recipe: Recipe | None, resources: Resources
    ) -> SnapshotFiles | None:
        # Templates bake in the default resources; anything else cold-boots.
        if not resources.is_default:
            logger.info(
                "Cold-booting with custom resources: mem=%dMiB, vcpu=%d",
                resources.mem_mib, resources.vcpus,
            )
            return None
        return await self.recipes.ensure_template(recipe)

    async def _snapshot_files_for(self, checkpoint: Checkpoint) -> SnapshotFiles | None:
        ckpt_dir = self.config.checkpoint_local_dir / checkpoint.id
        files = SnapshotFiles(vmstate=ckpt_dir / "vmstate", memory=ckpt_dir / "memory")
        if files.vmstate.exists() and files.memory.exists():
            return files
        if not checkpoint.r2_prefix:
            logger.info("Checkpoint %s has no R2 prefix, will cold-boot", checkpoint.id)
            return None
        try:
            await self.host.objects.download_dir(checkpoint.r2_prefix, ckpt_dir)
        except Exception:
            logger.info("No snapshot files for checkpoint %s, will cold-boot", checkpoint.id)
            return None
        if files.vmstate.exists() and files.memory.exists():
            return files
        return None

    async def _bring_up(
        self,
        account: Account,
        *,
        source_volume_id: int,
        recipe_id: str | None,
        source_checkpoint: Checkpoint | None,
        resources: Resources,
        files_for: Callable[[], Awaitable[SnapshotFiles | None]],
    ) -> Computer:
        """Snap the disk, boot or restore, warm SSH, record, route.

        Everything after the snap is guarded: on any failure the VM (if any)
        is killed, the route removed, the volume removed, the tap torn down,
        the slot released, and the error re-raised as HostError.
        """
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
        volume_name = f"mshkn-{computer_id}"
        slot, volume_id = await self.allocator.acquire()
        try:
            await self.host.blocks.snap(source_volume_id=source_volume_id, new_volume_id=volume_id)
        except BaseException:
            await self.allocator.release_slot(slot)
            raise
        vm: RunningVM | None = None
        routed = False
        try:
            files = await files_for()
            if files is not None:
                vm = await self.host.hypervisor.restore(
                    slot=slot, disk_volume_id=volume_id, disk_name=volume_name, snapshot=files
                )
            else:
                vm = await self.host.hypervisor.boot(
                    slot=slot, disk_volume_id=volume_id, disk_name=volume_name, resources=resources
                )
            await self.host.guest.warm(vm.vm_ip)
            computer = Computer(
                id=computer_id,
                account_id=account.id,
                thin_volume_id=volume_id,
                tap_device=vm.tap_device,
                vm_ip=vm.vm_ip,
                socket_path=vm.socket_path,
                firecracker_pid=vm.pid,
                status=ComputerStatus.RUNNING,
                created_at=datetime.now(UTC).isoformat(),
                last_exec_at=None,
                source_checkpoint_id=source_checkpoint.id if source_checkpoint else None,
                recipe_id=recipe_id,
            )
            await insert_computer(self.db, computer)
            await self.host.proxy.add_route(computer_id, vm.vm_ip)
            routed = True
        except BaseException as exc:
            await self._abandon(computer_id, slot, volume_id, volume_name, vm, routed)
            if isinstance(exc, MshknError | asyncio.CancelledError):
                raise
            raise HostError(f"bring-up of {computer_id} failed: {type(exc).__name__}: {exc}") from exc
        await self.refresh_active_gauge()
        return computer

    async def _abandon(
        self,
        computer_id: str,
        slot: int,
        volume_id: int,
        volume_name: str,
        vm: RunningVM | None,
        routed: bool,
    ) -> None:
        """Best-effort release of everything _bring_up acquired. Never raises."""
        logger.warning("Abandoning computer %s after a failed bring-up", computer_id)
        if routed:
            await self.host.proxy.remove_route(computer_id)
        if vm is not None:
            try:
                await self.host.hypervisor.kill(vm.pid)
            except Exception:
                logger.debug("kill during abandon failed for %s", computer_id, exc_info=True)
            try:
                await self.host.guest.evict(vm.vm_ip)
            except Exception:
                logger.debug("evict during abandon failed for %s", computer_id, exc_info=True)
        await self.host.blocks.remove(volume_id=volume_id, name=volume_name)
        try:
            await self.host.hypervisor.teardown_slot(slot)
        except Exception:
            logger.debug("teardown during abandon failed for slot %d", slot, exc_info=True)
        await self.allocator.release_slot(slot)
        stored = await get_computer(self.db, computer_id)
        if stored is not None:
            await update_computer_status(self.db, computer_id, ComputerStatus.DESTROYED)

    # -- destroy -------------------------------------------------------------

    async def destroy(self, computer_id: str) -> None:
        computer = await get_computer(self.db, computer_id)
        if computer is None:
            raise NotFound(f"Computer {computer_id} not found")
        if computer.status == ComputerStatus.DESTROYED:
            logger.info("Computer %s already destroyed", computer_id)
            return
        async with timed("destroy"):
            await self.host.proxy.remove_route(computer_id)
            if computer.firecracker_pid is not None:
                await self.host.hypervisor.kill(computer.firecracker_pid)
            await self.host.blocks.remove(volume_id=computer.thin_volume_id, name=computer.volume_name)
            await self.host.hypervisor.teardown_slot(computer.slot)
            await self.allocator.release_slot(computer.slot)
            if computer.vm_ip:
                await self.host.guest.evict(computer.vm_ip)
            await update_computer_status(self.db, computer_id, ComputerStatus.DESTROYED)
        await self.refresh_active_gauge()
        logger.info("Destroyed computer %s", computer_id)

    async def cleanup_dead(self, computer: Computer) -> None:
        """Release a VM whose Firecracker process is already gone. Every step is best-effort."""
        await self.host.proxy.remove_route(computer.id)
        await self.host.blocks.remove(volume_id=computer.thin_volume_id, name=computer.volume_name)
        try:
            await self.host.hypervisor.teardown_slot(computer.slot)
        except Exception:
            logger.debug("TAP removal failed for %s (may already be gone)", computer.id)
        await self.allocator.release_slot(computer.slot)
        if computer.vm_ip:
            try:
                await self.host.guest.evict(computer.vm_ip)
            except Exception:
                logger.debug("SSH eviction failed for %s", computer.id)
        await update_computer_status(self.db, computer.id, ComputerStatus.DESTROYED)
        await self.refresh_active_gauge()
        logger.info("Reaped dead VM %s", computer.id)

    # -- guest operations ----------------------------------------------------

    async def _touch(self, computer: Computer) -> None:
        await update_last_exec_at(self.db, computer.id, datetime.now(UTC).isoformat())

    async def exec(self, computer: Computer, command: str, *, timeout: float = 300.0) -> ExecResult:
        await self._touch(computer)
        async with timed("exec"):
            return await self.host.guest.exec(computer.vm_ip, command, timeout=timeout)

    async def stream(self, computer: Computer, command: str) -> AsyncIterator[OutputLine]:
        await self._touch(computer)
        async for item in self.host.guest.stream(computer.vm_ip, command):
            yield item

    async def exec_bg(self, computer: Computer, command: str) -> int:
        await self._touch(computer)
        return await self.host.guest.exec_bg(computer.vm_ip, command)

    async def exec_logs(self, computer: Computer, pid: int) -> list[str]:
        result = await self.host.guest.exec(
            computer.vm_ip, f"cat /tmp/bg-{pid}.log 2>/dev/null || echo ''", timeout=10.0
        )
        return result.stdout.splitlines()

    async def exec_kill(self, computer: Computer, pid: int) -> ExecResult:
        return await self.host.guest.exec(computer.vm_ip, f"kill {pid}")

    async def upload(self, computer: Computer, remote_path: str, data: bytes) -> None:
        await self.host.guest.upload(computer.vm_ip, remote_path, data)

    async def download(self, computer: Computer, remote_path: str) -> bytes:
        try:
            return await self.host.guest.download(computer.vm_ip, remote_path)
        except FileNotFoundError:
            raise NotFound(f"File not found: {remote_path}") from None

    async def metrics(self, computer: Computer) -> VmMetrics | None:
        try:
            return await asyncio.wait_for(
                self.host.guest.metrics(computer.vm_ip, timeout=10.0),
                timeout=STATUS_METRICS_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Failed to gather metrics for %s: %s", computer.id, type(exc).__name__)
            return None
```

(`Callable`, `Awaitable` from `collections.abc` under `TYPE_CHECKING`.) `stream` is an async generator so `_touch` runs before the first line; the router iterates it. `_bring_up` passes a zero-argument coroutine factory so the template/snapshot resolution happens inside the guarded region: a failing template build is logged and cold-boots (the old behaviour), but a failing download or restore is cleaned up.

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_computer_service.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1`
Expected: `11 passed`; clean; previous + 11.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(services): ComputerService with leak-free bring-up and a DB-derived active gauge"
```

---

### Task 7: CheckpointService and the merge move

**Files:**
- Create: `src/mshkn/services/checkpoints.py`, `src/mshkn/services/merge.py` (moved verbatim from `checkpoint/merge.py`), `tests/unit/test_checkpoint_service.py`
- Delete: `src/mshkn/checkpoint/` (package)
- Modify: `tests/unit/test_merge.py` (import path), `src/mshkn/api/checkpoints.py` (import path only; the router is rewritten in Task 10)

**Interfaces:**
- Produces: `MergeOutcome(checkpoint: Checkpoint, conflicts: list[str], auto_merged: int, unchanged: int)` frozen; `Deferred(deferred_id: str)` frozen; `CheckpointService(config, db, host, allocator, computers: ComputerService, tasks)` with:
  - `async create(computer, *, label: str | None, pin: bool = False, trigger: CheckpointTrigger) -> Checkpoint` — sync (bounded 15 s), hypervisor snapshot, evict, disk snap+activate, parent resolution, insert, `checkpoints_total{trigger}`, `timed("checkpoint")`, upload spawned under key `upload:<id>`.
  - `async get_owned(account, checkpoint_id) -> Checkpoint` (`NotFound`); `async list(account, *, label=None) -> list[Checkpoint]`; `async latest_for_label(account, label) -> Checkpoint | None`.
  - `async delete(checkpoint) -> None` — cancel the upload task first, then volume, local files, object prefix, row.
  - `async prune() -> int` — per-account retention through `delete`.
  - `async merge(account, parent_id, a_id, b_id) -> MergeOutcome` — validation (`NotFound`, `BadRequest`), snap+activate the output, four mounts, `three_way_merge` and the copy-back in `asyncio.to_thread`, insert with `label="merge"`, `timed("merge")`.
  - `async fork_or_defer(account, checkpoint, spec: ExecSpec, *, recipe_id, exclusive: ExclusiveMode | None) -> Computer | Deferred` — exclusive handling (`Conflict` for `error_on_conflict`, a queued row for `defer_on_conflict`), else `computers.fork`.
  - `upload_task_key(checkpoint_id) -> str` = `f"upload:{checkpoint_id}"`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_checkpoint_service.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import get_checkpoint, insert_account, insert_computer
from mshkn.errors import BadRequest, Conflict, NotFound
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import Account, CheckpointTrigger, Computer, ComputerStatus, ExecSpec
from mshkn.observability.metrics import checkpoints_total
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService, Deferred
from mshkn.services.computers import ComputerService
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")
SPEC = ExecSpec(command="echo hi", self_destruct=True, callback_url=None, label=None, meta_exec=None)


async def _services(
    db: aiosqlite.Connection, tmp_path: Path, *, retention: int = 20
) -> tuple[CheckpointService, ComputerService, FakeHost]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(
        domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts", checkpoint_retention_count=retention
    )
    allocator = SlotAllocator()
    tasks = BackgroundTasks()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
    computers = ComputerService(config, db, host, allocator, recipes)
    checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
    return checkpoints, computers, host


def _labelled(trigger: str) -> float:
    return checkpoints_total.labels(trigger=trigger)._value.get()


async def test_create_runs_the_five_steps_in_order_and_labels_the_metric(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["sync"] = ExecResult(0, "", "")
    before = _labelled("api")
    ckpt = await checkpoints.create(computer, label="base", pin=True, trigger=CheckpointTrigger.API)
    assert host.guest.commands[-1] == (computer.vm_ip, "sync")
    assert host.hypervisor.snapshots == [(computer.socket_path, tmp_path / "ckpts" / ckpt.id)]
    assert host.guest.evicted[-1] == computer.vm_ip
    assert host.blocks.volumes[ckpt.thin_volume_id or -1] == computer.thin_volume_id
    assert host.blocks.active[ckpt.volume_name] == ckpt.thin_volume_id
    assert ckpt.parent_id is None and ckpt.pinned and ckpt.label == "base"
    assert _labelled("api") == before + 1
    await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))
    assert sorted(host.objects.prefixes[f"acct-1/{ckpt.id}"]) == ["memory", "vmstate"]


async def test_parent_is_latest_then_source_then_none(db: aiosqlite.Connection, tmp_path: Path) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    first = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    second = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    assert first.parent_id is None and second.parent_id == first.id
    fork = await computers.fork(ACCOUNT, second, recipe_id=None)
    third = await checkpoints.create(fork, label=None, trigger=CheckpointTrigger.SELF_DESTRUCT)
    assert third.parent_id == second.id


async def test_delete_cancels_an_in_flight_upload_before_removing_files(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    started = asyncio.Event()

    async def slow_upload(local_dir: Path, prefix: str) -> None:
        started.set()
        await asyncio.sleep(30)
        assert local_dir.exists(), "upload must never see its directory vanish"

    monkeypatch.setattr(host.objects, "upload_dir", slow_upload)
    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    await started.wait()
    await checkpoints.delete(ckpt)
    assert len(checkpoints.tasks) == 0, "the upload task was cancelled and reaped"
    assert not (tmp_path / "ckpts" / ckpt.id).exists()
    assert ckpt.thin_volume_id not in host.blocks.volumes
    assert await get_checkpoint(db, ckpt.id) is None


async def test_prune_keeps_the_newest_and_pinned(db: aiosqlite.Connection, tmp_path: Path) -> None:
    checkpoints, computers, host = await _services(db, tmp_path, retention=2)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    ids = []
    for i in range(4):
        ckpt = await checkpoints.create(
            computer, label=None, pin=(i == 0), trigger=CheckpointTrigger.API
        )
        ids.append(ckpt.id)
        await db.execute(
            "UPDATE checkpoints SET created_at = ? WHERE id = ?", (f"2026-09-06T00:00:0{i}", ckpt.id)
        )
        await db.commit()
    assert await checkpoints.prune() == 1
    remaining = {c.id for c in await checkpoints.list(ACCOUNT)}
    assert remaining == {ids[0], ids[2], ids[3]}  # pinned oldest survives, unpinned oldest goes


async def test_merge_validates_then_merges_off_loop(db: aiosqlite.Connection, tmp_path: Path) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    parent = await checkpoints.create(computer, label="p", trigger=CheckpointTrigger.API)
    fork_a = await computers.fork(ACCOUNT, parent, recipe_id=None)
    fork_b = await computers.fork(ACCOUNT, parent, recipe_id=None)
    a = await checkpoints.create(fork_a, label="a", trigger=CheckpointTrigger.API)
    b = await checkpoints.create(fork_b, label="b", trigger=CheckpointTrigger.API)
    with pytest.raises(BadRequest):
        await checkpoints.merge(ACCOUNT, parent.id, a.id, a.id)
    with pytest.raises(NotFound):
        await checkpoints.merge(ACCOUNT, "ckpt-nope", a.id, b.id)
    with pytest.raises(BadRequest):
        await checkpoints.merge(ACCOUNT, a.id, parent.id, b.id)  # not children of a
    outcome = await checkpoints.merge(ACCOUNT, parent.id, a.id, b.id)
    assert outcome.checkpoint.parent_id == parent.id and outcome.checkpoint.label == "merge"
    assert outcome.conflicts == [] and outcome.checkpoint.thin_volume_id in host.blocks.volumes
    assert host.blocks.volumes[outcome.checkpoint.thin_volume_id or -1] == parent.thin_volume_id


async def test_fork_or_defer_honours_exclusive_modes(db: aiosqlite.Connection, tmp_path: Path) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    ckpt = await checkpoints.create(computer, label="chain", trigger=CheckpointTrigger.API)
    first = await checkpoints.fork_or_defer(ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive=None)
    assert isinstance(first, Computer)
    with pytest.raises(Conflict):
        await checkpoints.fork_or_defer(ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive="error_on_conflict")
    queued = await checkpoints.fork_or_defer(
        ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive="defer_on_conflict"
    )
    assert isinstance(queued, Deferred) and queued.deferred_id.startswith("def-")
    cur = await db.execute("SELECT request_payload FROM deferred_queue WHERE label = 'chain'")
    (payload,) = await cur.fetchone() or ("",)
    assert '"exec": "echo hi"' in payload and '"self_destruct": true' in payload
    await computers.destroy(first.id)
    again = await checkpoints.fork_or_defer(ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive="error_on_conflict")
    assert isinstance(again, Computer)  # chain is free again
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_checkpoint_service.py -q`
Expected: `ModuleNotFoundError: No module named 'mshkn.services.checkpoints'`.

- [ ] **Step 3: Implement**

`git mv src/mshkn/checkpoint/merge.py src/mshkn/services/merge.py`; delete `src/mshkn/checkpoint/__init__.py`; update the import in `tests/unit/test_merge.py` and (interim) `src/mshkn/api/checkpoints.py` to `mshkn.services.merge`.

`src/mshkn/services/checkpoints.py`:

```python
"""Checkpoints: the one create implementation, delete/prune, merge, exclusive fork (spec §6.3)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.db import (
    delete_checkpoint,
    get_active_computer_for_label,
    get_checkpoint,
    get_latest_checkpoint_for_computer,
    insert_checkpoint,
    insert_deferred,
    list_account_ids_with_checkpoints,
    list_checkpoints_by_account,
    list_prunable_checkpoints,
)
from mshkn.errors import BadRequest, Conflict, NotFound
from mshkn.models import Checkpoint, CheckpointTrigger, Computer
from mshkn.observability.metrics import checkpoints_total, timed
from mshkn.services.merge import three_way_merge

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import Host
    from mshkn.models import Account, ExclusiveMode, ExecSpec
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.allocator import SlotAllocator
    from mshkn.services.computers import ComputerService

logger = logging.getLogger(__name__)

_SYNC_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class MergeOutcome:
    checkpoint: Checkpoint
    conflicts: list[str]
    auto_merged: int
    unchanged: int


@dataclass(frozen=True)
class Deferred:
    deferred_id: str


class CheckpointService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        host: Host,
        allocator: SlotAllocator,
        computers: ComputerService,
        tasks: BackgroundTasks,
    ) -> None:
        self.config = config
        self.db = db
        self.host = host
        self.allocator = allocator
        self.computers = computers
        self.tasks = tasks

    @staticmethod
    def upload_task_key(checkpoint_id: str) -> str:
        return f"upload:{checkpoint_id}"

    # -- lookups -------------------------------------------------------------

    async def get_owned(self, account: Account, checkpoint_id: str) -> Checkpoint:
        ckpt = await get_checkpoint(self.db, checkpoint_id)
        if ckpt is None or ckpt.account_id != account.id:
            raise NotFound("Checkpoint not found")
        return ckpt

    async def list(self, account: Account, *, label: str | None = None) -> list[Checkpoint]:
        return await list_checkpoints_by_account(self.db, account.id, label=label)

    async def latest_for_label(self, account: Account, label: str) -> Checkpoint | None:
        ckpts = await list_checkpoints_by_account(self.db, account.id, label=label)
        return ckpts[0] if ckpts else None

    # -- create --------------------------------------------------------------

    async def create(
        self,
        computer: Computer,
        *,
        label: str | None,
        pin: bool = False,
        trigger: CheckpointTrigger,
    ) -> Checkpoint:
        checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
        snapshot_dir = self.config.checkpoint_local_dir / checkpoint_id
        async with timed("checkpoint"):
            # Flush the guest's page cache to the block device: dm-thin snapshots
            # see only what reached the disk.
            await asyncio.wait_for(
                self.host.guest.exec(computer.vm_ip, "sync", timeout=10.0),
                timeout=_SYNC_TIMEOUT_SECONDS,
            )
            await self.host.hypervisor.snapshot(computer.socket_path, snapshot_dir)
            # pause/resume breaks the pooled TCP session
            await self.host.guest.evict(computer.vm_ip)
            volume_id = await self.allocator.acquire_volume_id()
            volume_name = f"mshkn-ckpt-{checkpoint_id}"
            await self.host.blocks.snap(
                source_volume_id=computer.thin_volume_id, new_volume_id=volume_id
            )
            await self.host.blocks.activate(volume_id=volume_id, name=volume_name)
            latest = await get_latest_checkpoint_for_computer(self.db, computer.id)
            if latest is not None:
                parent_id: str | None = latest.id
            else:
                parent_id = computer.source_checkpoint_id
            ckpt = Checkpoint(
                id=checkpoint_id,
                account_id=computer.account_id,
                parent_id=parent_id,
                computer_id=computer.id,
                thin_volume_id=volume_id,
                r2_prefix=f"{computer.account_id}/{checkpoint_id}",
                disk_delta_size_bytes=None,
                memory_size_bytes=None,
                label=label,
                pinned=pin,
                created_at=datetime.now(UTC).isoformat(),
                recipe_id=computer.recipe_id,
            )
            await insert_checkpoint(self.db, ckpt)
        checkpoints_total.labels(trigger=trigger.value).inc()
        self.tasks.spawn(
            self._upload(snapshot_dir, ckpt.r2_prefix, checkpoint_id),
            name=self.upload_task_key(checkpoint_id),
            key=self.upload_task_key(checkpoint_id),
        )
        logger.info(
            "Checkpoint %s created for %s", checkpoint_id, computer.id,
            extra={"op": "checkpoint", "checkpoint_id": checkpoint_id,
                   "computer_id": computer.id, "trigger": trigger.value},
        )
        return ckpt

    async def _upload(self, snapshot_dir: Path, r2_prefix: str, checkpoint_id: str) -> None:
        try:
            await self.host.objects.upload_dir(snapshot_dir, r2_prefix)
        except Exception:
            logger.warning("R2 upload failed for checkpoint %s", checkpoint_id, exc_info=True)

    # -- delete / prune ------------------------------------------------------

    async def delete(self, checkpoint: Checkpoint) -> None:
        await self.tasks.cancel(self.upload_task_key(checkpoint.id))
        if checkpoint.thin_volume_id is not None:
            await self.host.blocks.remove(
                volume_id=checkpoint.thin_volume_id, name=checkpoint.volume_name
            )
        local_dir = self.config.checkpoint_local_dir / checkpoint.id
        shutil.rmtree(local_dir, ignore_errors=True)
        await self.host.objects.delete_prefix(checkpoint.r2_prefix)
        await delete_checkpoint(self.db, checkpoint.id)

    async def prune(self) -> int:
        keep = self.config.checkpoint_retention_count
        if keep <= 0:
            return 0
        pruned = 0
        for account_id in await list_account_ids_with_checkpoints(self.db):
            for ckpt in await list_prunable_checkpoints(self.db, account_id, keep):
                try:
                    await self.delete(ckpt)
                    pruned += 1
                    logger.info("Pruned checkpoint %s (account=%s)", ckpt.id, account_id)
                except Exception:
                    logger.exception("Failed to prune checkpoint %s", ckpt.id)
        return pruned

    # -- merge ---------------------------------------------------------------

    async def merge(self, account: Account, parent_id: str, a_id: str, b_id: str) -> MergeOutcome:
        parent = await get_checkpoint(self.db, parent_id)
        if parent is None or parent.account_id != account.id:
            raise NotFound("Parent checkpoint not found")
        if a_id == b_id:
            raise BadRequest("Cannot merge a checkpoint with itself")
        a = await get_checkpoint(self.db, a_id)
        b = await get_checkpoint(self.db, b_id)
        if a is None or a.account_id != account.id:
            raise NotFound("Checkpoint A not found")
        if b is None or b.account_id != account.id:
            raise NotFound("Checkpoint B not found")
        if a.parent_id != parent_id or b.parent_id != parent_id:
            raise BadRequest("Both checkpoints must be children of the specified parent")
        for name, ckpt in (("Parent", parent), ("A", a), ("B", b)):
            if ckpt.thin_volume_id is None:
                raise BadRequest(f"{name} checkpoint has no disk snapshot")
        assert parent.thin_volume_id is not None

        checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
        merged_volume_id = await self.allocator.acquire_volume_id()
        merged_volume_name = f"mshkn-ckpt-{checkpoint_id}"
        async with timed("merge"):
            await self.host.blocks.snap(
                source_volume_id=parent.thin_volume_id, new_volume_id=merged_volume_id
            )
            await self.host.blocks.activate(volume_id=merged_volume_id, name=merged_volume_name)
            async with (
                self.host.blocks.mounted(parent.volume_name, readonly=True) as mount_parent,
                self.host.blocks.mounted(a.volume_name, readonly=True) as mount_a,
                self.host.blocks.mounted(b.volume_name, readonly=True) as mount_b,
                self.host.blocks.mounted(merged_volume_name) as mount_output,
            ):
                result = await asyncio.to_thread(
                    _merge_into, mount_parent, mount_a, mount_b, mount_output
                )
        ckpt = Checkpoint(
            id=checkpoint_id,
            account_id=account.id,
            parent_id=parent_id,
            computer_id=None,
            thin_volume_id=merged_volume_id,
            r2_prefix=f"{account.id}/{checkpoint_id}",
            disk_delta_size_bytes=None,
            memory_size_bytes=None,
            label="merge",
            pinned=False,
            created_at=datetime.now(UTC).isoformat(),
            recipe_id=parent.recipe_id,
        )
        await insert_checkpoint(self.db, ckpt)
        logger.info(
            "Merged checkpoint %s: auto_merged=%d, unchanged=%d, conflicts=%d",
            checkpoint_id, result.auto_merged, result.unchanged, len(result.conflicts),
        )
        return MergeOutcome(
            checkpoint=ckpt,
            conflicts=[c.path for c in result.conflicts],
            auto_merged=result.auto_merged,
            unchanged=result.unchanged,
        )

    # -- exclusive fork ------------------------------------------------------

    async def fork_or_defer(
        self,
        account: Account,
        checkpoint: Checkpoint,
        spec: ExecSpec,
        *,
        recipe_id: str | None,
        exclusive: ExclusiveMode | None,
    ) -> Computer | Deferred:
        if exclusive is not None and checkpoint.label:
            active = await get_active_computer_for_label(self.db, account.id, checkpoint.label)
            if active is not None:
                if exclusive == "error_on_conflict":
                    raise Conflict("Checkpoint chain has active computer")
                deferred_id = f"def-{uuid.uuid4().hex[:12]}"
                payload = {
                    "checkpoint_id": checkpoint.id,
                    "recipe_id": recipe_id,
                    "exec": spec.command,
                    "self_destruct": spec.self_destruct,
                    "callback_url": spec.callback_url,
                    "meta_exec": spec.meta_exec,
                }
                await insert_deferred(
                    self.db, deferred_id, checkpoint.label, account.id,
                    json.dumps(payload), datetime.now(UTC).isoformat(),
                )
                return Deferred(deferred_id)
        return await self.computers.fork(account, checkpoint, recipe_id=recipe_id)


def _merge_into(parent: Path, fork_a: Path, fork_b: Path, output: Path) -> MergeResult:
    """Three-way merge into a scratch dir, then apply it onto the output mount. Blocking."""
    with tempfile.TemporaryDirectory(prefix="mshkn-merge-") as merge_dir:
        merge_output = Path(merge_dir) / "merge_result"
        result = three_way_merge(parent=parent, fork_a=fork_a, fork_b=fork_b, output=merge_output)
        for src in merge_output.rglob("*"):
            if src.is_file():
                dest = output / src.relative_to(merge_output)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        for src in parent.rglob("*"):
            if src.is_file():
                rel = src.relative_to(parent)
                if not (merge_output / rel).exists():
                    target = output / rel
                    if target.exists():
                        target.unlink()
    return result
```

(`MergeResult` imported from `mshkn.services.merge` at runtime, since `_merge_into` returns it.) The deferred payload gains `"recipe_id"`, which the REST path already wrote and the ingress path did not; the drain reads it in Task 8.

- [ ] **Step 4: Verify**

```bash
ls src/mshkn/checkpoint 2>/dev/null && echo "STILL THERE" || echo "checkpoint package gone"
uv run pytest tests/unit/test_checkpoint_service.py tests/unit/test_merge.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1
```

Expected: gone; `9 passed`; clean; previous + 6.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(services): CheckpointService (one create, cancel-before-delete, off-loop merge, exclusive fork); checkpoint/ package removed"
```

---

### Task 8: Lifecycle and the callback client

**Files:**
- Create: `src/mshkn/services/callback.py` (moved from `callback.py`, client injected), `src/mshkn/services/lifecycle.py`, `tests/unit/test_callback.py`, `tests/unit/test_lifecycle.py`
- Delete: `src/mshkn/callback.py`
- Modify: `src/mshkn/api/computers.py` (interim: import `deliver_callback` from the new module and pass `httpx.AsyncClient()` — replaced in Task 10), `tests/unit/test_self_destruct.py` (the `patch("mshkn.api.computers.deliver_callback", …)` target is unchanged, so only the import inside the module moves)

**Interfaces:**
- Produces: `deliver_callback(client: httpx.AsyncClient, url, payload, *, max_retries=3, sleep=asyncio.sleep) -> None` (never raises); `Lifecycle(db, computers, checkpoints, tasks, http)` with:
  - `async run_ephemeral(account, computer, spec: ExecSpec, *, source_checkpoint: Checkpoint | None) -> EphemeralResult` — no command → nothing happens; else exec, optional self-destruct (checkpoint with `trigger=SELF_DESTRUCT`, label = `spec.label` for a create or `source_checkpoint.label` for a fork, then destroy, then the callback as a background task, then `spawn_drain` for the label).
  - `async drain_deferred(account, label) -> None` — claim, fork from the newest labelled checkpoint, write `/tmp/exec/N.txt`, build the command, `run_ephemeral`.
  - `spawn_drain(account, label) -> None` — `tasks.spawn(self.drain_deferred(...), name=f"deferred:{label}")`.
  - `async drain_after_destroy(account, computer) -> None` — if the computer came from a labelled checkpoint, `spawn_drain` for that label.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_callback.py`:

```python
from __future__ import annotations

import httpx

from mshkn.services.callback import deliver_callback


async def test_delivers_once_on_success() -> None:
    seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers) | {"body": request.content.decode()})
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_callback(client, "http://cb/x", {"a": 1})
    assert len(seen) == 1 and seen[0]["body"] == '{"a":1}'


async def test_retries_on_5xx_with_backoff_then_gives_up() -> None:
    calls = 0
    slept: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_callback(client, "http://cb/x", {}, sleep=fake_sleep)
    assert calls == 3 and slept == [1, 2]


async def test_4xx_is_final_and_transport_errors_are_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(404)

    async def fake_sleep(seconds: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_callback(client, "http://cb/x", {}, sleep=fake_sleep)
    assert calls == 2
```

`tests/unit/test_lifecycle.py`:

```python
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, Request

from mshkn.config import Config
from mshkn.db import claim_deferred_by_label, get_computer, insert_account, insert_deferred
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import Account, CheckpointTrigger, ComputerStatus, ExecSpec
from mshkn.observability.metrics import checkpoints_total
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")


def _receiver() -> tuple[FastAPI, list[dict[str, Any]]]:
    app = FastAPI()
    received: list[dict[str, Any]] = []

    @app.post("/cb")
    async def cb(request: Request) -> dict[str, str]:
        received.append(await request.json())
        return {"ok": "yes"}

    return app, received


async def _lifecycle(
    db: aiosqlite.Connection, tmp_path: Path
) -> tuple[Lifecycle, ComputerService, CheckpointService, FakeHost, list[dict[str, Any]]]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    allocator = SlotAllocator()
    tasks = BackgroundTasks()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
    computers = ComputerService(config, db, host, allocator, recipes)
    checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
    app, received = _receiver()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://receiver")
    return Lifecycle(db, computers, checkpoints, tasks, http), computers, checkpoints, host, received


async def test_no_command_means_no_exec_and_no_self_destruct(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, _, host, _ = await _lifecycle(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    spec = ExecSpec(command=None, self_destruct=True, callback_url=None, label="x", meta_exec=None)
    result = await lifecycle.run_ephemeral(ACCOUNT, computer, spec, source_checkpoint=None)
    assert result.exec_exit_code is None and result.created_checkpoint_id is None
    assert host.guest.commands == []
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.RUNNING


async def test_self_destruct_checkpoints_destroys_and_calls_back(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, _, host, received = await _lifecycle(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["echo done"] = ExecResult(0, "done\n", "err\n")
    before = checkpoints_total.labels(trigger="self_destruct")._value.get()
    spec = ExecSpec(
        command="echo done", self_destruct=True, callback_url="http://receiver/cb",
        label="chain", meta_exec=None,
    )
    result = await lifecycle.run_ephemeral(ACCOUNT, computer, spec, source_checkpoint=None)
    assert result.exec_exit_code == 0 and result.created_checkpoint_id is not None
    assert host.guest.commands == [(computer.vm_ip, "echo done"), (computer.vm_ip, "sync")]
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    assert checkpoints_total.labels(trigger="self_destruct")._value.get() == before + 1
    await lifecycle.tasks.drain(timeout=2.0)
    assert received == [
        {
            "computer_id": computer.id,
            "checkpoint_id": None,
            "label": "chain",
            "exec_exit_code": 0,
            "exec_stdout": "done\n",
            "exec_stderr": "err\n",
            "created_checkpoint_id": result.created_checkpoint_id,
        }
    ]


async def test_fork_self_destruct_inherits_the_source_label(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, host, _ = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    fork = await computers.fork(ACCOUNT, source, recipe_id=None)
    spec = ExecSpec(command="true", self_destruct=True, callback_url=None, label=None, meta_exec=None)
    result = await lifecycle.run_ephemeral(ACCOUNT, fork, spec, source_checkpoint=source)
    created = await checkpoints.get_owned(ACCOUNT, result.created_checkpoint_id or "")
    assert created.label == "chain" and created.parent_id == source.id


async def test_drain_forks_once_writes_exec_files_and_self_destructs(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, host, received = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    for i, payload in enumerate(
        [
            {"checkpoint_id": source.id, "exec": "echo one", "self_destruct": False},
            {"checkpoint_id": source.id, "exec": "echo two", "self_destruct": True,
             "callback_url": "http://receiver/cb", "meta_exec": "bash /tmp/exec/1.txt"},
        ]
    ):
        await insert_deferred(db, f"def-{i}", "chain", "acct-1", json.dumps(payload), f"t{i}")
    await asyncio.gather(
        lifecycle.drain_deferred(ACCOUNT, "chain"), lifecycle.drain_deferred(ACCOUNT, "chain")
    )
    forks = [c for c in host.hypervisor.restored if c[0] != base.thin_volume_id]
    assert len(forks) == 1, "two concurrent drains must fork exactly once"
    commands = [cmd for _, cmd in host.guest.commands if cmd not in ("sync",)]
    assert commands[-2].startswith("mkdir -p /tmp/exec && printf '%s' 'echo one' > /tmp/exec/0.txt")
    assert commands[-1] == "bash /tmp/exec/1.txt"  # last meta_exec wins
    await lifecycle.tasks.drain(timeout=2.0)
    assert len(received) == 1 and received[0]["label"] == "chain"
    assert await claim_deferred_by_label(db, "chain") == []


async def test_drain_with_no_labelled_checkpoint_logs_and_returns(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, _, _, host, _ = await _lifecycle(db, tmp_path)
    await insert_deferred(db, "def-x", "orphan", "acct-1", "{}", "t")
    await lifecycle.drain_deferred(ACCOUNT, "orphan")
    assert host.hypervisor.restored == [] and host.hypervisor.booted == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_callback.py tests/unit/test_lifecycle.py -q`
Expected: `ModuleNotFoundError` for both new modules.

- [ ] **Step 3: Implement**

`src/mshkn/services/callback.py`:

```python
"""Best-effort webhook delivery with bounded exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def deliver_callback(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    max_retries: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """POST payload to url. Retries 5xx and transport errors; never raises."""
    for attempt in range(max_retries):
        try:
            resp = await client.post(url, json=payload, timeout=10)
            if resp.status_code < 500:
                logger.info("Callback delivered to %s (status %d)", url, resp.status_code)
                return
            logger.warning(
                "Callback to %s returned %d, retrying (%d/%d)",
                url, resp.status_code, attempt + 1, max_retries,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Callback to %s failed (%s), retrying (%d/%d)",
                url, type(exc).__name__, attempt + 1, max_retries,
            )
        if attempt < max_retries - 1:
            await sleep(float(2**attempt))
    logger.warning("Callback delivery failed after %d attempts: %s", max_retries, url)
```

`src/mshkn/services/lifecycle.py`:

```python
"""One implementation of "run a command on a fresh computer, then maybe checkpoint
and destroy it": REST create, REST fork, ingress create/fork, and the deferred
drain all go through here (spec §6.4)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from mshkn.db import claim_deferred_by_label
from mshkn.models import CheckpointTrigger, EphemeralResult
from mshkn.services.callback import deliver_callback

if TYPE_CHECKING:
    import aiosqlite
    import httpx

    from mshkn.models import Account, Checkpoint, Computer, ExecSpec
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.computers import ComputerService

logger = logging.getLogger(__name__)


class Lifecycle:
    def __init__(
        self,
        db: aiosqlite.Connection,
        computers: ComputerService,
        checkpoints: CheckpointService,
        tasks: BackgroundTasks,
        http: httpx.AsyncClient,
    ) -> None:
        self.db = db
        self.computers = computers
        self.checkpoints = checkpoints
        self.tasks = tasks
        self.http = http

    async def run_ephemeral(
        self,
        account: Account,
        computer: Computer,
        spec: ExecSpec,
        *,
        source_checkpoint: Checkpoint | None,
    ) -> EphemeralResult:
        if spec.command is None:
            return EphemeralResult(computer.id, None, None, None, None)
        result = await self.computers.exec(computer, spec.command)
        created_checkpoint_id: str | None = None
        if spec.self_destruct:
            label = source_checkpoint.label if source_checkpoint is not None else spec.label
            ckpt = await self.checkpoints.create(
                computer, label=label, trigger=CheckpointTrigger.SELF_DESTRUCT
            )
            created_checkpoint_id = ckpt.id
            await self.computers.destroy(computer.id)
            if spec.callback_url:
                payload = {
                    "computer_id": computer.id,
                    "checkpoint_id": source_checkpoint.id if source_checkpoint else None,
                    "label": label,
                    "exec_exit_code": result.exit_code,
                    "exec_stdout": result.stdout,
                    "exec_stderr": result.stderr,
                    "created_checkpoint_id": created_checkpoint_id,
                }
                self.tasks.spawn(
                    deliver_callback(self.http, spec.callback_url, payload),
                    name=f"callback:{computer.id}",
                )
            logger.info(
                "Self-destruct: computer %s checkpointed as %s and destroyed",
                computer.id, created_checkpoint_id,
            )
            if label:
                self.spawn_drain(account, label)
        return EphemeralResult(
            computer_id=computer.id,
            exec_exit_code=result.exit_code,
            exec_stdout=result.stdout,
            exec_stderr=result.stderr,
            created_checkpoint_id=created_checkpoint_id,
        )

    def spawn_drain(self, account: Account, label: str) -> None:
        self.tasks.spawn(self.drain_deferred(account, label), name=f"deferred:{label}")

    async def drain_after_destroy(self, account: Account, computer: Computer) -> None:
        if not computer.source_checkpoint_id:
            return
        source = await self.checkpoints.get_owned(account, computer.source_checkpoint_id)
        if source.label:
            self.spawn_drain(account, source.label)

    async def drain_deferred(self, account: Account, label: str) -> None:
        """Process every queued fork for a label on one new computer.

        The claim is a single DELETE … RETURNING, so a destroy and an idle reap
        draining the same label at once cannot both fork. Each request's exec
        is written to /tmp/exec/N.txt; the command run is the last meta_exec if
        any, else the execs joined by newlines; self_destruct if any asked;
        callback_url is the last one given.
        """
        items = await claim_deferred_by_label(self.db, label)
        if not items:
            return
        try:
            latest = await self.checkpoints.latest_for_label(account, label)
            if latest is None:
                logger.warning("No checkpoints found with label %s for deferred processing", label)
                return
            payloads = [json.loads(d.request_payload) for d in items]
            recipe_id = next((p["recipe_id"] for p in reversed(payloads) if p.get("recipe_id")), None)
            computer = await self.computers.fork(
                account, latest, recipe_id=recipe_id or latest.recipe_id
            )
            execs = [p.get("exec") or "" for p in payloads]
            writes = ["mkdir -p /tmp/exec"]
            for i, cmd in enumerate(execs):
                escaped = cmd.replace("'", "'\\''")
                writes.append(f"printf '%s' '{escaped}' > /tmp/exec/{i}.txt")
            await self.computers.exec(computer, " && ".join(writes))
            meta_exec = next((p["meta_exec"] for p in reversed(payloads) if p.get("meta_exec")), None)
            command = meta_exec or "\n".join(c for c in execs if c)
            if not command:
                logger.info("Deferred batch for %s had no command; computer %s left running", label, computer.id)
                return
            spec = ExecSpec(
                command=command,
                self_destruct=any(p.get("self_destruct") for p in payloads),
                callback_url=next((p["callback_url"] for p in reversed(payloads) if p.get("callback_url")), None),
                label=label,
                meta_exec=meta_exec,
            )
            outcome = await self.run_ephemeral(account, computer, spec, source_checkpoint=latest)
            logger.info(
                "Processed %d deferred request(s) for label %s -> computer %s (exit=%s)",
                len(items), label, computer.id, outcome.exec_exit_code,
            )
        except Exception:
            logger.exception("Failed to process deferred queue for label %s", label)
```

(`ExecSpec` is constructed at runtime, so import it from `mshkn.models` outside `TYPE_CHECKING`.) Behavioural note preserved from the old `_process_deferred`: an empty command leaves the forked computer running; `run_ephemeral` only drains again when the batch self-destructed, which matches the old recursion.

Interim wiring: `src/mshkn/api/computers.py` imports `deliver_callback` from `mshkn.services.callback` and calls it as `deliver_callback(httpx.AsyncClient(), callback_url, payload)` (a throwaway client; Task 10 replaces the whole router). `tests/unit/test_self_destruct.py::test_callback_url_fires_on_self_destruct` patches `mshkn.api.computers.deliver_callback`; update its fake to accept the leading `client` argument. Delete `src/mshkn/callback.py`.

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_callback.py tests/unit/test_lifecycle.py tests/unit/test_self_destruct.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1`
Expected: pass; clean; previous + 8.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(services): Lifecycle.run_ephemeral and an atomic deferred drain; callback client injected"
```

---

### Task 9: Reaper

**Files:**
- Create: `src/mshkn/services/reaper.py`, `tests/unit/test_reaper.py`
- (`vm/manager.py`'s reaper stays wired until Task 10.)

**Interfaces:**
- Produces: `Reaper(config, db, host, computers, checkpoints, lifecycle, alerts: deque[Alert], *, disk_usage=shutil.disk_usage, meminfo_path=Path("/proc/meminfo"))` with `async run(interval=60.0)` (loop; sleeps first), `async cycle() -> None`, `async reap_dead() -> int`, `async reap_idle() -> int`, `async check_host() -> list[Alert]`. Thresholds: NVMe warning above 80% (critical above 95%), RAM critical above 90%, thin-pool data and metadata warning above 0.80 and critical above 0.95; each check also sets its gauge (`host_ram_used_ratio`, `thin_pool_used_ratio{kind}`). `IDLE_LABEL = "auto-idle-timeout"`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_reaper.py`:

```python
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from mshkn.config import Config
from mshkn.db import claim_deferred_by_label, get_computer, insert_account, insert_deferred
from mshkn.host import PoolUsage
from mshkn.host.fake import FakeHost
from mshkn.models import Account, Alert, CheckpointTrigger, ComputerStatus
from mshkn.observability.metrics import checkpoints_total, thin_pool_used_ratio
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.recipes import RecipeService
from mshkn.services.reaper import IDLE_LABEL, Reaper

if TYPE_CHECKING:
    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")


class _Usage:
    def __init__(self, used: int, total: int) -> None:
        self.used, self.total = used, total


async def _reaper(
    db: aiosqlite.Connection, tmp_path: Path, *, idle_timeout: int = 0, disk_pct: float = 10.0
) -> tuple[Reaper, ComputerService, CheckpointService, FakeHost]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(
        domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts", idle_timeout_seconds=idle_timeout
    )
    allocator = SlotAllocator()
    tasks = BackgroundTasks()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
    computers = ComputerService(config, db, host, allocator, recipes)
    checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
    lifecycle = Lifecycle(db, computers, checkpoints, tasks, httpx.AsyncClient())
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       1000 kB\nMemAvailable:    800 kB\n")
    reaper = Reaper(
        config, db, host, computers, checkpoints, lifecycle, deque(maxlen=100),
        disk_usage=lambda _: _Usage(int(disk_pct), 100), meminfo_path=meminfo,
    )
    return reaper, computers, checkpoints, host


async def test_dead_vm_is_cleaned_up(db: aiosqlite.Connection, tmp_path: Path) -> None:
    reaper, computers, _, host = await _reaper(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.hypervisor.alive.pop(computer.firecracker_pid or -1)  # the process died
    assert await reaper.reap_dead() == 1
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    assert host.proxy.routes == {} and host.guest.evicted[-1] == computer.vm_ip
    assert computers.allocator.free_slots == frozenset({computer.slot})


async def test_idle_vm_is_checkpointed_with_trigger_idle_and_its_label_and_drained(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    reaper, computers, checkpoints, host = await _reaper(db, tmp_path, idle_timeout=60)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    fork = await computers.fork(ACCOUNT, source, recipe_id=None)
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, fork.id))
    await db.commit()
    await insert_deferred(
        db, "def-1", "chain", "acct-1", '{"checkpoint_id": "x", "exec": "echo q"}', "t"
    )
    before = checkpoints_total.labels(trigger="idle")._value.get()
    assert await reaper.reap_idle() == 1
    assert checkpoints_total.labels(trigger="idle")._value.get() == before + 1
    stored = await get_computer(db, fork.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    latest = await checkpoints.latest_for_label(ACCOUNT, "chain")
    assert latest is not None and latest.id != source.id and latest.parent_id == source.id
    await reaper.lifecycle.tasks.drain(timeout=2.0)
    assert await claim_deferred_by_label(db, "chain") == [], "the queue was drained after the reap"
    assert (host.hypervisor.alive != {}), "the deferred fork is running"


async def test_idle_vm_without_a_source_gets_the_default_label(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    reaper, computers, checkpoints, _ = await _reaper(db, tmp_path, idle_timeout=60)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, computer.id))
    await db.commit()
    assert await reaper.reap_idle() == 1
    assert (await checkpoints.latest_for_label(ACCOUNT, IDLE_LABEL)) is not None


async def test_recent_exec_keeps_a_vm_alive(db: aiosqlite.Connection, tmp_path: Path) -> None:
    reaper, computers, _, _ = await _reaper(db, tmp_path, idle_timeout=60)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, computer.id))
    await db.commit()
    await computers.exec(computer, "true")  # touches last_exec_at
    assert await reaper.reap_idle() == 0


async def test_host_checks_raise_pool_alerts_and_set_gauges(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    reaper, _, _, host = await _reaper(db, tmp_path, disk_pct=85.0)
    host.blocks.pool_usage = PoolUsage(data_used_ratio=0.96, metadata_used_ratio=0.5)
    alerts = await reaper.check_host()
    by_source = {a.source: a for a in alerts}
    assert by_source["nvme"].level == "warning"
    assert by_source["thin_pool_data"].level == "critical"
    assert "thin_pool_metadata" not in by_source and "ram" not in by_source
    assert thin_pool_used_ratio.labels(kind="data")._value.get() == 0.96
    assert list(reaper.alerts) == alerts
    assert all(isinstance(a, Alert) for a in alerts)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_reaper.py -q`
Expected: `ModuleNotFoundError: No module named 'mshkn.services.reaper'`.

- [ ] **Step 3: Implement**

`src/mshkn/services/reaper.py`:

```python
"""Background maintenance: dead VMs, idle VMs, checkpoint retention, host checks (spec §6.7)."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.db import get_account_by_id, get_checkpoint, list_all_computers
from mshkn.models import Alert, CheckpointTrigger, ComputerStatus
from mshkn.observability.metrics import host_ram_used_ratio, thin_pool_used_ratio

if TYPE_CHECKING:
    from collections import deque
    from collections.abc import Callable

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import Host
    from mshkn.models import Computer
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.computers import ComputerService
    from mshkn.services.lifecycle import Lifecycle

logger = logging.getLogger(__name__)

IDLE_LABEL = "auto-idle-timeout"
_IDLE_CONCURRENCY = 5
_POOL_WARNING = 0.80
_POOL_CRITICAL = 0.95


class _DiskUsage(Protocol):
    used: int
    total: int


class Reaper:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        host: Host,
        computers: ComputerService,
        checkpoints: CheckpointService,
        lifecycle: Lifecycle,
        alerts: deque[Alert],
        *,
        disk_usage: Callable[[str], _DiskUsage] = shutil.disk_usage,
        meminfo_path: Path = Path("/proc/meminfo"),
    ) -> None:
        self.config = config
        self.db = db
        self.host = host
        self.computers = computers
        self.checkpoints = checkpoints
        self.lifecycle = lifecycle
        self.alerts = alerts
        self._disk_usage = disk_usage
        self._meminfo_path = meminfo_path

    async def run(self, interval: float = 60.0) -> None:
        logger.info(
            "Reaper started (interval=%.0fs, idle_timeout=%ds, retention=%d)",
            interval, self.config.idle_timeout_seconds, self.config.checkpoint_retention_count,
        )
        while True:
            await asyncio.sleep(interval)
            try:
                await self.cycle()
            except Exception:
                logger.exception("Reaper cycle failed")

    async def cycle(self) -> None:
        dead = await self.reap_dead()
        idle = await self.reap_idle()
        pruned = await self.checkpoints.prune()
        alerts = await self.check_host()
        await self.computers.refresh_active_gauge()
        if dead or idle or pruned or alerts:
            logger.info(
                "Reaper cycle: %d dead, %d idle VM(s), %d checkpoint(s) pruned, %d alert(s)",
                dead, idle, pruned, len(alerts),
            )

    async def reap_dead(self) -> int:
        reaped = 0
        for computer in await self._running():
            if computer.firecracker_pid is None or self.host.hypervisor.is_alive(computer.firecracker_pid):
                continue
            logger.warning("Reaping dead VM %s (PID %d gone)", computer.id, computer.firecracker_pid)
            try:
                await self.computers.cleanup_dead(computer)
                reaped += 1
            except Exception:
                logger.exception("Failed to reap VM %s", computer.id)
        return reaped

    async def reap_idle(self) -> int:
        timeout = self.config.idle_timeout_seconds
        if timeout <= 0:
            return 0
        now = datetime.now(UTC)
        idle: list[Computer] = []
        for computer in await self._running():
            ref = computer.last_exec_at or computer.created_at
            try:
                ref_time = datetime.fromisoformat(ref)
            except ValueError:
                continue
            if ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=UTC)
            if (now - ref_time).total_seconds() >= timeout:
                idle.append(computer)
        if not idle:
            return 0
        sem = asyncio.Semaphore(_IDLE_CONCURRENCY)

        async def one(computer: Computer) -> bool:
            async with sem:
                try:
                    await self._checkpoint_and_destroy(computer)
                    return True
                except Exception:
                    logger.exception("Failed to reap idle VM %s", computer.id)
                    return False

        results = await asyncio.gather(*(one(c) for c in idle))
        return sum(results)

    async def _checkpoint_and_destroy(self, computer: Computer) -> None:
        label: str | None = None
        if computer.source_checkpoint_id:
            source = await get_checkpoint(self.db, computer.source_checkpoint_id)
            if source is not None:
                label = source.label
        effective_label = label or IDLE_LABEL
        try:
            await self.checkpoints.create(
                computer, label=effective_label, trigger=CheckpointTrigger.IDLE
            )
        except Exception:
            logger.exception("Auto-checkpoint failed for VM %s, destroying anyway", computer.id)
        await self.computers.destroy(computer.id)
        logger.info("Destroyed idle VM %s", computer.id)
        account = await get_account_by_id(self.db, computer.account_id)
        if account is not None:
            self.lifecycle.spawn_drain(account, effective_label)

    async def check_host(self) -> list[Alert]:
        now = datetime.now(UTC).isoformat()
        found: list[Alert] = []
        try:
            disk = self._disk_usage("/")
            pct = disk.used / disk.total * 100
            if pct > 80:
                found.append(Alert("critical" if pct > 95 else "warning", "nvme",
                                   f"NVMe usage at {pct:.1f}%", round(pct, 1), 80.0, now))
        except Exception:
            logger.exception("Failed to check disk usage")
        try:
            meminfo: dict[str, int] = {}
            for line in self._meminfo_path.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
            total, available = meminfo.get("MemTotal", 0), meminfo.get("MemAvailable", 0)
            if total > 0:
                used_pct = (total - available) / total * 100
                host_ram_used_ratio.set(used_pct / 100.0)
                if used_pct > 90:
                    found.append(Alert("critical", "ram", f"Host RAM usage at {used_pct:.1f}%",
                                       round(used_pct, 1), 90.0, now))
        except Exception:
            logger.exception("Failed to check RAM usage")
        try:
            usage = await self.host.blocks.usage()
            for kind, ratio in (("data", usage.data_used_ratio), ("metadata", usage.metadata_used_ratio)):
                thin_pool_used_ratio.labels(kind=kind).set(ratio)
                if ratio > _POOL_WARNING:
                    found.append(Alert(
                        "critical" if ratio > _POOL_CRITICAL else "warning",
                        f"thin_pool_{kind}",
                        f"thin pool {kind} at {ratio * 100:.1f}%",
                        round(ratio, 3), _POOL_WARNING, now,
                    ))
        except Exception:
            logger.exception("Failed to check thin pool usage")
        for alert in found:
            logger.warning("ALERT [%s]: %s", alert.level, alert.message)
            self.alerts.append(alert)
        return found

    async def _running(self) -> list[Computer]:
        return [c for c in await list_all_computers(self.db) if c.status == ComputerStatus.RUNNING]
```

(`Protocol` from `typing`.)

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_reaper.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1`
Expected: `5 passed`; clean; previous + 5.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(services): Reaper with idle checkpoints tagged trigger=idle and thin-pool alerts"
```

---

### Task 10: IngressService and the Starlark move

**Files:**
- Create: `src/mshkn/services/starlark.py` (moved verbatim from `ingress/starlark.py`), `src/mshkn/services/ingress.py`, `tests/unit/test_ingress_service.py`
- Modify: `src/mshkn/api/ingress.py` (interim: import `execute_transform`/`validate_starlark`/`StarlarkError` from `mshkn.services.starlark` and `validate_transform_result` from `mshkn.services.ingress`; the router is rewritten in Task 11), `tests/unit/test_ingress.py` (the `_validate_transform_result` tests move into the new file and change for `uses` rejection; the starlark tests change their import)
- (`src/mshkn/ingress/` is deleted in Task 11 when the Pydantic models move to `api/schemas.py`.)

**Interfaces:**
- Produces: `ForkAction` / `CreateAction` (Pydantic, `extra="forbid"`); `validate_transform_result(result: object) -> list[str]`; `TriggerOutcome(status_code: int, body: dict[str, object] | None)` frozen; `IngressService(config, db, computers, checkpoints, lifecycle, tasks)` with:
  - `async create_rule(account, *, name, starlark_source, response_mode, max_body_bytes, rate_limit_rpm) -> IngressRule` (`InvalidInput(detail={"starlark_errors": [...]})`).
  - `async list_rules(account)`, `async get_rule(account, rule_id) -> IngressRule` (`NotFound`), `async update_rule(account, rule_id, *, name=None, starlark_source=None, response_mode=None, max_body_bytes=None, rate_limit_rpm=None, enabled=None) -> IngressRule`, `async delete_rule(account, rule_id)`, `async rotate_rule(account, rule_id) -> IngressRule`, `async logs(account, rule_id) -> list[IngressLog]`.
  - `test_rule(rule, request_dict) -> tuple[dict | None, list[str], float]` (result, validation errors, elapsed ms).
  - `async trigger(rule_id, request_dict) -> TriggerOutcome` — 404 `NotFound` for unknown/disabled; 429 `LimitExceeded` per rule; Starlark failure → `TransformError(detail=f"Starlark execution error: {exc}")` after logging `failed`; `None` → 204; invalid result → `TransformError(detail={"errors": [...], "starlark_result": result})` after logging `failed`; async mode → spawn `execute_and_log` and 202 `{"status": "accepted"}`; sync mode → 200 with the action result, `completed` logged, or `failed` logged and the error re-raised.
  - `async execute(account, action: dict) -> dict[str, object]` — `fork`: resolve `label` to the newest checkpoint (`NotFound`), `checkpoints.fork_or_defer` then `lifecycle.run_ephemeral`; `create`: `Resources.from_needs(needs)`, `computers.create`, `lifecycle.run_ephemeral`. Returns the same dict shape as the REST responses (`ForkResponse`/`CreateResponse` fields, or `{"deferred_id", "status": "queued"}`).
  - `limiter_for(rule) -> RateLimiter`; limiters live on the service and follow rotations.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ingress_service.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from mshkn.config import Config
from mshkn.db import get_computer, insert_account
from mshkn.errors import InvalidInput, LimitExceeded, NotFound, TransformError
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import Account, CheckpointTrigger, ComputerStatus, IngressLogStatus
from mshkn.resources import DEFAULT_RESOURCES, Resources
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.ingress import IngressService, validate_transform_result
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")
REQ: dict[str, object] = {
    "method": "POST", "path": "/hook", "headers": {}, "query_params": {},
    "body_json": None, "body_form": None, "body_raw": "", "content_type": "",
}


async def _ingress(
    db: aiosqlite.Connection, tmp_path: Path
) -> tuple[IngressService, ComputerService, CheckpointService, FakeHost]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    allocator = SlotAllocator()
    tasks = BackgroundTasks()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
    computers = ComputerService(config, db, host, allocator, recipes)
    checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
    lifecycle = Lifecycle(db, computers, checkpoints, tasks, httpx.AsyncClient())
    return IngressService(config, db, computers, checkpoints, lifecycle, tasks), computers, checkpoints, host


def test_validate_transform_result_accepts_recipe_id_and_needs_and_rejects_uses() -> None:
    assert validate_transform_result(None) == []
    assert validate_transform_result({"action": "fork", "checkpoint_id": "cp_1"}) == []
    assert validate_transform_result({"action": "create", "recipe_id": "rcp-1", "needs": {"ram": "1GB"}}) == []
    assert any("checkpoint_id" in e or "label" in e for e in validate_transform_result({"action": "fork"}))
    assert validate_transform_result({"action": "restart"})
    assert any("bogus" in e for e in validate_transform_result({"action": "fork", "checkpoint_id": "x", "bogus": 1}))
    assert any("uses" in e for e in validate_transform_result({"action": "create", "uses": ["python"]}))
    assert any("capabilities" in e for e in validate_transform_result({"action": "create", "capabilities": []}))
    assert any("exclusive" in e for e in validate_transform_result({"action": "fork", "checkpoint_id": "x", "exclusive": "wrong"}))
    assert validate_transform_result("not a dict") == ["transform must return a dict or None"]


async def test_create_rule_validates_starlark(db: aiosqlite.Connection, tmp_path: Path) -> None:
    ingress, _, _, _ = await _ingress(db, tmp_path)
    with pytest.raises(InvalidInput) as info:
        await ingress.create_rule(
            ACCOUNT, name="bad", starlark_source="def other(req):\n  return None",
            response_mode="async", max_body_bytes=10485760, rate_limit_rpm=60,
        )
    assert isinstance(info.value.detail, dict) and "starlark_errors" in info.value.detail
    rule = await ingress.create_rule(
        ACCOUNT, name="ok", starlark_source="def transform(req):\n  return None",
        response_mode="sync", max_body_bytes=2048, rate_limit_rpm=5,
    )
    assert rule.id.startswith("ir_") and (await ingress.get_rule(ACCOUNT, rule.id)).name == "ok"
    rotated = await ingress.rotate_rule(ACCOUNT, rule.id)
    assert rotated.id != rule.id
    with pytest.raises(NotFound):
        await ingress.get_rule(ACCOUNT, rule.id)


async def test_trigger_outcomes(db: aiosqlite.Connection, tmp_path: Path) -> None:
    ingress, _, _, _ = await _ingress(db, tmp_path)
    with pytest.raises(NotFound):
        await ingress.trigger("ir_nope", REQ)
    none_rule = await ingress.create_rule(
        ACCOUNT, name="none", starlark_source="def transform(req):\n  return None",
        response_mode="async", max_body_bytes=1024, rate_limit_rpm=60,
    )
    assert (await ingress.trigger(none_rule.id, REQ)).status_code == 204
    boom = await ingress.create_rule(
        ACCOUNT, name="boom", starlark_source='def transform(req):\n  return req["x"]["y"]',
        response_mode="async", max_body_bytes=1024, rate_limit_rpm=60,
    )
    with pytest.raises(TransformError):
        await ingress.trigger(boom.id, REQ)
    logs = await ingress.logs(ACCOUNT, boom.id)
    assert logs and logs[0].status is IngressLogStatus.FAILED
    bad = await ingress.create_rule(
        ACCOUNT, name="bad", starlark_source='def transform(req):\n  return {"action": "create", "uses": ["x"]}',
        response_mode="sync", max_body_bytes=1024, rate_limit_rpm=60,
    )
    with pytest.raises(TransformError) as info:
        await ingress.trigger(bad.id, REQ)
    assert isinstance(info.value.detail, dict) and "errors" in info.value.detail
    limited = await ingress.create_rule(
        ACCOUNT, name="limited", starlark_source="def transform(req):\n  return None",
        response_mode="async", max_body_bytes=1024, rate_limit_rpm=1,
    )
    assert (await ingress.trigger(limited.id, REQ)).status_code == 204
    with pytest.raises(LimitExceeded):
        await ingress.trigger(limited.id, REQ)


async def test_sync_create_honours_recipe_id_and_needs_through_the_lifecycle(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    ingress, computers, _, host = await _ingress(db, tmp_path)
    host.guest.script["echo hi"] = ExecResult(0, "hi\n", "")
    rule = await ingress.create_rule(
        ACCOUNT, name="create",
        starlark_source=(
            'def transform(req):\n  return {"action": "create", "needs": {"ram": "1GB", "cores": 4},'
            ' "exec": "echo hi", "self_destruct": True, "label": "ing"}'
        ),
        response_mode="sync", max_body_bytes=1024, rate_limit_rpm=60,
    )
    outcome = await ingress.trigger(rule.id, REQ)
    assert outcome.status_code == 200 and outcome.body is not None
    assert outcome.body["exec_stdout"] == "hi\n" and outcome.body["created_checkpoint_id"]
    assert host.hypervisor.booted == [(host.hypervisor.booted[0][0], Resources(mem_mib=1024, vcpus=4))]
    stored = await get_computer(db, str(outcome.body["computer_id"]))
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    logs = await ingress.logs(ACCOUNT, rule.id)
    assert logs[0].status is IngressLogStatus.COMPLETED


async def test_async_fork_by_label_runs_in_the_background(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    ingress, computers, checkpoints, host = await _ingress(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    rule = await ingress.create_rule(
        ACCOUNT, name="fork",
        starlark_source='def transform(req):\n  return {"action": "fork", "label": "chain", "exec": "true"}',
        response_mode="async", max_body_bytes=1024, rate_limit_rpm=60,
    )
    outcome = await ingress.trigger(rule.id, REQ)
    assert outcome.status_code == 202 and outcome.body == {"status": "accepted"}
    await ingress.tasks.drain(timeout=2.0)
    assert len(host.hypervisor.restored) == 2  # base's template restore + the fork
    assert [log.status for log in await ingress.logs(ACCOUNT, rule.id)] == [IngressLogStatus.ACCEPTED]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_ingress_service.py -q`
Expected: `ModuleNotFoundError: No module named 'mshkn.services.ingress'`.

- [ ] **Step 3: Implement**

`git mv src/mshkn/ingress/starlark.py src/mshkn/services/starlark.py` (contents unchanged).

`src/mshkn/services/ingress.py`:

```python
"""Ingress rules and the trigger path (spec §6.5).

The Starlark result is validated with Pydantic models instead of hand-rolled
set arithmetic; `create` actions take `recipe_id` and `needs` exactly like
REST, and the retired `capabilities`/`uses` fields are rejected as unknown.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from mshkn.db import (
    delete_ingress_rule,
    get_account_by_id,
    get_ingress_rule_by_id,
    insert_ingress_log,
    insert_ingress_rule,
    list_ingress_logs,
    list_ingress_rules_by_account,
    rotate_ingress_rule_id,
    update_ingress_rule,
)
from mshkn.errors import InvalidInput, LimitExceeded, NotFound, TransformError
from mshkn.models import ExecSpec, IngressLog, IngressLogStatus, IngressRule
from mshkn.ratelimit import RateLimiter
from mshkn.resources import Resources
from mshkn.services.checkpoints import Deferred
from mshkn.services.starlark import StarlarkError, execute_transform, validate_starlark

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Account
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.computers import ComputerService
    from mshkn.services.lifecycle import Lifecycle

logger = logging.getLogger(__name__)


class ForkAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["fork"]
    checkpoint_id: str | None = None
    label: str | None = None
    exec: str | None = None
    self_destruct: bool = False
    exclusive: Literal["error_on_conflict", "defer_on_conflict"] | None = None
    callback_url: str | None = None
    meta_exec: str | None = None

    @model_validator(mode="after")
    def _target(self) -> ForkAction:
        if self.checkpoint_id is None and self.label is None:
            raise ValueError("fork action requires 'checkpoint_id' or 'label'")
        return self


class CreateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create"]
    recipe_id: str | None = None
    needs: dict[str, object] | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    label: str | None = None
    meta_exec: str | None = None


def validate_transform_result(result: object) -> list[str]:
    """Errors for a Starlark transform result; empty means valid. None is valid."""
    if result is None:
        return []
    if not isinstance(result, dict):
        return ["transform must return a dict or None"]
    action = result.get("action")
    model: type[BaseModel]
    if action == "fork":
        model = ForkAction
    elif action == "create":
        model = CreateAction
    else:
        return [f"action must be 'fork' or 'create', got {action!r}"]
    try:
        model.model_validate(result)
    except ValidationError as exc:
        return [_describe(e) for e in exc.errors()]
    return []


def _describe(error: dict[str, Any]) -> str:
    loc = ".".join(str(p) for p in error.get("loc", ()))
    if error.get("type") == "extra_forbidden":
        return f"unknown field for this action: {loc}"
    return f"{loc}: {error.get('msg', 'invalid')}" if loc else str(error.get("msg", "invalid"))


@dataclass(frozen=True)
class TriggerOutcome:
    status_code: int
    body: dict[str, object] | None


class IngressService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        computers: ComputerService,
        checkpoints: CheckpointService,
        lifecycle: Lifecycle,
        tasks: BackgroundTasks,
    ) -> None:
        self.config = config
        self.db = db
        self.computers = computers
        self.checkpoints = checkpoints
        self.lifecycle = lifecycle
        self.tasks = tasks
        self._limiters: dict[str, RateLimiter] = {}

    # -- rules ---------------------------------------------------------------

    async def create_rule(
        self,
        account: Account,
        *,
        name: str,
        starlark_source: str,
        response_mode: str,
        max_body_bytes: int,
        rate_limit_rpm: int,
    ) -> IngressRule:
        self._check_source(starlark_source)
        now = datetime.now(UTC).isoformat()
        rule = IngressRule(
            internal_id=str(uuid.uuid4()),
            id=f"ir_{secrets.token_urlsafe(20)}",
            account_id=account.id,
            name=name,
            starlark_source=starlark_source,
            response_mode=response_mode,
            max_body_bytes=max_body_bytes,
            rate_limit_rpm=rate_limit_rpm,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        await insert_ingress_rule(self.db, rule)
        return rule

    async def list_rules(self, account: Account) -> list[IngressRule]:
        return await list_ingress_rules_by_account(self.db, account.id)

    async def get_rule(self, account: Account, rule_id: str) -> IngressRule:
        rule = await get_ingress_rule_by_id(self.db, rule_id)
        if rule is None or rule.account_id != account.id:
            raise NotFound("Ingress rule not found")
        return rule

    async def update_rule(
        self,
        account: Account,
        rule_id: str,
        *,
        name: str | None = None,
        starlark_source: str | None = None,
        response_mode: str | None = None,
        max_body_bytes: int | None = None,
        rate_limit_rpm: int | None = None,
        enabled: bool | None = None,
    ) -> IngressRule:
        rule = await self.get_rule(account, rule_id)
        if starlark_source is not None:
            self._check_source(starlark_source)
            rule.starlark_source = starlark_source
        if name is not None:
            rule.name = name
        if response_mode is not None:
            rule.response_mode = response_mode
        if max_body_bytes is not None:
            rule.max_body_bytes = max_body_bytes
        if rate_limit_rpm is not None:
            rule.rate_limit_rpm = rate_limit_rpm
        if enabled is not None:
            rule.enabled = enabled
        rule.updated_at = datetime.now(UTC).isoformat()
        await update_ingress_rule(self.db, rule)
        return rule

    async def delete_rule(self, account: Account, rule_id: str) -> None:
        await self.get_rule(account, rule_id)
        await delete_ingress_rule(self.db, rule_id)
        self._limiters.pop(rule_id, None)

    async def rotate_rule(self, account: Account, rule_id: str) -> IngressRule:
        rule = await self.get_rule(account, rule_id)
        new_id = f"ir_{secrets.token_urlsafe(20)}"
        await rotate_ingress_rule_id(self.db, rule.internal_id, new_id)
        limiter = self._limiters.pop(rule_id, None)
        if limiter is not None:
            self._limiters[new_id] = limiter
        rule.id = new_id
        rule.updated_at = datetime.now(UTC).isoformat()
        return rule

    async def logs(self, account: Account, rule_id: str) -> list[IngressLog]:
        rule = await self.get_rule(account, rule_id)
        return await list_ingress_logs(self.db, rule.internal_id)

    def test_rule(
        self, rule: IngressRule, request_dict: dict[str, object]
    ) -> tuple[dict[str, Any] | None, list[str], float]:
        t0 = time.monotonic()
        try:
            result = execute_transform(rule.starlark_source, request_dict)
        except StarlarkError as exc:
            return None, [str(exc)], (time.monotonic() - t0) * 1000
        elapsed_ms = (time.monotonic() - t0) * 1000
        return result, validate_transform_result(result), elapsed_ms

    def limiter_for(self, rule: IngressRule) -> RateLimiter:
        limiter = self._limiters.get(rule.id)
        if limiter is None or limiter.max_requests != rule.rate_limit_rpm:
            limiter = RateLimiter(max_requests=rule.rate_limit_rpm, window_seconds=60.0)
            self._limiters[rule.id] = limiter
        return limiter

    @staticmethod
    def _check_source(source: str) -> None:
        errors = validate_starlark(source)
        if errors:
            raise InvalidInput("invalid starlark", detail={"starlark_errors": errors})

    # -- trigger -------------------------------------------------------------

    async def trigger(self, rule_id: str, request_dict: dict[str, object]) -> TriggerOutcome:
        rule = await get_ingress_rule_by_id(self.db, rule_id)
        if rule is None or not rule.enabled:
            raise NotFound("Ingress rule not found")
        if not self.limiter_for(rule).check(rule.id):
            raise LimitExceeded("Rate limit exceeded")
        try:
            result = execute_transform(rule.starlark_source, request_dict)
        except StarlarkError as exc:
            await self._log(rule, IngressLogStatus.FAILED, None, str(exc))
            raise TransformError(
                "starlark failed", detail=f"Starlark execution error: {exc}"
            ) from None
        if result is None:
            await self._log(rule, IngressLogStatus.COMPLETED, None, None)
            return TriggerOutcome(204, None)
        errors = validate_transform_result(result)
        if errors:
            await self._log(rule, IngressLogStatus.FAILED, json.dumps(result), "; ".join(errors))
            raise TransformError(
                "invalid transform result", detail={"errors": errors, "starlark_result": result}
            )
        account = await get_account_by_id(self.db, rule.account_id)
        if account is None:
            raise NotFound("Ingress rule's account not found")
        if rule.response_mode == "async":
            self.tasks.spawn(self._execute_and_log(account, rule, result), name=f"ingress:{rule.id}")
            await self._log(rule, IngressLogStatus.ACCEPTED, json.dumps(result), None)
            return TriggerOutcome(202, {"status": IngressLogStatus.ACCEPTED.value})
        try:
            body = await self.execute(account, result)
        except Exception as exc:
            await self._log(rule, IngressLogStatus.FAILED, json.dumps(result), _error_text(exc))
            raise
        await self._log(rule, IngressLogStatus.COMPLETED, json.dumps(result), None)
        return TriggerOutcome(200, body)

    async def execute(self, account: Account, action: dict[str, Any]) -> dict[str, object]:
        if action["action"] == "fork":
            fork = ForkAction.model_validate(action)
            if fork.checkpoint_id is not None:
                checkpoint = await self.checkpoints.get_owned(account, fork.checkpoint_id)
            else:
                assert fork.label is not None
                latest = await self.checkpoints.latest_for_label(account, fork.label)
                if latest is None:
                    raise NotFound(f"No checkpoint with label '{fork.label}'")
                checkpoint = latest
            spec = ExecSpec(
                command=fork.exec, self_destruct=fork.self_destruct,
                callback_url=fork.callback_url, label=None, meta_exec=fork.meta_exec,
            )
            forked = await self.checkpoints.fork_or_defer(
                account, checkpoint, spec, recipe_id=checkpoint.recipe_id, exclusive=fork.exclusive
            )
            if isinstance(forked, Deferred):
                return {"deferred_id": forked.deferred_id, "status": "queued"}
            outcome = await self.lifecycle.run_ephemeral(
                account, forked, spec, source_checkpoint=checkpoint
            )
            return {
                "computer_id": forked.id,
                "checkpoint_id": checkpoint.id,
                "exec_exit_code": outcome.exec_exit_code,
                "exec_stdout": outcome.exec_stdout,
                "exec_stderr": outcome.exec_stderr,
                "created_checkpoint_id": outcome.created_checkpoint_id,
            }
        create = CreateAction.model_validate(action)
        resources = Resources.from_needs(create.needs)
        computer = await self.computers.create(
            account, recipe_id=create.recipe_id, resources=resources
        )
        spec = ExecSpec(
            command=create.exec, self_destruct=create.self_destruct,
            callback_url=create.callback_url, label=create.label, meta_exec=create.meta_exec,
        )
        outcome = await self.lifecycle.run_ephemeral(account, computer, spec, source_checkpoint=None)
        return {
            "computer_id": computer.id,
            "url": f"https://{computer.id}.{self.config.domain}",
            "recipe_id": computer.recipe_id,
            "exec_exit_code": outcome.exec_exit_code,
            "exec_stdout": outcome.exec_stdout,
            "exec_stderr": outcome.exec_stderr,
            "created_checkpoint_id": outcome.created_checkpoint_id,
        }

    async def _execute_and_log(
        self, account: Account, rule: IngressRule, action: dict[str, Any]
    ) -> None:
        try:
            await self.execute(account, action)
        except Exception as exc:
            logger.warning("Async ingress action failed for rule %s: %s", rule.id, exc)
            await self._log(rule, IngressLogStatus.FAILED, json.dumps(action), _error_text(exc))

    async def _log(
        self, rule: IngressRule, status: IngressLogStatus, result: str | None, error: str | None
    ) -> None:
        log = IngressLog(
            id=f"ilog-{uuid.uuid4().hex[:12]}",
            rule_internal_id=rule.internal_id,
            status=status,
            starlark_result=result,
            error_message=error,
            created_at=datetime.now(UTC).isoformat(),
        )
        try:
            await insert_ingress_log(self.db, log)
        except Exception:
            logger.warning("Failed to write ingress log for %s", rule.internal_id)


def _error_text(exc: Exception) -> str:
    message = getattr(exc, "message", None)
    return str(message) if message else str(exc)
```

Two behaviour notes, both intended: the async path now records a `failed` log when the background action raises (before, the failure was only a warning line), and a `fork` action carries `label=None` in its `ExecSpec` because `run_ephemeral` takes the label from the source checkpoint (as the old `_do_fork` did).

Interim wiring in `src/mshkn/api/ingress.py`: change the three imports (`mshkn.services.starlark`, and `validate_transform_result` from `mshkn.services.ingress` in place of the local `_validate_transform_result`), delete the local validator and its `VALID_*` sets. In `tests/unit/test_ingress.py`, delete the seven `_validate_transform_result` tests (covered by the new file) and repoint the starlark imports; leave the endpoint tests as they are (they pass through the unchanged router until Task 11).

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_ingress_service.py tests/unit/test_ingress.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1`
Expected: pass; clean; previous + 5 − 7.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(services): IngressService with Pydantic-validated actions; create honours recipe_id and needs"
```

---

### Task 11: Runtime, schemas, thin routers; delete VMManager

**Files:**
- Create: `src/mshkn/api/schemas.py`
- Rewrite: `src/mshkn/runtime.py`, `src/mshkn/api/computers.py`, `src/mshkn/api/checkpoints.py`, `src/mshkn/api/recipes.py`, `src/mshkn/api/ingress.py`, `src/mshkn/api/system.py` (alerts only; health is Task 12)
- Delete: `src/mshkn/vm/` (package), `src/mshkn/ingress/` (package), `tests/unit/test_vm_manager.py`
- Modify: `src/mshkn/app.py` (no change expected beyond imports), `tests/unit/conftest.py`, `tests/flow/conftest.py`, and every unit test that built an `AsyncMock()` VMManager: `tests/unit/test_vm_limit.py`, `test_exec_on_create.py`, `test_self_destruct.py`, `test_status_timeout.py`, `test_exec_stream.py`, `test_runtime.py`, `test_ingress.py`, `test_health.py`, `test_metrics.py`

**Interfaces:**
- `Runtime` (spec §5):

```python
@dataclass
class Runtime:
    config: Config
    db: aiosqlite.Connection
    host: Host
    tasks: BackgroundTasks
    allocator: SlotAllocator
    rate_limiter: RateLimiter
    recipes: RecipeService
    computers: ComputerService
    checkpoints: CheckpointService
    lifecycle: Lifecycle
    ingress: IngressService
    reaper: Reaper
    alerts: deque[Alert]
    http: httpx.AsyncClient

    @classmethod
    def build(cls, config, db, host, *, http: httpx.AsyncClient | None = None) -> Runtime: ...
    @classmethod
    async def from_env(cls) -> Runtime: ...
    async def start(self) -> None: ...   # allocator.initialize, reaper.reap_dead, spawn reaper.run under key "reaper"
    async def close(self) -> None: ...   # cancel reaper, drain 30 s, http.aclose, guest.close, proxy.close, db.close
```

- `tests/unit/conftest.py::make_runtime(db, *, config=None, host=None, http=None) -> Runtime` = `Runtime.build(...)` with `Config(domain="test.dev", checkpoint_local_dir=<tmp>)`; because the fake hypervisor writes template files, `make_runtime` takes the `tmp_path` fixture through a new `runtime_config` fixture (see Step 3). `make_app(runtime)` unchanged.
- `api/schemas.py`: `CreateRequest`, `CreateResponse`, `ExecRequest`, `ExecBgResponse(pid)`, `ExecKillResponse(status, stderr=None)`, `UploadResponse(status, path)`, `ComputerStatusResponse(computer_id, status, url, vm_ip, recipe_id, created_at, last_exec_at, cpu_pct=None, ram_usage_mb=None, ram_total_mb=None, disk_usage_mb=None, disk_total_mb=None, processes=None)`, `CheckpointRequest`, `CheckpointResponse`, `DestroyResponse(status)`, `ForkRequest`, `ForkResponse`, `DeferredResponse(deferred_id, status)`, `MergeRequest`, `MergeConflict(path, resolution)`, `MergeResponse(checkpoint_id, conflicts, auto_merged, unchanged)`, `CheckpointSummary(id, checkpoint_id, parent_id, computer_id, recipe_id, r2_prefix, disk_delta_size_bytes, memory_size_bytes, label, pinned, created_at)`, `DeleteResponse(status)`, `CreateRecipeRequest`, `RecipeResponse`, the five ingress models moved from `ingress/models.py` plus `IngressRuleDetail(IngressRuleResponse)` with `starlark_source`, `AlertResponse`, `HealthResponse(status, subsystems)`.

- [ ] **Step 1: Write the failing tests**

Convert the mocked-manager tests to real services. The rule for every conversion: an `AsyncMock()` VMManager and a hand-inserted `Computer` row become a real create through the API (or `rt.computers.create`) on a `FakeHost`; assertions on `vm_mgr.x.assert_called…` become assertions on fake-host state or the database. Concretely:

`tests/unit/conftest.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.host.fake import FakeHost
from mshkn.runtime import Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite
    from fastapi import FastAPI

    from mshkn.host import Host


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(tmp_path / "test.db")
    await run_migrations(conn, Path("migrations"))
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def runtime_config(tmp_path: Path) -> Config:
    """A Config whose writable paths live under tmp_path (templates, checkpoints)."""
    return Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")


def make_runtime(
    db: aiosqlite.Connection,
    *,
    config: Config,
    host: Host | None = None,
    http: httpx.AsyncClient | None = None,
) -> Runtime:
    """A Runtime for API tests: real DB and services, in-memory Host, no reaper loop."""
    return Runtime.build(config, db, host if host is not None else FakeHost(), http=http)


def make_app(runtime: Runtime) -> FastAPI:
    return create_app(runtime)
```

`tests/unit/test_vm_limit.py` becomes:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account
from mshkn.models import Account
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config

AUTH = {"Authorization": "Bearer test-key"}


async def _account(db: aiosqlite.Connection, vm_limit: int) -> None:
    await insert_account(
        db, Account(id="acct-1", api_key="test-key", vm_limit=vm_limit, created_at="t")
    )


async def test_create_is_limited_and_destroyed_computers_do_not_count(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    await _account(db, vm_limit=2)
    app = make_app(make_runtime(db, config=runtime_config))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/computers", json={}, headers=AUTH)
        second = await client.post("/computers", json={}, headers=AUTH)
        assert first.status_code == 200 and second.status_code == 200
        third = await client.post("/computers", json={}, headers=AUTH)
        assert third.status_code == 429 and third.json()["detail"] == "VM limit reached"
        gone = await client.delete(f"/computers/{first.json()['computer_id']}", headers=AUTH)
        assert gone.status_code == 200
        fourth = await client.post("/computers", json={}, headers=AUTH)
        assert fourth.status_code == 200
```

`tests/unit/test_exec_on_create.py`, `test_self_destruct.py`, `test_exec_stream.py`, `test_status_timeout.py`: keep every assertion, replace the setup. Pattern for each test body:

```python
    await _account(db)
    host = FakeHost()
    host.guest.script["echo hello"] = ExecResult(0, "hello\n", "")
    rt = make_runtime(db, config=runtime_config, host=host)
    app = make_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/computers", json={"exec": "echo hello"}, headers=AUTH)
```

and where a test needs an existing running computer (exec stream, status timeout, exec-on-fork), create it through `await rt.computers.create(account, recipe_id=None, resources=DEFAULT_RESOURCES)` and use its id and `vm_ip` in the assertions instead of the hard-coded `comp-1`/`172.16.1.2`. `vm_mgr.destroy.assert_called_once_with(...)` becomes `assert (await get_computer(db, cid)).status is ComputerStatus.DESTROYED`; `vm_mgr.destroy.assert_not_called()` becomes `… is ComputerStatus.RUNNING`. The `"uses": []` keys in request bodies are deleted (they were ignored). `test_callback_url_fires_on_self_destruct` stops patching `deliver_callback`: build an in-process receiver exactly as `tests/unit/test_lifecycle.py::_receiver` does and pass `http=httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver), base_url="http://receiver")` to `make_runtime`, with `callback_url="http://receiver/cb"`, then `await rt.tasks.drain(timeout=2.0)` before asserting on the received payload. `test_status_timeout.py` monkeypatches `mshkn.services.computers.STATUS_METRICS_TIMEOUT_SECONDS` instead of the router constant.

`tests/unit/test_runtime.py::test_lifespan_closes_runtime_even_when_start_fails`: build a real runtime on a `FakeHost` whose `blocks.max_volume_id` raises (monkeypatch the fake's method with `async def boom(): raise RuntimeError("pool missing")`), wrap `db.close` with a recording spy (`monkeypatch.setattr(rt.db, "close", spy)` where `spy` is an `AsyncMock(wraps=rt.db.close)`), and assert the spy was awaited once.

`tests/unit/test_ingress.py`: `_app` becomes `make_app(make_runtime(db, config=Config(checkpoint_local_dir=tmp_path / "ckpts")))` (the production domain is what its `ingress_url` assertion checks). `tests/unit/test_health.py`, `test_metrics.py`: `make_runtime(db, config=runtime_config)`.

`tests/flow/conftest.py`: replace the `VMManager` construction and the `Runtime(...)` literal with `runtime = Runtime.build(config, db, host)`; `await runtime.start()` is **not** called (it would spawn the reaper loop); instead `await runtime.allocator.initialize(db, host.blocks)`. Teardown: `await runtime.tasks.drain(timeout=2.0)`, `await runtime.http.aclose()`, `await db.close()`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_vm_limit.py tests/unit/conftest.py -q`
Expected: `ImportError: cannot import name 'build'`/`TypeError: Runtime.__init__() got an unexpected keyword argument` — the tests target the new Runtime.

- [ ] **Step 3: Implement**

`src/mshkn/runtime.py` — keep `BackgroundTasks` as is; replace `Runtime`:

```python
@dataclass
class Runtime:
    config: Config
    db: aiosqlite.Connection
    host: Host
    tasks: BackgroundTasks
    allocator: SlotAllocator
    rate_limiter: RateLimiter
    recipes: RecipeService
    computers: ComputerService
    checkpoints: CheckpointService
    lifecycle: Lifecycle
    ingress: IngressService
    reaper: Reaper
    alerts: deque[Alert]
    http: httpx.AsyncClient

    @classmethod
    def build(
        cls,
        config: Config,
        db: aiosqlite.Connection,
        host: Host,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> Runtime:
        """Wire the services once. Tests call this with a FakeHost."""
        tasks = BackgroundTasks()
        allocator = SlotAllocator()
        client = http if http is not None else httpx.AsyncClient()
        alerts: deque[Alert] = deque(maxlen=_ALERT_HISTORY_SIZE)
        recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
        computers = ComputerService(config, db, host, allocator, recipes)
        checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
        lifecycle = Lifecycle(db, computers, checkpoints, tasks, client)
        ingress = IngressService(config, db, computers, checkpoints, lifecycle, tasks)
        reaper = Reaper(config, db, host, computers, checkpoints, lifecycle, alerts)
        return cls(
            config=config, db=db, host=host, tasks=tasks, allocator=allocator,
            rate_limiter=RateLimiter(max_requests=80, window_seconds=10.0),
            recipes=recipes, computers=computers, checkpoints=checkpoints,
            lifecycle=lifecycle, ingress=ingress, reaper=reaper, alerts=alerts, http=client,
        )

    @classmethod
    async def from_env(cls) -> Runtime:
        config = Config.from_env()
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await connect(config.db_path)
        await run_migrations(db, config.migrations_dir)
        return cls.build(config, db, firecracker_host(config))

    async def start(self) -> None:
        """Recover host state and start the reaper. Called from the app lifespan."""
        await self.allocator.initialize(self.db, self.host.blocks)
        reaped = await self.reaper.reap_dead()
        if reaped:
            logger.info("Startup: reaped %d dead VM(s)", reaped)
        await self.computers.refresh_active_gauge()
        self.tasks.spawn(self.reaper.run(), name="reaper", key="reaper")

    async def close(self) -> None:
        await self.tasks.cancel("reaper")
        await self.tasks.drain(_DRAIN_TIMEOUT_SECONDS)
        await self.http.aclose()
        await self.host.guest.close()
        await self.host.proxy.close()
        await self.db.close()
```

(`_ALERT_HISTORY_SIZE = 100`; `deque` from `collections`; `httpx` imported at runtime; the service classes imported at module level — `services` never imports `runtime` at runtime, only under `TYPE_CHECKING` for `BackgroundTasks`, so there is no cycle. If mypy flags a cycle through the `TYPE_CHECKING` import, move `BackgroundTasks` into `src/mshkn/tasks.py` and re-export it from `runtime.py`; record that in the report.)

`src/mshkn/api/schemas.py`: every model listed under Interfaces, each a `pydantic.BaseModel` with the fields named there, all `str | None = None` where the current dict responses can omit or null a value. `ComputerStatusResponse.processes: list[dict[str, object]] | None = None`. Ingress models are moved verbatim.

Routers. Each handler: resolve `rt = get_runtime(request)`, translate the body into service arguments, call one service method, translate the result into a schema. No `HTTPException` remains except the 401s in `deps.py` and the 429 exec rate limit (`_check_rate_limit` stays; it is a per-key HTTP concern). No `import` inside a function. The shapes:

```python
# api/computers.py
@router.post("", response_model=CreateResponse)
async def create_computer(request: Request, body: CreateRequest, account: Account = _require_account) -> CreateResponse:
    rt = get_runtime(request)
    resources = Resources.from_needs(body.needs)
    computer = await rt.computers.create(account, recipe_id=body.recipe_id, resources=resources)
    spec = ExecSpec(command=body.exec, self_destruct=body.self_destruct, callback_url=body.callback_url, label=body.label, meta_exec=body.meta_exec)
    outcome = await rt.lifecycle.run_ephemeral(account, computer, spec, source_checkpoint=None)
    return CreateResponse(computer_id=computer.id, url=f"https://{computer.id}.{rt.config.domain}", recipe_id=computer.recipe_id, exec_exit_code=outcome.exec_exit_code, exec_stdout=outcome.exec_stdout, exec_stderr=outcome.exec_stderr, created_checkpoint_id=outcome.created_checkpoint_id)


@router.post("/{computer_id}/exec")
async def exec_command(computer_id: str, body: ExecRequest, request: Request, account: Account = _require_account) -> EventSourceResponse:
    rt = get_runtime(request)
    _check_rate_limit(rt, request)
    computer = await rt.computers.get_running(account, computer_id)

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        t0 = time.monotonic()
        try:
            async for stream, line in rt.computers.stream(computer, body.command):
                yield {"event": stream, "data": line}
        except Exception as exc:
            logger.warning("exec stream for %s failed: %s", computer_id, type(exc).__name__)
            yield {"event": "error", "data": f"{type(exc).__name__}: {exc}"}
            yield {"event": "exit", "data": "255"}
        finally:
            exec_duration_seconds.observe(time.monotonic() - t0)

    return EventSourceResponse(event_stream())
```

`exec_bg` → `ExecBgResponse(pid=await rt.computers.exec_bg(computer, body.command))`; `exec_logs` → the same SSE shape as today over `await rt.computers.exec_logs(computer, pid)`; `exec_kill` → `ExecKillResponse(status="killed")` or `("not_found", stderr)`; `upload`/`download` → `rt.computers.upload/download`; `computer_status` → `rt.computers.get_owned` then `rt.computers.metrics` and a `ComputerStatusResponse`; `checkpoint_computer` → `rt.computers.get_running`, `rt.checkpoints.create(computer, label=body.label if body else None, pin=body.pin if body else False, trigger=CheckpointTrigger.API)`; `destroy_computer` → `computer = await rt.computers.get_owned(account, computer_id)`, `await rt.computers.destroy(computer.id)`, `await rt.lifecycle.drain_after_destroy(account, computer)`, `DestroyResponse(status=ComputerStatus.DESTROYED)`. `_self_destruct` and `_process_deferred` are deleted.

```python
# api/checkpoints.py
@router.post("/{checkpoint_id}/fork", response_model=None)
async def fork_checkpoint(checkpoint_id: str, request: Request, body: ForkRequest | None = None, account: Account = _require_account) -> ForkResponse | JSONResponse:
    rt = get_runtime(request)
    body = body or ForkRequest()
    ckpt = await rt.checkpoints.get_owned(account, checkpoint_id)
    spec = ExecSpec(command=body.exec, self_destruct=body.self_destruct, callback_url=body.callback_url, label=None, meta_exec=body.meta_exec)
    forked = await rt.checkpoints.fork_or_defer(account, ckpt, spec, recipe_id=body.recipe_id, exclusive=body.exclusive)
    if isinstance(forked, Deferred):
        return JSONResponse(status_code=202, content=DeferredResponse(deferred_id=forked.deferred_id, status="queued").model_dump())
    outcome = await rt.lifecycle.run_ephemeral(account, forked, spec, source_checkpoint=ckpt)
    return ForkResponse(computer_id=forked.id, checkpoint_id=checkpoint_id, exec_exit_code=outcome.exec_exit_code, exec_stdout=outcome.exec_stdout, exec_stderr=outcome.exec_stderr, created_checkpoint_id=outcome.created_checkpoint_id)
```

`merge_checkpoints` → `outcome = await rt.checkpoints.merge(account, parent_id, body.checkpoint_a, body.checkpoint_b)` → `MergeResponse(checkpoint_id=outcome.checkpoint.id, conflicts=[MergeConflict(path=p, resolution="fork_a") for p in outcome.conflicts], auto_merged=…, unchanged=…)`; `list_checkpoints` → `[CheckpointSummary(...) for c in await rt.checkpoints.list(account, label=label)]`; `delete_checkpoint` → `ckpt = await rt.checkpoints.get_owned(...)`, `await rt.checkpoints.delete(ckpt)`, `DeleteResponse(status="deleted")`.

`api/recipes.py`: `create_recipe` → `recipe, created = await rt.recipes.create(account, body.dockerfile)`; `JSONResponse(status_code=202 if created else 200, content=RecipeResponse(...).model_dump())`; `get`/`list`/`delete` → `rt.recipes.get/list/delete`.

`api/ingress.py`: rule endpoints call `rt.ingress.create_rule(account, name=body.name, …)` etc. and build `IngressRuleResponse` with `ingress_url=f"https://{rt.config.domain}/ingress/{rule.id}"`; `get_rule` returns `IngressRuleDetail`; `test_rule` builds the request dict as today and calls `rt.ingress.test_rule(rule, request_dict)`; `get_rule_logs` → `rt.ingress.logs`. `handle_ingress`: `request_dict = await _parse_ingress_body(request, rule.max_body_bytes)` needs the rule for the limit, so first `rule = await get_ingress_rule_by_id(rt.db, rule_id)` with `NotFound` on missing/disabled (the service repeats the check; that is fine), then `outcome = await rt.ingress.trigger(rule_id, request_dict)`; `Response(status_code=204)` when `outcome.body is None`, else `JSONResponse(status_code=outcome.status_code, content=outcome.body)`. `_parse_ingress_body` raises `PayloadTooLarge("Request body too large")` for both size checks and `InvalidInput("Failed to parse request body")` for the generic parse failure. `_do_create`, `_do_fork`, `_execute_action`, `_execute_action_and_log`, `_log_invocation`, `_get_account` are deleted.

`api/system.py`: `alerts` → `[AlertResponse(**asdict(a)) for a in rt.alerts]`.

Delete `src/mshkn/vm/`, `src/mshkn/ingress/`, `tests/unit/test_vm_manager.py` (its slot test is covered by `test_allocator.py`, its reaper test by `test_reaper.py`).

- [ ] **Step 4: Verify**

```bash
grep -rnE "mshkn\.vm\b|mshkn\.ingress\b|vm_manager|VMManager|HTTPException" src | grep -v "api/deps.py" | grep -v "_check_rate_limit" || echo "clean"
grep -rn "^    from \|^        from \|^    import \|^        import " src/mshkn --include='*.py' | grep -v "TYPE_CHECKING" | grep -v "^src/mshkn/.*:\s*#" || echo "no function-local imports"
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1
```

Expected: both greps clean (the second may list indented imports inside `if TYPE_CHECKING:` blocks, which is fine — inspect anything else); gate clean; report the count.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "refactor: Runtime wires the services; routers are thin; VMManager and the vm/ingress packages are gone"
```

---

### Task 12: Accounts CLI, health subsystems, deploy scripts

**Files:**
- Create: `src/mshkn/cli.py`, `src/mshkn/__main__.py`, `tests/unit/test_cli.py`
- Modify: `pyproject.toml` (`[project.scripts]`), `src/mshkn/api/system.py` (health), `tests/unit/test_health.py`, `scripts/e2e.sh`, `DEPLOY.md` (§10 and the `sqlite3` mention in the package list)

**Interfaces:**
- `mshkn.cli.main(argv: Sequence[str] | None = None) -> int` with subcommands `accounts create --id <id> --api-key <key> [--vm-limit <n>=10]`, `accounts list`, `migrate`. `Config.from_env()` supplies `db_path` and `migrations_dir`; `migrate` and `accounts create` run migrations first so a fresh box works in one step. `accounts create` on an existing id prints `account <id> already exists` to stderr and returns 1. `accounts list` prints one line per account: `<id>\t<vm_limit>\t<created_at>` (never the key).
- `GET /health` → `HealthResponse(status="ok"|"degraded", subsystems={"database": …, "firecracker": …, "storage": …, "proxy": …})`, each `"ok"` or an error string, HTTP 200 always. Checks: database `SELECT 1`; firecracker `shutil.which("firecracker")` is not None and `config.kernel_path.exists()`; storage `await host.blocks.usage()`; proxy `await host.proxy.healthy()`. The firecracker check is a module-level `_firecracker_present(config) -> str` so tests can monkeypatch `shutil.which`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_cli.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mshkn.cli import main
from mshkn.db import connect, get_account_by_id


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("MSHKN_DB_PATH", str(db_path))
    monkeypatch.setenv("MSHKN_MIGRATIONS_DIR", str(Path("migrations").resolve()))
    return db_path


async def test_accounts_create_and_list(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["accounts", "create", "--id", "acct-x", "--api-key", "secret", "--vm-limit", "7"]) == 0
    db = await connect(env)
    try:
        account = await get_account_by_id(db, "acct-x")
    finally:
        await db.close()
    assert account is not None and account.api_key == "secret" and account.vm_limit == 7
    assert main(["accounts", "list"]) == 0
    out = capsys.readouterr().out
    assert "acct-x\t7\t" in out and "secret" not in out


def test_accounts_create_twice_fails_cleanly(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["accounts", "create", "--id", "acct-x", "--api-key", "k"]) == 0
    assert main(["accounts", "create", "--id", "acct-x", "--api-key", "k2"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_migrate_is_idempotent(env: Path) -> None:
    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    assert env.exists()
```

`tests/unit/test_health.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

import mshkn.api.system as system_module
from mshkn.host.fake import FakeHost
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config


async def _health(db: aiosqlite.Connection, config: Config, host: FakeHost) -> dict[str, object]:
    app = make_app(make_runtime(db, config=config, host=host))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body: dict[str, object] = resp.json()
    return body


async def test_health_is_ok_when_every_subsystem_answers(
    db: aiosqlite.Connection, runtime_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_module, "_firecracker_present", lambda config: "ok")
    body = await _health(db, runtime_config, FakeHost())
    assert body == {
        "status": "ok",
        "subsystems": {"database": "ok", "firecracker": "ok", "storage": "ok", "proxy": "ok"},
    }


async def test_health_is_degraded_but_200_when_a_subsystem_fails(
    db: aiosqlite.Connection, runtime_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_module, "_firecracker_present", lambda config: "ok")
    host = FakeHost()
    host.proxy.is_healthy = False
    body = await _health(db, runtime_config, host)
    assert body["status"] == "degraded"
    subsystems = body["subsystems"]
    assert isinstance(subsystems, dict) and subsystems["proxy"] != "ok" and subsystems["database"] == "ok"


async def test_health_reports_a_missing_firecracker_binary(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    body = await _health(db, runtime_config, FakeHost())
    subsystems = body["subsystems"]
    assert isinstance(subsystems, dict)
    assert body["status"] == "degraded" and "firecracker" in str(subsystems["firecracker"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_health.py -q`
Expected: `ModuleNotFoundError: No module named 'mshkn.cli'`; the health tests fail on the missing `subsystems` key.

- [ ] **Step 3: Implement**

`src/mshkn/cli.py`:

```python
"""`python -m mshkn`: operator commands that work directly on the configured database."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mshkn.config import Config
from mshkn.db import connect, get_account_by_id, insert_account, list_accounts, run_migrations
from mshkn.models import Account

if TYPE_CHECKING:
    from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mshkn")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations")
    accounts = sub.add_parser("accounts", help="manage API accounts").add_subparsers(
        dest="accounts_command", required=True
    )
    create = accounts.add_parser("create", help="create an account")
    create.add_argument("--id", required=True)
    create.add_argument("--api-key", required=True)
    create.add_argument("--vm-limit", type=int, default=10)
    accounts.add_parser("list", help="list accounts (never prints keys)")
    return parser


async def _run(args: argparse.Namespace) -> int:
    config = Config.from_env()
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await connect(config.db_path)
    try:
        await run_migrations(db, config.migrations_dir)
        if args.command == "migrate":
            return 0
        if args.accounts_command == "list":
            for account in await list_accounts(db):
                print(f"{account.id}\t{account.vm_limit}\t{account.created_at}")
            return 0
        if await get_account_by_id(db, args.id) is not None:
            print(f"account {args.id} already exists", file=sys.stderr)
            return 1
        try:
            await insert_account(
                db,
                Account(
                    id=args.id,
                    api_key=args.api_key,
                    vm_limit=args.vm_limit,
                    created_at=datetime.now(UTC).isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:  # duplicate api_key
            print(f"cannot create account {args.id}: {exc}", file=sys.stderr)
            return 1
        print(f"created account {args.id} (vm_limit={args.vm_limit})")
        return 0
    finally:
        await db.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))
```

`src/mshkn/__main__.py`:

```python
from __future__ import annotations

import sys

from mshkn.cli import main

sys.exit(main())
```

`pyproject.toml`, under `[project]`:

```toml
[project.scripts]
mshkn = "mshkn.cli:main"
```

(then `uv lock` so `uv lock --check` stays green; commit the lock change.)

`src/mshkn/api/system.py`:

```python
"""Unauthenticated system endpoints: health, metrics, alerts."""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from mshkn.api.deps import get_runtime
from mshkn.api.schemas import AlertResponse, HealthResponse

if TYPE_CHECKING:
    from mshkn.config import Config
    from mshkn.runtime import Runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


def _firecracker_present(config: Config) -> str:
    if shutil.which("firecracker") is None:
        return "firecracker binary not on PATH"
    if not config.kernel_path.exists():
        return f"kernel not found at {config.kernel_path}"
    return "ok"


async def _database(rt: Runtime) -> str:
    cursor = await rt.db.execute("SELECT 1")
    await cursor.fetchone()
    return "ok"


async def _storage(rt: Runtime) -> str:
    await rt.host.blocks.usage()
    return "ok"


async def _proxy(rt: Runtime) -> str:
    return "ok" if await rt.host.proxy.healthy() else "proxy admin API not reachable"


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    rt = get_runtime(request)
    subsystems: dict[str, str] = {}
    for name, check in (("database", _database), ("storage", _storage), ("proxy", _proxy)):
        try:
            subsystems[name] = await check(rt)
        except Exception as exc:
            subsystems[name] = f"{type(exc).__name__}: {exc}"
    subsystems["firecracker"] = _firecracker_present(rt.config)
    ordered = {k: subsystems[k] for k in ("database", "firecracker", "storage", "proxy")}
    status = "ok" if all(v == "ok" for v in ordered.values()) else "degraded"
    if status != "ok":
        logger.warning("health degraded: %s", {k: v for k, v in ordered.items() if v != "ok"})
    return HealthResponse(status=status, subsystems=ordered)


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/alerts", response_model=list[AlertResponse])
async def alerts(request: Request) -> list[AlertResponse]:
    return [AlertResponse(**asdict(a)) for a in get_runtime(request).alerts]
```

`scripts/e2e.sh`: replace the `sqlite3 … INSERT OR IGNORE …` line (inside the remote script) with:

```bash
cd /opt/mshkn && (.venv/bin/python -m mshkn accounts list | grep -q '^acct-mike	' \
  || .venv/bin/python -m mshkn accounts create --id acct-mike --api-key '${API_KEY}' --vm-limit 20)
```

(that is a literal tab after `acct-mike` inside the grep pattern; keep the remote script's existing quoting/escaping style.) `DEPLOY.md` §10 becomes the same two commands run by hand (with the real key), and the package list drops `sqlite3` from the "used by scripts/e2e.sh" sentence (the package can stay installed; it is handy for inspection).

- [ ] **Step 4: Verify**

`uv lock && uv lock --check && uv run mshkn --help | head -3 && uv run pytest tests/unit/test_cli.py tests/unit/test_health.py -q && uv run ruff check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1`
Expected: help text prints; `6 passed`; clean; previous + 5.

- [ ] **Step 5: Commit**

```bash
git add -A src tests pyproject.toml uv.lock scripts DEPLOY.md && git commit -m "feat: accounts CLI, health subsystems, alerts from the runtime; e2e.sh and DEPLOY.md use the CLI"
```

---

### Task 13: Flow tests for the PR 4 behaviours

**Files:**
- Create: `tests/flow/test_exclusive.py`, `tests/flow/test_self_destruct.py`, `tests/flow/test_reaper.py`, `tests/flow/test_failures.py`, `tests/flow/test_ingress.py`, `tests/flow/test_recipes.py`, `tests/flow/test_system.py`
- Modify: `tests/flow/conftest.py` (the `Flow` fixture gains an in-process callback receiver and `received: list[dict]`; a `flow_factory` for tests that need a custom `Config`)

These pin, through HTTP, the behaviours PR 4 changes. The exhaustive §11 matrix (tenant isolation on every resource, every domain error code, the streaming-order test, …) is PR 5.

- [ ] **Step 1: Extend the fixture**

`tests/flow/conftest.py` — `Flow` gets `received: list[dict[str, Any]]`; the fixture builds the receiver app (a FastAPI app with `POST /cb` appending the JSON body) and passes `http=httpx.AsyncClient(transport=ASGITransport(app=receiver), base_url="http://receiver")` into `Runtime.build`. Add:

```python
@pytest.fixture
def flow_factory(tmp_path: Path) -> Callable[..., AbstractAsyncContextManager[Flow]]:
    """Build a Flow with Config overrides (idle_timeout_seconds, checkpoint_retention_count)."""

    @asynccontextmanager
    async def make(**overrides: Any) -> AsyncIterator[Flow]:
        config = Config(
            domain="test.dev", checkpoint_local_dir=tmp_path / "checkpoints", idle_timeout_seconds=0
        )
        config = replace(config, **overrides)
        ...  # identical body to the `flow` fixture from here on, yielding a Flow

    return make
```

(`replace` from `dataclasses`; factor the shared body into `async def _build_flow(config, tmp_path) -> AsyncIterator[Flow]` used by both.)

- [ ] **Step 2: Write the tests**

`tests/flow/test_exclusive.py`:

```python
async def test_error_on_conflict_is_409_and_defer_drains_after_destroy(flow: Flow) -> None:
    host = flow.host
    host.guest.script["sync"] = ExecResult(0, "", "")
    host.guest.script["echo deferred"] = ExecResult(0, "deferred\n", "")
    base = (await flow.client.post("/computers", json={})).json()["computer_id"]
    ckpt = (await flow.client.post(f"/computers/{base}/checkpoint", json={"label": "chain"})).json()["checkpoint_id"]
    await flow.client.delete(f"/computers/{base}")
    first = await flow.client.post(f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"})
    assert first.status_code == 200
    second = await flow.client.post(f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"})
    assert second.status_code == 409
    queued = await flow.client.post(
        f"/checkpoints/{ckpt}/fork",
        json={"exclusive": "defer_on_conflict", "exec": "echo deferred", "self_destruct": True,
              "callback_url": "http://receiver/cb"},
    )
    assert queued.status_code == 202 and queued.json()["status"] == "queued"
    resp = await flow.client.delete(f"/computers/{first.json()['computer_id']}")
    assert resp.status_code == 200
    await flow.runtime.tasks.drain(timeout=5.0)
    assert any(cmd == "echo deferred" for _, cmd in host.guest.commands)
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert len(chain) == 2, "the deferred run self-destructed into a second labelled checkpoint"
    assert flow.received and flow.received[0]["label"] == "chain"
    cur = await flow.runtime.db.execute("SELECT COUNT(*) FROM deferred_queue")
    assert (await cur.fetchone()) == (0,)
    assert host.hypervisor.alive == {}, "nothing left running after the drained self-destruct"
```

`tests/flow/test_self_destruct.py`:

```python
async def test_create_with_exec_self_destruct_and_callback(flow: Flow) -> None:
    flow.host.guest.script["echo out"] = ExecResult(0, "out\n", "err\n")
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    resp = await flow.client.post(
        "/computers",
        json={"exec": "echo out", "self_destruct": True, "label": "sd", "callback_url": "http://receiver/cb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["exec_exit_code"] == 0 and body["created_checkpoint_id"].startswith("ckpt-")
    status = await flow.client.get(f"/computers/{body['computer_id']}/status")
    assert status.status_code == 404
    await flow.runtime.tasks.drain(timeout=2.0)
    assert flow.received == [{
        "computer_id": body["computer_id"], "checkpoint_id": None, "label": "sd",
        "exec_exit_code": 0, "exec_stdout": "out\n", "exec_stderr": "err\n",
        "created_checkpoint_id": body["created_checkpoint_id"],
    }]
    listed = (await flow.client.get("/checkpoints", params={"label": "sd"})).json()
    assert listed[0]["checkpoint_id"] == body["created_checkpoint_id"]
    assert "manifest_hash" not in listed[0] and "recipe_id" in listed[0]
```

`tests/flow/test_reaper.py`:

```python
async def test_idle_reap_checkpoints_with_trigger_idle_and_dead_reap_cleans_up(flow_factory) -> None:
    async with flow_factory(idle_timeout_seconds=60) as flow:
        host = flow.host
        host.guest.script["sync"] = ExecResult(0, "", "")
        cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
        stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        await flow.runtime.db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, cid))
        await flow.runtime.db.commit()
        before = checkpoints_total.labels(trigger="idle")._value.get()
        await flow.runtime.reaper.cycle()
        assert checkpoints_total.labels(trigger="idle")._value.get() == before + 1
        assert (await flow.client.get(f"/computers/{cid}/status")).status_code == 404
        listed = (await flow.client.get("/checkpoints", params={"label": "auto-idle-timeout"})).json()
        assert len(listed) == 1
        dead = (await flow.client.post("/computers", json={})).json()["computer_id"]
        row = await get_computer(flow.runtime.db, dead)
        assert row is not None
        host.hypervisor.alive.pop(row.firecracker_pid or -1)
        await flow.runtime.reaper.cycle()
        assert (await flow.client.get(f"/computers/{dead}/status")).status_code == 404
        assert row.slot in flow.runtime.allocator.free_slots


async def test_prune_honours_retention_and_pin_and_cancels_uploads(flow_factory, monkeypatch) -> None:
    async with flow_factory(checkpoint_retention_count=1) as flow:
        host = flow.host
        host.guest.script["sync"] = ExecResult(0, "", "")
        cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
        pinned = (await flow.client.post(f"/computers/{cid}/checkpoint", json={"pin": True})).json()["checkpoint_id"]
        old = (await flow.client.post(f"/computers/{cid}/checkpoint", json={})).json()["checkpoint_id"]
        gate = asyncio.Event()

        async def slow_upload(local_dir: Path, prefix: str) -> None:
            await gate.wait()
            assert local_dir.exists()

        monkeypatch.setattr(host.objects, "upload_dir", slow_upload)
        new = (await flow.client.post(f"/computers/{cid}/checkpoint", json={})).json()["checkpoint_id"]
        for ckpt_id, ts in ((pinned, "2026-01-01T00:00:00"), (old, "2026-01-02T00:00:00"), (new, "2026-01-03T00:00:00")):
            await flow.runtime.db.execute("UPDATE checkpoints SET created_at = ? WHERE id = ?", (ts, ckpt_id))
        await flow.runtime.db.commit()
        assert await flow.runtime.checkpoints.prune() == 1
        ids = {c["id"] for c in (await flow.client.get("/checkpoints")).json()}
        assert ids == {pinned, new}
        resp = await flow.client.delete(f"/checkpoints/{new}")
        assert resp.status_code == 200
        assert len(flow.runtime.tasks) == 0, "the in-flight upload was cancelled, not left to fail"
        gate.set()
```

`tests/flow/test_failures.py`:

```python
async def test_boot_failure_after_snap_is_502_and_leaks_nothing(flow: Flow) -> None:
    host = flow.host
    host.hypervisor.fail_next("boot")
    resp = await flow.client.post("/computers", json={"needs": {"ram": "512MB"}})
    assert resp.status_code == 502 and resp.json() == {"detail": "host operation failed"}
    assert host.blocks.volumes == {0: None} and host.hypervisor.alive == {}
    assert flow.runtime.allocator.free_slots == frozenset({1})
    assert computers_active._value.get() == 0
    ok = await flow.client.post("/computers", json={})
    assert ok.status_code == 200


async def test_domain_errors_keep_their_codes(flow: Flow) -> None:
    assert (await flow.client.post("/computers", json={"recipe_id": "rcp-nope"})).status_code == 404
    assert (await flow.client.post("/computers", json={"needs": {"ram": "lots"}})).status_code == 422
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    assert (await flow.client.get(f"/computers/{cid}/download", params={"path": "/nope"})).status_code == 404
    await flow.client.delete(f"/computers/{cid}")
    resp = await flow.client.post(f"/computers/{cid}/exec/bg", json={"command": "true"})
    assert resp.status_code == 400 and "destroyed" in resp.json()["detail"]
    assert (await flow.client.delete(f"/computers/{cid}")).status_code == 404


async def test_active_gauge_equals_the_db_count(flow: Flow) -> None:
    ids = [(await flow.client.post("/computers", json={})).json()["computer_id"] for _ in range(3)]
    assert computers_active._value.get() == 3
    await flow.client.delete(f"/computers/{ids[0]}")
    assert computers_active._value.get() == 2
    row = await get_computer(flow.runtime.db, ids[1])
    assert row is not None
    flow.host.hypervisor.alive.pop(row.firecracker_pid or -1)
    await flow.runtime.reaper.cycle()
    assert computers_active._value.get() == 1
```

`tests/flow/test_ingress.py`:

```python
STARLARK_CREATE = (
    'def transform(req):\n  return {"action": "create", "needs": {"ram": "1GB", "cores": 2},'
    ' "exec": "echo ing", "self_destruct": True, "label": "ing"}'
)
STARLARK_USES = 'def transform(req):\n  return {"action": "create", "uses": ["python"]}'


async def _rule(flow: Flow, source: str, mode: str) -> str:
    resp = await flow.client.post(
        "/ingress_rules", json={"name": "r", "starlark_source": source, "response_mode": mode}
    )
    assert resp.status_code == 200, resp.text
    rule_id: str = resp.json()["id"]
    return rule_id


async def test_sync_create_honours_needs_and_uses_is_rejected(flow: Flow) -> None:
    flow.host.guest.script["echo ing"] = ExecResult(0, "ing\n", "")
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    rule = await _rule(flow, STARLARK_CREATE, "sync")
    resp = await flow.client.post(f"/ingress/{rule}", headers={})
    assert resp.status_code == 200 and resp.json()["exec_stdout"] == "ing\n"
    assert flow.host.hypervisor.booted[0][1] == Resources(mem_mib=1024, vcpus=2)
    bad = await _rule(flow, STARLARK_USES, "sync")
    resp = await flow.client.post(f"/ingress/{bad}")
    assert resp.status_code == 502 and any("uses" in e for e in resp.json()["detail"]["errors"])
    logs = (await flow.client.get(f"/ingress_rules/{bad}/logs")).json()
    assert logs[0]["status"] == "failed"


async def test_async_fork_by_label_runs_in_the_background(flow: Flow) -> None:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "chain"})
    await flow.client.delete(f"/computers/{cid}")
    rule = await _rule(flow, 'def transform(req):\n  return {"action": "fork", "label": "chain", "exec": "true", "self_destruct": True}', "async")
    resp = await flow.client.post(f"/ingress/{rule}")
    assert resp.status_code == 202
    await flow.runtime.tasks.drain(timeout=5.0)
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert len(chain) == 2
```

(The unauthenticated trigger must not carry the fixture's default `Authorization` header? It may; the endpoint ignores it. Leave the client as is.)

`tests/flow/test_recipes.py`:

```python
async def test_recipe_build_state_machine_and_create_from_recipe(flow: Flow, monkeypatch) -> None:
    async def build_image(cmd: str) -> str:
        return "ok"

    async def run(cmd: str, check: bool = True) -> str:
        return ""

    monkeypatch.setattr(flow.runtime.recipes, "_build_image", build_image)
    monkeypatch.setattr(flow.runtime.recipes, "_run", run)
    (flow.runtime.config.ssh_key_path.parent).mkdir(parents=True, exist_ok=True)
    resp = await flow.client.post("/recipes", json={"dockerfile": "FROM mshkn-base\nRUN true"})
    assert resp.status_code == 202 and resp.json()["status"] == "pending"
    rid = resp.json()["recipe_id"]
    again = await flow.client.post("/recipes", json={"dockerfile": "FROM mshkn-base\nRUN true"})
    assert again.status_code == 200 and again.json()["recipe_id"] == rid
    await flow.runtime.tasks.wait(f"recipe_build:{rid}")
    assert (await flow.client.get(f"/recipes/{rid}")).json()["status"] == "ready"
    created = await flow.client.post("/computers", json={"recipe_id": rid})
    assert created.status_code == 200 and created.json()["recipe_id"] == rid
    assert (await flow.client.delete(f"/recipes/{rid}")).status_code == 409
    await flow.client.delete(f"/computers/{created.json()['computer_id']}")
    assert (await flow.client.delete(f"/recipes/{rid}")).status_code == 200
```

(`Config.ssh_key_path` defaults to `/root/.ssh/id_ed25519`, which the build reads only through `.with_suffix(".pub").exists()`; the flow fixture should set `ssh_key_path=tmp_path / "id_ed25519"` and write a `.pub` next to it so `_post_process_rootfs` has a key. Add both to `_build_flow`.)

`tests/flow/test_system.py`:

```python
async def test_health_subsystems_and_alerts(flow: Flow, monkeypatch) -> None:
    monkeypatch.setattr(system_module, "_firecracker_present", lambda config: "ok")
    body = (await flow.client.get("/health")).json()
    assert body["status"] == "ok" and set(body["subsystems"]) == {"database", "firecracker", "storage", "proxy"}
    flow.host.blocks.pool_usage = PoolUsage(data_used_ratio=0.9, metadata_used_ratio=0.1)
    await flow.runtime.reaper.check_host()
    alerts = (await flow.client.get("/alerts")).json()
    assert any(a["source"] == "thin_pool_data" and a["level"] == "warning" for a in alerts)
```

- [ ] **Step 3: Run and fix**

Run: `uv run pytest tests/flow -q -m flow`
Expected: all pass. A failure here is a defect in Tasks 4–12 or in the fake's recording; fix the service or the fake and say which in the report. Never weaken an assertion.

- [ ] **Step 4: Verify the whole tree**

`uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1 && uv run pytest --cov -q -p no:cacheprovider 2>&1 | grep TOTAL`
Expected: clean; report the count and coverage (expect above 70%).

- [ ] **Step 5: Commit**

```bash
git add -A tests && git commit -m "test(flow): exclusive fork and drain, self-destruct callback, reaper, failure cleanup, ingress, recipes, health"
```

---

### Task 14: Final verification, PR, CI, live E2E

- [ ] **Step 1:** Full local validation (verification-before-completion): `uv sync --frozen && uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov -p no:cacheprovider 2>&1 | grep -E "passed|TOTAL"`; clean tree; `grep -rn "vm_manager\|VMManager\|manifest_hash" src tests/unit tests/flow` empty.
- [ ] **Step 2:** Push `pr4-services`, open the PR with this body skeleton (fill the `<...>`):

```
Part 4 of 6 of the quality overhaul (spec §5, §6, §7, §9, §10, §11; plan docs/superpowers/plans/2026-09-06-pr4-services.md).

**What this does**
Replaces VMManager and the fat routers with a service layer: SlotAllocator, RecipeService, ComputerService, CheckpointService, Lifecycle (one implementation of exec → self-destruct → callback → deferred drain behind REST create/fork, ingress, and the reaper), IngressService, and Reaper. Routers translate HTTP to service calls and back through api/schemas.py; every endpoint has a response model; domain errors map through one handler. Adds `python -m mshkn accounts create|list` and `migrate`. Deletes vm/, checkpoint/, recipe/, ingress/, callback.py.

**Deliberate behavior fixes**
- create/fork failures after the disk snap release the volume and slot and answer 502 (was a leak and a 500).
- Deferred queue claimed atomically; a destroy and an idle reap cannot both drain a label.
- Checkpoint delete and prune cancel the in-flight upload first (no more `rclone … directory not found` in the journal).
- Download of a missing file is 404.
- Ingress create actions take recipe_id and needs; capabilities/uses are rejected.
- status and checkpoint listings: manifest_hash → recipe_id.
- /health reports subsystems (200 with "degraded"); /alerts includes thin-pool data/metadata.
- Metrics: checkpoints_total{trigger}, computers_created_total{source}, computers_active from the DB count.
- Two concurrent first creates build a template once.
- Firecracker/SSH failures are HostErrors (502); CaddyProxy.healthy() is False after close.

**Design alignment**
- §5 Runtime: <fields as built>; shutdown order cancel reaper → drain → http → guest → proxy → db.
- §6.1–6.7: one class each; deviations recorded in the plan header (Lifecycle class, BadRequest/PayloadTooLarge/TransformError, acquire_volume_id, RecipeService.create tuple, type placement, callback client injection, uniform sync bound, last_exec_at on exec).
- §7: CheckpointTrigger; manifest columns written as constants.
- §9: error mapping; response models; accounts CLI replaces the sqlite3 step.
- §10: labelled counters; DB-derived gauge; pool alerts and gauges.
- §11: seven flow-test files for the PR 4 behaviours; the full matrix is PR 5.

**Validation performed**
- Baseline before: <paste docs/superpowers/plans/2026-09-06-pr4-baseline.txt>
- After: ruff/format/mypy clean; `uv run pytest` <N> passed; coverage TOTAL <n>%.
- CI: <link>
- Live E2E (`scripts/e2e.sh` against 65.21.22.161 at <sha>): <151 passed, 6 skipped, 0 failed>; PR 3 baseline was 151/6/0. Journal: `journalctl -u mshkn --since '<start>' | grep -c 'directory not found'` = <0>.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01CPKyFZiT4pPi4v5gkph5KZ
```

- [ ] **Step 3:** `gh pr checks --watch` to green.
- [ ] **Step 4:** Live E2E, detached: `setsid nohup env MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000 scripts/e2e.sh -p no:cacheprovider > <scratchpad>/e2e-pr4.log 2>&1 < /dev/null & disown`; monitor the log for `FAILED|passed|failed`. Expected 151/6/0. Then on the server: `journalctl -u mshkn --since '30 min ago' --no-pager | grep -c "directory not found"` must be 0, and `grep -c "Cannot find device tap254"` must be 0. Confirm `python -m mshkn accounts list` on the server shows `acct-mike`.
- [ ] **Step 5:** Triage bot reviews; report with the CI link and E2E summary; do not merge.

---

## Self-review

**Spec coverage:** §5 Runtime fields and shutdown order → Task 11. §6.1 → Task 4. §6.2 (create/fork/destroy/exec…/active counts, leak cleanup) → Task 6. §6.3 (single create with trigger, delete, prune, merge off-loop, fork_or_defer) → Task 7. §6.4 (`run_ephemeral`, `drain_deferred` with one-statement claim) → Task 8 (claim itself in Task 2). §6.5 (Pydantic-validated actions, recipe_id/needs, per-rule limiters, both modes through one path) → Task 10. §6.6 (dedupe by hash, per-account build lock, template dedupe) → Task 5. §6.7 (dead, idle with trigger=idle, prune, host checks with pool ratios and gauges) → Task 9. §7 (`CheckpointTrigger`, manifest columns as constants, `DeferredRequest` dataclass already) → Task 2. §9 (error mapping incl. 404/409/422 on create, status/listing field changes, health shape, accounts CLI) → Tasks 2, 11, 12. §10 (labelled counters, gauge from DB, pool gauges, alerts) → Tasks 2, 6, 9, 12. §11 flow items owned by PR 4 → Task 13. §14 step 4 as a whole. Carry-overs from the PR 3 ledger: missing-file 404 (Task 6), prune cancels uploads (Task 7), template dedupe (Task 5), leak on failure (Task 6), Firecracker/SSH → HostError and `healthy()` after close (Task 3), ingress reaching the guest through the manager (gone with Task 11), socket-path doc line (PR 6).

**Placeholder scan:** the only `...` in the plan are the two annotated verbatim-move markers in Task 5 (`_post_process_rootfs` body) and Task 13 (`flow_factory` body), each stating exactly which existing code fills them. No `TBD`/`TODO`. PR-body `<...>` fields are filled at submission.

**Type consistency:** `SlotAllocator.acquire() -> tuple[int, int]` and `acquire_volume_id()` (Task 4) are what `ComputerService._bring_up` (Task 6), `CheckpointService.create/merge` (Task 7), and `RecipeService.create` (Task 5) call. `RecipeService.resolve` and `ensure_template` (Task 5) are what `ComputerService.create`/`_template_for` (Task 6) call. `ComputerService.exec/fork/destroy/get_owned/get_running/cleanup_dead/refresh_active_gauge` (Task 6) are what `CheckpointService` (Task 7), `Lifecycle` (Task 8), `Reaper` (Task 9), `IngressService` (Task 10), and the routers (Task 11) call. `CheckpointService.create(computer, *, label, pin, trigger)`, `delete`, `prune`, `merge`, `fork_or_defer`, `get_owned`, `latest_for_label`, `upload_task_key` (Task 7) match every caller in Tasks 8–13. `Lifecycle.run_ephemeral(account, computer, spec, *, source_checkpoint)`, `spawn_drain`, `drain_after_destroy`, `drain_deferred` (Task 8) match Tasks 9–11. `IngressService.trigger -> TriggerOutcome`, `execute`, `create_rule(...)` keyword signature, `test_rule`, `logs`, `rotate_rule` (Task 10) match Task 11's router and Task 13's flow tests. `Runtime.build(config, db, host, *, http)` (Task 11) is what `make_runtime` (Task 11), the flow fixture (Task 11/13), and `from_env` use. `ExecSpec`/`EphemeralResult`/`CheckpointTrigger`/`Alert`/`ExclusiveMode` (Task 2) are used with the same field names throughout. `claim_deferred_by_label` (Task 2) is the only queue reader (Task 8). `computers_active` is only ever `.set()` (Tasks 6, 9, 11). `checkpoints_total.labels(trigger=…)` (Tasks 2, 7) and `computers_created_total.labels(source=…)` (Tasks 2, 6).
