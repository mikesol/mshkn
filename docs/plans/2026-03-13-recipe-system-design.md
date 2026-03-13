# Recipe System Design

Replaces the Nix capability system with Docker-based recipes for building VM environments. Recipes are Dockerfiles built by Docker on the host, exported to ext4, and injected into dm-thin volumes.

## Motivation

The Nix capability system handles system-level tools well (python, node, ffmpeg) but fails at project-level package management (npm install from package.json, pip install from requirements.txt) and arbitrary CLIs not in nixpkgs. The purity shims that block npm/pip/apt create a leaky abstraction: declarative system packages alongside a wild-west filesystem.

Docker solves all three problems naturally: `RUN npm install`, `RUN pip install`, `RUN curl | sh` all work. AI agents write Dockerfiles fluently. Docker layer caching provides fast rebuilds. E2B and Fly.io both use this exact pattern (Dockerfile → export → ext4 → Firecracker).

## Data Model

### Recipe

```
recipes table:
  id              TEXT PRIMARY KEY   -- rcp-{uuid12}
  account_id      TEXT NOT NULL      -- FK to accounts
  dockerfile      TEXT NOT NULL      -- full Dockerfile content
  content_hash    TEXT NOT NULL      -- SHA256 of dockerfile (dedup key)
  status          TEXT NOT NULL      -- pending | building | ready | failed
  build_log       TEXT               -- stdout/stderr from docker build
  base_volume_id  INTEGER            -- dm-thin volume ID (once built)
  template_vmstate TEXT              -- path to L3 template vmstate (optional)
  template_memory  TEXT              -- path to L3 template memory (optional)
  created_at      TEXT NOT NULL
  built_at        TEXT               -- when build completed
```

Unique constraint on `(account_id, content_hash)` for dedup within an account. The asyncio.Lock (per-account build serialization) is the primary concurrency control; the index is a secondary safeguard.

### Changes to Existing Models

**computers table**: Keep `manifest_hash` and `manifest_json` (SQLite can't easily drop NOT NULL columns). New rows use sentinel values (`"none"` / `"{}"`). Add `recipe_id TEXT` (nullable, FK to recipes).

**checkpoints table**: Same treatment — keep old columns with sentinel values, add `recipe_id TEXT` (nullable, FK to recipes).

## API

### Recipe Endpoints

**POST /recipes**
```json
{
  "dockerfile": "FROM mshkn-base\nRUN apt-get update && apt-get install -y nodejs npm\nRUN npm install -g here.now\nWORKDIR /app\nRUN npm create vite@latest . --template react -y && npm install"
}
```
Response (202 Accepted):
```json
{
  "recipe_id": "rcp-a1b2c3d4e5f6",
  "status": "building",
  "content_hash": "8f3a..."
}
```

If a recipe with the same content_hash already exists for this account:
- If `ready`: return 200 with the existing recipe_id
- If `building`: return 200 with the existing recipe_id and status
- If `failed`: delete the failed row, then create a new recipe (retry the build)

**GET /recipes/{id}** — Recipe details including status, build_log.

**GET /recipes** — List recipes for account.

**DELETE /recipes/{id}** — Delete recipe and its base volume. Fails with 409 if any computers or checkpoints still reference this recipe_id. The reaper handles orphaned recipe volumes (recipes with no referencing computers/checkpoints) via LRU eviction when disk pressure rises.

### Changes to Computer Endpoints

**POST /computers**
```json
{
  "recipe_id": "rcp-a1b2c3d4e5f6",
  "exec": "python3 server.py",
  "self_destruct": true
}
```

If `recipe_id` is provided, the computer boots from the recipe's base volume (must be status=ready). If omitted, boots from the default bare rootfs.

The `uses` parameter is removed.

**POST /checkpoints/{id}/fork** — `recipe_id` can optionally be specified. If provided, it is stored as metadata on the new computer/checkpoint but does NOT change the disk contents (the fork always gets the checkpoint's disk snapshot). This is a metadata-only change — actual environment changes require building a new recipe and creating a fresh computer from it.

## Build Pipeline

When POST /recipes triggers a build:

### Phase 1: Docker Build

1. Write Dockerfile to a temp directory (`/tmp/mshkn-build-{content_hash}/`)
2. `docker build -t mshkn-recipe-{content_hash} --memory=4g --cpuset-cpus=0-1 /tmp/mshkn-build-{content_hash}/`
3. Build timeout: 10 minutes (enforced via `asyncio.wait_for`)
4. On success, proceed to Phase 2. On failure, set status=failed with build_log.
5. Cleanup on any failure: remove temp dir, `docker rmi` if image was created.

### Phase 2: Export to ext4

1. `docker create --name tmp-{content_hash} mshkn-recipe-{content_hash}`
2. `docker export tmp-{content_hash} > /tmp/recipe-{content_hash}.tar`
3. `docker rm tmp-{content_hash}`

### Phase 3: Inject into dm-thin volume

1. Allocate a new dm-thin volume in the pool (8GB, same as current capability volumes)
2. Activate as device mapper target
3. `mkfs.ext4 /dev/mapper/{volume_name}`
4. Mount the volume
5. Extract the tar into the mount point
6. Post-processing (forcefully overwrite — user Dockerfiles may have changed these):
   - Force-write SSH host keys if missing
   - Force-write SSH authorized_keys with the mshkn key
   - Force-write sshd_config to allow root login + pubkey auth
   - Force-create /sbin/init symlink to systemd (overwrite if exists)
   - Force-write systemd PATH drop-in to include standard locations
7. Unmount

### Phase 3.5: Cleanup

After Phase 3 completes (success or failure), clean up all intermediate artifacts:
1. `rm -rf /tmp/mshkn-build-{content_hash}/` (temp Dockerfile dir)
2. `rm -f /tmp/recipe-{content_hash}.tar` (exported tar)
3. `docker rmi mshkn-recipe-{content_hash}` (built image — no longer needed after export)

All cleanup runs in a `finally` block so it executes even on failure.

### Phase 4: Ready

1. Set status=ready, base_volume_id, built_at
2. Recipe is now available for use by POST /computers

### Phase 5: L3 Template (lazy, background)

Built on first computer creation from this recipe, not during the recipe build itself. This avoids blocking the build on staging slot availability.

1. Cold-boot the volume on staging slot 254
2. Wait for SSH ready
3. Pause VM, create snapshot (vmstate + memory)
4. Kill VM, destroy staging tap
5. Store template paths on recipe record

### Concurrency

Recipe builds are serialized per account (asyncio.Lock keyed on account_id). Multiple accounts can build concurrently. The staging slot lock (`_restore_lock`) serializes L3 template builds with VM creation.

## mshkn-base Docker Image

The base image provides the Firecracker-compatible rootfs foundation:

```dockerfile
FROM ubuntu:24.04

# Minimal system for Firecracker microVM
RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd systemd-sysv dbus udev \
    openssh-server \
    iproute2 iputils-ping curl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# SSH setup
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh
COPY mshkn_key.pub /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys
RUN sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
RUN sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

# Systemd init
RUN ln -sf /lib/systemd/systemd /sbin/init
```

This image is built once and stored locally. It does NOT need to be published to a registry — `FROM mshkn-base` resolves locally.

Build command: `docker build -t mshkn-base -f Dockerfile.mshkn-base .` from the repo root, where `mshkn_key.pub` is the public half of the SSH key at `config.ssh_key_path`.

### Server Dependency

Docker Engine (daemon + CLI) must be installed on the host. Add to the server setup alongside Firecracker/rclone/Caddy.

## What Gets Removed

### Files
- `src/mshkn/capability/resolver.py`
- `src/mshkn/capability/builder.py`
- `src/mshkn/capability/template_cache.py`
- `src/mshkn/capability/cache.py`
- `src/mshkn/capability/eviction.py`
- `src/mshkn/capability/__init__.py`
- Related test files

### Database Tables
- `capability_cache`
- `snapshot_templates`

### Server Dependencies
- Nix (`/root/.nix-profile`) — no longer needed for capability builds
- Nix store paths — can be cleaned up

### Code in Remaining Files
- `manager.py`: Remove `_get_or_build_capability_volume`, `_build_l3_template`, all Nix/capability imports
- `manager.py`: `create()` uses recipe's base_volume_id instead of capability volume
- `manager.py`: `initialize()` must scan `recipes.base_volume_id` to set `_next_volume_id` (replaces `get_max_capability_volume_id`)
- `manager.py`: L3 template build logic stays here (reads/writes `recipes` table instead of `snapshot_templates`)
- `computers.py`: Remove `uses`/`manifest` parameters from create endpoint; `CreateRequest` gets `recipe_id` field
- `checkpoints.py`: Remove `manifest`/`skip_manifest_check` from `ForkRequest`; add optional `recipe_id` field
- `models.py`: Remove `Manifest` dataclass. `Computer` and `Checkpoint` keep `manifest_hash`/`manifest_json` as vestigial fields (always sentinel values), add `recipe_id: str | None`
- `db.py`: All INSERT statements for computers/checkpoints use sentinel values for manifest columns

## What Stays

- dm-thin pool, CoW snapshots, staging slot 254
- Checkpoint/fork system (checkpoints reference recipe_id)
- Ingress system, self-destruct, deferred queue, labels
- SSE exec streaming, SSH pool
- R2 checkpoint upload/download
- Caddy reverse proxy
- Reaper (idle timeout, dead VM cleanup, checkpoint pruning)
- All networking (tap devices, slot allocation)

## Migration

Migration assumes a clean deployment — existing computers and checkpoints are destroyed before migration (pre-alpha, zero users). Sequential DB migration:

```sql
-- Add recipes table
CREATE TABLE recipes (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    dockerfile TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    build_log TEXT,
    base_volume_id INTEGER,
    template_vmstate TEXT,
    template_memory TEXT,
    created_at TEXT NOT NULL,
    built_at TEXT
);
CREATE UNIQUE INDEX idx_recipes_account_hash ON recipes(account_id, content_hash)
    WHERE status != 'failed';

-- Add recipe_id to computers and checkpoints
ALTER TABLE computers ADD COLUMN recipe_id TEXT REFERENCES recipes(id);
ALTER TABLE checkpoints ADD COLUMN recipe_id TEXT REFERENCES recipes(id);

-- Note: manifest_hash and manifest_json columns are left in place
-- (SQLite doesn't support DROP COLUMN cleanly). They become unused.
-- New INSERT statements for computers/checkpoints must use sentinel values
-- ("none" for manifest_hash, "{}" for manifest_json) to satisfy NOT NULL constraints.
```

## Performance

- **Cache hit** (recipe already built): Snapshot from recipe's base volume → boot. Same speed as today's L2 cache hit. Sub-second with L3 template.
- **Cache miss** (first build): Docker build time (30s-5min depending on Dockerfile). Docker layer cache makes subsequent builds with similar base layers fast.
- **Incremental rebuild**: Agent changes one RUN line → Docker rebuilds only changed layers (1-10s).

## E2E Tests

Existing capability tests (T3.1–T3.9) are replaced:

| Old Test | New Test |
|----------|----------|
| T3.1 pip blocked | Removed (pip works now) |
| T3.2 npm blocked | Removed (npm works now) |
| T3.3 apt blocked | Removed (apt works now) |
| T3.4 /nix/store readonly | Removed (no Nix) |
| T3.5 capability caching | T3.5' recipe dedup (same Dockerfile → same recipe) |
| T3.6 composition | T3.6' multi-tool Dockerfile (node + python + ffmpeg) |
| T3.7 version pinning | T3.7' Dockerfile with pinned versions |
| T3.8 tarball escape | Removed (just `RUN curl` in Dockerfile) |
| T3.9 manifest compat | T3.9' recipe change on fork |

New tests:
- Recipe CRUD (create, get, list, delete)
- Build lifecycle (pending → building → ready)
- Build failure (bad Dockerfile → failed + build_log)
- Content-hash dedup (same Dockerfile → same recipe)
- Computer from recipe (boot, SSH, verify tools present)
- Fork with recipe change
- Concurrent recipe builds (different accounts)
