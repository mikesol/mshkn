# Orchestrator Design

**Date:** 2026-03-08
**Status:** Design approved
**Prerequisite:** All foundation primitives proven in Phase 1

## What This Is

A single Python asyncio process on the Hetzner host that ties together the foundation primitives (Firecracker, dm-thin, VM snapshots, R2, Nix) into a REST API that agents can use.

## Architecture

```
Agent (internet) → HTTPS → Caddy (TLS + routing) → Orchestrator (FastAPI) → Firecracker VMs
                                                   → dm-thin volumes
                                                   → R2 (checkpoints)
                                                   → Nix (capabilities)
                                                   → SQLite (state)
```

Everything runs on the same Hetzner box. Caddy terminates TLS on a wildcard cert for `*.mshkn.dev` and reverse-proxies to the orchestrator or directly to VM ports.

## Components

### HTTP API (FastAPI + uvicorn)

14 agent-facing endpoints as REST:

| Method | Path | Maps to |
|--------|------|---------|
| POST | `/computers` | computer_create |
| POST | `/computers/{id}/exec` | computer_exec |
| POST | `/computers/{id}/exec/bg` | computer_exec_bg |
| GET | `/computers/{id}/exec/logs/{pid}` | computer_exec_logs |
| POST | `/computers/{id}/exec/kill/{pid}` | computer_exec_kill |
| POST | `/computers/{id}/upload` | computer_upload |
| GET | `/computers/{id}/download` | computer_download |
| GET | `/computers/{id}/status` | computer_status |
| POST | `/computers/{id}/checkpoint` | computer_checkpoint |
| DELETE | `/computers/{id}` | computer_destroy |
| POST | `/checkpoints/{id}/fork` | checkpoint_fork |
| POST | `/checkpoints/merge` | checkpoint_merge |
| POST | `/checkpoints/{id}/resolve` | checkpoint_resolve_conflicts |
| GET | `/checkpoints` | checkpoint_list |
| DELETE | `/checkpoints/{id}` | checkpoint_delete |

Plus internal: `GET /metrics` (Prometheus), `GET /health`.

Authentication: API key in `Authorization: Bearer {key}` header. Checked on every request, scoped to account.

Streaming exec: Server-Sent Events (SSE) for stdout/stderr on `computer_exec`. The response streams lines as they arrive, then closes with exit_code.

### VM Manager

Manages the lifecycle of Firecracker VMs.

**Resource allocation per VM:**
- Thin volume ID (monotonic counter)
- Tap device + IP: `tap{N}` at `172.16.{N}.1/30`, VM at `172.16.{N}.2`
- Firecracker API socket: `/tmp/fc-{computer_id}.socket`
- MAC address: `06:00:AC:10:{N_hex}:02` (encodes IP for fcnet-setup.sh)

**create(manifest):**
1. Resolve manifest → capability image (cache hit or Nix build + overlayfs compose)
2. dm-thin: `create_snap` from capability base → new volume
3. Start Firecracker via `--api-sock`, configure via HTTP API, `InstanceStart`
4. Wait for SSH readiness on VM IP
5. Register Caddy route for `*-{computer_id}.mshkn.dev` → VM IP
6. Insert into SQLite, return computer_id + URL

**destroy(computer_id):**
1. Kill Firecracker process
2. dm-thin: remove thin volume
3. Remove tap device, free IP
4. Remove Caddy route
5. Update SQLite

### Checkpoint Manager

**checkpoint(computer_id):**
1. Pause VM (10ms)
2. Firecracker snapshot create — vmstate + memory file (702ms)
3. Resume VM immediately — agent sees sub-1s latency
4. **Async background task:** compress memory (lz4), export disk delta (thin_delta), upload all to R2
5. Insert checkpoint into SQLite with R2 paths

**fork(checkpoint_id, manifest?):**
1. If manifest unchanged: download checkpoint from R2 (or NVMe cache), dm-thin snap from checkpoint disk, restore Firecracker from memory snapshot (16ms resume)
2. If manifest changed (additive): build new capability image, apply checkpoint disk delta on top, cold boot (no memory restore — capabilities changed)
3. New tap + IP, Caddy route, return new computer_id

**merge(ckpt_a, ckpt_b):**
1. Find common ancestor checkpoint
2. Mount all three disk volumes (ancestor, a, b)
3. Run 3-way file-level merge (proven in Phase 1)
4. Write merged result to new thin volume
5. Create checkpoint (filesystem only, no memory — merged checkpoint is always a cold boot)
6. Return checkpoint_id + conflicts list

### Exec Transport

SSH from orchestrator to VM over the tap network. Internal only — agents never SSH directly.

- `computer_exec`: SSH command, stream stdout/stderr back via SSE
- `computer_exec_bg`: SSH with `nohup ... & echo $!`, return PID
- `computer_exec_logs`: SSH `tail -f` on process output
- `computer_exec_kill`: SSH `kill {pid}`
- `computer_upload`: pipe data via SSH `cat > {path}`
- `computer_download`: SSH `cat {path}`, return bytes

asyncssh (Python) for non-blocking SSH from the async orchestrator.

### Capability Resolver

**manifest → bootable ext4 image:**
1. Hash the manifest → check `capability_cache` table
2. Cache hit: return cached image path
3. Cache miss: generate Nix expression → `nix-build` → copy closure → overlayfs (base + closure) → `mkfs.ext4` → cache the image

All of this was proven in Phase 1. The orchestrator just wraps it.

### Reverse Proxy (Caddy)

Caddy runs as a separate process, managed by systemd. The orchestrator talks to it via the Caddy admin API (`localhost:2019`).

- Wildcard DNS: `*.mshkn.dev` → server IP (set up once in Cloudflare)
- Wildcard cert: Let's Encrypt, issued once, auto-renewed by Caddy
- On VM create: `POST /config/apps/http/servers/.../routes` → add route for `{port}-{computer_id}.mshkn.dev` → `172.16.{N}.2:{port}`
- On VM destroy: remove route
- API route: `api.mshkn.dev` → `localhost:8000` (orchestrator)

## Database

SQLite with migration files. No ORM. Raw SQL with typed wrapper functions.

### Schema (001_initial.sql)

```sql
CREATE TABLE _migrations (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    api_key TEXT UNIQUE NOT NULL,
    vm_limit INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE computers (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    thin_volume_id INTEGER NOT NULL,
    tap_device TEXT NOT NULL,
    vm_ip TEXT NOT NULL,
    socket_path TEXT NOT NULL,
    firecracker_pid INTEGER,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'creating',  -- creating, running, paused, destroyed
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_exec_at TEXT
);

CREATE TABLE checkpoints (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    parent_id TEXT REFERENCES checkpoints(id),
    computer_id TEXT,
    manifest_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    r2_prefix TEXT NOT NULL,
    disk_delta_size_bytes INTEGER,
    memory_size_bytes INTEGER,
    label TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE capability_cache (
    manifest_hash TEXT PRIMARY KEY,
    image_path TEXT NOT NULL,
    nix_closure_size_bytes INTEGER,
    image_size_bytes INTEGER,
    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Migration Runner

A function that reads `migrations/*.sql`, checks `_migrations` for what's applied, runs the rest in order. ~30 lines.

### Query Layer (db.py)

Typed functions over raw SQL:

```python
async def get_computer(db: aiosqlite.Connection, computer_id: str) -> Computer | None: ...
async def list_computers(db: aiosqlite.Connection, account_id: str) -> list[Computer]: ...
async def insert_checkpoint(db: aiosqlite.Connection, checkpoint: Checkpoint) -> None: ...
```

Returns dataclasses defined in `models.py`. mypy checks the types. SQL is just SQL.

## Deployment

Git-based. Repo on GitHub, cloned on server.

```bash
# Deploy
ssh root@135.181.6.215 'cd /opt/mshkn && git pull && systemctl restart mshkn'
```

Systemd unit (`/etc/systemd/system/mshkn.service`) runs the orchestrator. Caddy has its own systemd unit.

## Project Structure

```
mshkn/
├── docs/plans/
├── migrations/
│   └── 001_initial.sql
├── src/mshkn/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, startup/shutdown
│   ├── config.py            # settings from env
│   ├── db.py                # migration runner, typed queries
│   ├── models.py            # dataclasses
│   ├── api/
│   │   ├── computers.py     # computer endpoints
│   │   └── checkpoints.py   # checkpoint endpoints
│   ├── vm/
│   │   ├── firecracker.py   # Firecracker API client
│   │   ├── network.py       # tap + IP allocation
│   │   └── storage.py       # dm-thin management
│   ├── checkpoint/
│   │   ├── snapshot.py       # VM snapshot (pause/create/resume)
│   │   ├── delta.py          # thin_delta export/import
│   │   ├── r2.py             # R2 upload/download
│   │   └── merge.py          # 3-way merge
│   ├── capability/
│   │   ├── resolver.py       # manifest → Nix expression
│   │   ├── builder.py        # nix-build + overlayfs → ext4
│   │   └── cache.py          # NVMe cache management
│   └── proxy/
│       └── caddy.py          # Caddy admin API client
├── pyproject.toml
└── .env
```

## Tooling

- **uv** for package management
- **ruff** for linting and formatting (strict, opinionated config)
- **mypy** in strict mode
- **pytest** for tests

## What This Design Does NOT Cover

- Observability (Prometheus, Grafana, alerts) — add after core works
- Billing / account_usage API — add after core works
- pip install blocking / suggested_action — add after core works
- Idle timeout / auto-checkpoint — add after core works
- Checkpoint retention / cleanup — add after core works

These are all real features from the design doc, but they're not load-bearing for the first working end-to-end flow. They layer on top.
