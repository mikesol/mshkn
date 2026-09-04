# Quality Overhaul: Architecture, Tests, CI, Observability

**Date:** 2026-09-04
**Status:** Approved (design), awaiting spec review
**Scope:** Make the repo as good as it can be in its current shape. No new product surface except a tiny accounts CLI that replaces a documented endpoint that never existed. Features may change only where they are broken or inconsistent.

## 1. Why

The codebase was built feature-by-feature and it shows:

- No CI. `ruff check tests/` has 60 errors, `ruff format --check` wants to rewrite 43 of 75 files, tests are not type-checked, and a bare `pytest` runs 157 live-server E2E tests against nothing.
- The checkpoint flow is implemented four times (endpoint, self-destruct, idle reaper, merge). The exec-then-self-destruct flow is implemented three times (REST, ingress, deferred drain) and the copies disagree: ingress create ignores `recipe_id`/`needs`, ingress never uses the SSH pool, and the active-computers gauge only moves on REST calls.
- `VMManager` imports from the API layer to drain the deferred queue; 52 function-local imports exist to dodge the resulting cycles.
- Module-level mutable state (rate limiter, three background-task sets, per-rule limiters, build locks, staging lock) forces tests to mutate the shared `app.state` in 54 places and patch internals.
- Known correctness gaps: create/fork leak a thin volume and slot when boot fails; host alerts watch `/` instead of the dm-thin pool (the failure mode that has actually taken the service down); merge blocks the event loop while walking a rootfs; SQLite runs without WAL, foreign keys, or a busy timeout; unknown recipe returns 500; exec "streaming" buffers all output and emits it at the end.
- Dead code and stale docs from the Nix era: `manifest_hash`/`manifest_json` hardcoded to `"none"`/`"{}"` yet exposed in responses, `uses`/`capabilities` accepted by ingress validation, `checkpoint/delta.py`, unused pool-init/base-volume/NAT helpers, `tests/integration`, `e2e_test.sh`, a `uses` E2E helper parameter kept "for backward compatibility", README/roadmap/CLAUDE.md describing a system that no longer exists, DEPLOY.md pointing at a `/accounts` endpoint that does not exist and omitting Docker entirely.
- Tooling split three ways: uv-created venv and `deploy.sh` use uv, `poetry.lock` is committed alongside `uv.lock`, DEPLOY.md installs with pip.

## 2. Decisions already made

| Decision | Choice |
|---|---|
| Refactor depth | Service layer plus a host backend boundary with in-memory fakes |
| Boundary shape | Split by concern: `Hypervisor`, `BlockStore`, `Guest`, `ObjectStore`, `Proxy` |
| Package manager | uv only; delete `poetry.lock` |
| Telegram bridge and `skills/` | Move out of this repo (copied to `../mshkn-devtools/` before deletion) |
| Validation | Every PR is validated locally and against live E2E on `135.181.6.215` before merge is requested |

## 3. Target layout

```
src/mshkn/
  __main__.py          python -m mshkn → cli
  cli.py               accounts create/list, migrate
  app.py               create_app(runtime); lifespan builds Runtime from Config
  runtime.py           Runtime: config, db, host, services, BackgroundTasks, locks
  config.py            Config with generic MSHKN_<FIELD> env mapping, typed parsing
  models.py            Account, Computer, Checkpoint, Recipe, DeferredRequest, StrEnums
  errors.py            NotFound, Conflict, InvalidInput, LimitExceeded, HostError
  resources.py         Resources(mem_mib, vcpus): defaults, parsing, bounds

  db/
    __init__.py        connect() with pragmas; run_migrations()
    accounts.py computers.py checkpoints.py recipes.py deferred.py ingress.py templates.py
                       one COLUMNS tuple + one from_row per table

  host/
    __init__.py        Host container + the five Protocols + shared result types
    shell.py           run(), ShellError
    network.py         slot ↔ ip/mac/tap helpers, create_tap, destroy_tap
    firecracker.py     FirecrackerHypervisor (staging slot lives here)
    dmthin.py          DmThinBlockStore
    ssh.py             SshGuest (connection pool inside)
    r2.py              RcloneObjectStore
    caddy.py           CaddyProxy
    fake.py            FakeHypervisor, FakeBlockStore, FakeGuest, FakeObjectStore, FakeProxy

  services/
    allocator.py       SlotAllocator (slots + volume ids, derived from DB and pool at startup)
    computers.py       ComputerService: create, fork, destroy, exec, exec_bg, upload, download, status
    checkpoints.py     CheckpointService: create (single impl), delete, prune, merge
    lifecycle.py       run_ephemeral(): exec → optional self-destruct → callback → deferred drain
    recipes.py         RecipeService: build pipeline, L3 template build (one impl for bare and recipe)
    ingress.py         IngressService: rule CRUD, trigger handling, starlark
    reaper.py          Reaper: dead VMs, idle VMs, prune, host checks
    merge.py           three_way_merge (pure, unchanged algorithm)

  api/
    deps.py            get_runtime, require_account
    schemas.py         every request/response model
    errors.py          domain error → HTTP exception handlers
    computers.py checkpoints.py recipes.py ingress.py system.py (health, metrics, alerts)

  observability/
    logging.py         JSONFormatter, request-id contextvar, RequestIdFilter
    metrics.py         histograms/counters/gauges, timed(op) context manager
```

Dependency direction is strict: `api → services → host, db`. `models`, `errors`, `config`, `resources`, `observability` are leaves. Nothing in `services` or `host` imports `api`. There are no function-local imports except where an import is genuinely optional (none are expected).

## 4. Host boundary

### 4.1 Protocols

```python
@dataclass(frozen=True)
class RunningVM:
    pid: int
    socket_path: str
    slot: int
    vm_ip: str
    tap_device: str

@dataclass(frozen=True)
class SnapshotFiles:
    vmstate: Path
    memory: Path

class Hypervisor(Protocol):
    async def boot(self, *, slot: int, disk_volume_id: int, disk_name: str,
                   resources: Resources) -> RunningVM: ...
    async def restore(self, *, slot: int, disk_volume_id: int, disk_name: str,
                      snapshot: SnapshotFiles) -> RunningVM: ...
    async def snapshot(self, vm: RunningVM, dest_dir: Path) -> SnapshotFiles: ...
    async def build_template(self, *, disk_volume_id: int, dest_dir: Path) -> SnapshotFiles: ...
    async def kill(self, pid: int) -> None: ...
    def is_alive(self, pid: int) -> bool: ...
    async def teardown_slot(self, slot: int) -> None: ...  # destroy tap + iptables rules

class BlockStore(Protocol):
    async def snap(self, *, source_volume_id: int, new_volume_id: int) -> None: ...
    async def activate(self, *, volume_id: int, name: str) -> None: ...
    async def remove(self, *, volume_id: int, name: str) -> None: ...
    def mounted(self, name: str, *, readonly: bool = False) -> AbstractAsyncContextManager[Path]: ...
    async def max_volume_id(self) -> int | None: ...
    async def usage(self) -> PoolUsage: ...   # data_used_ratio, metadata_used_ratio
    async def mkfs(self, name: str) -> None: ...

class Guest(Protocol):
    async def exec(self, vm_ip: str, command: str, *, timeout: float = 300.0) -> ExecResult: ...
    def stream(self, vm_ip: str, command: str, *, timeout: float = 60.0) -> AsyncIterator[OutputLine]: ...
    async def exec_bg(self, vm_ip: str, command: str) -> int: ...
    async def upload(self, vm_ip: str, remote_path: str, data: bytes) -> None: ...
    async def download(self, vm_ip: str, remote_path: str) -> bytes: ...
    async def metrics(self, vm_ip: str, *, timeout: float = 10.0) -> VmMetrics: ...
    async def warm(self, vm_ip: str) -> None: ...        # open and pool a connection
    async def evict(self, vm_ip: str) -> None: ...    # drop pooled connection
    async def close(self) -> None: ...

class ObjectStore(Protocol):
    async def upload_dir(self, local_dir: Path, prefix: str) -> None: ...
    async def download_dir(self, prefix: str, local_dir: Path) -> None: ...
    async def delete_prefix(self, prefix: str) -> None: ...

class Proxy(Protocol):
    async def add_route(self, computer_id: str, vm_ip: str) -> None: ...
    async def remove_route(self, computer_id: str) -> None: ...
    async def healthy(self) -> bool: ...
    async def close(self) -> None: ...

@dataclass
class Host:
    hypervisor: Hypervisor
    blocks: BlockStore
    guest: Guest
    objects: ObjectStore
    proxy: Proxy
```

`OutputLine` is `(stream: Literal["stdout","stderr","exit"], data: str)`.

### 4.2 Firecracker implementation

`FirecrackerHypervisor` absorbs `vm/staging.py`, `vm/firecracker.py`, and the two template builders from `VMManager`. `boot` and `restore` share one private `_stage(...)` that does: ensure staging clean, map disk as staging drive + create staging tap + start process in parallel, then either `configure_and_boot` or `load_snapshot`, wait for SSH at the staging IP, add the final IP via SSH, rename tap and drive. The staging lock is an instance attribute. `build_template` cold-boots on the staging slot, pauses, snapshots, kills, and cleans up; it is the single implementation behind bare and recipe templates. The SSH key path used for the staging hop comes from config, not a literal.

`DmThinBlockStore` wraps the current `storage.py` functions. `usage()` parses `dmsetup status <pool>` into used/total for data and metadata blocks.

`SshGuest` owns the connection pool. `stream()` yields lines as they arrive: two reader tasks push `OutputLine`s onto a queue; the consumer yields from the queue while racing `process.wait()`; after exit, the existing 2 s grace drain runs, then `("exit", code)` is yielded. The previous behavior of collecting everything and yielding at the end is removed.

### 4.3 Fakes

`host/fake.py` ships in the package so flow tests and future local development can use it. Each fake records calls, keeps in-memory state (volumes with parent links, running VMs by pid, routes, object prefixes, guest files), and exposes knobs for fault injection: `FakeBlockStore.fail_next("snap")`, `FakeHypervisor.fail_next("restore")`, `FakeGuest.script(command → ExecResult)`, `FakeGuest.stream_script(command → list[OutputLine])`. `FakeGuest.exec` records the commands it ran so tests can assert the `sync` before snapshot, the `/tmp/exec/N.txt` writes in deferred drain, and so on. `FakeHypervisor.boot`/`restore` return deterministic `RunningVM`s and mark pids alive until `kill`.

## 5. Runtime and state ownership

```python
@dataclass
class Runtime:
    config: Config
    db: aiosqlite.Connection
    host: Host
    tasks: BackgroundTasks
    allocator: SlotAllocator
    rate_limiter: RateLimiter
    computers: ComputerService
    checkpoints: CheckpointService
    recipes: RecipeService
    ingress: IngressService
    reaper: Reaper
    alerts: deque[Alert]
    http: httpx.AsyncClient      # outbound HTTP (callbacks); tests inject an ASGI transport
```

`BackgroundTasks.spawn(coro, *, name, key=None)` creates a task, keeps a strong reference, logs exceptions, and lets `cancel(key)` and `wait(key)` target a specific task (used for checkpoint uploads keyed by checkpoint id). `drain(timeout)` on shutdown awaits outstanding tasks and cancels stragglers.

`create_app(runtime)` builds the FastAPI app with routers and exception handlers; the production lifespan constructs the `Runtime` from `Config.from_env()` and a `FirecrackerHost`. Routers obtain the runtime via `Depends(get_runtime)`. There are no module-level globals holding mutable state anywhere under `src/`.

Shutdown order: cancel and await the reaper task, `tasks.drain(30s)`, `host.guest.close()`, `host.proxy.close()`, `db.close()`.

## 6. Services

### 6.1 SlotAllocator

Owns `_next_slot`, `_free_slots`, `_next_volume_id`, and one lock. `initialize()` derives state from the DB (running computers, max checkpoint volume, max recipe volume) and `blocks.max_volume_id()`. `acquire()` returns `(slot, volume_id)`; `release_slot(slot)`. Slot 254 is never handed out.

### 6.2 ComputerService

- `create(account, *, recipe_id, resources) -> Computer`. Resolves the recipe (404 if unknown, 409 if not ready), acquires slot and volume, snaps the disk, then boots or restores. Any failure after `snap` removes the volume, releases the slot, and re-raises as `HostError`. Restores use the recipe template or the bare template, building lazily via `RecipeService.ensure_template`. Registers the proxy route, warms the guest connection, inserts the row.
- `fork(account, checkpoint, *, recipe_id) -> Computer`. Same acquire/snap/restore-or-boot/cleanup structure; downloads snapshot files from the object store on a local miss.
- `destroy(computer_id)`: remove route, kill, remove volume, `hypervisor.teardown_slot`, `allocator.release_slot`, evict guest connection, mark destroyed. Idempotent on already-destroyed.
- `exec`, `exec_bg`, `exec_logs`, `exec_kill`, `upload`, `download`, `status` delegate to `host.guest` and update `last_exec_at`.
- `active_count(account_id)` and `active_count_total()` back the VM limit check and the gauge.

### 6.3 CheckpointService

- `create(computer, *, label, pin, trigger) -> Checkpoint` is the only checkpoint implementation. Steps: guest `sync`, hypervisor snapshot (pause/create/resume), guest evict, block snap of the computer's volume, parent resolution (latest checkpoint for this computer, else the computer's source checkpoint, else none), insert, metrics, spawn upload keyed by checkpoint id. `trigger` is an enum (`api`, `self_destruct`, `idle`) and is logged.
- `delete(checkpoint)`: cancel/await its upload task, remove volume, remove local files, delete object prefix, delete row.
- `prune()`: per-account retention as today, via `delete`.
- `merge(parent, a, b) -> MergeOutcome`: validation as today; the walk and copy run in `asyncio.to_thread`; mounts go through `blocks.mounted(...)`. The output still starts as a snap of the parent, conflicts still default to fork A and are reported as such.
- `fork(account, checkpoint, request) -> Forked | Deferred`: exclusive-restore handling (`error_on_conflict` → `Conflict`, `defer_on_conflict` → insert deferred and return `Deferred`), else `ComputerService.fork`.

### 6.4 lifecycle.run_ephemeral

```python
@dataclass(frozen=True)
class ExecSpec:
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
```

`run_ephemeral(rt, computer, spec, *, source_checkpoint)` runs the command through the pooled guest, optionally checkpoints via `CheckpointService.create(trigger=self_destruct)`, destroys, fires the callback as a background task, then drains the deferred queue for the label. It is the single implementation behind REST create, REST fork, ingress create, ingress fork, and `drain_deferred`.

`drain_deferred(rt, account, label)` claims items with one `DELETE ... RETURNING` statement so the destroy endpoint and the reaper cannot both process a batch, forks from the newest labeled checkpoint, writes `/tmp/exec/N.txt`, builds the command (last `meta_exec` wins, else newline-joined execs), and calls `run_ephemeral` with `self_destruct` if any item asked for it and the last `callback_url` present.

### 6.5 IngressService

Rule CRUD as today. Trigger handling: validate the Starlark result against a Pydantic model instead of hand-rolled set arithmetic. `create` actions accept `recipe_id` and `needs` (matching REST); `capabilities`/`uses` are rejected as unknown fields. Per-rule rate limiters live on the service. Both sync and async modes call `run_ephemeral` through the same code path as REST.

### 6.6 RecipeService

`create` dedupes by content hash, allocates a volume id, and spawns the build under a per-account lock held by the service. `build` is the current pipeline behind the `BlockStore` (snap, mkfs, mounted, remove) with Docker commands via `shell.run`. `ensure_template(recipe | None) -> SnapshotFiles` builds lazily via `hypervisor.build_template` and caches in `recipes.template_*` or `snapshot_templates`.

### 6.7 Reaper

One loop: dead VMs (via `hypervisor.is_alive`), idle VMs (checkpoint with `trigger=idle` then destroy, then drain deferred for the source label), prune, host checks. Host checks add thin-pool data and metadata ratios (warning above 0.80, critical above 0.95) alongside the existing root-disk and RAM checks, and update the pool gauges.

## 7. Data layer

- `db.connect(path)` sets `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`, and `row_factory = aiosqlite.Row`.
- Migrations are applied with `executescript` per file, no `;` splitting. Runner behavior (bootstrap `_migrations`, skip applied, commit per file) is unchanged.
- Migration `010_indexes.sql`: `computers(account_id, status)`, `checkpoints(account_id, created_at)`, `checkpoints(computer_id, created_at)`, `checkpoints(label)`, `deferred_queue(label, created_at)`.
- Vestigial columns `computers.manifest_hash`, `computers.manifest_json`, `checkpoints.manifest_hash`, `checkpoints.manifest_json` stay in the schema (additive rule) but disappear from the dataclasses. Inserts write `''` and `'{}'` as SQL constants. `capability_cache` and `snapshot_templates.manifest_hash` likewise stay; the bare template row keeps key `'bare'`.
- Each table module has one `COLUMNS` tuple and one `from_row`. `DeferredRequest` is a dataclass, not a dict.
- Statuses are `StrEnum`s: `ComputerStatus {creating, running, destroyed}`, `RecipeStatus {pending, building, exporting, injecting, ready, failed}`, `IngressLogStatus {accepted, completed, failed}`, `CheckpointTrigger {api, self_destruct, idle}`.

## 8. Config and resources

`Config` stays a frozen dataclass. `from_env` maps every field from `MSHKN_<FIELD_UPPER>` with parsing driven by the annotated type (`int`, `float`, `bool`, `Path`, `str`); unparseable values raise `ConfigError` at startup with the variable name. The R2 variables keep their current names via an explicit alias table (`R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`). `MSHKN_IDLE_TIMEOUT` and `MSHKN_CHECKPOINT_RETENTION` keep working as aliases for the same reason: the live server's `.env` uses them.

`Resources` replaces `parse_needs`: defaults `mem_mib=256`, `vcpus=2`; `from_needs(dict)` accepts `ram` as `"<n>MB"`/`"<n>GB"` and `cores` as int or numeric string; bounds are 128 MiB to 32 GiB and 1 to 16 vCPUs; anything else raises `InvalidInput` (422). A request with default resources uses the template path; anything else cold-boots, as today.

## 9. API contract

Every endpoint declares a Pydantic response model. Error responses keep FastAPI's `{"detail": ...}` shape so E2E clients do not churn. Domain errors map via exception handlers: `NotFound` 404, `Conflict` 409, `InvalidInput` 422, `LimitExceeded` 429, `HostError` 502 with a generic message and the detail logged. Unexpected exceptions remain 500.

Changes visible to clients, all of which correct an inconsistency:

| Endpoint | Change |
|---|---|
| `POST /computers` | unknown recipe 404 (was 500); recipe not ready 409 (was 500); bad `needs` 422 (was silently defaulted) |
| `GET /computers/{id}/status` | drops `manifest_hash`, adds `recipe_id` |
| `GET /checkpoints` | drops `manifest_hash` |
| `POST /ingress/{rule}` create action | accepts `recipe_id`, `needs`; rejects `capabilities`, `uses` |
| `GET /health` | `{"status": "ok", "subsystems": {"database", "firecracker", "storage", "proxy"}}` each `"ok"` or an error string; overall status is `"degraded"` if any is not ok, HTTP still 200. Checks: database `SELECT 1`; firecracker binary on PATH and kernel path exists; storage `blocks.usage()` succeeds; proxy `proxy.healthy()` |
| `GET /metrics` | additional series listed in §10 |

Rate limiting stays on exec only, which is what T5.9 exercises; create is bounded by the VM limit.

New: `python -m mshkn accounts create --id <id> --api-key <key> --vm-limit <n>` and `python -m mshkn accounts list`, operating directly on the configured DB. DEPLOY.md's nonexistent `POST /accounts` step is replaced with this.

## 10. Observability

**Logging.** `request_id_middleware` sets a contextvar; `RequestIdFilter` adds `request_id` to every record (or `"-"` outside a request, and a background task name where one is spawned by `BackgroundTasks`). Lifecycle events log `extra` fields: `op`, `computer_id`, `checkpoint_id`, `account_id`, `recipe_id`, `duration_ms`, `trigger`. Logging is configured in `create_app`, not at import.

**Metrics.**

| Series | Type | Labels |
|---|---|---|
| `mshkn_operation_duration_seconds` | histogram | `op` ∈ create, fork, restore, boot, checkpoint, merge, exec, destroy, recipe_build |
| `mshkn_operation_errors_total` | counter | `op`, `kind` (domain, host, unexpected) |
| `mshkn_computers_active` | gauge | — (set from `active_count_total()` after every state change and each reaper cycle) |
| `mshkn_computers_created_total` | counter | `source` ∈ create, fork |
| `mshkn_checkpoints_total` | counter | `trigger` |
| `mshkn_thin_pool_used_ratio` | gauge | `kind` ∈ data, metadata |
| `mshkn_host_ram_used_ratio` | gauge | — |

`timed(op)` is an async context manager that observes the histogram and increments the error counter on exception.

**Alerts.** Existing NVMe and RAM checks plus `thin_pool` data and metadata ratio checks. Alert shape unchanged.

## 11. Tests

Three tiers, each a pytest marker registered in `pyproject.toml`; `addopts` deselects `e2e` by default.

**`tests/unit/`** (pure, no I/O beyond temp files): `Resources`, `Config.from_env` (aliases, type errors), `three_way_merge` (kept from `test_merge.py`, extended with the delete-vs-modify and both-added cases), `RateLimiter` with an injected clock, starlark validation and transform, `JSONFormatter` and `RequestIdFilter`, `deliver_callback` with an injected `httpx.AsyncClient` on a mock transport and a fake sleep, `SlotAllocator` (including the 254 skip and recycling), row mappers and migrations (idempotent, executescript, indexes exist), `PoolUsage` parsing of `dmsetup status` output, `SshGuest.stream` ordering and grace drain via an injected connect factory and a fake process, `BackgroundTasks` keyed cancel/wait/drain.

**`tests/flow/`** (real ASGI app via `create_app`, `FakeHost`, temp SQLite): lifecycle create → exec → checkpoint → fork → destroy; exclusive fork with `error_on_conflict` and `defer_on_conflict`, including drain after destroy and after idle reap, and the no-double-drain guarantee; self-destruct on create and fork with callback delivered to an in-process ASGI receiver; idle reaper produces a checkpoint with `trigger=idle` and preserved label; dead reaper cleans up when `is_alive` is false; prune honors retention and pin and cancels in-flight uploads; boot failure after snap removes the volume and releases the slot; recipe build state machine including failure and dedupe by hash; ingress trigger for create and fork, sync and async, with `recipe_id`/`needs` honored and `uses` rejected; tenant isolation on every resource; exec rate limit returns 429; `mshkn_computers_active` equals the DB count after REST, ingress, and reaper changes; health subsystems; every domain error's status code.

**`tests/e2e/`** stays the live source of truth. Changes: `uses` parameter and `timed()` stub removed from helpers and all call sites; the streaming test records arrival timestamps and asserts the last line arrives at least 300 ms after the first for a 5 × 100 ms loop; the exit-code test asserts an `exit` event with `42`; the two `manifest_hash` assertions become `recipe_id`; no ingress E2E test passes `uses` or `capabilities`, so none change; all tests get type annotations. Latency thresholds are unchanged. `tests/integration/` and `e2e_test.sh` are deleted.

Coverage runs via `pytest-cov` over `src/mshkn` for unit plus flow; the floor is set to the measured value at the end of PR 5, rounded down to a whole percent, and enforced with `--cov-fail-under`.

Tests are type-checked under the same mypy strict config as `src/` and linted with the same ruff rules; the only per-file ignore is `ARG` for pytest fixtures that are used for their side effects.

## 12. Tooling and CI

- `pyproject.toml` is pure PEP 621: `[project]`, `[dependency-groups] dev`, `[tool.uv]`, ruff, mypy, pytest, coverage. `requests` is removed. `poetry.lock` is deleted; `uv.lock` is the only lock.
- `ruff format` is enforced. Ruff rules extend to `tests/` and `scripts/`.
- `.github/workflows/ci.yml` on push to `main` and on pull requests: checkout, `astral-sh/setup-uv`, `uv sync --frozen`, `uv lock --check`, `ruff check .`, `ruff format --check .`, `mypy src tests`, `pytest -m "not e2e" --cov`. Concurrency group cancels superseded runs.
- `.pre-commit-config.yaml` with ruff check and format.
- `scripts/deploy.sh`: ssh deploy (`git pull`, `uv sync --frozen`, `systemctl restart mshkn litestream`). Replaces the root-level `deploy.sh`.
- `scripts/e2e.sh`: `git push`, `scripts/deploy.sh`, orphan cleanup (firecracker processes, taps, thin devices not backed by a running computer), ensure the test account via the CLI, run `pytest tests/e2e -m e2e`, print a pass/fail summary.
- No Makefile; the commands are documented in README and CLAUDE.md.

## 13. Docs and repo hygiene

- `README.md`: rewritten to describe what exists (Firecracker microVMs, dm-thin CoW, Docker recipes, checkpoints/fork/merge, ingress, ephemeral exec), what does not (multi-host, billing, true snapshot-restore across hosts, exec log retention), how to run each test tier, and the layout.
- `docs/ARCHITECTURE.md`: request path, service responsibilities, host boundary, state ownership, lifecycle of a computer and a checkpoint, failure handling and cleanup guarantees, how to run against the fake host.
- `docs/plans/README.md`: index of every plan with status (implemented, partially implemented, superseded by X).
- `CLAUDE.md`: Telegram section removed; "Current phase" replaced with a pointer to the roadmap index; validation command becomes the CI command; E2E gate references `scripts/e2e.sh`; stale test counts and pool-recovery references to `capability_cache` removed.
- `DEPLOY.md`: adds Docker install and `docker build -t mshkn-base`, uses `uv sync --frozen`, uses the accounts CLI, removes the Nix `PATH`, notes the litestream `PartOf` dependency.
- Removed from the repo: `telegram/`, `skills/`, `skills-lock.json`, `e2e_test.sh`, root `deploy.sh` (moved to `scripts/`), `tests/integration/`, `src/mshkn/checkpoint/delta.py`, `init_thin_pool`, `create_base_volume`, `ensure_nat`, the `get_db` placeholder, `poetry.lock`. `telegram/` and `skills/` are copied to `../mshkn-devtools/` (outside git) first.
- `.gitignore` drops the telegram entries and adds `.coverage`, `htmlcov/`.

## 14. Delivery plan

Six PRs, each: `ruff check . && ruff format --check . && mypy src tests && pytest -m "not e2e"` green locally, CI green, then `scripts/e2e.sh` against the live server with no regressions versus the run before the PR, then merge requested.

1. **Tooling, CI, hygiene.** uv-only packaging, ruff/format/mypy on tests, pytest markers, CI workflow, pre-commit, delete dead code and legacy scripts, fix existing lint and typing in tests, `scripts/e2e.sh`. Behavior unchanged.
2. **Foundations.** `errors`, `resources`, `models` enums, `db/` package with pragmas and mappers, migration 010, `Config` generic mapping, `observability/` (request-id logging, metrics helpers), `BackgroundTasks`, `Runtime` and `create_app`. Existing routers adapted with minimal edits.
3. **Host boundary.** Protocols, Firecracker/dm-thin/SSH/rclone/Caddy implementations extracted from current modules, fakes, `SshGuest.stream` real streaming, `PoolUsage`. `VMManager` switched to `Host`. Flow-test harness with the first lifecycle test.
4. **Services.** `SlotAllocator`, `ComputerService`, `CheckpointService`, `lifecycle`, `IngressService`, `RecipeService`, `Reaper`; routers become thin; ingress create honors `recipe_id`/`needs`; leak-on-failure cleanup; merge off-loop; gauge from DB; deferred claim; API error mapping; `manifest_*` removed from models and responses; accounts CLI; health subsystems; pool alerts.
5. **Tests.** Full unit and flow suites, E2E cleanup, coverage floor.
6. **Docs and devtools move.** README, ARCHITECTURE, plans index, CLAUDE.md, DEPLOY.md, `../mshkn-devtools/` copy and removal.

## 15. Out of scope

- Issue 58 (exec log retention), issue 59 (`/forward`), issues 55–57 (restore path experiments), issue 34 (Telegram agent).
- Multi-host, billing, snapshot restore to a different host.
- Changing latency targets or the E2E test plan's intent.
- Table rebuilds to drop vestigial columns.

## 16. Risks

- **E2E drift.** The recipe rewrite changed the E2E suite after the last recorded full run. PR 1 establishes a fresh baseline run before any refactor lands, and every later PR is compared against it.
- **Behavior preservation during extraction.** PRs 3 and 4 move a lot of code. The fake host lets each moved flow be exercised locally before touching the server, and the E2E gate catches what fakes cannot.
- **Hidden coupling in the live server's `.env` and systemd unit.** Env aliases are preserved; the deploy script is run manually on the first PR to confirm nothing depends on the removed `deploy.sh`.
