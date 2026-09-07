# PR 6: Docs and Devtools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every document in the repo describes the system as it is after PRs 1–5, a test keeps them that way, and the retired Telegram artifacts leave the tree.

**Architecture:** Docs are written from the code, not from memory: a unit test (`tests/unit/test_docs.py`) parses every backticked path, module, route, metric, and environment variable in the docs and asserts it exists, and asserts the architecture doc's route and metric tables are complete. Each doc task adds its file to that test's list and must pass it. The plans index states each historical plan's status from evidence gathered on 2026-09-07 (recorded in this plan). Product code does not change; the spec's §3 layout is amended to match the code.

**Tech Stack:** Markdown; Python 3.12; pytest; `prometheus_client` registry and the FastAPI route table as the sources of truth.

**Spec:** `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` §3 (layout), §13 (docs and repo hygiene), §14 (delivery plan, item 6), §15 (out of scope).

## Global Constraints

- Python `>=3.12`; uv only; every command runs as `uv run <tool>` inside the worktree.
- Local validation, identical to CI: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`. Green at the end of every task, with zero warnings and the coverage floor (`fail_under = 98`) intact.
- **Nothing under `src/` changes.** This PR is docs, one test file, one script file, and the removal of untracked artifacts. If a doc task discovers a product defect, it files an issue and describes the current behaviour honestly.
- No xfail, no new skips, no `# type: ignore` in tests (the two `[misc]` in `tests/unit/test_models.py` stay), no assertion-free tests.
- Plain, honest prose: no marketing, no claims the code does not back. Every path, module, route, metric, and env var a doc mentions is real (`tests/unit/test_docs.py` enforces it). Historical plan documents under `docs/plans/` are not edited; their status lives in the index.
- Live E2E gate, detached: `MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000 scripts/e2e.sh` must report **144 passed, 6 skipped, 7 failed**, the seven being exactly the `Not implemented` tests listed in issue #65 (PR 5 baseline at 06715b9). Any other failure is a regression.
- Commit messages end with the trailer block (Co-Authored-By and Claude-Session lines). Never merge; open the PR and request authorization.

---

## Evidence gathered for this plan (2026-09-07, main 4c2978c)

The doc tasks below state facts; these are where they come from. Implementers verify against the code, not against this list, and correct the plan if the code disagrees.

- Routes (from `create_app().routes`, excluding `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json`): `POST /computers`, `DELETE /computers/{computer_id}`, `POST /computers/{computer_id}/checkpoint`, `GET /computers/{computer_id}/download`, `POST /computers/{computer_id}/exec`, `POST /computers/{computer_id}/exec/bg`, `POST /computers/{computer_id}/exec/kill/{pid}`, `GET /computers/{computer_id}/exec/logs/{pid}`, `GET /computers/{computer_id}/status`, `POST /computers/{computer_id}/upload`, `GET /checkpoints`, `DELETE /checkpoints/{checkpoint_id}`, `POST /checkpoints/{checkpoint_id}/fork`, `POST /checkpoints/{parent_id}/merge`, `POST /recipes`, `GET /recipes`, `GET /recipes/{recipe_id}`, `DELETE /recipes/{recipe_id}`, `POST /ingress_rules`, `GET /ingress_rules`, `GET /ingress_rules/{rule_id}`, `PUT /ingress_rules/{rule_id}`, `DELETE /ingress_rules/{rule_id}`, `POST /ingress_rules/{rule_id}/rotate`, `POST /ingress_rules/{rule_id}/test`, `GET /ingress_rules/{rule_id}/logs`, `GET|POST|PUT|PATCH /ingress/{rule_id}`, `GET /health`, `GET /metrics`, `GET /alerts`.
- Metrics (`src/mshkn/observability/metrics.py`): `mshkn_computers_active` (gauge), `mshkn_computers_created_total{source}` (counter), `mshkn_checkpoints_total{trigger}` (counter), `mshkn_operation_duration_seconds{op}` (histogram), `mshkn_operation_errors_total{op,kind}` (counter), `mshkn_thin_pool_used_ratio{kind}` (gauge), `mshkn_host_ram_used_ratio` (gauge).
- Config (`src/mshkn/config.py`): every field reads `MSHKN_<FIELD_UPPER>`; aliases `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `MSHKN_IDLE_TIMEOUT` (→ `idle_timeout_seconds`), `MSHKN_CHECKPOINT_RETENTION` (→ `checkpoint_retention_count`) win over the generic name. Fields and defaults: `host` 0.0.0.0, `port` 8000, `db_path` /opt/mshkn/mshkn.db, `migrations_dir` migrations, `base_rootfs_path` /opt/firecracker/rootfs.ext4, `kernel_path` /opt/firecracker/vmlinux.bin, `checkpoint_local_dir` /opt/mshkn/checkpoints, `ssh_key_path` /root/.ssh/id_ed25519, `thin_pool_data_path` /opt/mshkn/thin-pool-data, `thin_pool_meta_path` /opt/mshkn/thin-pool-meta, `thin_pool_data_size_gb` 100, `thin_pool_name` mshkn-pool, `thin_volume_sectors` 16777216, `r2_bucket` mshkn-checkpoints, `r2_endpoint` "", `r2_access_key_id` "", `r2_secret_access_key` "", `idle_timeout_seconds` 1800, `checkpoint_retention_count` 20, `domain` mshkn.dev, `caddy_admin_url` http://localhost:2019.
- Runtime (`src/mshkn/runtime.py`): `Runtime.build(config, db, host, *, http)`; fields `config, db, host, tasks, allocator, rate_limiter (80 requests / 10 s per API key on exec), recipes, computers, checkpoints, lifecycle, ingress, reaper, alerts (deque), http`; `from_env()` connects, migrates, builds with `firecracker_host(config)`; `start()` = `allocator.initialize` → `reaper.reap_dead` → `computers.refresh_active_gauge` → spawn `reaper.run()` under key `"reaper"`; `close()` = cancel reaper → `tasks.drain` → `http.aclose` → `guest.close` → `proxy.close` → `db.close`.
- DB (`src/mshkn/db/__init__.py`): `PRAGMA busy_timeout=5000`, `journal_mode=WAL`, `synchronous=NORMAL`; migrations `001_initial` … `010_indexes`, sequential and additive.
- Errors (`src/mshkn/errors.py`, `src/mshkn/api/errors.py`): `NotFound` 404, `Conflict` 409, `BadRequest` 400, `InvalidInput` 422, `PayloadTooLarge` 413, `LimitExceeded` 429, `TransformError` 502 (with detail), `HostError` 502 with the generic body `{"detail": "host operation failed"}`, anything else 500 `{"detail": "internal error"}`.
- Networking (`src/mshkn/host/network.py`): slot N ⇒ host `172.16.N.1`, VM `172.16.N.2`, tap `tapN`, MAC `06:00:AC:10:NN:02` (NN hex); staging slot 254; API socket `/tmp/fc-<disk name>.socket` where the disk name is `mshkn-<computer id>` (`mshkn-restore-staging` while staged, `template-<volume>` for template builds).
- Health (`src/mshkn/api/system.py`): subsystems `database`, `firecracker`, `storage`, `proxy`; status `ok` or `degraded` (HTTP 200 either way).
- Plan status table: the "Status of every plan" section in Task 3 (gathered by reading the code on 2026-09-07).
- Repo state: `skills/`, `skills-lock.json`, `e2e_test.sh`, root `deploy.sh`, `tests/integration/`, `src/mshkn/checkpoint/delta.py`, `poetry.lock` are already gone (PR 1). `.gitignore` already has `.coverage` and `htmlcov/` and no telegram entries. Untracked: `telegram/` (runtime logs of the retired bridge: `.env`, `incoming.jsonl`, `outgoing.jsonl`, `offset.txt`), an empty file named `json`, and a stale ignored `src/mshkn/capability/__pycache__/`.
- Off-layout modules the spec's §3 does not list: `src/mshkn/main.py` (ASGI entry `uvicorn mshkn.main:app`, used by `systemd/mshkn.service`), `src/mshkn/host/firecracker_host.py` (`firecracker_host(config) -> Host`, the one production wiring point), `src/mshkn/ratelimit.py` (`RateLimiter`), `src/mshkn/services/callback.py` (`deliver_callback`), `src/mshkn/services/starlark.py` (validation and transform).
- `scripts/build-rootfs.sh` still carries Nix-era steps (pre-creates `/nix`, a "PATH for Nix" profile block, apt removal "to enforce purity"). It builds the bare base volume and is not exercised by E2E, so this PR does not edit it; Task 6 files an issue.

## File structure

| File | Responsibility | Task |
|---|---|---|
| `tests/unit/test_docs.py` | The docs name real things: paths exist, modules import, routes and metrics match the app and registry both ways, env vars are Config fields or aliases, banned stale terms are absent. | 1 |
| `README.md` | What mshkn is, what exists, what does not, layout, how to run each test tier, links. | 4 |
| `docs/ARCHITECTURE.md` | Request path, runtime wiring, host boundary, services, state ownership, lifecycles, cleanup guarantees, networking, observability, config, running against the fake host. | 2 |
| `docs/plans/README.md` | Index of every plan with status and evidence. | 3 |
| `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` §3 | Layout amended with the five modules the code has and the spec does not. | 3 |
| `CLAUDE.md` | Working rules for agents: the gate, the E2E expectation, the workflow, the server. | 5 |
| `DEPLOY.md` | Fresh-server setup; verification step states the expected E2E result; teardown sweeps sockets. | 5 |
| `../mshkn-devtools/telegram/` (outside git) | Where the retired bridge's runtime artifacts go. | 1 |

---

### Task 1: Doc-consistency test and repo hygiene

**Files:**
- Create: `tests/unit/test_docs.py`
- Remove from the working tree (untracked): `telegram/` (after copying to `../mshkn-devtools/telegram/`), `json`, `src/mshkn/capability/__pycache__/`

**Interfaces:**
- Produces: `tests/unit/test_docs.py` with a module constant `DOCS: tuple[str, ...]` that Tasks 2 and 3 extend, and `BANNED: dict[str, tuple[str, ...]]` that Tasks 4 and 5 rely on.

- [ ] **Step 1: Move the Telegram artifacts out and delete the stray files.**

```bash
mkdir -p ../mshkn-devtools
cp -a telegram ../mshkn-devtools/telegram
diff -r telegram ../mshkn-devtools/telegram && echo "copy verified"
rm -rf telegram json src/mshkn/capability
git status --short     # must print nothing: all three were untracked or ignored
ls ../mshkn-devtools/telegram   # .env incoming.jsonl offset.txt outgoing.jsonl
```

`../mshkn-devtools/` already exists if a previous session created it (PR 1 copied `skills/` there); `cp -a` into it is fine either way. Do not commit anything for this step; record the four filenames in the report.

- [ ] **Step 2: Write the test.** `tests/unit/test_docs.py`:

```python
"""The docs name real things.

Every backticked path, dotted module name, `METHOD /route`, `mshkn_*` metric and
`MSHKN_*`/`R2_*` variable in the documents listed in DOCS must exist in the
code, and the architecture doc's route and metric tables must be complete.
A doc that drifts from the code fails here instead of misleading a reader.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import fields
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from mshkn.app import create_app
from mshkn.config import _ALIASES, Config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# Extended by later tasks as each document lands.
DOCS: tuple[str, ...] = ("README.md", "CLAUDE.md", "DEPLOY.md", "docs/infrastructure.md")

# Words a document must not contain because the thing they name is gone.
BANNED: dict[str, tuple[str, ...]] = {
    "README.md": ("Nix", "VMManager", "poetry", "xfail", "Telegram"),
    "CLAUDE.md": ("Telegram", "capability_cache", "Nix", "Priority 1 (Bug Fixes)"),
    "DEPLOY.md": ("Nix", "poetry", "nix-env"),
}

# Variables that scripts read, not Config; they are allowed in the docs.
SCRIPT_VARS = frozenset({"MSHKN_SERVER", "MSHKN_API_URL", "MSHKN_API_KEY"})

# Routes FastAPI adds on its own; the architecture doc does not list them.
FRAMEWORK_ROUTES = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

PATH_RE = re.compile(r"`((?:src|tests|scripts|docs|migrations|systemd)/[A-Za-z0-9_./{}-]+)`")
MODULE_RE = re.compile(r"`(mshkn(?!\.dev`)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")  # not the domain
ROUTE_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/[A-Za-z0-9_{}/]*)`")
METRIC_RE = re.compile(r"`(mshkn_[a-z_]+)(?:\{[^}]*\})?`")
ENV_RE = re.compile(r"`((?:MSHKN|R2)_[A-Z0-9_]+)(?:=[^`]*)?`")


def _text(doc: str) -> str:
    return (ROOT / doc).read_text()


def _app_routes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in create_app().routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if not methods or path in FRAMEWORK_ROUTES:
            continue
        found.update((m, path) for m in methods if m not in ("HEAD", "OPTIONS"))
    return found


def _registry_names() -> tuple[set[str], set[str]]:
    """(every name a doc may use, the family names a complete doc must mention)."""
    allowed: set[str] = set()
    families: set[str] = set()
    for family in REGISTRY.collect():
        if not family.name.startswith("mshkn_"):
            continue
        families.add(family.name)
        allowed.update(
            {family.name, f"{family.name}_total", f"{family.name}_count", f"{family.name}_sum"}
        )
    return allowed, families


def _resolves(name: str) -> bool:
    """`mshkn.runtime.Runtime.build` resolves as module `mshkn.runtime`, then attributes."""
    parts = name.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj: object = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for attr in parts[cut:]:
            if not hasattr(obj, attr):
                return False
            obj = getattr(obj, attr)
        return True
    return False


def _env_names() -> set[str]:
    return {f"MSHKN_{f.name.upper()}" for f in fields(Config)} | set(_ALIASES) | SCRIPT_VARS


@pytest.mark.parametrize("doc", DOCS)
def test_every_path_exists(doc: str) -> None:
    missing = sorted(
        {p for p in PATH_RE.findall(_text(doc)) if not (ROOT / p.rstrip("/")).exists()}
    )
    assert missing == [], f"{doc} names paths that do not exist: {missing}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_module_imports(doc: str) -> None:
    broken = sorted(n for n in set(MODULE_RE.findall(_text(doc))) if not _resolves(n))
    assert broken == [], f"{doc} names modules or attributes that do not exist: {broken}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_route_exists(doc: str) -> None:
    unknown = sorted(set(ROUTE_RE.findall(_text(doc))) - _app_routes())
    assert unknown == [], f"{doc} names routes the app does not serve: {unknown}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_metric_exists(doc: str) -> None:
    allowed, _ = _registry_names()
    unknown = sorted(set(METRIC_RE.findall(_text(doc))) - allowed)
    assert unknown == [], f"{doc} names metrics the registry does not have: {unknown}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_env_var_is_config(doc: str) -> None:
    unknown = sorted(set(ENV_RE.findall(_text(doc))) - _env_names())
    assert unknown == [], f"{doc} names variables Config does not read: {unknown}"


@pytest.mark.parametrize("doc", sorted(BANNED))
def test_retired_terms_are_absent(doc: str) -> None:
    present = [term for term in BANNED[doc] if term in _text(doc)]
    assert present == [], f"{doc} still mentions retired things: {present}"


@pytest.mark.skipif("docs/ARCHITECTURE.md" not in DOCS, reason="landed by a later task")
def test_architecture_lists_every_route_and_metric() -> None:
    text = _text("docs/ARCHITECTURE.md")
    documented = set(ROUTE_RE.findall(text))
    undocumented = sorted(_app_routes() - documented)
    assert undocumented == [], f"routes missing from ARCHITECTURE.md: {undocumented}"
    _, families = _registry_names()
    named = set(METRIC_RE.findall(text))
    missing = sorted(f for f in families if f not in named and f"{f}_total" not in named)
    assert missing == [], f"metrics missing from ARCHITECTURE.md: {missing}"
```

The one `skipif` is a build-order device inside this PR: Task 2 turns it into a live test by adding the file to `DOCS`, and Task 6 verifies the skip is gone (`uv run pytest tests/unit/test_docs.py -q` reports no skips). It never ships as a skip.

- [ ] **Step 3: Run it against the current docs; expect the stale README to fail.**

Run: `uv run pytest tests/unit/test_docs.py -q -p no:cacheprovider`
Expected: exactly `3 failed, 20 passed, 1 skipped` (verified while writing this plan): `test_retired_terms_are_absent[README.md]` (`Nix`, `VMManager`), `test_retired_terms_are_absent[CLAUDE.md]` (`Priority 1 (Bug Fixes)`), and the path/module/route/metric/env checks pass for all four documents (if one fails, the doc has a genuine stale reference: fix it in the task that owns that doc and note it in the report; do not loosen the regex). The architecture test is skipped.

- [ ] **Step 4: Commit the test alone.** The failing cases are honest: they are what Tasks 4 and 5 fix. CI is not run on the intermediate commit, and the final gate (Task 6) is green.

```bash
git add tests/unit/test_docs.py
git commit -m "test: the docs must name real paths, modules, routes, metrics and env vars"
```

Report: the four moved filenames, the exact failing test ids from Step 3, and any unexpected failure.

---

### Task 2: docs/ARCHITECTURE.md

**Files:**
- Create: `docs/ARCHITECTURE.md`
- Modify: `tests/unit/test_docs.py` (`DOCS` gains `"docs/ARCHITECTURE.md"`)

**Interfaces:**
- Consumes: the route, metric, config, runtime and lifecycle facts in "Evidence gathered for this plan"; verify each against the code while writing.

- [ ] **Step 1: Add the doc to the test first and watch it fail.**

In `tests/unit/test_docs.py` change `DOCS` to:

```python
DOCS: tuple[str, ...] = (
    "README.md",
    "CLAUDE.md",
    "DEPLOY.md",
    "docs/infrastructure.md",
    "docs/ARCHITECTURE.md",
)
```

Run: `uv run pytest tests/unit/test_docs.py -q -p no:cacheprovider -k ARCHITECTURE`
Expected: FAIL with `FileNotFoundError` for every parametrized case (the file does not exist), and `test_architecture_lists_every_route_and_metric` now runs and fails the same way.

- [ ] **Step 2: Write `docs/ARCHITECTURE.md`.** Every backticked name below is checked by the test; every claim is checked by you against the code. Correct the text where the code disagrees and say so in the report.

````markdown
# Architecture

mshkn is one Python process on one Linux host. It owns a SQLite database, a dm-thin pool, a set of Firecracker microVMs, an SSH connection to each VM, an rclone remote for R2, and a Caddy instance for public HTTPS. This document describes how a request moves through it, who owns which state, what happens over a computer's and a checkpoint's life, and what is guaranteed on failure.

Dependency direction is strict: `mshkn.api` → `mshkn.services` → `mshkn.host` and `mshkn.db`. `mshkn.models`, `mshkn.errors`, `mshkn.config`, `mshkn.resources` and `mshkn.observability` are leaves. Nothing in `services` or `host` imports `api`.

## 1. Request path

`systemd/mshkn.service` runs `uvicorn mshkn.main:app`. `src/mshkn/main.py` calls `mshkn.app.create_app`, whose lifespan builds a `mshkn.runtime.Runtime` from the environment (`Runtime.from_env`: connect, migrate, wire the production host), starts it, and closes it on shutdown.

A request passes through:

1. The request-id middleware in `src/mshkn/app.py`: it takes `X-Request-Id` or mints one, stores it in the logging contextvar so every log line carries it, and echoes it in the response.
2. `mshkn.api.deps.require_account`: `Authorization: Bearer <api key>` is looked up in the `accounts` table; a missing or unknown key is 401. The ingress trigger endpoint is the one unauthenticated route.
3. A router in `src/mshkn/api/`. Handlers resolve the runtime (`mshkn.api.deps.get_runtime`), call one service method, and shape the result with a model from `src/mshkn/api/schemas.py`. Orchestration does not live in routers.
4. A service in `src/mshkn/services/`, which talks to the host boundary and the database.

Domain errors are mapped in one place, `src/mshkn/api/errors.py`:

| Exception (`mshkn.errors`) | Status | Body |
|---|---|---|
| `NotFound` | 404 | `{"detail": <message>}` |
| `Conflict` | 409 | message |
| `BadRequest` | 400 | message (for example exec on a computer that is not running) |
| `InvalidInput` | 422 | message, plus a `detail` payload for validation lists |
| `PayloadTooLarge` | 413 | message |
| `LimitExceeded` | 429 | message (VM limit, slots, ingress rate limit) |
| `TransformError` | 502 | the Starlark error |
| `HostError` | 502 | always `{"detail": "host operation failed"}`; the cause is logged, never returned |
| anything else | 500 | `{"detail": "internal error"}` |

`HostError` is the boundary contract: every host implementation raises it (or a subclass such as `mshkn.host.shell.ShellError`) for a failed shell command, SSH session, Firecracker API call, rclone transfer or Caddy call, so services never see `asyncssh`, `httpx` or `subprocess` exceptions.

### Endpoints

| Route | Service call |
|---|---|
| `POST /computers` | `ComputerService.create` then `Lifecycle.run_ephemeral` |
| `DELETE /computers/{computer_id}` | `ComputerService.destroy` then `Lifecycle.drain_after_destroy` |
| `GET /computers/{computer_id}/status` | `ComputerService.get_owned`, `ComputerService.metrics` when running |
| `POST /computers/{computer_id}/exec` | `ComputerService.stream` as server-sent events, timed under `op="exec"` |
| `POST /computers/{computer_id}/exec/bg` | `ComputerService.exec_bg` |
| `GET /computers/{computer_id}/exec/logs/{pid}` | `ComputerService.exec_logs` |
| `POST /computers/{computer_id}/exec/kill/{pid}` | `ComputerService.exec_kill` |
| `POST /computers/{computer_id}/upload` | `ComputerService.upload` (`?path=`, raw body) |
| `GET /computers/{computer_id}/download` | `ComputerService.download` (`?path=`) |
| `POST /computers/{computer_id}/checkpoint` | `CheckpointService.create` with trigger `api` |
| `GET /checkpoints` | `CheckpointService.list` (`?label=`) |
| `DELETE /checkpoints/{checkpoint_id}` | `CheckpointService.delete` |
| `POST /checkpoints/{checkpoint_id}/fork` | `CheckpointService.fork_or_defer` then `Lifecycle.run_ephemeral` |
| `POST /checkpoints/{parent_id}/merge` | `CheckpointService.merge` |
| `POST /recipes`, `GET /recipes`, `GET /recipes/{recipe_id}`, `DELETE /recipes/{recipe_id}` | `RecipeService.create` (builds in the background), `list`, `get`, `delete` |
| `POST /ingress_rules`, `GET /ingress_rules`, `GET /ingress_rules/{rule_id}`, `PUT /ingress_rules/{rule_id}`, `DELETE /ingress_rules/{rule_id}` | `IngressService` rule CRUD |
| `POST /ingress_rules/{rule_id}/rotate` | new secret id for the rule |
| `POST /ingress_rules/{rule_id}/test` | dry-run the transform against a synthetic request |
| `GET /ingress_rules/{rule_id}/logs` | recent trigger outcomes |
| `GET /ingress/{rule_id}`, `POST /ingress/{rule_id}`, `PUT /ingress/{rule_id}`, `PATCH /ingress/{rule_id}` | the unauthenticated trigger: parse the body, `IngressService.trigger` |
| `GET /health` | subsystem checks |
| `GET /metrics` | Prometheus exposition |
| `GET /alerts` | the runtime's alert deque |

## 2. Runtime and wiring

`mshkn.runtime.Runtime` is the composition root. `Runtime.build(config, db, host, *, http)` constructs, in dependency order: `mshkn.runtime.BackgroundTasks`, `SlotAllocator`, the exec `RateLimiter` (80 requests per 10 seconds per API key), `RecipeService`, `ComputerService`, `CheckpointService`, `Lifecycle`, `IngressService`, `Reaper`, and the alert deque. The `http` client is what callbacks are delivered with; tests pass one whose transport is an in-process receiver.

`Runtime.start()`: `SlotAllocator.initialize` (derive free slots and the next volume id from the database and the pool), `Reaper.reap_dead` once, refresh the active-computers gauge from the database, spawn `Reaper.run` under the task key `reaper`.

`Runtime.close()`: cancel the reaper, drain background tasks (checkpoint uploads, callbacks, deferred drains, recipe builds) with a timeout, close the HTTP client, the SSH pool, the Caddy client, then the database.

`BackgroundTasks` names every task and lets a caller cancel or await one by key; checkpoint uploads use the key `upload:<checkpoint id>` so that deleting a checkpoint can cancel its upload first.

The production host is built once by `mshkn.host.firecracker_host.firecracker_host`: exactly one `FirecrackerHypervisor` exists per process because the staging slot is host-global and its lock is an instance attribute.

## 3. Host boundary

`src/mshkn/host/__init__.py` defines five protocols and the `Host` container. Services depend only on these.

| Protocol | Methods | Production | Fake |
|---|---|---|---|
| `Hypervisor` | `boot`, `restore`, `snapshot`, `build_template`, `kill`, `is_alive`, `teardown_slot` | `mshkn.host.firecracker.FirecrackerHypervisor` | `FakeHypervisor` |
| `BlockStore` | `snap`, `activate`, `deactivate`, `remove`, `mkfs`, `mounted`, `max_volume_id`, `usage` | `mshkn.host.dmthin.DmThinBlockStore` | `FakeBlockStore` (temp directories per device) |
| `Guest` | `exec`, `stream`, `exec_bg`, `upload`, `download`, `metrics`, `warm`, `evict`, `close` | `mshkn.host.ssh.SshGuest` (connection pool, real line streaming) | `FakeGuest` (scripted output, minted pids from 4000) |
| `ObjectStore` | `upload_dir`, `download_dir`, `delete_prefix` | `mshkn.host.r2.RcloneObjectStore` | `FakeObjectStore` |
| `Proxy` | `add_route`, `remove_route`, `healthy`, `close` | `mshkn.host.caddy.CaddyProxy` (admin API) | `FakeProxy` |

Shared result types live beside the protocols: `RunningVM(pid, socket_path, slot, vm_ip, tap_device)`, `SnapshotFiles`, `ExecResult(exit_code, stdout, stderr)`, `VmMetrics`, `PoolUsage`.

`mshkn.host.fake.FakeHost()` returns a `FakeHostInstance` with all five fakes; it records calls, can be told to fail a named operation, and cleans its temp directories on `close()`.

`src/mshkn/host/shell.py` (`run`, `ShellError`) and `src/mshkn/host/network.py` (slot arithmetic, `create_tap`, `destroy_tap`) are the only places shell commands are formed.

### Firecracker specifics

A VM is started as a `firecracker --api-sock /tmp/fc-<disk name>.socket` process and configured over that Unix socket (`FirecrackerClient`). `boot` configures kernel, root drive and tap and starts the machine. `restore` and `build_template` go through a staging pass: the disk is mapped as `mshkn-restore-staging` on slot 254 (`tap254`, `172.16.254.2`, MAC `06:00:AC:10:FE:02`) because a Firecracker snapshot bakes in the tap and MAC it was taken with; the VM is loaded there, then re-addressed over SSH to its final slot, the staging tap and mapping are released, and the final tap takes over. One staging pass runs at a time (`_staging_lock`). `kill` sends SIGKILL, waits for the process to exit, and unlinks the API socket it recorded for that pid.

## 4. Services

| Service | Owns |
|---|---|
| `mshkn.services.allocator.SlotAllocator` | Free slot set and next thin-volume id, both derived at start from the database and `BlockStore.max_volume_id`; `acquire`, `acquire_volume_id`, `release_slot` under one lock. |
| `mshkn.services.computers.ComputerService` | `create`, `fork`, `destroy`, `cleanup_dead`, guest operations (`exec`, `stream`, `exec_bg`, `exec_logs`, `exec_kill`, `upload`, `download`, `metrics`), ownership checks (`get_owned`, `get_running`), the active gauge. |
| `mshkn.services.checkpoints.CheckpointService` | `create` (the single implementation for API, self-destruct and idle triggers), `delete`, `prune`, `merge`, `fork_or_defer`, `latest_for_label`, `source_label`. |
| `mshkn.services.lifecycle.Lifecycle` | `run_ephemeral` (exec → optional self-destruct checkpoint → destroy → callback → drain), `drain_after_destroy`, `drain_deferred`. |
| `mshkn.services.recipes.RecipeService` | Recipe rows and the build pipeline (`docker build` → export → thin volume → template snapshot), de-duplicated per recipe and per template key. |
| `mshkn.services.ingress.IngressService` | Rule CRUD, `enabled_rule`, `trigger` (Starlark transform → `ForkAction`/`CreateAction` → outcome), per-rule rate limiters, trigger logs. |
| `mshkn.services.reaper.Reaper` | `reap_dead`, `reap_idle`, prune, `check_host` (pool and RAM thresholds), the 60-second cycle. |
| `mshkn.services.merge` | `three_way_merge`, a pure function over three directory trees. |
| `mshkn.services.callback.deliver_callback` | POST with retries and backoff; never raises. |
| `mshkn.services.starlark` | Validation and execution of transform source in the `starlark_go` sandbox. |

## 5. State ownership

**Durable (SQLite, `src/mshkn/db/`).** One file, opened with `PRAGMA busy_timeout=5000`, `journal_mode=WAL` and `synchronous=NORMAL`. Tables: `accounts`, `computers`, `checkpoints`, `recipes`, `snapshot_templates`, `deferred_queue`, `ingress_rules`, `ingress_log`, plus `_migrations` (applied migration names); `capability_cache` from the first migration was dropped by `migrations/009_recipes.sql`. Migrations in `migrations/` are sequential and additive; nothing is dropped or rebuilt. Litestream replicates the file to R2. Each `db/` module holds one table's column tuple, one row mapper and its queries; there is no ORM.

**Durable (disk).** The thin pool `mshkn-pool` with base volume 0 (the bare rootfs), one volume per computer (`mshkn-<computer id>`), one per checkpoint (`mshkn-<checkpoint id>`), one per recipe base and template. Checkpoint snapshot files under `checkpoint_local_dir/<checkpoint id>/` (`vmstate`, `mem`), mirrored to R2 under `<account id>/<checkpoint id>/`.

**Process-local (rebuilt at start).** The allocator's free-slot set and next volume id; the SSH connection pool; the hypervisor's staging lock and its pid-to-socket registry; ingress rate limiters (keyed by internal rule id); the exec rate limiter; the alert deque; background tasks. A restart loses in-flight uploads and callbacks (the reaper and the next checkpoint create recover the rest).

**Kernel and daemons.** Tap devices, dm-thin mappings, Firecracker processes, Caddy routes. `Runtime.start` and `scripts/e2e.sh` reconcile these against the database.

## 6. Lifecycle of a computer

**Create** (`ComputerService.create`, timed as `op="create"`):

1. Reject if the account is at its `vm_limit` (`LimitExceeded`).
2. Resolve the recipe if given; its base volume is the source, else volume 0.
3. `_bring_up`: acquire a slot and a volume id; `BlockStore.snap` the source into the new volume (on failure, release the slot and re-raise).
4. If a template snapshot exists for this recipe and resource shape, `Hypervisor.restore` it (timed `op="restore"`); otherwise `Hypervisor.boot` (timed `op="boot"`).
5. `Guest.warm` opens the SSH connection; the `computers` row is inserted with status `running`; `Proxy.add_route` publishes `*-<computer id>.<domain>` to the VM's address.
6. The active gauge is refreshed from the database.

**Fork** (`ComputerService.fork`, timed `op="fork"`) is the same `_bring_up` with the checkpoint's thin volume as the source and its snapshot files as the restore input; the checkpoint's recipe carries over unless the request names another.

**Exec.** Every guest operation touches `last_exec_at`. `stream` yields `(stream, line)` pairs as the SSH session produces them; the router turns them into `stdout`, `stderr` and a final `exit` event, and any failure becomes an `error` event followed by `exit 255`.

**Destroy** (`ComputerService.destroy`, timed `op="destroy"`): remove the Caddy route, kill the VM (which unlinks its API socket), remove the thin volume, tear down the tap, release the slot, evict the SSH connection, mark the row `destroyed`, refresh the gauge. Destroying an already destroyed computer is a no-op.

**Dead VM** (`Reaper.reap_dead` → `ComputerService.cleanup_dead`): when a running computer's Firecracker pid is gone, the same teardown runs with every step best-effort.

**Idle** (`Reaper.reap_idle`): a running computer whose `last_exec_at` (or `created_at`) is older than `idle_timeout_seconds` is checkpointed with trigger `idle` and label `auto-idle-timeout` (or its chain's label), then destroyed and its label drained.

## 7. Lifecycle of a checkpoint

**Create** (`CheckpointService.create`, timed `op="checkpoint"`): `sync` inside the guest so the page cache reaches the block device; `Hypervisor.snapshot` (pause, write `vmstate` and `mem`, resume); evict the SSH connection (pause breaks the pooled session); acquire a volume id and `BlockStore.snap` the computer's volume; insert the row with `parent_id` = the computer's latest checkpoint, else the checkpoint it was forked from; count `mshkn_checkpoints_total{trigger}`; spawn the R2 upload under key `upload:<checkpoint id>`.

**Delete** cancels the upload task first, then removes the local directory, the R2 prefix and the thin volume, then the row.

**Prune** (`Reaper` cycle) keeps the newest `checkpoint_retention_count` unpinned checkpoints per account and deletes the rest.

**Fork or defer** (`CheckpointService.fork_or_defer`): with `exclusive` set and a labelled checkpoint, an active computer on that label means either `Conflict` (`error_on_conflict`) or a row in `deferred_queue` and a `Deferred` result (`defer_on_conflict`). Otherwise it is a plain fork.

**Drain** (`Lifecycle.drain_deferred`): after a self-destruct or destroy on a labelled chain, one background task claims every queued request for the label atomically (`DELETE … RETURNING`), forks one computer from the newest checkpoint carrying the label, writes each queued `exec` to `/tmp/exec/N.txt`, runs the last `meta_exec` (or the concatenated writes), and runs `run_ephemeral` on it. A claim that finds nothing ends the drain; a drain that produces another self-destruct spawns the next.

**Merge** (`CheckpointService.merge`): the parent and both forks are activated read-only and mounted; `three_way_merge` runs in a worker thread into a fresh volume snapped from the parent; the result is a new checkpoint whose response lists conflicts.

## 8. Recipes and templates

A recipe is a Dockerfile whose first line is `FROM mshkn-base` (the image built in `DEPLOY.md` §7 from `Dockerfile.mshkn-base`). `RecipeService.build` runs `docker build` with a 4 GB memory reservation and a timeout (the build is killed on timeout), exports the container filesystem, post-processes it for Firecracker (init symlink, network unit, SSH key) in a worker thread, and writes it into a thin volume that becomes the recipe's base. The first computer from a recipe and resource shape triggers `Hypervisor.build_template`: a VM is booted on the staging slot, snapshotted, and the files are recorded in `snapshot_templates`, so later creates restore in the fork path's time instead of cold-booting. Builds and template builds are de-duplicated per key so concurrent requests share one.

## 9. Ingress

An ingress rule has a Starlark `transform(request)` returning either `{"action": "fork", "checkpoint_id": ..., "exec": ..., ...}` or `{"action": "create", "recipe_id": ..., "needs": ..., "exec": ..., ...}`, or nothing. `POST /ingress_rules` validates the source (a pre-flight run against a sample request). A trigger request is parsed into the dict the transform sees (method, path, headers, query, JSON or form body, raw body) with the rule's `max_body_bytes` enforced on both the declared and the streamed length (413), then rate-limited per rule (429), transformed (`TransformError` → 502), validated against `ForkAction`/`CreateAction` (`extra=forbid`), and executed. `response_mode` `sync` returns the same body as the REST fork or create; `async` returns 202 with a `queued`/`accepted` marker; a deferred fork returns the deferred id. Every trigger writes an `ingress_log` row.

## 10. Networking

Slot N gives host address `172.16.N.1`, VM address `172.16.N.2`, tap `tapN` and MAC `06:00:AC:10:NN:02` (NN in hex). Egress is host NAT (`scripts/mshkn-pool-up` sets the forwarding rules Docker otherwise drops). Inbound traffic reaches a VM only through Caddy: `CaddyProxy.add_route` creates a route with id `route-<computer id>` matching `*-<computer id>.<domain>`, and the request's port prefix selects the VM port. Slot 254 is reserved for staging.

## 11. Failure handling and cleanup guarantees

- **Bring-up is all or nothing.** Any failure after the disk snap in `_bring_up` runs `_abandon`: route removal, kill and evict if a VM was started, volume removal, tap teardown, status update, gauge refresh, slot release, each best-effort and logged, then the error is re-raised (`HostError` unless it already was a domain error or a cancellation). Cancellation mid-bring-up completes the abandon before propagating.
- **Every host failure is a `HostError`** with the cause logged; callers never see transport exceptions and clients never see internals.
- **Timeouts kill what they abandon.** A Firecracker process whose socket never appears is killed; a `docker build` past its deadline is killed; a stream whose process ignores exit is killed after 60 seconds.
- **Checkpoint upload and delete do not race.** Delete cancels the upload task by key before removing files.
- **Deferred requests are claimed once.** The drain's `DELETE … RETURNING` makes two drains on one label safe.
- **Dead VMs are reaped**, at startup and every reaper cycle, with every step best-effort so one broken resource does not block the rest.
- **Known gaps** are tracked as issues: #70 (a REST destroy and the dead-VM reaper can tear down the same computer concurrently), #66 (an abandoned bring-up can leave a Firecracker that had already spawned), #67 (the socket registry is process-local).

## 12. Observability

Logs are JSON lines (`mshkn.observability.logging.JSONFormatter`) with `timestamp`, `level`, `logger`, `msg`, `request_id`, and structured extras where the service supplies them (`op`, `computer_id`, `checkpoint_id`, `account_id`, `recipe_id`, `trigger`).

| Metric | Type | Labels | Set by |
|---|---|---|---|
| `mshkn_computers_active` | gauge | | `ComputerService.refresh_active_gauge`, from the database after every create, destroy and reap |
| `mshkn_computers_created_total` | counter | `source` = `create` or `fork` | `ComputerService` |
| `mshkn_checkpoints_total` | counter | `trigger` = `api`, `self_destruct`, `idle` | `CheckpointService.create` |
| `mshkn_operation_duration_seconds` | histogram | `op` = `create`, `fork`, `boot`, `restore`, `checkpoint`, `destroy`, `exec`, `merge`, `recipe_build` | `mshkn.observability.metrics.timed` |
| `mshkn_operation_errors_total` | counter | `op`, `kind` = `domain`, `host`, `unexpected` | `timed`, on exit by exception |
| `mshkn_thin_pool_used_ratio` | gauge | `kind` = `data`, `metadata` | `Reaper.check_host` |
| `mshkn_host_ram_used_ratio` | gauge | | `Reaper.check_host` |

`GET /health` reports `database`, `firecracker`, `storage` and `proxy` as `ok` or an error string, with overall `ok` or `degraded` (always HTTP 200; the deploy script's readiness check reads the body). `GET /alerts` returns the reaper's recent alerts: thin pool over 80 % (warning) or 95 % (critical) of data or metadata, and host RAM pressure.

## 13. Configuration

`mshkn.config.Config` is a frozen dataclass; `Config.from_env` reads `MSHKN_<FIELD>` for every field, parsing by the field's type, and honours the older aliases `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `MSHKN_IDLE_TIMEOUT` and `MSHKN_CHECKPOINT_RETENTION`, which win over the generic names. A malformed value raises `ConfigError` naming the variable.

| Field | Default | Variable |
|---|---|---|
| `host`, `port` | `0.0.0.0`, `8000` | `MSHKN_HOST`, `MSHKN_PORT` |
| `db_path` | `/opt/mshkn/mshkn.db` | `MSHKN_DB_PATH` |
| `migrations_dir` | `migrations` | `MSHKN_MIGRATIONS_DIR` |
| `base_rootfs_path`, `kernel_path` | `/opt/firecracker/rootfs.ext4`, `/opt/firecracker/vmlinux.bin` | `MSHKN_BASE_ROOTFS_PATH`, `MSHKN_KERNEL_PATH` |
| `checkpoint_local_dir` | `/opt/mshkn/checkpoints` | `MSHKN_CHECKPOINT_LOCAL_DIR` |
| `ssh_key_path` | `/root/.ssh/id_ed25519` | `MSHKN_SSH_KEY_PATH` |
| `thin_pool_name`, `thin_volume_sectors` | `mshkn-pool`, `16777216` (8 GiB) | `MSHKN_THIN_POOL_NAME`, `MSHKN_THIN_VOLUME_SECTORS` |
| `thin_pool_data_path`, `thin_pool_meta_path`, `thin_pool_data_size_gb` | `/opt/mshkn/thin-pool-data`, `/opt/mshkn/thin-pool-meta`, `100` | `MSHKN_THIN_POOL_DATA_PATH`, `MSHKN_THIN_POOL_META_PATH`, `MSHKN_THIN_POOL_DATA_SIZE_GB` |
| `r2_bucket`, `r2_endpoint`, `r2_access_key_id`, `r2_secret_access_key` | `mshkn-checkpoints`, empty | `R2_BUCKET`, `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` |
| `idle_timeout_seconds` | `1800` | `MSHKN_IDLE_TIMEOUT` |
| `checkpoint_retention_count` | `20` | `MSHKN_CHECKPOINT_RETENTION` |
| `domain` | `mshkn.dev` | `MSHKN_DOMAIN` |
| `caddy_admin_url` | `http://localhost:2019` | `MSHKN_CADDY_ADMIN_URL` |

Resources per computer come from the request, not the environment: `mshkn.resources.Resources.from_needs` parses `{"ram": "512MB", "cores": 2}` with bounds of 128 MiB to 32 GiB and 1 to 16 vCPUs, defaulting to 256 MiB and 1 vCPU.

## 14. Running against the fake host

The flow tier (`tests/flow/`) runs the real app and services with no host at all:

```python
from httpx import ASGITransport, AsyncClient

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.host.fake import FakeHost
from mshkn.runtime import Runtime

db = await connect(tmp_path / "flow.db")
await run_migrations(db, Path("migrations"))
host = FakeHost()
runtime = Runtime.build(Config(ssh_key_path=tmp_path / "key"), db, host, http=AsyncClient())
await runtime.allocator.initialize(db, host.blocks)
app = create_app(runtime)
client = AsyncClient(transport=ASGITransport(app=app), base_url="http://flow")
```

`tests/flow/conftest.py` does exactly this, adds two accounts, points the callback client at an in-process receiver, and closes everything after each test. The fakes expose their state (`host.hypervisor.alive`, `host.blocks.mounts`, `host.proxy.routes`, `host.guest.evicted`) so a test asserts on outcomes, not on mocks. Unit tests of a host module (`tests/unit/test_firecracker_stage.py`, `test_dmthin.py`, `test_ssh_guest.py`) exercise the real command chains against recorders and fake binaries instead.
````

- [ ] **Step 3: Run the docs test.**

Run: `uv run pytest tests/unit/test_docs.py -q -p no:cacheprovider`
Expected: every ARCHITECTURE.md case passes, including `test_architecture_lists_every_route_and_metric` (no longer skipped). A failure names the stale reference; fix the doc (or, if the doc is right and the plan's evidence was wrong, fix the doc to match the code and note it). Never widen `FRAMEWORK_ROUTES` or a regex to make a case pass. The README.md and CLAUDE.md banned-term cases still fail (Tasks 4 and 5).

Also check the resources bounds sentence against `src/mshkn/resources.py` (`MIN_MEM_MIB`, `MAX_MEM_MIB`, the vCPU bounds) and correct it if the code differs.

- [ ] **Step 4: Commit.**

```bash
git add docs/ARCHITECTURE.md tests/unit/test_docs.py
git commit -m "docs: ARCHITECTURE.md — request path, wiring, host boundary, lifecycles, guarantees, config"
```

---

### Task 3: Plans index and the spec's layout

**Files:**
- Create: `docs/plans/README.md`
- Modify: `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` (§3 layout block and one sentence under it)
- Modify: `tests/unit/test_docs.py` (`DOCS` gains `"docs/plans/README.md"`)

- [ ] **Step 1: Add the index to the test and watch it fail.**

```python
DOCS: tuple[str, ...] = (
    "README.md",
    "CLAUDE.md",
    "DEPLOY.md",
    "docs/infrastructure.md",
    "docs/ARCHITECTURE.md",
    "docs/plans/README.md",
)
```

Run: `uv run pytest tests/unit/test_docs.py -q -p no:cacheprovider -k "plans"`
Expected: FAIL with `FileNotFoundError` for each parametrized case.

- [ ] **Step 2: Write `docs/plans/README.md`.** The statuses were determined on 2026-09-07 by reading the code; re-verify any row you doubt (the evidence column says where to look).

````markdown
# Plans index

Every design and implementation document in this repository, with its status as of 2026-09-07 (main after PR #64). Status is judged from the code, not from what the document says about itself. The documents are historical records and are not edited; this index is what changes.

Statuses: **implemented**, **partially implemented**, **superseded by …**, **retired** (never built, not replaced), **reference** (not a plan).

## Product

| Document | What it proposed | Status | Evidence |
|---|---|---|---|
| `docs/plans/2026-03-07-disposable-cloud-computers-design.md` | The product: disposable Firecracker VMs identified by checkpoints, fork and merge, Nix capability layers, sleep-for-free economics. | partially implemented: the computer, checkpoint, fork and merge model is live; the Nix capability layer was replaced by Docker recipes | `src/mshkn/services/computers.py`, `src/mshkn/services/checkpoints.py`, `src/mshkn/services/recipes.py` |
| `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` | The definition of done: 157 end-to-end tests, T0 to T13. | reference; the suite is `tests/e2e/`. Seven tests (T8.6, T9.1, T10.1, T10.2, T10.5, T11.3 and the audit-log check) fail as `Not implemented` until built (#65). | `tests/e2e/` collects 157 |
| `docs/plans/2026-03-08-roadmap.md` | Prioritised backlog from the first E2E run. | partially implemented: see the breakdown below | |
| `docs/plans/2026-03-08-orchestrator-design.md` | One FastAPI process over Firecracker, dm-thin, R2, Nix and SQLite. | partially implemented: everything but Nix; the module layout became `host/`, `services/`, `db/` in the quality overhaul | `src/mshkn/app.py`, `src/mshkn/host/`, `src/mshkn/services/` |
| `docs/plans/2026-03-08-orchestrator-implementation.md` | Task plan for the orchestrator. | implemented, later restructured by PRs 2 to 4 of the quality overhaul | `src/mshkn/runtime.py` |
| `docs/plans/2026-03-09-nix-capability-system-design.md` | Two-level Nix capability cache. | superseded by `docs/plans/2026-03-13-recipe-system-design.md` | `migrations/009_recipes.sql` drops `capability_cache` |
| `docs/plans/2026-03-09-nix-capability-implementation.md` | Task plan for the Nix cache. | superseded by `docs/plans/2026-03-13-recipe-system-implementation.md` | no `capability` package exists |
| `docs/plans/2026-03-10-codex-agent-integration.md` | Drive computers from a Codex subscription. | retired: never built, nothing references it | |
| `docs/plans/2026-03-11-parallel-fc-launch-design.md` | Overlap Firecracker start with disk and tap setup. | implemented | `_stage` in `src/mshkn/host/firecracker.py` gathers the disk map and tap while the process starts |
| `docs/plans/2026-03-11-parallel-fc-launch-plan.md` | Task plan for the above. | implemented | same |
| `docs/plans/2026-03-11-phase1-latency-gates-design.md` | Turn print-only latency checks into p95 assertions. | implemented | `tests/e2e/test_phase1_latency.py` |
| `docs/plans/2026-03-11-phase1-latency-gates-plan.md` | Task plan for the above. | implemented | same |
| `docs/plans/2026-03-12-snapshot-restore-design.md` | Restore snapshots through a staging slot; L3 template cache. | implemented | `restore` and `build_template` in `src/mshkn/host/firecracker.py`; `src/mshkn/db/templates.py` |
| `docs/plans/2026-03-12-snapshot-restore-plan.md` | Task plan for the above. | implemented | same |
| `docs/plans/2026-03-13-recipe-system-design.md` | Replace Nix with Dockerfile recipes. | implemented | `src/mshkn/services/recipes.py`, `Dockerfile.mshkn-base` |
| `docs/plans/2026-03-13-recipe-system-implementation.md` | Task plan for recipes; remove `capability_cache` and manifests from the API. | implemented | `src/mshkn/api/schemas.py` has `recipe_id`, no manifest |
| `docs/plans/2026-03-13-telegram-agent-design.md` | A Telegram back-office agent on mshkn computers. | retired: the bridge was moved out of the repo (Claude remote control replaced it) | issue #34 |
| `docs/superpowers/plans/2026-03-12-ingress-mapping.md` | Webhook ingress with Starlark transforms. | implemented | `src/mshkn/services/ingress.py`, `src/mshkn/services/starlark.py` |

## Quality overhaul (2026-09)

Spec: `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md`. Six PRs; each plan has a `-baseline.txt` beside it recording the test count and coverage before the PR.

| Plan | PR | Status |
|---|---|---|
| `docs/superpowers/plans/2026-09-04-pr1-tooling-ci-hygiene.md` | #60 | implemented (merged 2026-09-04) |
| `docs/superpowers/plans/2026-09-04-pr2-foundations.md` | #61 | implemented (merged 2026-09-05) |
| `docs/superpowers/plans/2026-09-05-pr3-host-boundary.md` | #62 | implemented (merged 2026-09-06) |
| `docs/superpowers/plans/2026-09-06-pr4-services.md` | #63 | implemented (merged 2026-09-06) |
| `docs/superpowers/plans/2026-09-06-pr5-tests.md` | #64 | implemented (merged 2026-09-07); made seven stub E2E tests fail honestly (#65) |
| `docs/superpowers/plans/2026-09-07-pr6-docs-devtools.md` | this PR | docs, the docs test, the devtools move |

## Roadmap breakdown (`docs/plans/2026-03-08-roadmap.md`)

| Item | Status |
|---|---|
| P1 destroy ownership check, startup recovery, allocation locking | done: `ComputerService.get_owned`, `Runtime.start`, `SlotAllocator` |
| P2 Caddy dynamic routing | done: `src/mshkn/host/caddy.py` |
| P3 Nix capability system | superseded by recipes |
| P4 merge end to end | done: `POST /checkpoints/{parent_id}/merge` |
| P5 VM limits, rate limiting, idle timeout, retention, `needs`, stale cleanup | done: `ComputerService.create`, `src/mshkn/ratelimit.py`, `src/mshkn/services/reaper.py`, `src/mshkn/resources.py` |
| P6 metrics, JSON logs, status enrichment, checkpoint DAG, alerts | done: `src/mshkn/observability/`, `GET /alerts`; Grafana dashboards are not automatable and were never built |
| P7 Litestream | done: `systemd/litestream.service`, `DEPLOY.md` §12 |
| Economics validation (T9.x), dumb agent (T10.5), S3 isolation (T8.6) | not implemented; part of #65 |

## Open follow-ups

Filed from PR 5's final review and live runs: #65 (seven unimplemented E2E tests), #66 (an abandoned bring-up can orphan a spawned Firecracker), #67 (socket registry is process-local), #68 (`_post_process_rootfs` and a pre-existing `fcnet.service` symlink), #69 (test-harness hygiene), #70 (REST destroy and the dead-VM reaper can tear down the same computer). Earlier: #55 to #57 (restore-path experiments), #58 (exec log retention), #59 (`/forward`).
````

- [ ] **Step 3: Amend the spec's §3 layout.** In `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md`, inside the §3 code block, add these lines so the layout matches the code (place each beside its siblings, keeping the two-column style):

```
  main.py              ASGI entry point: uvicorn mshkn.main:app (systemd unit)
  ratelimit.py         RateLimiter (sliding window; exec per API key, ingress per rule)
```

under `host/`:

```
    firecracker_host.py production Host wiring; the one FirecrackerHypervisor per process
```

under `services/`:

```
    callback.py        deliver_callback: POST with retries, never raises
    starlark.py        transform validation and execution (starlark_go)
```

And directly after the code block, before "Dependency direction is strict", add one sentence:

> Amended 2026-09-07 (PR 6): the five entries above marked with their purpose were added because the implementation has them; the spec is the record of the layout as built.

Do not touch any other section of the spec.

- [ ] **Step 4: Run the docs test.**

Run: `uv run pytest tests/unit/test_docs.py -q -p no:cacheprovider`
Expected: every `docs/plans/README.md` case passes. A path failure means a filename in the index is wrong: fix the index, never the regex.

- [ ] **Step 5: Commit.**

```bash
git add docs/plans/README.md docs/superpowers/specs/2026-09-04-quality-overhaul-design.md tests/unit/test_docs.py
git commit -m "docs: plans index with evidence-based statuses; spec layout matches the code"
```

---

### Task 4: README.md

**Files:**
- Rewrite: `README.md`

- [ ] **Step 1: Replace the file with this content.** Where a sentence below states a number or a name, check it against the code before keeping it; the report lists any correction.

````markdown
# mshkn

Disposable cloud computers for AI agents. A computer is a Firecracker microVM that boots in about two seconds from a copy-on-write snapshot of a base disk. You run commands on it, checkpoint it, fork the checkpoint into more computers, merge two forks, and throw everything away. Checkpoints are the durable thing; computers are not.

This is a single-host research system with no users. The API changes without notice.

## What exists

- **Computers.** `POST /computers` boots a VM from the bare base volume or from a recipe's volume, with 256 MiB and 1 vCPU unless `needs` says otherwise (`{"ram": "512MB", "cores": 2}`). The request can carry an `exec` command to run immediately, `self_destruct` to checkpoint and destroy afterwards, a `callback_url` to be told the result, and a `label` for the checkpoint chain.
- **Exec.** `POST /computers/{computer_id}/exec` streams stdout, stderr and the exit code as server-sent events over SSH. Background commands (`exec/bg`, `exec/logs/{pid}`, `exec/kill/{pid}`), file `upload` and `download`, and `status` with live CPU, memory, disk and process counts.
- **Checkpoints.** `POST /computers/{computer_id}/checkpoint` pauses the VM, writes a Firecracker memory and device snapshot, takes a dm-thin snapshot of the disk, and resumes. The snapshot files upload to R2 in the background. Checkpoints have labels, parents (a DAG), a pin flag, and a per-account retention count.
- **Fork.** `POST /checkpoints/{checkpoint_id}/fork` restores the snapshot on a fresh slot: memory state comes back, the disk is a copy-on-write child. A fork of a 50 MB working set costs the same as a fork of 1 MB.
- **Exclusive chains.** A fork with `exclusive` set either fails while another computer is active on the label (`error_on_conflict`) or is queued (`defer_on_conflict`) and run when that computer self-destructs or is destroyed.
- **Merge.** `POST /checkpoints/{parent_id}/merge` does a three-way filesystem merge of two forks against their parent into a new checkpoint and reports conflicts.
- **Recipes.** `POST /recipes` takes a Dockerfile that starts `FROM mshkn-base`; the image is built, exported and written into a thin volume, and a booted template snapshot is cached so computers from the recipe restore instead of cold-booting.
- **Ingress.** Unauthenticated webhook URLs (`/ingress/{rule_id}`) whose Starlark transform decides whether to create or fork a computer, synchronously or not, with per-rule body-size and rate limits.
- **Reaper.** Dead VMs are cleaned up, idle VMs are checkpointed and destroyed after `MSHKN_IDLE_TIMEOUT` seconds, old checkpoints are pruned, and thin-pool and host RAM pressure raise alerts at `GET /alerts`.
- **Observability.** JSON logs with request ids, Prometheus metrics at `GET /metrics`, subsystem health at `GET /health`.
- **Tenancy.** API keys with a per-account VM limit and an exec rate limit. Accounts are created with `python -m mshkn accounts create`.

`docs/ARCHITECTURE.md` explains how these fit together.

## What does not exist

- More than one host. Slots, taps, thin volumes and the checkpoint directory are local to the machine; a checkpoint cannot be restored on another host.
- Billing, quotas beyond the VM limit, or any notion of a user beyond an API key.
- Retention of exec output after the computer is gone (#58) and an HTTP forwarding endpoint (#59).
- Seven of the 157 end-to-end tests describe workflows that are not implemented and fail on purpose until they are (#65): three phase-10 agent workflows, structured-log and audit-log checks, the checkpoint storage-cost measurement, and the R2 bucket-policy check.

## Layout

```
src/mshkn/
  main.py            ASGI entry point (uvicorn mshkn.main:app)
  app.py             create_app(): routers, request-id middleware, lifespan
  runtime.py         Runtime: config, db, host, services, background tasks
  cli.py             python -m mshkn accounts create|list, migrate
  config.py          Config from MSHKN_<FIELD> environment variables
  models.py          Account, Computer, Checkpoint, Recipe, IngressRule, enums
  errors.py          NotFound, Conflict, BadRequest, InvalidInput, ... HostError
  resources.py       Resources(mem_mib, vcpus) parsing and bounds
  ratelimit.py       sliding-window RateLimiter
  db/                aiosqlite connection, migrations, one module per table
  host/              Hypervisor, BlockStore, Guest, ObjectStore, Proxy protocols;
                     Firecracker, dm-thin, SSH, rclone and Caddy implementations;
                     in-memory fakes in fake.py
  services/          allocator, computers, checkpoints, lifecycle, recipes,
                     ingress, reaper, merge, callback, starlark
  api/               FastAPI routers, request/response schemas, error mapping
  observability/     JSON logging with request ids; metrics and timed()
migrations/          sequential, additive SQL migrations
scripts/             deploy.sh, e2e.sh, build-rootfs.sh, mshkn-pool-up
systemd/             mshkn, mshkn-pool and litestream units
tests/               unit/, flow/ (real app over the fake host), e2e/ (live server)
docs/                ARCHITECTURE.md, infrastructure.md, plans/ (with an index)
```

## Tests

Three tiers. The first two run anywhere; the third needs the live host.

```bash
uv sync
uv run pytest                 # unit + flow tiers; coverage floor 98%
uv run pytest tests/flow      # the real app and services over the fake host
MSHKN_SERVER=root@<ip> scripts/e2e.sh   # pushes, deploys, runs tests/e2e on the live server
```

The E2E suite is the definition of done for the product (`docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md`). It currently reports 144 passed, 6 skipped and 7 failed; the seven are the unimplemented workflows in #65, and anything else failing is a regression.

The full local gate, which is exactly what CI runs:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

To drive the whole stack without a host, build a `mshkn.host.fake.FakeHost` and hand it to `mshkn.runtime.Runtime.build`; `tests/flow/conftest.py` shows the wiring.

## Running it

It needs a bare-metal Linux host with `/dev/kvm`, Firecracker, the dm-thin kernel module, Docker, and a public address for the wildcard domain. `docs/infrastructure.md` states the minimum; `DEPLOY.md` is the fresh-server procedure, executed verbatim on the current host.

## Working on it

`CLAUDE.md` holds the working rules (the gate, the E2E expectation, no merges without authorization). `docs/plans/README.md` indexes every design and implementation plan with its status.

## License

MIT
````

- [ ] **Step 2: Run the docs test and the gate.**

Run: `uv run pytest tests/unit/test_docs.py -q -p no:cacheprovider`
Expected: every README case passes (paths, modules, routes, metrics, env vars, banned terms); `docs/ARCHITECTURE.md` and `docs/plans/README.md` exist from Tasks 2 and 3. The CLAUDE.md banned-term case still fails (Task 5).

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean (the README is not Python; this confirms nothing else moved).

- [ ] **Step 3: Commit.**

```bash
git add README.md
git commit -m "docs: README describes what exists, what does not, and how to run each test tier"
```

---

### Task 5: CLAUDE.md and DEPLOY.md

**Files:**
- Rewrite: `CLAUDE.md`
- Modify: `DEPLOY.md` §13 (verify) and Teardown

- [ ] **Step 1: Replace `CLAUDE.md` with this content.** The review-triage API block and the skills table are kept verbatim from the current file; everything else is rewritten to match the repo after PR 5.

````markdown
# mshkn

Disposable cloud computers for AI agents: Firecracker microVMs you create, exec on, checkpoint, fork, merge and destroy. `README.md` says what exists, `docs/ARCHITECTURE.md` says how it works, `docs/plans/README.md` indexes every plan with its status, and `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` is the definition of done (157 E2E tests).

## The gate

Run this before every commit you would show anyone. It is exactly what CI runs:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

uv is the only package manager; every tool runs through the project venv as `uv run <tool>`. `pytest` runs the unit and flow tiers (zero warnings, coverage floor 98 %); the E2E tier is deselected by default. `tests/unit/test_docs.py` fails when a document names a path, module, route, metric or variable that does not exist: fix the document, not the test.

## The live E2E gate

E2E tests on the live host are the source of truth for the product. After deploying, run:

```bash
export MSHKN_SERVER=root@<ip>       # or an ssh config alias
scripts/e2e.sh                       # pushes, deploys, cleans orphaned VM resources, ensures the test account, runs tests/e2e
```

Run it detached (`setsid nohup … > log 2>&1 < /dev/null &`) and read the log; a full run takes about 14 minutes. The expected result is **144 passed, 6 skipped, 7 failed**, and the seven must be exactly the `Not implemented` tests in #65. Any other failure is a regression: fix it or stop and discuss. Never mark a test xfail, never weaken an assertion, never skip to get green. Failing tests are honest reminders of what is left; a test with no assertion is worse than a failing one.

After a run, check the service journal for tracebacks:

```bash
ssh $MSHKN_SERVER journalctl -u mshkn --since '20 min ago' --no-pager | grep -ci traceback
```

## How to find work

Open issues are the backlog: `gh issue list`. `docs/plans/README.md` shows what each historical plan delivered and what is still open. Read an issue fully before starting; if it names something out of scope, leave it out.

1. Create a worktree: `git worktree add ../mshkn-<name> -b <branch>` and work there.
2. Open a PR against `main` with `gh pr create`.

## How to submit work

The PR body must include:

- `Closes #<N>` when there is an issue.
- **What this does**: two or three sentences.
- **Design alignment**: for each principle in `docs/ARCHITECTURE.md` or the spec the change touches, how the implementation matches. A deviation references an approved `spec-change` issue.
- **Validation performed**: the gate output, the CI link, and the live E2E summary line with the failing set named. Evidence, not claims.

## Required skills for all workflow operations

If you are implementing a GitHub issue and, having studied the codebase, feel that it is relatively straightforward and mechanical to implement and needs just a few decisions here and there, you can go about implementing directly and ask questions as they come up.

Otherwise, for creative, open-ended, or large tickets, you MUST use the superpowers skills for brainstorming, planning, worktree management, and sub-agent dispatch. Do NOT hand-roll these operations with raw Task tool calls — the skills handle permissions, directory routing, and agent coordination correctly. Raw background agents WILL fail on file writes due to auto-denied permissions.

| Operation | Required skill |
|---|---|
| Creative/design work before implementation | `superpowers:brainstorming` |
| Writing implementation plans | `superpowers:writing-plans` |
| Creating/managing git worktrees | `superpowers:using-git-worktrees` |
| Dispatching parallel sub-agents | `superpowers:dispatching-parallel-agents` |
| Executing plans with sub-agents (same session) | `superpowers:subagent-driven-development` |
| Executing plans (separate session) | `superpowers:executing-plans` |
| Finishing a branch (merge/PR/cleanup) | `superpowers:finishing-a-development-branch` |
| Code review | `superpowers:requesting-code-review` |
| Verifying work before claiming done | `superpowers:verification-before-completion` |
| TDD workflow | `superpowers:test-driven-development` |

**Never** use `run_in_background: true` with the Task tool for implementation work. Background agents cannot prompt for permissions and will silently fail or write to wrong directories.

## How to handle PR reviews

After creating a PR, bot reviewers (CodeRabbit, Copilot) may leave comments. Triage them:

1. **Reply to every comment** with a concise rationale (fix, defer, or dismiss with reason)
2. **Resolve every thread** after replying — use the GraphQL `resolveReviewThread` mutation
3. **Fix only what's actually wrong** — bot reviewers lack project context and frequently suggest over-engineering

**API reference** (so you don't have to rediscover this):

```bash
# Get review comment IDs
gh api repos/mikesol/mshkn/pulls/<N>/comments --jq '.[] | {id, user: .user.login, path, line, body: .body[:80]}'

# Reply to a review comment (in_reply_to creates a thread reply)
gh api repos/mikesol/mshkn/pulls/<N>/comments -f body="Your reply" -F in_reply_to=<comment_id>

# Get thread IDs for resolving
gh api graphql -f query='{ repository(owner: "mikesol", name: "mshkn") { pullRequest(number: <N>) { reviewThreads(first: 50) { nodes { id isResolved } } } } }'

# Resolve a thread
gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "<thread_id>"}) { thread { isResolved } } }'
```

## Standing rules

- **NEVER merge PRs without explicit user authorization.** Wait for the user to say "merge it" (or equivalent). Creating a PR is fine; merging is not. No exceptions.
- **Wait for CI before requesting a merge.** `gh pr checks <N> --watch`. Never merge a red PR.
- **Spec seems wrong?** Stop. Open a GitHub issue labeled `spec-change` with the problem and its evidence, the affected sections, the proposed change and the downstream impact. Don't build on a wrong assumption.
- **No papering over failures.** If you can't solve something, say so. No xfail, no weakened assertions, no workarounds that hide the real issue.
- **Be mega-rigorous.** Don't code to the benchmark. Don't sweep stuff under the carpet. Evidence before assertions.
- **No backwards compatibility or versioning.** This is a pre-alpha research project with zero users. Don't version APIs, don't keep fallback paths, don't create a "v2" beside the old thing; replace it. The one exception is database migrations, which are sequential and additive.
- **Product behaviour changes need a test that found or pins them**, in the unit or flow tier; the E2E tier proves them on the live host.
- **Infrastructure comes before workarounds.** If a task needs a host or a service the project does not have, write the minimum into `docs/infrastructure.md` and ask for it; do not make the product optional or add indirection to work around missing infrastructure.

## Deployment

`DEPLOY.md` is the fresh-server procedure (Firecracker, dm-thin pool, Docker base image, Caddy, R2, Litestream), executed verbatim on the current host. `scripts/deploy.sh` pushes the current branch to the host and restarts the service; `scripts/e2e.sh` does that and runs the suite.

## Server reference

The live E2E server is a dedicated KVM host set up from `DEPLOY.md`. Export its address once per shell (`MSHKN_SERVER=root@<ip>` or an ssh config alias):

- **Deploy**: `scripts/deploy.sh`
- **E2E**: `scripts/e2e.sh`
- **Service**: `ssh $MSHKN_SERVER systemctl {restart,status,stop} mshkn`
- **Logs**: `ssh $MSHKN_SERVER journalctl -u mshkn --since '5 min ago' --no-pager`
- **Test account**: `acct-mike` / `mk-test-key-2026` (recreated by `scripts/e2e.sh` if the database was reset)
- **Secrets on the host**: `/opt/mshkn/.env` (R2), `/etc/caddy/env` (Cloudflare DNS token), `/etc/litestream.yml`. Never print them.
````

- [ ] **Step 2: Edit `DEPLOY.md`.** Two changes only.

In §13 "Verify", after the `scripts/e2e.sh` command block, add:

```markdown
Expect **144 passed, 6 skipped, 7 failed**; the seven failures are the `Not implemented` tests tracked in #65, and any other failure means the host or the deployment is wrong. The suite runs for about 14 minutes.
```

In "Teardown", add one line after `pkill -x firecracker || true`:

```bash
rm -f /tmp/fc-*.socket
```

- [ ] **Step 3: Run the docs test and the gate.**

Run: `uv run pytest tests/unit/test_docs.py -q -p no:cacheprovider`
Expected: everything passes, including `test_retired_terms_are_absent[CLAUDE.md]` and the CLAUDE.md path, module, route and env-var cases. No skips.

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -p no:cacheprovider 2>&1 | tail -1`
Expected: green; report the exact test count.

- [ ] **Step 4: Commit.**

```bash
git add CLAUDE.md DEPLOY.md
git commit -m "docs: CLAUDE.md states the gate, the E2E expectation and the working rules; DEPLOY.md verify and teardown"
```

---

### Task 6: Final verification, PR, CI, live E2E

**Files:** none new.

- [ ] **Step 1: Full local validation.**

```bash
uv sync --frozen && uv lock --check
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest --cov -p no:cacheprovider 2>&1 | grep -E "passed|skipped|TOTAL|warning"
uv run pytest tests/unit/test_docs.py -q -p no:cacheprovider 2>&1 | tail -1     # no "skipped" in the line
git status --short                                                                # empty
git diff --stat main -- src/                                                     # empty: nothing under src changed
ls ../mshkn-devtools/telegram                                                     # the four moved files
```

Expected: green; TOTAL still 98 %; the docs test line has no skip; `src/` unchanged; the tree clean.

- [ ] **Step 2: File the rootfs-script issue.**

```bash
gh issue create --title "scripts/build-rootfs.sh still carries Nix-era steps" --body "The bare base-volume build (DEPLOY.md §5) pre-creates /nix, appends a 'PATH for Nix' profile block, and removes apt 'to enforce purity' — all from the retired Nix capability system. The script is not exercised by E2E (the live base volume was built from it on 2026-09-04), so PR 6 left it alone. Removing the Nix steps needs a rebuild of the base rootfs on the host and a full E2E run to prove nothing depended on them.

Found during the quality overhaul PR 6.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Record the issue number for the PR body.

- [ ] **Step 3: Push and open the PR** with this body (fill the `<...>`):

```
Part 6 of 6 of the quality overhaul (spec §3, §13, §14; plan docs/superpowers/plans/2026-09-07-pr6-docs-devtools.md).

**What this does**
Rewrites README.md, adds docs/ARCHITECTURE.md and a docs/plans/README.md index with evidence-based statuses for all 23 plans, rewrites CLAUDE.md around the gate and the live E2E expectation (144/6/7 with the seven #65 tests named), and adds the expected result and a socket sweep to DEPLOY.md. A new unit test, tests/unit/test_docs.py, parses every backticked path, module, route, metric and environment variable in those documents and asserts it exists, and asserts the architecture doc's route and metric tables are complete both ways, so the docs cannot drift silently. The spec's §3 layout is amended with the five modules the code has (main, ratelimit, firecracker_host, callback, starlark). The retired Telegram bridge's runtime artifacts were copied to ../mshkn-devtools/telegram/ (outside git) and removed from the tree along with an empty stray file and a stale ignored __pycache__.

**Not changed**
Nothing under src/. scripts/build-rootfs.sh still carries Nix-era steps: <issue from Step 2>.

**Validation performed**
- `uv sync --frozen && uv lock --check`, ruff, format, mypy clean; `uv run pytest --cov`: <N> passed, 0 warnings, TOTAL 98 %; no skips in tests/unit/test_docs.py.
- `git diff --stat main -- src/` is empty.
- CI: <link>
- Live E2E (`scripts/e2e.sh` against 65.21.22.161 at <sha>): <144 passed, 6 skipped, 7 failed>; the seven are exactly the #65 tests; journal tracebacks: 0.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01CPKyFZiT4pPi4v5gkph5KZ
```

- [ ] **Step 4: `gh pr checks <N> --watch` to green.**

- [ ] **Step 5: Live E2E, detached**, even though nothing under `src/` changed (the rule is every PR):

```bash
setsid nohup env MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000 scripts/e2e.sh -p no:cacheprovider > <scratchpad>/e2e-pr6.log 2>&1 < /dev/null & disown
```

Monitor for `FAILED|passed|failed`. Expected: `7 failed, 144 passed, 6 skipped`; `grep -c '^E +Failed: Not implemented' <log>` prints 7 and `grep ^FAILED <log>` lists only the #65 tests. Check the journal (`grep -ci traceback` is 0) and that `ls /tmp/fc-*.socket` on the host prints nothing after the run.

- [ ] **Step 6:** Triage bot reviews; fill the PR body; report with the CI link and the E2E summary; do not merge.

---

## Self-review

**Spec coverage.** §13 README (Task 4: what exists, what does not, test tiers, layout); ARCHITECTURE (Task 2: request path, service responsibilities, host boundary, state ownership, both lifecycles, failure handling and cleanup guarantees, the fake host); plans index with status (Task 3); CLAUDE.md (Task 5: no Telegram section, "Current phase" replaced by the plans index and the issue list, the gate is the CI command, the E2E gate references `scripts/e2e.sh`, no stale counts, no `capability_cache`); DEPLOY.md (already has Docker, `uv sync --frozen`, the accounts CLI, no Nix `PATH`, and the Litestream `PartOf` note from PR 1; Task 5 adds the expected result and the socket sweep); removals (`telegram/` copied out then removed in Task 1; `skills/`, `skills-lock.json`, `e2e_test.sh`, root `deploy.sh`, `tests/integration/`, `delta.py`, `poetry.lock` were removed in PR 1 and Task 6 confirms the tree); `.gitignore` already correct (verified in the evidence section). §3 layout reconciled by amending the spec (Task 3) rather than moving code, because moving code in a docs PR would violate this plan's "nothing under `src/`" constraint and `mshkn.main:app` is what the systemd unit runs. §14 item 6 delivered as one PR. §15: nothing here touches the out-of-scope items.

**Placeholder scan.** Every document's full text is in its task. The PR body's `<...>` fields are filled at submission; the issue number from Task 6 Step 2 is the only value produced during execution. No "TBD", no "similar to".

**Type consistency.** `DOCS` and `BANNED` are defined in Task 1 and extended by name in Tasks 2 and 3; the regexes in Task 1 match the notation used in Tasks 2 to 5 (backticked paths with directory prefixes, backticked dotted `mshkn.` names, backticked `METHOD /route`, backticked `mshkn_` metrics with optional `{labels}`, backticked `MSHKN_`/`R2_` names with optional `=value`). Task 2's endpoint table lists every route in the evidence list; Task 2's metrics table lists all seven families. Task 3's index names 23 documents, matching the 17 under `docs/plans/` and 6 under `docs/superpowers/plans/` (the six quality-overhaul plans plus the ingress plan, with PR 6's own plan making the seventh entry in that directory once this file is committed; the index lists it too).
