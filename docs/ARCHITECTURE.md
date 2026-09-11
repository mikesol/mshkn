# Architecture

mshkn is one Python process on one Linux host. It owns a SQLite database, a dm-thin pool, a set of Firecracker microVMs, an SSH connection to each VM, an rclone remote for R2, and a Caddy instance for public HTTPS. This document describes how a request moves through it, who owns which state, what happens over a computer's and a checkpoint's life, and what is guaranteed on failure.

Dependency direction is strict: `mshkn.api` → `mshkn.services` → `mshkn.host` and `mshkn.db`. `mshkn.models`, `mshkn.errors`, `mshkn.config`, `mshkn.resources` and `mshkn.observability` are leaves that import nothing from mshkn but `mshkn.errors`. Nothing in `services`, `host` or `db` imports `api`, and nothing in `host` imports `db` or `services`.

## 1. Request path

`systemd/mshkn.service` runs `uvicorn mshkn.main:app`. `src/mshkn/main.py` calls `mshkn.app.create_app`, whose lifespan builds a `mshkn.runtime.Runtime` from the environment (`Runtime.from_env`: connect, migrate, wire the production host), starts it, and closes it on shutdown.

A request passes through:

1. The request-id middleware in `src/mshkn/app.py`: it takes `X-Request-Id` or mints one, stores it in the logging contextvar so every log line carries it, and echoes it in the response.
2. `mshkn.api.deps.require_principal`: `Authorization: Bearer <secret>` is looked up in `accounts.api_key` first and then in `api_keys.secret`, and resolves to a `mshkn.models.Principal` (the account, plus the scoped key when the bearer was one); a missing or unknown secret is 401. Routes a scoped key may never call depend on `mshkn.api.deps.require_account_key` instead and answer 403 to one. The unauthenticated routes are the ingress trigger and the three system endpoints (`GET /health`, `GET /metrics`, `GET /alerts`).
3. A router in `src/mshkn/api/`. Handlers resolve the runtime (`mshkn.api.deps.get_runtime`), run the scope checks of `src/mshkn/api/scopes.py` (§1a), call one service method, and shape the result with a model from `src/mshkn/api/schemas.py`. Orchestration does not live in routers.
4. A service in `src/mshkn/services/`, which talks to the host boundary and the database.

Domain errors are mapped in one place, `src/mshkn/api/errors.py`, which handles `mshkn.errors.MshknError` and its subclasses:

| Exception (`mshkn.errors`) | Status | Body |
|---|---|---|
| `NotFound` | 404 | `{"detail": <message>}` |
| `Forbidden` | 403 | message naming the scope that stopped a scoped key |
| `Conflict` | 409 | message |
| `BadRequest` | 400 | message (for example exec on a computer that is not running) |
| `InvalidInput` | 422 | the `detail` payload when the error carries one (validation lists), else the message |
| `PayloadTooLarge` | 413 | message |
| `LimitExceeded` | 429 | message (VM limit, slots, ingress rate limit) |
| `TransformError` | 502 | the Starlark error |
| `HostError` | 502 | always `{"detail": "host operation failed"}`; the cause is logged, never returned |
| any other `MshknError` (`ConfigError`) | 500 | `{"detail": "internal error"}` |

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
| `GET /computers/{computer_id}/exec_log` | `Lifecycle.exec_log`: the record of the exec that ran on create or fork, kept after the computer is gone |
| `POST /computers/{computer_id}/exec/kill/{pid}` | `ComputerService.exec_kill` |
| `POST /computers/{computer_id}/upload` | `ComputerService.upload` (`?path=`, raw body) |
| `GET /computers/{computer_id}/download` | `ComputerService.download` (`?path=`) |
| `POST /computers/{computer_id}/checkpoint` | `CheckpointService.create` with trigger `api` |
| `GET /checkpoints` | `CheckpointService.list` (`?label=`) |
| `DELETE /checkpoints/{checkpoint_id}` | `CheckpointService.delete` |
| `POST /checkpoints/{checkpoint_id}/fork` | `CheckpointService.fork_or_defer` then `Lifecycle.run_ephemeral` |
| `POST /checkpoints/fork` | `CheckpointService.fork_by_label` (the body carries `label` plus the fork fields) then `Lifecycle.run_ephemeral` |
| `POST /checkpoints/{parent_id}/merge` | `CheckpointService.merge` |
| `POST /recipes`, `GET /recipes`, `GET /recipes/{recipe_id}`, `DELETE /recipes/{recipe_id}` | `RecipeService.create` (builds in the background), `list`, `get`, `delete` |
| `POST /ingress_rules`, `GET /ingress_rules`, `GET /ingress_rules/{rule_id}`, `PUT /ingress_rules/{rule_id}`, `DELETE /ingress_rules/{rule_id}` | `IngressService` rule CRUD |
| `POST /ingress_rules/{rule_id}/rotate` | new public id for the rule |
| `POST /ingress_rules/{rule_id}/test` | dry-run the transform against a synthetic request |
| `GET /ingress_rules/{rule_id}/logs` | recent trigger outcomes |
| `GET /ingress/{rule_id}`, `POST /ingress/{rule_id}`, `PUT /ingress/{rule_id}`, `PATCH /ingress/{rule_id}` | the unauthenticated trigger: parse the body, `IngressService.trigger` |
| `POST /keys`, `GET /keys`, `DELETE /keys/{key_id}` | `KeyService.create` (the secret is returned once), `list` (never the secret), `delete`; account key only |
| `GET /health` | subsystem checks |
| `GET /metrics` | Prometheus exposition |
| `GET /alerts` | the runtime's alert deque |

### 1a. Tenancy: two kinds of credential

An account has one unrestricted credential, `accounts.api_key`, created with `python -m mshkn accounts create`. It can also mint any number of **scoped keys** (`api_keys` table, `migrations/012_api_keys.sql`, `mshkn.services.keys.KeyService`), each carrying a scope document that says what the key may do. A scoped key exists so that a credential can live somewhere less trusted than the operator's shell (the embryo's brain VM holds one): compromise of everything that key can reach must not confer powers the account did not grant.

The scope document (`mshkn.models.Scopes`, parsed by `mshkn.models.parse_scopes`, which rejects unknown fields with 422) has three optional fields; an absent field means none:

```json
{"recipes": {"create": true, "read": true}, "computers": {"create_from": "*"}, "labels": ["verb/"]}
```

| Route | Account key | Scoped key |
|---|---|---|
| `POST /recipes` | always | `recipes.create` |
| `GET /recipes`, `GET /recipes/{recipe_id}` | always | `recipes.read` |
| `DELETE /recipes/{recipe_id}` | always | never |
| `POST /computers` | always | the `recipe_id` (or `"bare"` for none) must be allowed by `computers.create_from` (`"*"` is any recipe on the account, never bare); a `label` must start with one of `labels`. The computer records the key in `computers.api_key_id` |
| every other `/computers/{computer_id}/…` route | always | only when `computers.api_key_id` is this key; a checkpoint `label` must start with one of `labels` |
| `GET /checkpoints` | everything | only checkpoints whose label starts with one of `labels`; an unlabelled checkpoint is never visible |
| `POST /checkpoints/{checkpoint_id}/fork`, `DELETE /checkpoints/{checkpoint_id}` | always | the checkpoint's label must start with one of `labels`; a fork records the key on the new computer |
| `POST /checkpoints/fork` | always | the body's `label` must start with one of `labels`, checked before the head is resolved; the fork records the key |
| `POST /relay` | always; `deliver` optional | the `relay` scope: `target` must lie under one of `relay.targets` (same scheme, host and port, path under the prefix's), and `deliver` must be absent (the scope pins it). The job records the key |
| `GET /relay/{job_id}` | any job on the account | only a job this key created |
| `POST /checkpoints/{parent_id}/merge`, `/ingress_rules*`, `/keys*` | always | never |

A request outside scope is a 403 raised in the router before any service runs, with a detail naming the scope. Ownership by account (404 for another account's resource) is checked first, so a scoped key learns nothing about other accounts' ids. `GET /alerts` is unauthenticated and stays so: a scoped key sees there what anyone sees. A computer the deferred drain forks on a label's behalf belongs to the account, not to any key.

## 2. Runtime and wiring

`mshkn.runtime.Runtime` is the composition root. `Runtime.build(config, db, host, *, http)` constructs, in dependency order: `mshkn.runtime.BackgroundTasks`, `SlotAllocator`, the HTTP client, the alert deque, `RecipeService`, `ComputerService`, `CheckpointService`, `Lifecycle`, `IngressService`, `RelayService`, `KeyService`, `Reaper`, and the exec `RateLimiter` (80 requests per 10 seconds per API key, checked on the exec endpoint only). The `http` client is what callbacks are delivered with; tests pass one whose transport is an in-process receiver.

`Runtime.start()`: `SlotAllocator.initialize` (derive free slots and the next volume id from the database and the pool), `ComputerService.resume_teardowns` (finish any teardown a previous process claimed but did not complete), `Reaper.reap_dead` once, `RelayService.resume` (re-run every relay job still `queued` or `in_progress`), refresh the active-computers gauge from the database, spawn `Reaper.run` under the task key `reaper`.

`Runtime.close()`: cancel the reaper, drain background tasks (checkpoint uploads, callbacks, deferred drains, recipe builds) with a timeout, close the HTTP client, the SSH pool, the Caddy client, then the database.

`BackgroundTasks` names every task and lets a caller cancel or await one by key; checkpoint uploads use the key `upload:<checkpoint id>` so that deleting a checkpoint can cancel its upload first.

The production host is built once by `mshkn.host.firecracker_host.firecracker_host`: exactly one `FirecrackerHypervisor` exists per process because the staging slot is host-global and its lock is an instance attribute.

## 3. Host boundary

`src/mshkn/host/__init__.py` defines five protocols and the `Host` container. Services depend only on these.

| Protocol | Methods | Production | Fake |
|---|---|---|---|
| `Hypervisor` | `boot`, `restore`, `snapshot`, `build_template`, `kill`, `is_alive`, `teardown_slot` | `mshkn.host.firecracker.FirecrackerHypervisor` | `FakeHypervisor` |
| `BlockStore` | `snap`, `activate`, `deactivate`, `remove`, `mkfs`, `mounted`, `max_volume_id`, `usage` | `mshkn.host.dmthin.DmThinBlockStore` | `FakeBlockStore` (a temp directory per volume, reached through stable per-device mounts) |
| `Guest` | `exec`, `stream`, `exec_bg`, `upload`, `download`, `metrics`, `warm`, `evict`, `close` | `mshkn.host.ssh.SshGuest` (connection pool, real line streaming) | `FakeGuest` (scripted output, minted pids from 4000) |
| `ObjectStore` | `upload_dir`, `download_dir`, `delete_prefix` | `mshkn.host.r2.RcloneObjectStore` | `FakeObjectStore` |
| `Proxy` | `add_route`, `remove_route`, `healthy`, `close` | `mshkn.host.caddy.CaddyProxy` (admin API) | `FakeProxy` |

Shared result types live beside the protocols: `RunningVM(pid, socket_path, slot, vm_ip, tap_device)`, `SnapshotFiles`, `ExecResult(exit_code, stdout, stderr)`, `VmMetrics`, `PoolUsage`.

`mshkn.host.fake.FakeHost()` returns a `FakeHostInstance` with all five fakes; it records calls, can be told to fail a named operation, and cleans its temp directories on `close()`.

`src/mshkn/host/shell.py` (`run`, `ShellError`) is where the host layer executes shell command strings: the dm-thin, rclone, tap and staging paths build their commands and hand them to it. Outside it, `start_firecracker_process` spawns the Firecracker binary directly and the recipe build runs `docker build` through its own shell call. `src/mshkn/host/network.py` owns the slot arithmetic and `create_tap` / `destroy_tap`.

### Firecracker specifics

A VM is started as a `firecracker --api-sock /tmp/fc-<disk name>.socket` process and configured over that Unix socket (`FirecrackerClient`). Every boot and every restore goes through a staging pass, because a Firecracker snapshot bakes in the tap and MAC it was taken with: the disk is mapped as `mshkn-restore-staging` on slot 254 (`tap254`, `172.16.254.2`, MAC `06:00:AC:10:FE:02`), the machine is booted or the snapshot loaded there, the guest's clock is set to the host's and it is given its final address over SSH (a restored snapshot keeps the time it was taken at), and then the staging tap is renamed to the final tap and the mapping renamed to the computer's volume name in one shell call. The staging address is left on the guest on purpose: a checkpoint snapshots the guest's memory as it is, so a restore is only reachable on the staging slot if `172.16.254.2` is still configured in there, which is also why a guest lists the staging address first and a fork carries its parent's — a computer's address comes from the API, never from the guest. One staging pass runs at a time (`_staging_lock`). The staging slot is cleaned before a pass only when something may be on it: on the first pass after start-up and after a failed pass; a successful pass renames both the tap and the mapping away. `build_template` cold-boots on the staging slot, completes one SSH session so the snapshot holds a settled sshd rather than one that has merely started accepting connections, snapshots it, and tears the staging resources down. `kill` sends SIGKILL, waits on a pidfd for the process to exit and be reaped, and unlinks the API socket it recorded for that pid.

## 4. Services

| Service | Owns |
|---|---|
| `mshkn.services.allocator.SlotAllocator` | Free slot set and next thin-volume id, both derived at start from the database (every non-destroyed row holds its slot) and `BlockStore.max_volume_id`; `acquire`, `acquire_volume_id`, `release_slot` under one lock. `release_slot` refuses, with a warning, a slot that is not held. |
| `mshkn.services.computers.ComputerService` | `create`, `fork`, `destroy`, `cleanup_dead`, `resume_teardowns`, guest operations (`exec`, `stream`, `exec_bg`, `exec_logs`, `exec_kill`, `upload`, `download`, `metrics`), ownership checks (`get_owned`, `get_running`, `get_record`), the active gauge. |
| `mshkn.services.checkpoints.CheckpointService` | `create` (the single implementation for API, self-destruct and idle triggers), `delete`, `prune`, `merge`, `fork_by_label` and `fork_or_defer` (admission to a labelled chain under a per-label lock), `latest_for_label`, `source_label`. |
| `mshkn.services.lifecycle.Lifecycle` | `run_ephemeral` (exec → exec-log row → optional self-destruct checkpoint → destroy → callback → drain), `exec_log`, `expire_exec_logs`, `drain_after_destroy`, `drain_deferred`. |
| `mshkn.services.recipes.RecipeService` | Recipe rows and the build pipeline (`docker build` → export → thin volume → template snapshot), serialised per account and de-duplicated per template key. |
| `mshkn.services.ingress.IngressService` | Rule CRUD, `enabled_rule`, `trigger` (Starlark transform → `ForkAction`/`CreateAction` → outcome), per-rule rate limiters, trigger logs (one row per trigger, brought to its final status and the computer it ran on). |
| `mshkn.services.relay.RelayService` | Relay jobs (#110): `submit` (the SSRF guard, the caps, a row, a background task), `run` (the upstream call with lampas's retry policy, headers cleared when it settles, a streamed body reassembled by `mshkn.services.sse`), `deliver` (a fork by label with the job id, deferred behind a running fork), `get_owned`, `resume` (re-run after a restart). `mshkn.services.ssrf` is the guard. |
| `mshkn.services.keys.KeyService` | Scoped keys (§1a): `create` (mints the id and the secret), `list`, `delete`. |
| `mshkn.services.reaper.Reaper` | `reap_dead`, `reap_idle`, prune, exec-log expiry, `check_host` (pool, root filesystem and RAM thresholds), the 60-second cycle. |
| `mshkn.services.merge` | `three_way_merge`, a function over three directory trees that writes a fourth. |
| `mshkn.services.callback.deliver_callback` | POST with retries and backoff; never raises. |
| `mshkn.services.starlark` | Validation and execution of transform source in the `starlark_go` sandbox. |

## 5. State ownership

**Durable (SQLite, `src/mshkn/db/`).** One file, one connection shared by every service and the reaper, opened by `connect` in `src/mshkn/db/__init__.py` in autocommit mode with `PRAGMA busy_timeout=5000`, `journal_mode=WAL` and `synchronous=NORMAL`. Autocommit means every statement is its own transaction: a write that loses the lock leaves nothing open, so a later read cannot pin a WAL snapshot that an outside commit (Litestream's, once a second) would make stale and refuse every later write until restart. Request-time writes are single statements; the one multi-statement unit, a migration and its ledger row, runs under `transaction` at startup, when nothing else shares the connection. Tables: `accounts`, `api_keys`, `computers`, `checkpoints`, `recipes`, `snapshot_templates`, `deferred_queue`, `ingress_rules`, `ingress_log`, `exec_log`, `relay_jobs` (forwarded headers in their own column, cleared when the call settles), plus `_migrations` (applied migration names). `capability_cache`, from `migrations/001_initial.sql` and rebuilt by `migrations/004_capability_cache_volume_id.sql`, still exists in the schema but no code reads or writes it; `migrations/009_recipes.sql` replaced what used it without dropping the table. Migrations in `migrations/` are sequential and applied once each, in name order. Litestream replicates the file to R2. Each `db/` module holds one table's column tuple, one row mapper and its queries, ingress rules and their log sharing one; there is no ORM.

**Durable (disk).** The thin pool `mshkn-pool` with base volume 0 (the export of the `mshkn-base` image, written by `python -m mshkn base-volume`), one volume per computer (`mshkn-<computer id>`), one per checkpoint (`mshkn-ckpt-<checkpoint id>`), and one per recipe base (`mshkn-recipe-<recipe id>`). Checkpoint snapshot files under `checkpoint_local_dir/<checkpoint id>/` (`vmstate`, `memory`), mirrored to R2 under `<account id>/<checkpoint id>/`, and template snapshots under `checkpoint_local_dir/templates/<key>/`. A checkpoint's files are written first under `checkpoint_staging_dir/<checkpoint id>/` (tmpfs, `/dev/shm/mshkn` by default), because Firecracker fsyncs the memory file; the upload task copies them into the durable directory by rename, uploads, and removes the staging copy after a linger. A fork looks in the durable directory, then the staging one, then R2.

**Process-local (rebuilt at start).** The allocator's free-slot set and next volume id; the SSH connection pool; the hypervisor's staging lock and its pid-to-socket registry; the per-label fork locks in `CheckpointService`; ingress rate limiters (keyed by internal rule id); the exec rate limiter; the alert deque; background tasks. A restart loses in-flight uploads and callbacks (the reaper and the next checkpoint create recover the rest).

**Kernel and daemons.** Tap devices, dm-thin mappings, Firecracker processes, Caddy routes. `Runtime.start` finishes interrupted teardowns, reaps computers whose Firecracker process is gone and re-derives slots and volume ids from the database and the pool; a resource with no database row is not reclaimed, so on the test host `scripts/e2e.sh` clears whatever a previous run left behind.

## 6. Lifecycle of a computer

**Create** (`ComputerService.create`, timed as `op="create"`):

1. Reject if the account is at its `vm_limit` (`LimitExceeded`).
2. Resolve the recipe if given; its base volume is the source, else volume 0.
3. `_bring_up`: acquire a slot and a volume id; `BlockStore.snap` the source into the new volume (on failure, release the slot and re-raise).
4. If a template snapshot exists for this recipe, `Hypervisor.restore` it (timed `op="restore"`); otherwise `Hypervisor.boot` (timed `op="boot"`). A request with non-default resources always cold-boots.
5. The `computers` row is inserted with status `running`; then `Guest.warm` opens the SSH connection and `Proxy.add_route` publishes `*-<computer id>.<domain>` to the VM's address, together, since both depend only on the address.
6. The active gauge is refreshed from the database.

**Fork** (`ComputerService.fork`, timed `op="fork"`) is the same `_bring_up` with the checkpoint's thin volume as the source and its snapshot files as the restore input, always at the default resources; the checkpoint's recipe carries over unless the request names another. A checkpoint whose snapshot files are gone locally is fetched from R2, and cold-boots if that fails too.

**Exec.** `exec`, `stream` and `exec_bg` touch `last_exec_at`; the log, kill, upload, download and metrics calls do not. `stream` yields `(stream, line)` pairs as the SSH session produces them; the router turns them into `stdout`, `stderr` and a final `exit` event, and any failure becomes an `error` event followed by `exit 255`. `timeout_seconds` on the request (60 by default, 1 to 600) bounds the command: a process still running at the deadline is killed and its exit event is 137. The kill happens inside the guest (`timeout --preserve-status -s KILL`), because sshd ignores the SSH signal request, so that 137 arrives as an ordinary exit status; the host's own deadline, five seconds later, is the backstop, and a kill it lands reports 128 plus the signal instead. A process that ends with neither a status nor a signal reports 255, so a killed command never reads as a success.

**Destroy** (`ComputerService.destroy`, timed `op="destroy"`): claim the teardown, then remove the Caddy route, kill the VM (which unlinks its API socket) and evict the SSH connection together; then remove the thin volume and tear down the tap together; then mark the row `destroyed`, refresh the gauge, and release the slot last. The claim is one statement, `UPDATE computers SET status = 'destroying' WHERE id = ? AND status = 'running'` (`claim_teardown`), and only the caller whose update changed the row runs the pass: a destroy that finds the computer already `destroying` or `destroyed` returns at once and leaves the work to the owner. Every step of the pass runs whatever the earlier ones did, so a claimed computer always ends `destroyed` with its slot released; the first failing step is re-raised as `HostError` afterwards, so the caller and the error metric see it. A computer that is `destroying` is still listed by its owner, counts towards the VM limit, and rejects guest operations.

**Dead VM** (`Reaper.reap_dead` → `ComputerService.cleanup_dead`): when a running computer's Firecracker pid is gone, the same claim and the same pass run, tolerant of resources that are already gone. The reaper works from a snapshot of the running computers, so a destroy can land in between; `cleanup_dead` then loses the claim, returns `False` without touching the host, and is not counted as a reap. The reaper catches a failure per computer, so one broken computer does not block the others.

**Interrupted teardown** (`ComputerService.resume_teardowns`): a row still `destroying` when the process starts belongs to a teardown the previous process claimed and did not finish. `Runtime.start` runs the pass for each such row before the reaper starts and before any request is served, which is why no claim is needed then.

**Idle** (`Reaper.reap_idle`): a running computer whose `last_exec_at` (or `created_at`) is older than `idle_timeout_seconds` is checkpointed with trigger `idle` and label `auto-idle-timeout` (or its chain's label), then destroyed and its label drained.

**Ephemeral runs and their record** (`Lifecycle.run_ephemeral`): a create or fork that carries `exec` runs it through `ComputerService.exec` and writes one `exec_log` row keyed by the computer id (`src/mshkn/db/exec_log.py`): the command, exit code, stdout and stderr, the checkpoint it was forked from and the chain's label. The row is written before the self-destruct checkpoint is taken, so a checkpoint that fails does not also lose the output that led to it, and it is updated with `created_checkpoint_id` once the checkpoint exists. Stored stdout and stderr are each bounded to 8 KiB (`mshkn.services.lifecycle.EXEC_LOG_OUTPUT_BYTES`): past that the head and the tail are kept around a marker naming the bytes dropped, and the row's `stdout_truncated` / `stderr_truncated` flags say so; the response and the callback still carry the whole output. `GET /computers/{computer_id}/exec_log` returns the row to its owner whether the computer is alive or gone, which is how an agent reads what an ephemeral turn did: a checkpoint's `computer_id` in `GET /checkpoints` names the row, as does an ingress log entry's `computer_id`. The reaper deletes rows older than `exec_log_retention_seconds` (24 hours by default; 0 keeps them).

## 7. Lifecycle of a checkpoint

**Create** (`CheckpointService.create`, timed `op="checkpoint"`): `sync` inside the guest so the page cache reaches the block device; `Hypervisor.snapshot` (pause, write `vmstate` and `memory` onto the staging directory, resume) and, alongside it, acquire a volume id, snap the computer's volume into a new checkpoint volume and activate it; the pooled SSH session is kept across the pause; insert the row with `parent_id` = the computer's latest checkpoint, else the checkpoint it was forked from; count `mshkn_checkpoints_total{trigger}`; spawn the persist-and-upload task under key `upload:<checkpoint id>` (uploads run one at a time, under `nice` and `ionice`).

**Delete** cancels the upload task first, then removes the thin volume, the local directory and the R2 prefix, then the row.

**Prune** (`Reaper` cycle, `list_prunable_checkpoints` in `src/mshkn/db/checkpoints.py`) never deletes a pinned checkpoint or the newest checkpoint of any label on the account; of the rest it keeps the newest `checkpoint_retention_count` per account and deletes everything older, oldest first. A labelled chain is therefore durable by construction: its history is pruned, its head never is, and a chain is removed by deleting its checkpoints by label. Deleting a checkpoint leaves its computer's `exec_log` row alone; that row goes on its own clock (`exec_log_retention_seconds`).

**Fork by label, fork or defer** (`CheckpointService.fork_by_label`, `CheckpointService.fork_or_defer`): admission to a labelled chain is one operation under an `asyncio.Lock` per `(account, label)`, held from resolving the head through the active-computer check to the computer row's insert and released before the exec runs. `fork_by_label` resolves the newest checkpoint carrying the label (`NotFound` if none) and admits the fork against it; `fork_or_defer` admits a fork of a checkpoint given by id and takes the same lock when that checkpoint is labelled, so a fork by id cannot slip past a fork by label. Admission with `exclusive` set: an active computer on the label means either `Conflict` (`error_on_conflict`) or a row in `deferred_queue` and a `Deferred` result (`defer_on_conflict`); otherwise it is a plain fork. The lock is what makes "advance the head of X" atomic: two callers arriving together cannot both pass the check before either has a computer row, and a caller cannot fork a head that a concurrent fork has already replaced. The locks are process-local, which is correct because the server is one asyncio process.

**Drain** (`Lifecycle.drain_deferred`): after a self-destruct or destroy on a labelled chain, one background task claims every queued request for the label atomically (`DELETE … RETURNING`), forks one computer from the newest checkpoint carrying the label through `fork_by_label`, writes each queued `exec` to `/tmp/exec/N.txt`, and runs the last `meta_exec` if any, else the queued commands joined by newlines. A claim that finds nothing ends the drain; a drain that produces another self-destruct spawns the next.

**Merge** (`CheckpointService.merge`): both forks must be children of the named parent, or the request is a 400. The parent and both forks are mounted read-only alongside a fresh volume snapped from the parent; `three_way_merge` runs in a worker thread and its output is copied onto that volume; the result is a new checkpoint labelled `merge` whose response lists conflicts. Symlinks are compared and reproduced as links and never followed, so an absolute link on a volume — a guest rootfs is full of them — can neither be read through nor written through onto the host's own tree.

## 8. Recipes and templates

A recipe is a Dockerfile whose final stage must be `FROM mshkn-base` (the image built from `Dockerfile.mshkn-base` by `python -m mshkn base-volume`, `DEPLOY.md` §6, which also writes that image's export into volume 0 so bare and recipe computers share one filesystem); `RecipeService.create` reads the last `FROM` (`mshkn.services.recipes.dockerfile_base_image`) and rejects any other base with `InvalidInput` before a row or a build exists, and nothing verifies the result boots until the first computer is created from it. Two identical Dockerfiles for one account resolve to the same recipe by content hash, and one account's builds run one at a time. `RecipeService.build` runs `docker build` with a 4 GB memory limit on two CPUs and a ten-minute timeout (the build is killed on timeout), exports the container filesystem (`mshkn.services.recipes.export_image`), unpacks it into a thin volume snapped from volume 0 and post-processes it for Firecracker (init symlink, network unit, SSH keys, and the `hostname`, `hosts` and `resolv.conf` that Docker bind-mounts over and the export therefore leaves empty) in a worker thread (`mshkn.services.recipes.inject_tar`, the same step `mshkn.services.base_volume.write_base_volume` uses for volume 0); that volume becomes the recipe's base. The built image (`mshkn-recipe-img-<recipe id>`) is kept: the host's Docker uses the legacy builder, whose cache is the image layer chain, so a recipe that appends a layer to an existing recipe's text rebuilds only that layer. `RecipeService.delete` removes the image with the volume, and a failed build removes its own.

The first create at the default resources for a recipe (or for the bare base) triggers `Hypervisor.build_template`: a VM is cold-booted on the staging slot and snapshotted, so later creates restore in the fork path's time instead of cold-booting. A recipe's template paths are stored on its own row; the bare template is the single row in `snapshot_templates`. Template builds are de-duplicated per key, so concurrent first callers share one, and a failed template build logs a warning and cold-boots.

## 9. Ingress

An ingress rule has a Starlark `transform(request)` returning either `{"action": "fork", "checkpoint_id": ..., "exec": ..., ...}` (`checkpoint_id` or `label` is required) or `{"action": "create", "recipe_id": ..., "needs": ..., "exec": ..., ...}`, or nothing. `POST /ingress_rules` validates the source by executing it and requiring a `transform` global. A trigger request is parsed into the dict the transform sees (method, path, headers, query, JSON or form body, raw body) with the rule's `max_body_bytes` enforced on both the declared and the streamed length (413), then rate-limited per rule over a one-minute window (429), transformed (`TransformError` → 502), validated against `ForkAction`/`CreateAction` (`extra=forbid`), and executed. A transform that returns nothing gives 204. `response_mode` `sync` returns the same body as the REST fork or create; `async` runs the action in the background and returns 202 with an `accepted` marker; a deferred fork returns the deferred id with `queued`. Every transform that runs writes one `ingress_log` row: for a sync action it records the final status and the `computer_id` the action ran on; for an async action the row is written as `accepted` before the task starts and the task brings that same row to `completed` (with its `computer_id`) or `failed` (with the error), so a trigger is one row whose status is its outcome. A deferred fork has no computer yet. A request rejected before the transform — unknown rule, oversized body, rate limit — writes none.

## 9a. The relay

`mshkn.services.relay.RelayService` (#110) makes an HTTP call on a caller's behalf in the background and, when it settles, wakes a labelled chain by forking it. It is what lets a turn of the embryo (`docs/superpowers/specs/2026-09-09-async-turn-relay-design.md`) outlive the fork that started it: the caller posts a request and its own wake-up, and gets an acknowledgement back at once.

**The job.** A job (`relay_jobs`, one row) is one upstream HTTP call plus at most one delivery: `target`, `method`, `forward_headers` (sent upstream only, never returned by any route), a JSON or string `body`, a `retry` policy (attempts, exponential backoff), a `timeout_seconds` for the whole call, and an optional `deliver` (the label to fork and the exec to run, the job id appended as its last argument). Status moves `queued` → `in_progress` → `completed` (an upstream response was received, whatever its status code) or `failed` (no response after the attempts, or a final failure); a transport error, `408`, `409`, `429` or any `5xx` is retried with the job's policy, a timeout or an oversized body is final at once.

**The guard.** `mshkn.services.ssrf` allows only `http` and `https`, resolves a hostname through an injectable resolver (A and AAAA) and checks every address, unwrapping IPv4-mapped IPv6; a raw or resolved address in `0.0.0.0/8`, `10.0.0.0/8`, `127.0.0.0/8`, `169.254.0.0/16`, `172.16.0.0/12`, `192.168.0.0/16`, `::1/128`, `fc00::/7` or `fe80::/10` is refused, as is a hostname that does not resolve at all (fail closed, unlike lampas's fail-open behind Cloudflare's own guard). The guard runs at `POST /relay` and answers 422 naming the reason.

**The response.** Stored whole: `status`, `headers`, `body`. An upstream `text/event-stream` body is read to the end and reassembled by `mshkn.services.sse` into the final message (each content block, cumulative usage, `message_delta`, `ping` and unknown types ignored) before it is stored; any other body is stored verbatim, parsed as JSON when the content type says so.

**Delivery.** When the job settles and it has a `deliver`, the relay forks the pinned label with the pinned exec plus the job id (`CheckpointService.fork_by_label`, `self_destruct: true`), `defer_on_conflict` against a running fork so a busy chain queues the wake-up instead of losing it, as the account (no key is recorded on the fork). Delivery status moves `pending` → `delivered` (a computer ran it, or the deferred queue holds it) or `failed` after the job's retries are spent; a failed delivery never loses the upstream result, since the brain's next command settles a completed job on its own.

**Restart and expiry.** `Runtime.start` calls `RelayService.resume`, which re-runs every job still `queued` or `in_progress`: their headers are still stored and the brain has seen nothing, so a repeated model call costs less than a turn that never closes. The reaper expires jobs, responses included, on `exec_log_retention_seconds`, in the same cycle as exec logs.

## 10. Networking

Slot N gives host address `172.16.N.1`, VM address `172.16.N.2`, tap `tapN` and MAC `06:00:AC:10:NN:02` (NN in hex). Egress is host NAT: `scripts/mshkn-pool-up` masquerades `172.16.0.0/12` out of the default interface and re-allows return traffic to `tap+`, which Docker's `FORWARD` DROP policy would otherwise eat; each tap gets a `FORWARD` pair that accepts its VM's traffic to the outside and drops it towards other VMs. Inbound traffic reaches a VM only through Caddy: `CaddyProxy.add_route` creates a route with id `route-<computer id>` matching `*-<computer id>.<domain>`, and the request's port prefix selects the VM port. Slot 254 is reserved for staging.

## 11. Failure handling and cleanup guarantees

- **Bring-up is all or nothing.** Any failure after the disk snap in `_bring_up` runs `_abandon`: route removal, kill and evict if a VM was started, volume removal, tap teardown, status update, gauge refresh, slot release, each best-effort and logged, then the error is re-raised (`HostError` unless it already was a domain error or a cancellation). Cancellation mid-bring-up completes the abandon before propagating.
- **Every host failure is a `HostError`** with the cause logged; callers never see transport exceptions and clients never see internals.
- **Timeouts kill what they abandon.** A Firecracker process whose socket never appears is killed; a `docker build` past its deadline is killed; a stream's command is killed by the guest's `timeout` at its `timeout_seconds`, and by the host five seconds later if that fails.
- **Checkpoint upload and delete do not race.** Delete cancels the upload task by key before removing files.
- **Deferred requests are claimed once.** The drain's `DELETE … RETURNING` makes two drains on one label safe.
- **Dead VMs are reaped**, at startup and every reaper cycle, tolerant of resources that are already gone, and the reaper catches a failure per computer so one broken computer does not block the others.
- **One teardown per computer.** `destroy`, `cleanup_dead` and the idle reap all go through `claim_teardown`; the loser of the race does nothing. `SlotAllocator.release_slot` refuses a slot that is not held, so even a stray second release cannot hand one slot to two VMs.
- **Relay jobs survive a restart.** Unsettled jobs are re-run at start; their headers are still stored and the brain has seen nothing.
- **Known gaps** are tracked as issues: #66 (an abandoned bring-up can leave a Firecracker that had already spawned), #67 (the socket registry is process-local).

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

`GET /health` reports `database`, `firecracker`, `storage` and `proxy` as `ok` or an error string, with overall `ok` or `degraded`. The database check reads, then reports the reaper's count of consecutive failed cycles (`Reaper.consecutive_failures`, with the last exception) when it is not zero: the reaper writes every cycle, so its failing is what a database that answers reads but refuses writes looks like (always HTTP 200, so a caller has to read `status` in the body; `scripts/e2e.sh` only waits for the endpoint to answer before running the suite). `GET /alerts` returns the reaper's recent alerts: thin pool data or metadata over 80 % (warning) or 95 % (critical), root filesystem usage over 80 % (critical over 95 %), and host RAM over 90 %.

## 13. Configuration

`mshkn.config.Config` is a frozen dataclass; `Config.from_env` reads `MSHKN_<FIELD>` for every field, parsing by the field's type, and honours the older aliases `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `MSHKN_IDLE_TIMEOUT` and `MSHKN_CHECKPOINT_RETENTION`, which win over the generic names. A malformed value raises `ConfigError` naming the variable.

| Field | Default | Variable |
|---|---|---|
| `host`, `port` | `0.0.0.0`, `8000` | `MSHKN_HOST`, `MSHKN_PORT` |
| `db_path` | `/opt/mshkn/mshkn.db` | `MSHKN_DB_PATH` |
| `migrations_dir` | `migrations` | `MSHKN_MIGRATIONS_DIR` |
| `kernel_path` | `/opt/firecracker/vmlinux.bin` | `MSHKN_KERNEL_PATH` |
| `checkpoint_local_dir` | `/opt/mshkn/checkpoints` | `MSHKN_CHECKPOINT_LOCAL_DIR` |
| `checkpoint_staging_dir` | `/dev/shm/mshkn` | `MSHKN_CHECKPOINT_STAGING_DIR` |
| `ssh_key_path` | `/root/.ssh/id_ed25519` | `MSHKN_SSH_KEY_PATH` |
| `thin_pool_name`, `thin_volume_sectors` | `mshkn-pool`, `16777216` (8 GiB) | `MSHKN_THIN_POOL_NAME`, `MSHKN_THIN_VOLUME_SECTORS` |
| `thin_pool_data_path`, `thin_pool_meta_path`, `thin_pool_data_size_gb` | `/opt/mshkn/thin-pool-data`, `/opt/mshkn/thin-pool-meta`, `100` | `MSHKN_THIN_POOL_DATA_PATH`, `MSHKN_THIN_POOL_META_PATH`, `MSHKN_THIN_POOL_DATA_SIZE_GB` |
| `r2_bucket`, `r2_endpoint`, `r2_access_key_id`, `r2_secret_access_key` | `mshkn-checkpoints`, empty | `R2_BUCKET`, `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` |
| `idle_timeout_seconds` | `1800` | `MSHKN_IDLE_TIMEOUT` |
| `checkpoint_retention_count` | `20` | `MSHKN_CHECKPOINT_RETENTION` |
| `exec_log_retention_seconds` | `86400` | `MSHKN_EXEC_LOG_RETENTION_SECONDS` |
| `domain` | `mshkn.dev` | `MSHKN_DOMAIN` |
| `caddy_admin_url` | `http://localhost:2019` | `MSHKN_CADDY_ADMIN_URL` |
| `relay_timeout_seconds` | `3600` | `MSHKN_RELAY_TIMEOUT_SECONDS` |
| `relay_body_bytes` | `8388608` | `MSHKN_RELAY_BODY_BYTES` |

Resources per computer come from the request, not the environment: `mshkn.resources.Resources.from_needs` parses `{"ram": "512MB", "cores": 2}` with bounds of 128 MiB to 32 GiB and 1 to 16 vCPUs, defaulting to 256 MiB and 2 vCPUs.

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

`tests/flow/conftest.py` does exactly this, adds two accounts, points the callback client at an in-process receiver, and closes everything after each test. The fakes expose their state (`host.hypervisor.alive`, `host.blocks.mounts`, `host.proxy.routes`, `host.guest.evicted`) so a test asserts on outcomes, not on mocks. Unit tests of a host module (`tests/unit/test_firecracker_stage.py`, `tests/unit/test_dmthin.py`, `tests/unit/test_ssh_guest.py`) exercise the real command chains against recorders and fake binaries instead.
