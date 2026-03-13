# Recipe System Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Nix capability system with Docker-based recipes for building VM environments.

**Architecture:** Recipes are Dockerfiles built by Docker on the host, exported to ext4 tarballs, and injected into dm-thin volumes. A new `recipes` table tracks build state; the existing `capability_cache` and `snapshot_templates` tables are removed. The `_get_or_build_capability_volume` flow in `manager.py` is replaced by a simple recipe lookup.

**Tech Stack:** Python 3.12, asyncio, aiosqlite, Docker CLI (subprocess), dm-thin (dmsetup), FastAPI/Pydantic, pytest

**Spec:** `docs/plans/2026-03-13-recipe-system-design.md`

---

## File Structure

### New Files
- `migrations/009_recipes.sql` — Adds `recipes` table, `recipe_id` columns to computers/checkpoints
- `src/mshkn/recipe/__init__.py` — Empty package init
- `src/mshkn/recipe/builder.py` — Docker build pipeline (build → export → inject into dm-thin)
- `src/mshkn/api/recipes.py` — Recipe CRUD API endpoints
- `Dockerfile.mshkn-base` — Base image for Firecracker-compatible rootfs
- `tests/test_recipe_builder.py` — Unit tests for recipe builder
- `tests/test_recipe_db.py` — Unit tests for recipe DB operations
- `tests/test_recipe_api.py` — Unit tests for recipe API

### Modified Files
- `src/mshkn/models.py` — Add `Recipe` dataclass, add `recipe_id` to `Computer`/`Checkpoint`, remove `Manifest`/`CapabilityCacheEntry`
- `src/mshkn/db.py` — Add recipe CRUD functions, update ALL computer/checkpoint insert+select queries to include `recipe_id`, add `get_max_recipe_volume_id`
- `src/mshkn/vm/manager.py` — Replace `_get_or_build_capability_volume` with recipe lookup, update `create()` and `fork_from_checkpoint()`, update `initialize()`, update `_build_l3_template` to use recipes table
- `src/mshkn/api/computers.py` — Replace `uses` with `recipe_id` in `CreateRequest`/`CreateResponse`, update `_self_destruct`, `checkpoint_computer`, `_process_deferred`, `CheckpointResponse`
- `src/mshkn/api/checkpoints.py` — Replace `manifest`/`skip_manifest_check` with `recipe_id` in `ForkRequest`
- `src/mshkn/api/ingress.py` — Replace `Manifest` usage in `_do_create` and `_do_fork` with recipe_id-based calls
- `src/mshkn/main.py` — Register recipes router
- `src/mshkn/config.py` — Remove `capability_cache_dir`

### Removed Files
- `src/mshkn/capability/__init__.py`
- `src/mshkn/capability/resolver.py`
- `src/mshkn/capability/builder.py`
- `src/mshkn/capability/cache.py`
- `src/mshkn/capability/eviction.py`
- `src/mshkn/capability/template_cache.py`
- `tests/test_capability.py`

---

## Chunk 1: Foundation (Database, Models, DB Operations)

### Task 1: Database Migration

**Files:**
- Create: `migrations/009_recipes.sql`

- [ ] **Step 1: Write the migration SQL**

```sql
-- 009_recipes.sql
-- Recipe system: Docker-based environment builds replacing Nix capabilities

CREATE TABLE IF NOT EXISTS recipes (
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_account_hash ON recipes(account_id, content_hash)
    WHERE status != 'failed';

ALTER TABLE computers ADD COLUMN recipe_id TEXT REFERENCES recipes(id);
ALTER TABLE checkpoints ADD COLUMN recipe_id TEXT REFERENCES recipes(id);
```

- [ ] **Step 2: Verify migration loads**

Run: `.venv/bin/python -c "from pathlib import Path; sql = Path('migrations/009_recipes.sql').read_text(); print(f'Migration has {len(sql)} chars, {sql.count(chr(59))} statements'); print('OK')"`
Expected: Shows char count and statement count, prints OK

- [ ] **Step 3: Commit**

```bash
git add migrations/009_recipes.sql
git commit -m "feat: add 009_recipes migration for recipe system"
```

---

### Task 2: Recipe Model and Updated Dataclasses

**Files:**
- Modify: `src/mshkn/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing test for Recipe model**

Add to `tests/test_models.py`:

```python
def test_recipe_dataclass():
    from mshkn.models import Recipe

    r = Recipe(
        id="rcp-abc123",
        account_id="acct-test",
        dockerfile="FROM mshkn-base\nRUN apt-get install -y curl",
        content_hash="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        status="pending",
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    assert r.id == "rcp-abc123"
    assert r.status == "pending"
    assert r.base_volume_id is None


def test_computer_recipe_id():
    from mshkn.models import Computer

    c = Computer(
        id="comp-test",
        account_id="acct-test",
        thin_volume_id=100,
        tap_device="tap1",
        vm_ip="172.16.1.2",
        socket_path="/tmp/fc-test.socket",
        firecracker_pid=None,
        manifest_hash="none",
        manifest_json="{}",
        status="running",
        created_at="2026-03-13T00:00:00Z",
        last_exec_at=None,
        recipe_id="rcp-abc123",
    )
    assert c.recipe_id == "rcp-abc123"


def test_checkpoint_recipe_id():
    from mshkn.models import Checkpoint

    ck = Checkpoint(
        id="ckpt-test",
        account_id="acct-test",
        parent_id=None,
        computer_id="comp-test",
        thin_volume_id=100,
        manifest_hash="none",
        manifest_json="{}",
        r2_prefix="acct-test/ckpt-test",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-03-13T00:00:00Z",
        recipe_id="rcp-abc123",
    )
    assert ck.recipe_id == "rcp-abc123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_models.py::test_recipe_dataclass tests/test_models.py::test_computer_recipe_id tests/test_models.py::test_checkpoint_recipe_id -v`
Expected: FAIL — `Recipe` not found, `Computer`/`Checkpoint` don't accept `recipe_id`

- [ ] **Step 3: Update models.py**

In `src/mshkn/models.py`:

1. Add `Recipe` dataclass:

```python
@dataclass
class Recipe:
    id: str
    account_id: str
    dockerfile: str
    content_hash: str
    status: str  # pending | building | ready | failed
    build_log: str | None
    base_volume_id: int | None
    template_vmstate: str | None
    template_memory: str | None
    created_at: str
    built_at: str | None
```

2. Add `recipe_id: str | None = None` field to `Computer` (after `source_checkpoint_id`):

```python
    source_checkpoint_id: str | None = None
    recipe_id: str | None = None
```

3. Add `recipe_id: str | None = None` field to `Checkpoint` (after `created_at`):

```python
    created_at: str
    recipe_id: str | None = None
```

Do NOT remove `Manifest` or `CapabilityCacheEntry` yet — they're still imported elsewhere. That happens in Task 9.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/models.py tests/test_models.py
git commit -m "feat: add Recipe model, recipe_id to Computer/Checkpoint"
```

---

### Task 3: Recipe DB Operations

**Files:**
- Modify: `src/mshkn/db.py`
- Create: `tests/test_recipe_db.py`

- [ ] **Step 1: Write failing tests for recipe DB operations**

Create `tests/test_recipe_db.py`:

```python
from __future__ import annotations

import pytest
import aiosqlite

from mshkn.db import run_migrations
from mshkn.models import Recipe

# Import the functions we're about to write
from mshkn.db import (
    insert_recipe,
    get_recipe,
    list_recipes_by_account,
    update_recipe_status,
    update_recipe_build_result,
    delete_recipe,
    get_recipe_by_content_hash,
    get_max_recipe_volume_id,
    count_recipe_references,
)
from pathlib import Path


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await run_migrations(conn, Path("migrations"))
    # Insert a test account
    await conn.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        ("acct-test", "key-test", 10, "2026-01-01T00:00:00Z"),
    )
    await conn.commit()
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_insert_and_get_recipe(db: aiosqlite.Connection):
    recipe = Recipe(
        id="rcp-test1",
        account_id="acct-test",
        dockerfile="FROM mshkn-base\nRUN echo hello",
        content_hash="abc123",
        status="pending",
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    await insert_recipe(db, recipe)
    got = await get_recipe(db, "rcp-test1")
    assert got is not None
    assert got.id == "rcp-test1"
    assert got.dockerfile == "FROM mshkn-base\nRUN echo hello"
    assert got.status == "pending"


@pytest.mark.asyncio
async def test_list_recipes_by_account(db: aiosqlite.Connection):
    for i in range(3):
        r = Recipe(
            id=f"rcp-{i}",
            account_id="acct-test",
            dockerfile=f"FROM mshkn-base\nRUN echo {i}",
            content_hash=f"hash{i}",
            status="ready",
            build_log=None,
            base_volume_id=i + 100,
            template_vmstate=None,
            template_memory=None,
            created_at=f"2026-03-13T0{i}:00:00Z",
            built_at=None,
        )
        await insert_recipe(db, r)
    recipes = await list_recipes_by_account(db, "acct-test")
    assert len(recipes) == 3


@pytest.mark.asyncio
async def test_update_recipe_status(db: aiosqlite.Connection):
    r = Recipe(
        id="rcp-upd",
        account_id="acct-test",
        dockerfile="FROM mshkn-base",
        content_hash="updhash",
        status="pending",
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    await insert_recipe(db, r)
    await update_recipe_status(db, "rcp-upd", "building")
    got = await get_recipe(db, "rcp-upd")
    assert got is not None
    assert got.status == "building"


@pytest.mark.asyncio
async def test_update_recipe_build_result(db: aiosqlite.Connection):
    r = Recipe(
        id="rcp-built",
        account_id="acct-test",
        dockerfile="FROM mshkn-base",
        content_hash="builthash",
        status="building",
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    await insert_recipe(db, r)
    await update_recipe_build_result(
        db, "rcp-built",
        status="ready",
        build_log="build ok",
        base_volume_id=200,
        built_at="2026-03-13T01:00:00Z",
    )
    got = await get_recipe(db, "rcp-built")
    assert got is not None
    assert got.status == "ready"
    assert got.base_volume_id == 200
    assert got.build_log == "build ok"


@pytest.mark.asyncio
async def test_get_recipe_by_content_hash(db: aiosqlite.Connection):
    r = Recipe(
        id="rcp-hash",
        account_id="acct-test",
        dockerfile="FROM mshkn-base",
        content_hash="deduphash",
        status="ready",
        build_log=None,
        base_volume_id=150,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    await insert_recipe(db, r)
    got = await get_recipe_by_content_hash(db, "acct-test", "deduphash")
    assert got is not None
    assert got.id == "rcp-hash"

    # Non-existent hash
    got2 = await get_recipe_by_content_hash(db, "acct-test", "nope")
    assert got2 is None


@pytest.mark.asyncio
async def test_get_max_recipe_volume_id(db: aiosqlite.Connection):
    assert await get_max_recipe_volume_id(db) is None
    r = Recipe(
        id="rcp-vol",
        account_id="acct-test",
        dockerfile="FROM mshkn-base",
        content_hash="volhash",
        status="ready",
        build_log=None,
        base_volume_id=999,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    await insert_recipe(db, r)
    assert await get_max_recipe_volume_id(db) == 999


@pytest.mark.asyncio
async def test_delete_recipe(db: aiosqlite.Connection):
    r = Recipe(
        id="rcp-del",
        account_id="acct-test",
        dockerfile="FROM mshkn-base",
        content_hash="delhash",
        status="ready",
        build_log=None,
        base_volume_id=300,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    await insert_recipe(db, r)
    await delete_recipe(db, "rcp-del")
    assert await get_recipe(db, "rcp-del") is None


@pytest.mark.asyncio
async def test_count_recipe_references(db: aiosqlite.Connection):
    r = Recipe(
        id="rcp-ref",
        account_id="acct-test",
        dockerfile="FROM mshkn-base",
        content_hash="refhash",
        status="ready",
        build_log=None,
        base_volume_id=400,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    await insert_recipe(db, r)
    assert await count_recipe_references(db, "rcp-ref") == 0

    # Add a computer referencing this recipe
    await db.execute(
        "INSERT INTO computers (id, account_id, thin_volume_id, tap_device, vm_ip, "
        "socket_path, firecracker_pid, manifest_hash, manifest_json, status, "
        "created_at, last_exec_at, source_checkpoint_id, recipe_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("comp-ref", "acct-test", 100, "tap1", "172.16.1.2", "/tmp/fc.socket",
         None, "none", "{}", "running", "2026-03-13T00:00:00Z", None, None, "rcp-ref"),
    )
    await db.commit()
    assert await count_recipe_references(db, "rcp-ref") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_recipe_db.py -v`
Expected: FAIL — `insert_recipe` etc. not found in `mshkn.db`

- [ ] **Step 3: Implement recipe DB operations in db.py**

Add the following imports at top of `src/mshkn/db.py`:

```python
from mshkn.models import Account, Checkpoint, Computer, Recipe
```

(Replace the existing `from mshkn.models import Account, Checkpoint, Computer` line.)

Add the following functions at the end of `src/mshkn/db.py`:

```python
# ── Recipe operations ──


async def insert_recipe(db: aiosqlite.Connection, recipe: Recipe) -> None:
    await db.execute(
        "INSERT INTO recipes "
        "(id, account_id, dockerfile, content_hash, status, build_log, "
        "base_volume_id, template_vmstate, template_memory, created_at, built_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            recipe.id,
            recipe.account_id,
            recipe.dockerfile,
            recipe.content_hash,
            recipe.status,
            recipe.build_log,
            recipe.base_volume_id,
            recipe.template_vmstate,
            recipe.template_memory,
            recipe.created_at,
            recipe.built_at,
        ),
    )
    await db.commit()


async def get_recipe(db: aiosqlite.Connection, recipe_id: str) -> Recipe | None:
    cursor = await db.execute(
        "SELECT id, account_id, dockerfile, content_hash, status, build_log, "
        "base_volume_id, template_vmstate, template_memory, created_at, built_at "
        "FROM recipes WHERE id = ?",
        (recipe_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return Recipe(
        id=row[0],
        account_id=row[1],
        dockerfile=row[2],
        content_hash=row[3],
        status=row[4],
        build_log=row[5],
        base_volume_id=row[6],
        template_vmstate=row[7],
        template_memory=row[8],
        created_at=row[9],
        built_at=row[10],
    )


async def get_recipe_by_content_hash(
    db: aiosqlite.Connection, account_id: str, content_hash: str,
) -> Recipe | None:
    """Find a non-failed recipe with matching content_hash for this account."""
    cursor = await db.execute(
        "SELECT id, account_id, dockerfile, content_hash, status, build_log, "
        "base_volume_id, template_vmstate, template_memory, created_at, built_at "
        "FROM recipes WHERE account_id = ? AND content_hash = ? AND status != 'failed' "
        "LIMIT 1",
        (account_id, content_hash),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return Recipe(
        id=row[0],
        account_id=row[1],
        dockerfile=row[2],
        content_hash=row[3],
        status=row[4],
        build_log=row[5],
        base_volume_id=row[6],
        template_vmstate=row[7],
        template_memory=row[8],
        created_at=row[9],
        built_at=row[10],
    )


async def list_recipes_by_account(
    db: aiosqlite.Connection, account_id: str,
) -> list[Recipe]:
    cursor = await db.execute(
        "SELECT id, account_id, dockerfile, content_hash, status, build_log, "
        "base_volume_id, template_vmstate, template_memory, created_at, built_at "
        "FROM recipes WHERE account_id = ? ORDER BY created_at DESC",
        (account_id,),
    )
    rows = await cursor.fetchall()
    return [
        Recipe(
            id=r[0],
            account_id=r[1],
            dockerfile=r[2],
            content_hash=r[3],
            status=r[4],
            build_log=r[5],
            base_volume_id=r[6],
            template_vmstate=r[7],
            template_memory=r[8],
            created_at=r[9],
            built_at=r[10],
        )
        for r in rows
    ]


async def update_recipe_status(
    db: aiosqlite.Connection, recipe_id: str, status: str,
) -> None:
    await db.execute(
        "UPDATE recipes SET status = ? WHERE id = ?",
        (status, recipe_id),
    )
    await db.commit()


async def update_recipe_build_result(
    db: aiosqlite.Connection,
    recipe_id: str,
    *,
    status: str,
    build_log: str | None = None,
    base_volume_id: int | None = None,
    built_at: str | None = None,
) -> None:
    await db.execute(
        "UPDATE recipes SET status = ?, build_log = ?, base_volume_id = ?, built_at = ? "
        "WHERE id = ?",
        (status, build_log, base_volume_id, built_at, recipe_id),
    )
    await db.commit()


async def update_recipe_template(
    db: aiosqlite.Connection,
    recipe_id: str,
    template_vmstate: str,
    template_memory: str,
) -> None:
    await db.execute(
        "UPDATE recipes SET template_vmstate = ?, template_memory = ? WHERE id = ?",
        (template_vmstate, template_memory, recipe_id),
    )
    await db.commit()


async def delete_recipe(db: aiosqlite.Connection, recipe_id: str) -> None:
    await db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    await db.commit()


async def delete_failed_recipes_by_hash(
    db: aiosqlite.Connection, account_id: str, content_hash: str,
) -> None:
    """Delete failed recipe rows for this account+hash (cleanup before retry)."""
    await db.execute(
        "DELETE FROM recipes WHERE account_id = ? AND content_hash = ? AND status = 'failed'",
        (account_id, content_hash),
    )
    await db.commit()


async def get_max_recipe_volume_id(db: aiosqlite.Connection) -> int | None:
    cursor = await db.execute(
        "SELECT MAX(base_volume_id) FROM recipes WHERE base_volume_id IS NOT NULL"
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else None


async def count_recipe_references(db: aiosqlite.Connection, recipe_id: str) -> int:
    """Count non-destroyed computers + checkpoints referencing this recipe."""
    cursor = await db.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM computers WHERE recipe_id = ? AND status != 'destroyed') + "
        "(SELECT COUNT(*) FROM checkpoints WHERE recipe_id = ?)",
        (recipe_id, recipe_id),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_recipe_db.py -v`
Expected: All PASS

- [ ] **Step 4.5: Update existing insert/select queries to include recipe_id**

**CRITICAL**: The existing `insert_computer`, `insert_checkpoint`, and ALL `get_*`/`list_*` queries in `db.py` must be updated to include the `recipe_id` column. Without this, `recipe_id` would be silently lost on every database round-trip.

**For `insert_computer`**: Add `recipe_id` to the INSERT column list and VALUES:
```python
async def insert_computer(db: aiosqlite.Connection, computer: Computer) -> None:
    await db.execute(
        "INSERT INTO computers "
        "(id, account_id, thin_volume_id, tap_device, vm_ip, socket_path, "
        "firecracker_pid, manifest_hash, manifest_json, status, created_at, last_exec_at, "
        "source_checkpoint_id, recipe_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            computer.id, computer.account_id, computer.thin_volume_id,
            computer.tap_device, computer.vm_ip, computer.socket_path,
            computer.firecracker_pid, computer.manifest_hash, computer.manifest_json,
            computer.status, computer.created_at, computer.last_exec_at,
            computer.source_checkpoint_id, computer.recipe_id,
        ),
    )
    await db.commit()
```

**For `insert_checkpoint`**: Add `recipe_id` to the INSERT column list and VALUES:
```python
async def insert_checkpoint(db: aiosqlite.Connection, checkpoint: Checkpoint) -> None:
    await db.execute(
        "INSERT INTO checkpoints "
        "(id, account_id, parent_id, computer_id, thin_volume_id, manifest_hash, manifest_json, "
        "r2_prefix, disk_delta_size_bytes, memory_size_bytes, label, pinned, created_at, recipe_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            checkpoint.id, checkpoint.account_id, checkpoint.parent_id,
            checkpoint.computer_id, checkpoint.thin_volume_id,
            checkpoint.manifest_hash, checkpoint.manifest_json,
            checkpoint.r2_prefix, checkpoint.disk_delta_size_bytes,
            checkpoint.memory_size_bytes, checkpoint.label,
            int(checkpoint.pinned), checkpoint.created_at, checkpoint.recipe_id,
        ),
    )
    await db.commit()
```

**For ALL Computer query functions** (`get_computer`, `list_all_computers`, `list_computers_by_account`, `get_active_computer_for_label`): Add `recipe_id` to the SELECT column list and add `recipe_id=row[13]` to the `Computer(...)` constructor.

**For ALL Checkpoint query functions** (`get_checkpoint`, `list_checkpoints_by_account`, `get_latest_checkpoint_for_computer`, `list_prunable_checkpoints`): Add `recipe_id` to the SELECT column list and add `recipe_id=row[13]` to the `Checkpoint(...)` constructor.

This is mechanical but critical — every function that constructs a `Computer` or `Checkpoint` from a DB row must be updated.

- [ ] **Step 5: Run full test suite + lint**

Run: `.venv/bin/ruff check src/ && .venv/bin/mypy src/ && .venv/bin/pytest tests/ --ignore=tests/e2e --ignore=tests/integration -x`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/mshkn/db.py tests/test_recipe_db.py
git commit -m "feat: add recipe DB operations (insert, get, list, update, delete)"
```

---

## Chunk 2: Recipe Builder

### Task 4: mshkn-base Dockerfile

**Files:**
- Create: `Dockerfile.mshkn-base`

- [ ] **Step 1: Create the base Dockerfile**

Create `Dockerfile.mshkn-base` in the repo root:

```dockerfile
FROM ubuntu:24.04

# Minimal system for Firecracker microVM
RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd systemd-sysv dbus udev \
    openssh-server \
    iproute2 iputils-ping curl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# SSH setup — key is injected by build context
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh
COPY mshkn_key.pub /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys
RUN sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

# Systemd init
RUN ln -sf /lib/systemd/systemd /sbin/init
```

- [ ] **Step 2: Commit**

```bash
git add Dockerfile.mshkn-base
git commit -m "feat: add mshkn-base Dockerfile for Firecracker-compatible rootfs"
```

---

### Task 5: Recipe Builder Module

**Files:**
- Create: `src/mshkn/recipe/__init__.py`
- Create: `src/mshkn/recipe/builder.py`
- Create: `tests/test_recipe_builder.py`

- [ ] **Step 1: Create package init**

Create empty `src/mshkn/recipe/__init__.py`.

- [ ] **Step 2: Write failing test for `dockerfile_content_hash`**

Create `tests/test_recipe_builder.py`:

```python
from __future__ import annotations

import pytest


def test_dockerfile_content_hash():
    from mshkn.recipe.builder import dockerfile_content_hash

    h1 = dockerfile_content_hash("FROM mshkn-base\nRUN echo hello")
    h2 = dockerfile_content_hash("FROM mshkn-base\nRUN echo hello")
    h3 = dockerfile_content_hash("FROM mshkn-base\nRUN echo world")

    assert h1 == h2  # deterministic
    assert h1 != h3  # different content
    assert len(h1) == 64  # full SHA256
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_recipe_builder.py::test_dockerfile_content_hash -v`
Expected: FAIL — module not found

- [ ] **Step 4: Implement builder.py with `dockerfile_content_hash`**

Create `src/mshkn/recipe/builder.py`:

```python
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.shell import run

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config

logger = logging.getLogger(__name__)

# Build resource limits
_BUILD_TIMEOUT_SECONDS = 600  # 10 minutes
_BUILD_MEMORY_LIMIT = "4g"
_BUILD_CPUSET = "0-1"  # 2 CPUs


def dockerfile_content_hash(dockerfile: str) -> str:
    """SHA256 hash of Dockerfile content."""
    return hashlib.sha256(dockerfile.encode()).hexdigest()


async def build_recipe(
    db: aiosqlite.Connection,
    config: Config,
    recipe_id: str,
    dockerfile: str,
    content_hash: str,
    allocate_volume_id: int,
) -> None:
    """Full recipe build pipeline: Docker build → export → inject into dm-thin.

    Updates recipe status in DB throughout. On failure, sets status=failed with build_log.
    """
    from mshkn.db import update_recipe_build_result, update_recipe_status
    from mshkn.vm.storage import create_snapshot

    await update_recipe_status(db, recipe_id, "building")

    build_dir = Path(f"/tmp/mshkn-build-{content_hash}")
    tar_path = Path(f"/tmp/recipe-{content_hash}.tar")
    image_tag = f"mshkn-recipe-{content_hash}"
    container_name = f"tmp-{recipe_id}"  # use recipe_id to avoid cross-account collision
    build_log_parts: list[str] = []

    try:
        # ── Phase 1: Docker Build ──
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "Dockerfile").write_text(dockerfile)

        try:
            # Use subprocess directly to capture both stdout and stderr for build log
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    f"docker build -t {image_tag} "
                    f"--memory={_BUILD_MEMORY_LIMIT} "
                    f"--cpuset-cpus={_BUILD_CPUSET} "
                    f"{build_dir}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                ),
                timeout=_BUILD_TIMEOUT_SECONDS,
            )
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_BUILD_TIMEOUT_SECONDS,
            )
            build_output = stdout_bytes.decode()
            build_log_parts.append(build_output)
            if proc.returncode != 0:
                raise RuntimeError(f"docker build exited with code {proc.returncode}")
        except asyncio.TimeoutError:
            build_log_parts.append("Build timed out after 10 minutes")
            await update_recipe_build_result(
                db, recipe_id,
                status="failed",
                build_log="\n".join(build_log_parts),
            )
            return
        except Exception as e:
            build_log_parts.append(f"Docker build failed: {e}")
            await update_recipe_build_result(
                db, recipe_id,
                status="failed",
                build_log="\n".join(build_log_parts),
            )
            return

        # ── Phase 2: Export to tar ──
        await run(f"docker create --name {container_name} {image_tag}")
        await run(f"docker export {container_name} -o {tar_path}")
        await run(f"docker rm {container_name}")

        # ── Phase 3: Inject into dm-thin volume ──
        volume_name = f"mshkn-recipe-{content_hash[:16]}"

        await create_snapshot(
            pool_name=config.thin_pool_name,
            source_volume_id=0,  # base volume
            new_volume_id=allocate_volume_id,
            new_volume_name=volume_name,
            sectors=config.thin_volume_sectors,
        )

        mount_point = tempfile.mkdtemp(prefix="mshkn-recipe-mount-")
        try:
            await run(f"mkfs.ext4 -F /dev/mapper/{volume_name}")
            await run(f"mount /dev/mapper/{volume_name} {mount_point}")
            await run(f"tar xf {tar_path} -C {mount_point}")

            # Post-processing: force-write Firecracker-required config
            await _post_process_rootfs(mount_point, config)

            await run(f"umount {mount_point}")
        except Exception:
            await run(f"umount {mount_point}", check=False)
            raise
        finally:
            shutil.rmtree(mount_point, ignore_errors=True)

        # Deactivate the device mapper target (staging will reactivate as needed)
        await run(f"dmsetup remove {volume_name}", check=False)

        # ── Phase 4: Ready ──
        from datetime import UTC, datetime

        await update_recipe_build_result(
            db, recipe_id,
            status="ready",
            build_log="\n".join(build_log_parts) if build_log_parts else "build ok",
            base_volume_id=allocate_volume_id,
            built_at=datetime.now(UTC).isoformat(),
        )
        logger.info("Recipe %s built successfully (volume %d)", recipe_id, allocate_volume_id)

    except Exception as e:
        logger.exception("Recipe build failed: %s", recipe_id)
        build_log_parts.append(f"Build pipeline error: {e}")
        await update_recipe_build_result(
            db, recipe_id,
            status="failed",
            build_log="\n".join(build_log_parts),
        )

    finally:
        # ── Phase 3.5: Cleanup ──
        shutil.rmtree(build_dir, ignore_errors=True)
        tar_path.unlink(missing_ok=True)
        await run(f"docker rm {container_name}", check=False)
        await run(f"docker rmi {image_tag}", check=False)


async def _post_process_rootfs(mount_point: str, config: Config) -> None:
    """Force-write SSH config and init symlink into the rootfs.

    User Dockerfiles may have changed these, but Firecracker VMs
    require specific SSH and init setup to function.
    """
    root = Path(mount_point)

    # SSH host keys (generate if missing)
    ssh_dir = root / "etc" / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    for key_type in ("rsa", "ecdsa", "ed25519"):
        key_file = ssh_dir / f"ssh_host_{key_type}_key"
        if not key_file.exists():
            await run(
                f"ssh-keygen -t {key_type} -f {key_file} -N '' -q"
            )

    # SSH authorized_keys
    dot_ssh = root / "root" / ".ssh"
    dot_ssh.mkdir(parents=True, exist_ok=True)
    (dot_ssh / "authorized_keys").write_text(
        config.ssh_key_path.with_suffix(".pub").read_text()
    )
    # Fix permissions
    await run(f"chmod 700 {dot_ssh}")
    await run(f"chmod 600 {dot_ssh / 'authorized_keys'}")

    # sshd_config: force root login + pubkey auth
    sshd_config = ssh_dir / "sshd_config"
    if sshd_config.exists():
        text = sshd_config.read_text()
        import re
        text = re.sub(r"#?PermitRootLogin\s+.*", "PermitRootLogin yes", text)
        text = re.sub(r"#?PubkeyAuthentication\s+.*", "PubkeyAuthentication yes", text)
        sshd_config.write_text(text)

    # /sbin/init symlink to systemd
    init_path = root / "sbin" / "init"
    init_path.parent.mkdir(parents=True, exist_ok=True)
    init_path.unlink(missing_ok=True)
    # Use relative symlink since this is inside the rootfs
    systemd_path = root / "lib" / "systemd" / "systemd"
    if systemd_path.exists():
        init_path.symlink_to("/lib/systemd/systemd")

    # systemd PATH drop-in: ensure standard locations are in PATH for services
    dropin_dir = root / "etc" / "systemd" / "system" / "mshkn-env.conf.d"
    dropin_dir.mkdir(parents=True, exist_ok=True)
    env_conf = root / "etc" / "environment"
    env_conf.write_text(
        'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
    )


async def ensure_base_image(config: Config) -> None:
    """Build the mshkn-base Docker image if it doesn't exist locally."""
    try:
        await run("docker image inspect mshkn-base", check=True)
        logger.info("mshkn-base image already exists")
        return
    except Exception:
        pass

    # Extract public key for the build context
    pub_key_path = config.ssh_key_path.with_suffix(".pub")
    if not pub_key_path.exists():
        raise RuntimeError(f"SSH public key not found: {pub_key_path}")

    build_ctx = tempfile.mkdtemp(prefix="mshkn-base-build-")
    try:
        # Copy Dockerfile and key into build context
        shutil.copy2("Dockerfile.mshkn-base", Path(build_ctx) / "Dockerfile")
        shutil.copy2(pub_key_path, Path(build_ctx) / "mshkn_key.pub")
        await run(f"docker build -t mshkn-base {build_ctx}")
        logger.info("Built mshkn-base Docker image")
    finally:
        shutil.rmtree(build_ctx, ignore_errors=True)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_recipe_builder.py::test_dockerfile_content_hash -v`
Expected: PASS

- [ ] **Step 6: Write test for `ensure_base_image` (mock Docker)**

Add to `tests/test_recipe_builder.py`:

```python
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_ensure_base_image_already_exists():
    from mshkn.recipe.builder import ensure_base_image
    from mshkn.config import Config

    config = Config(ssh_key_path=Path("/tmp/test-key"))

    with patch("mshkn.recipe.builder.run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ""  # docker image inspect succeeds
        await ensure_base_image(config)
        mock_run.assert_called_once_with("docker image inspect mshkn-base", check=True)
```

- [ ] **Step 7: Run test**

Run: `.venv/bin/pytest tests/test_recipe_builder.py -v`
Expected: All PASS

- [ ] **Step 8: Run lint + type check**

Run: `.venv/bin/ruff check src/mshkn/recipe/ && .venv/bin/mypy src/mshkn/recipe/`
Expected: Clean

- [ ] **Step 9: Commit**

```bash
git add src/mshkn/recipe/__init__.py src/mshkn/recipe/builder.py tests/test_recipe_builder.py
git commit -m "feat: add recipe builder (Docker build → export → dm-thin inject)"
```

---

## Chunk 3: API Endpoints and Manager Integration

### Task 6: Recipe API Endpoints

**Files:**
- Create: `src/mshkn/api/recipes.py`
- Modify: `src/mshkn/main.py` (register router)

- [ ] **Step 1: Create recipes.py API**

Create `src/mshkn/api/recipes.py`:

```python
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from mshkn.api.auth import require_account
from mshkn.db import (
    count_recipe_references,
    delete_failed_recipes_by_hash,
    delete_recipe,
    get_recipe,
    get_recipe_by_content_hash,
    insert_recipe,
    list_recipes_by_account,
)
from mshkn.models import Recipe
from mshkn.recipe.builder import build_recipe, dockerfile_content_hash

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.models import Account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])

_require_account = Depends(require_account)

# Per-account build locks to serialize recipe builds
_build_locks: dict[str, asyncio.Lock] = {}


def _get_build_lock(account_id: str) -> asyncio.Lock:
    if account_id not in _build_locks:
        _build_locks[account_id] = asyncio.Lock()
    return _build_locks[account_id]


class CreateRecipeRequest(BaseModel):
    dockerfile: str


class RecipeResponse(BaseModel):
    recipe_id: str
    status: str
    content_hash: str
    build_log: str | None = None
    base_volume_id: int | None = None
    created_at: str | None = None
    built_at: str | None = None


@router.post("", response_model=RecipeResponse, status_code=200)
async def create_recipe(
    request: Request,
    body: CreateRecipeRequest,
    account: Account = _require_account,
) -> RecipeResponse:
    db: aiosqlite.Connection = request.app.state.db
    content_hash = dockerfile_content_hash(body.dockerfile)

    # Check for existing recipe with same content hash
    existing = await get_recipe_by_content_hash(db, account.id, content_hash)
    if existing is not None:
        return RecipeResponse(
            recipe_id=existing.id,
            status=existing.status,
            content_hash=existing.content_hash,
            build_log=existing.build_log,
            base_volume_id=existing.base_volume_id,
            created_at=existing.created_at,
            built_at=existing.built_at,
        )

    # Delete any failed recipes with this hash (retry)
    await delete_failed_recipes_by_hash(db, account.id, content_hash)

    recipe_id = f"rcp-{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()

    recipe = Recipe(
        id=recipe_id,
        account_id=account.id,
        dockerfile=body.dockerfile,
        content_hash=content_hash,
        status="pending",
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at=now,
        built_at=None,
    )
    await insert_recipe(db, recipe)

    # Allocate a volume ID for the build
    vm_mgr = request.app.state.vm_manager
    async with vm_mgr._alloc_lock:
        volume_id = vm_mgr._allocate_volume_id()

    # Start build in background, serialized per account
    lock = _get_build_lock(account.id)
    config = request.app.state.config

    async def _do_build() -> None:
        async with lock:
            await build_recipe(db, config, recipe_id, body.dockerfile, content_hash, volume_id)

    task = asyncio.create_task(_do_build())
    # Store task reference to prevent GC
    vm_mgr._bg_tasks.add(task)
    task.add_done_callback(vm_mgr._bg_tasks.discard)

    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=202,
        content=RecipeResponse(
            recipe_id=recipe_id,
            status="pending",
            content_hash=content_hash,
            created_at=now,
        ).model_dump(),
    )


@router.get("/{recipe_id}", response_model=RecipeResponse)
async def get_recipe_endpoint(
    recipe_id: str,
    request: Request,
    account: Account = _require_account,
) -> RecipeResponse:
    db: aiosqlite.Connection = request.app.state.db
    recipe = await get_recipe(db, recipe_id)
    if recipe is None or recipe.account_id != account.id:
        raise HTTPException(status_code=404, detail="Recipe not found")

    return RecipeResponse(
        recipe_id=recipe.id,
        status=recipe.status,
        content_hash=recipe.content_hash,
        build_log=recipe.build_log,
        base_volume_id=recipe.base_volume_id,
        created_at=recipe.created_at,
        built_at=recipe.built_at,
    )


@router.get("", response_model=list[RecipeResponse])
async def list_recipes(
    request: Request,
    account: Account = _require_account,
) -> list[RecipeResponse]:
    db: aiosqlite.Connection = request.app.state.db
    recipes = await list_recipes_by_account(db, account.id)
    return [
        RecipeResponse(
            recipe_id=r.id,
            status=r.status,
            content_hash=r.content_hash,
            build_log=r.build_log,
            base_volume_id=r.base_volume_id,
            created_at=r.created_at,
            built_at=r.built_at,
        )
        for r in recipes
    ]


@router.delete("/{recipe_id}")
async def delete_recipe_endpoint(
    recipe_id: str,
    request: Request,
    account: Account = _require_account,
) -> dict[str, str]:
    db: aiosqlite.Connection = request.app.state.db
    recipe = await get_recipe(db, recipe_id)
    if recipe is None or recipe.account_id != account.id:
        raise HTTPException(status_code=404, detail="Recipe not found")

    ref_count = await count_recipe_references(db, recipe_id)
    if ref_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Recipe still referenced by {ref_count} computer(s)/checkpoint(s)",
        )

    # Remove dm-thin volume if it exists
    if recipe.base_volume_id is not None:
        from mshkn.vm.storage import remove_volume

        volume_name = f"mshkn-recipe-{recipe.content_hash[:16]}"
        try:
            await remove_volume(
                request.app.state.config.thin_pool_name,
                volume_name,
                recipe.base_volume_id,
            )
        except Exception:
            logger.warning("Failed to remove recipe volume %s", volume_name)

    await delete_recipe(db, recipe_id)
    return {"status": "deleted"}
```

- [ ] **Step 2: Register router in main.py**

In `src/mshkn/main.py`, add import:

```python
from mshkn.api.recipes import router as recipes_router
```

And add to the router registrations (after `computers_router`):

```python
app.include_router(recipes_router)
```

- [ ] **Step 3: Run lint + type check**

Run: `.venv/bin/ruff check src/mshkn/api/recipes.py && .venv/bin/mypy src/mshkn/api/recipes.py`
Expected: Clean

- [ ] **Step 4: Commit**

```bash
git add src/mshkn/api/recipes.py src/mshkn/main.py
git commit -m "feat: add recipe API endpoints (POST/GET/DELETE /recipes)"
```

---

### Task 7: Manager Integration

**Files:**
- Modify: `src/mshkn/vm/manager.py`

This is the core integration task. Replace capability volume lookup with recipe lookup.

- [ ] **Step 1: Update `initialize()` to scan recipe volumes**

In `src/mshkn/vm/manager.py`, replace the capability cache volume scan (lines 121-126):

```python
        # Also check capability cache volumes
        from mshkn.capability.cache import get_max_capability_volume_id

        cap_max = await get_max_capability_volume_id(self.db)
        if cap_max is not None:
            max_vol = max(max_vol, cap_max)
```

With:

```python
        # Also check recipe base volumes
        from mshkn.db import get_max_recipe_volume_id

        recipe_max = await get_max_recipe_volume_id(self.db)
        if recipe_max is not None:
            max_vol = max(max_vol, recipe_max)
```

- [ ] **Step 2: Update `create()` signature and body**

Change the `create()` method signature from:

```python
    async def create(
        self,
        account_id: str,
        manifest: Manifest,
        needs: dict[str, object] | None = None,
    ) -> Computer:
```

To:

```python
    async def create(
        self,
        account_id: str,
        recipe_id: str | None = None,
        needs: dict[str, object] | None = None,
    ) -> Computer:
```

Replace the capability volume lookup block (lines 397-399):

```python
        # Get capability base volume (L1/L2 cache, builds if miss)
        source_volume_id = await self._get_or_build_capability_volume(manifest)
        manifest_hash = manifest.content_hash() if manifest.uses else "bare"
```

With:

```python
        # Get source volume: recipe base volume or bare rootfs
        if recipe_id is not None:
            from mshkn.db import get_recipe

            recipe = await get_recipe(self.db, recipe_id)
            if recipe is None:
                raise ValueError(f"Recipe {recipe_id} not found")
            if recipe.status != "ready":
                raise ValueError(f"Recipe {recipe_id} is not ready (status={recipe.status})")
            if recipe.base_volume_id is None:
                raise ValueError(f"Recipe {recipe_id} has no base volume")
            source_volume_id = recipe.base_volume_id
        else:
            recipe = None
            source_volume_id = 0  # bare base image
```

Replace the L3 template cache block (lines 430-454) — the `else` branch of `if custom_resources:`:

```python
        else:
            # Default resources: use L3 template cache for fast restore
            if recipe is not None and recipe.template_vmstate and recipe.template_memory:
                # Recipe has a cached L3 template
                vmstate_path = recipe.template_vmstate
                memory_path = recipe.template_memory
            elif recipe is not None:
                # Build L3 template on first use (lazy)
                await self._build_l3_template_for_recipe(recipe)
                recipe = await get_recipe(self.db, recipe.id)
                if recipe and recipe.template_vmstate and recipe.template_memory:
                    vmstate_path = recipe.template_vmstate
                    memory_path = recipe.template_memory
                else:
                    # L3 build failed — fall back to cold boot
                    from mshkn.vm.staging import cold_boot_from_disk

                    result = await cold_boot_from_disk(
                        disk_volume_id=volume_id,
                        final_slot=slot,
                        pool_name=self.config.thin_pool_name,
                        thin_volume_sectors=self.config.thin_volume_sectors,
                        final_volume_name=volume_name,
                        kernel_path=str(self.config.kernel_path),
                        socket_path=f"/tmp/fc-{computer_id}.socket",
                    )
                    vmstate_path = None
                    memory_path = None
            else:
                # Bare rootfs: cold-boot directly (no L3 template for bare)
                from mshkn.vm.staging import cold_boot_from_disk

                result = await cold_boot_from_disk(
                    disk_volume_id=volume_id,
                    final_slot=slot,
                    pool_name=self.config.thin_pool_name,
                    thin_volume_sectors=self.config.thin_volume_sectors,
                    final_volume_name=volume_name,
                    kernel_path=str(self.config.kernel_path),
                    socket_path=f"/tmp/fc-{computer_id}.socket",
                )
                vmstate_path = None
                memory_path = None

            if vmstate_path is not None and memory_path is not None:
                from mshkn.vm.staging import restore_from_snapshot

                result = await restore_from_snapshot(
                    vmstate_path=vmstate_path,
                    memory_path=memory_path,
                    disk_volume_id=volume_id,
                    final_slot=slot,
                    pool_name=self.config.thin_pool_name,
                    thin_volume_sectors=self.config.thin_volume_sectors,
                    final_volume_name=volume_name,
                    socket_path=f"/tmp/fc-{computer_id}.socket",
                )
```

Update the Computer record creation (lines 463-476) to use sentinel manifest values and recipe_id:

```python
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=result.tap_device,
            vm_ip=result.vm_ip,
            socket_path=result.socket_path,
            firecracker_pid=result.pid,
            manifest_hash="none",
            manifest_json="{}",
            status="running",
            created_at=now,
            last_exec_at=None,
            recipe_id=recipe_id,
        )
```

- [ ] **Step 3: Add `_build_l3_template_for_recipe` method**

Add after `_build_l3_template`:

```python
    async def _build_l3_template_for_recipe(self, recipe: Recipe) -> None:
        """Build an L3 template for a recipe: cold-boot on staging slot, snapshot, cache."""
        from mshkn.db import update_recipe_template
        from mshkn.vm.staging import (
            STAGING_DRIVE_NAME,
            STAGING_MAC,
            STAGING_SLOT,
            STAGING_TAP,
            STAGING_VM_IP,
            _restore_lock,
        )

        template_dir = self.config.checkpoint_local_dir / "templates" / recipe.id
        template_dir.mkdir(parents=True, exist_ok=True)
        vmstate_path = template_dir / "vmstate"
        memory_path = template_dir / "memory"

        socket_path = f"/tmp/fc-template-{recipe.id}.socket"
        pid: int | None = None

        async with _restore_lock:
            try:
                from mshkn.vm.staging import _ensure_staging_clean

                await _ensure_staging_clean()

                await asyncio.gather(
                    run(
                        f"dmsetup create {STAGING_DRIVE_NAME} "
                        f"--table '0 {self.config.thin_volume_sectors} thin "
                        f"/dev/mapper/{self.config.thin_pool_name} "
                        f"{recipe.base_volume_id}'"
                    ),
                    create_tap(STAGING_SLOT),
                )

                pid = await start_firecracker_process(socket_path)
                fc_client = FirecrackerClient(socket_path)
                try:
                    await fc_client.configure_and_boot(
                        FirecrackerConfig(
                            socket_path=socket_path,
                            kernel_path=str(self.config.kernel_path),
                            rootfs_path=f"/dev/mapper/{STAGING_DRIVE_NAME}",
                            tap_device=STAGING_TAP,
                            guest_mac=STAGING_MAC,
                        )
                    )
                finally:
                    await fc_client.close()

                await self._wait_for_ssh(STAGING_VM_IP)

                fc_client = FirecrackerClient(socket_path)
                try:
                    await fc_client.pause()
                    await fc_client.create_snapshot(str(vmstate_path), str(memory_path))
                finally:
                    await fc_client.close()

                await kill_firecracker_process(pid)
                pid = None
                await destroy_tap(STAGING_SLOT)
                await run(f"dmsetup remove {STAGING_DRIVE_NAME}")

                await update_recipe_template(
                    self.db, recipe.id, str(vmstate_path), str(memory_path),
                )
                logger.info("Built L3 template for recipe %s", recipe.id)

            except Exception:
                logger.exception("Failed to build L3 template for recipe %s", recipe.id)
                if pid is not None:
                    await kill_firecracker_process(pid)
                await destroy_tap(STAGING_SLOT)
                await run(f"dmsetup remove {STAGING_DRIVE_NAME}", check=False)
                raise
```

- [ ] **Step 4: Update `fork_from_checkpoint` to use recipe_id**

Change the signature from:

```python
    async def fork_from_checkpoint(
        self, account_id: str, checkpoint: Checkpoint, manifest: Manifest | None = None,
    ) -> Computer:
```

To:

```python
    async def fork_from_checkpoint(
        self, account_id: str, checkpoint: Checkpoint, recipe_id: str | None = None,
    ) -> Computer:
```

Replace the manifest handling in the Computer record (lines 586-603):

```python
        effective_manifest = manifest if manifest is not None else Manifest.from_json(
            checkpoint.manifest_json,
        )
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=result.tap_device,
            vm_ip=result.vm_ip,
            socket_path=result.socket_path,
            firecracker_pid=result.pid,
            manifest_hash=effective_manifest.content_hash(),
            manifest_json=effective_manifest.to_json(),
            status="running",
            created_at=now,
            last_exec_at=None,
            source_checkpoint_id=checkpoint.id,
        )
```

With:

```python
        effective_recipe_id = recipe_id if recipe_id is not None else checkpoint.recipe_id
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=result.tap_device,
            vm_ip=result.vm_ip,
            socket_path=result.socket_path,
            firecracker_pid=result.pid,
            manifest_hash="none",
            manifest_json="{}",
            status="running",
            created_at=now,
            last_exec_at=None,
            source_checkpoint_id=checkpoint.id,
            recipe_id=effective_recipe_id,
        )
```

- [ ] **Step 5: Update imports at top of manager.py**

Remove `Manifest` from the import on line 22:

```python
from mshkn.models import Checkpoint, Computer
```

Add `Recipe` import:

```python
from mshkn.models import Checkpoint, Computer, Recipe
```

- [ ] **Step 6: Run lint**

Run: `.venv/bin/ruff check src/mshkn/vm/manager.py`
Expected: Clean (or fix any issues)

- [ ] **Step 7: Commit**

```bash
git add src/mshkn/vm/manager.py
git commit -m "feat: integrate recipe system into VMManager (replace capability lookup)"
```

---

### Task 8: Update Computer and Checkpoint API Endpoints

**Files:**
- Modify: `src/mshkn/api/computers.py`
- Modify: `src/mshkn/api/checkpoints.py`

- [ ] **Step 1: Update CreateRequest in computers.py**

Replace the `CreateRequest` class (lines 97-104):

```python
class CreateRequest(BaseModel):
    uses: list[str] = []
    needs: dict[str, object] | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    label: str | None = None
    meta_exec: str | None = None
```

With:

```python
class CreateRequest(BaseModel):
    recipe_id: str | None = None
    needs: dict[str, object] | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    label: str | None = None
    meta_exec: str | None = None
```

- [ ] **Step 2: Update CreateResponse in computers.py**

Replace `manifest_hash` with `recipe_id` in `CreateResponse` (lines 107-114):

```python
class CreateResponse(BaseModel):
    computer_id: str
    url: str
    recipe_id: str | None = None
    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None
```

- [ ] **Step 3: Update create_computer endpoint**

Replace the manifest creation and manager call (lines 273-274):

```python
    manifest = Manifest(uses=body.uses)
    computer = await vm_mgr.create(account.id, manifest, needs=body.needs)
```

With:

```python
    computer = await vm_mgr.create(account.id, recipe_id=body.recipe_id, needs=body.needs)
```

Replace `manifest_hash=computer.manifest_hash` in the response (line 314):

```python
        recipe_id=computer.recipe_id,
```

Remove the `from mshkn.models import Manifest` import in `computers.py` (line 34) — change to:

```python
from mshkn.models import Checkpoint
```

(Keep `Checkpoint` import if it's used in `_self_destruct`, which it is.)

- [ ] **Step 4: Update ForkRequest in checkpoints.py**

Replace the `ForkRequest` class (lines 35-42):

```python
class ForkRequest(BaseModel):
    manifest: dict[str, object] | None = None
    skip_manifest_check: bool = False
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    exclusive: Literal["error_on_conflict", "defer_on_conflict"] | None = None
    meta_exec: str | None = None
```

With:

```python
class ForkRequest(BaseModel):
    recipe_id: str | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    exclusive: Literal["error_on_conflict", "defer_on_conflict"] | None = None
    meta_exec: str | None = None
```

- [ ] **Step 5: Update fork_checkpoint endpoint**

Replace the manifest handling block (lines 74-92):

```python
    # Determine manifest for fork
    if body and body.manifest and "uses" in body.manifest:
        raw_uses = body.manifest["uses"]
        if not isinstance(raw_uses, list):
            raise HTTPException(status_code=422, detail="uses must be a list")
        new_uses = [str(u) for u in raw_uses]
        parent_manifest = Manifest.from_json(ckpt.manifest_json)

        is_breaking = not _is_manifest_additive(parent_manifest.uses, new_uses)
        if is_breaking and not body.skip_manifest_check:
            raise HTTPException(
                status_code=409,
                detail="Breaking manifest change (removal or version change). "
                       "Set skip_manifest_check: true to proceed anyway.",
            )

        fork_manifest = Manifest(uses=new_uses)
    else:
        fork_manifest = Manifest.from_json(ckpt.manifest_json)
```

With:

```python
    # Determine recipe_id for fork (metadata only — doesn't change disk contents)
    fork_recipe_id = body.recipe_id if body and body.recipe_id else None
```

Replace the fork call (line 125):

```python
    computer = await vm_mgr.fork_from_checkpoint(account.id, ckpt, fork_manifest)
```

With:

```python
    computer = await vm_mgr.fork_from_checkpoint(account.id, ckpt, recipe_id=fork_recipe_id)
```

Remove `_is_manifest_additive` function (lines 54-56) and the `from mshkn.models import Manifest` import in fork_checkpoint (line 66).

Update the deferred payload to use recipe_id instead of manifest (lines 106-113):

```python
                payload = {
                    "checkpoint_id": checkpoint_id,
                    "recipe_id": body.recipe_id,
                    "exec": body.exec,
                    "self_destruct": body.self_destruct,
                    "callback_url": body.callback_url,
                    "meta_exec": body.meta_exec,
                }
```

- [ ] **Step 6: Update _self_destruct checkpoint creation in computers.py**

In `_self_destruct` (line ~182-196), the `Checkpoint` constructor uses `manifest_hash=computer.manifest_hash` and `manifest_json=computer.manifest_json`. Add `recipe_id=computer.recipe_id`:

```python
    ckpt = Checkpoint(
        id=checkpoint_id,
        account_id=account.id,
        parent_id=parent_id,
        computer_id=computer.id,
        thin_volume_id=ckpt_volume_id,
        manifest_hash=computer.manifest_hash,
        manifest_json=computer.manifest_json,
        r2_prefix=r2_prefix,
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=label,
        pinned=False,
        created_at=now,
        recipe_id=computer.recipe_id,
    )
```

- [ ] **Step 6b: Update checkpoint_computer endpoint in computers.py**

In `checkpoint_computer` (line ~549-563), same change — add `recipe_id=computer.recipe_id` to the Checkpoint constructor.

Also update `CheckpointResponse` (line ~492-494) to include recipe_id:

```python
class CheckpointResponse(BaseModel):
    checkpoint_id: str
    recipe_id: str | None = None
```

And update the return (line ~570-573):

```python
    return CheckpointResponse(
        checkpoint_id=checkpoint_id,
        recipe_id=computer.recipe_id,
    )
```

- [ ] **Step 7: Update _process_deferred in computers.py**

In `_process_deferred` (line ~607-608), replace:

```python
        fork_manifest = Manifest.from_json(latest_ckpt.manifest_json)
        computer = await vm_mgr.fork_from_checkpoint(account.id, latest_ckpt, fork_manifest)
```

With:

```python
        computer = await vm_mgr.fork_from_checkpoint(
            account.id, latest_ckpt, recipe_id=latest_ckpt.recipe_id,
        )
```

Remove the `from mshkn.models import Manifest` import at the top of `_process_deferred` (it's no longer needed here — it only appears in `computers.py` for `Checkpoint` import which stays).

- [ ] **Step 7b: Update ingress.py (CRITICAL — missing from original plan)**

In `src/mshkn/api/ingress.py`, the `_do_create` function (line ~420-483) and `_do_fork` function (line ~486-549) both use `Manifest`. Update both:

**In `_do_create` (line ~424-446):** Change signature from `uses: list[str]` to remove `uses`, and replace:

```python
    manifest = Manifest(uses=uses)
    computer = await vm_manager.create(account_id, manifest)
```

With:

```python
    computer = await vm_manager.create(account_id)
```

Update the return dict to use `recipe_id` instead of `manifest_hash`:

```python
    "recipe_id": computer.recipe_id,
```

**In `_do_fork` (line ~501-538):** Remove `Manifest` import and replace:

```python
    fork_manifest = Manifest.from_json(ckpt.manifest_json)
    ...
    computer = await vm_manager.fork_from_checkpoint(account_id, ckpt, fork_manifest)
```

With:

```python
    computer = await vm_manager.fork_from_checkpoint(account_id, ckpt, recipe_id=ckpt.recipe_id)
```

Also update the deferred payload in `_do_fork` to remove manifest-related fields.

NOTE: Ingress Starlark transforms may pass `uses` in the action dict. The Starlark action handling that calls `_do_create` will need its `uses` parameter removed or ignored. Review the callers of `_do_create` to ensure they don't pass `uses` anymore.

- [ ] **Step 8: Run lint + type check**

Run: `.venv/bin/ruff check src/mshkn/api/ && .venv/bin/mypy src/mshkn/api/`
Expected: Clean (fix any issues)

- [ ] **Step 9: Run unit tests**

Run: `.venv/bin/pytest tests/ --ignore=tests/e2e --ignore=tests/integration -x`
Expected: Some tests may need updates due to API changes. Fix them.

- [ ] **Step 10: Commit**

```bash
git add src/mshkn/api/computers.py src/mshkn/api/checkpoints.py
git commit -m "feat: replace uses/manifest with recipe_id in computer/checkpoint APIs"
```

---

## Chunk 4: Cleanup and Deployment

### Task 9: Remove Nix Capability System

**Files:**
- Delete: `src/mshkn/capability/__init__.py`
- Delete: `src/mshkn/capability/resolver.py`
- Delete: `src/mshkn/capability/builder.py`
- Delete: `src/mshkn/capability/cache.py`
- Delete: `src/mshkn/capability/eviction.py`
- Delete: `src/mshkn/capability/template_cache.py`
- Delete: `tests/test_capability.py`
- Modify: `src/mshkn/vm/manager.py` (remove old methods + imports)
- Modify: `src/mshkn/config.py` (remove `capability_cache_dir`)
- Modify: `src/mshkn/models.py` (remove `Manifest`, `CapabilityCacheEntry`)

- [ ] **Step 1: Delete capability directory and test file**

```bash
rm -rf src/mshkn/capability/
rm -f tests/test_capability.py
```

- [ ] **Step 2: Remove old methods from manager.py**

Delete `_get_or_build_capability_volume` method entirely (the old one, lines 226-292 area).

Delete `_build_l3_template` method (the old one that uses `capability.template_cache`, lines 294-383 area).

Keep `_build_l3_template_for_recipe` (added in Task 7).

Remove the bare rootfs L3 template fallback in `create()` — the `else` branch that uses `get_cached_template(self.db, "bare")`. For bare rootfs (no recipe), always cold-boot:

```python
            else:
                # Bare rootfs: cold-boot directly
                from mshkn.vm.staging import cold_boot_from_disk

                result = await cold_boot_from_disk(
                    disk_volume_id=volume_id,
                    final_slot=slot,
                    pool_name=self.config.thin_pool_name,
                    thin_volume_sectors=self.config.thin_volume_sectors,
                    final_volume_name=volume_name,
                    kernel_path=str(self.config.kernel_path),
                    socket_path=f"/tmp/fc-{computer_id}.socket",
                )
                vmstate_path = None
                memory_path = None
```

- [ ] **Step 3: Remove `Manifest` and `CapabilityCacheEntry` from models.py**

Delete the `Manifest` class (lines 8-23) and `CapabilityCacheEntry` class (lines 68-75) from `src/mshkn/models.py`.

Remove `import hashlib` and `import json` if no longer used.

- [ ] **Step 4: Remove `capability_cache_dir` from config.py**

Delete this line from `src/mshkn/config.py`:

```python
    capability_cache_dir: Path = field(default_factory=lambda: Path("/opt/mshkn/capability-cache"))
```

- [ ] **Step 5: Grep for remaining capability/manifest references**

Search for any remaining references to the old system:

```bash
.venv/bin/python -c "
import subprocess
result = subprocess.run(
    ['grep', '-rn', '--include=*.py', '-E', 'capability|Manifest|manifest_to_nix|nix_build|inject_closure|template_cache|CapabilityCacheEntry', 'src/', 'tests/'],
    capture_output=True, text=True
)
print(result.stdout or 'No references found')
"
```

Fix any remaining references. Expected remaining references:
- `manifest_hash` and `manifest_json` fields — these are vestigial and intentionally kept
- `_build_l3_template_for_recipe` — this is the new replacement

- [ ] **Step 6: Run full test suite + lint**

Run: `.venv/bin/ruff check src/ && .venv/bin/mypy src/ && .venv/bin/pytest tests/ --ignore=tests/e2e --ignore=tests/integration -x`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: remove Nix capability system (replaced by Docker recipes)"
```

---

### Task 10: Deploy and E2E Tests

**Files:**
- Modify: `tests/e2e/test_capability.py` (rewrite as recipe tests)

- [ ] **Step 1: Install Docker on server**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215 \
  "apt-get update && apt-get install -y docker.io && systemctl enable docker && systemctl start docker"
```

- [ ] **Step 2: Deploy to server**

```bash
git push
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215 \
  "cd /opt/mshkn && git pull && systemctl restart mshkn && systemctl restart litestream"
```

- [ ] **Step 3: Build mshkn-base image on server**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215 \
  "cd /opt/mshkn && cp /root/.ssh/id_ed25519.pub mshkn_key.pub && docker build -t mshkn-base -f Dockerfile.mshkn-base ."
```

- [ ] **Step 4: Clean stale VMs**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215 \
  "pkill -f firecracker || true; for tap in \$(ip -o link show type tun | awk -F: '{print \$2}' | tr -d ' '); do ip link del \"\$tap\" 2>/dev/null; done; for vol in \$(dmsetup ls --target thin 2>/dev/null | awk '{print \$1}' | grep -v mshkn-pool | grep -v mshkn-base); do dmsetup remove \"\$vol\" 2>/dev/null; done"
```

- [ ] **Step 5: Recreate test account if needed**

Check if account exists, recreate if DB was reset:

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215 \
  "sqlite3 /opt/mshkn/mshkn.db \"SELECT id FROM accounts WHERE id='acct-mike'\""
```

If empty, recreate:

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215 \
  "sqlite3 /opt/mshkn/mshkn.db \"INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES ('acct-mike', 'mk-test-key-2026', 20, datetime('now'))\""
```

- [ ] **Step 6: Rewrite E2E capability tests as recipe tests**

Replace `tests/e2e/test_capability.py` with recipe-focused tests:

```python
"""E2E tests for the Docker-based recipe system."""
from __future__ import annotations

import time

import httpx
import pytest

BASE = "http://135.181.6.215:8000"
HEADERS = {"Authorization": "Bearer mk-test-key-2026"}


@pytest.fixture
def client():
    return httpx.Client(base_url=BASE, headers=HEADERS, timeout=120)


def wait_for_recipe(client: httpx.Client, recipe_id: str, timeout: int = 300) -> dict:
    """Poll until recipe is ready or failed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/recipes/{recipe_id}")
        assert r.status_code == 200
        data = r.json()
        if data["status"] in ("ready", "failed"):
            return data
        time.sleep(5)
    raise TimeoutError(f"Recipe {recipe_id} did not complete in {timeout}s")


class TestRecipeCRUD:
    """T3.5' — Recipe CRUD and dedup."""

    def test_create_recipe(self, client: httpx.Client):
        r = client.post("/recipes", json={
            "dockerfile": "FROM mshkn-base\nRUN apt-get update && apt-get install -y jq",
        })
        assert r.status_code == 200
        data = r.json()
        assert "recipe_id" in data
        assert data["status"] in ("pending", "building", "ready")
        recipe_id = data["recipe_id"]

        # Wait for build
        result = wait_for_recipe(client, recipe_id)
        assert result["status"] == "ready"

    def test_dedup_same_dockerfile(self, client: httpx.Client):
        dockerfile = "FROM mshkn-base\nRUN echo dedup-test-unique-string"
        r1 = client.post("/recipes", json={"dockerfile": dockerfile})
        assert r1.status_code == 200
        id1 = r1.json()["recipe_id"]

        # Wait for first build
        wait_for_recipe(client, id1)

        # Same dockerfile should return same recipe
        r2 = client.post("/recipes", json={"dockerfile": dockerfile})
        assert r2.status_code == 200
        assert r2.json()["recipe_id"] == id1

    def test_list_recipes(self, client: httpx.Client):
        r = client.get("/recipes")
        assert r.status_code == 200
        recipes = r.json()
        assert isinstance(recipes, list)

    def test_get_recipe(self, client: httpx.Client):
        # Create one first
        r = client.post("/recipes", json={
            "dockerfile": "FROM mshkn-base\nRUN echo get-test",
        })
        recipe_id = r.json()["recipe_id"]
        wait_for_recipe(client, recipe_id)

        r2 = client.get(f"/recipes/{recipe_id}")
        assert r2.status_code == 200
        assert r2.json()["recipe_id"] == recipe_id

    def test_delete_recipe(self, client: httpx.Client):
        r = client.post("/recipes", json={
            "dockerfile": "FROM mshkn-base\nRUN echo delete-test",
        })
        recipe_id = r.json()["recipe_id"]
        wait_for_recipe(client, recipe_id)

        r2 = client.delete(f"/recipes/{recipe_id}")
        assert r2.status_code == 200

        r3 = client.get(f"/recipes/{recipe_id}")
        assert r3.status_code == 404


class TestBuildFailure:
    """Build failure produces status=failed with build_log."""

    def test_bad_dockerfile(self, client: httpx.Client):
        r = client.post("/recipes", json={
            "dockerfile": "FROM nonexistent-image-that-does-not-exist-12345",
        })
        assert r.status_code == 200
        recipe_id = r.json()["recipe_id"]

        result = wait_for_recipe(client, recipe_id)
        assert result["status"] == "failed"
        assert result["build_log"] is not None
        assert len(result["build_log"]) > 0


class TestComputerFromRecipe:
    """T3.6' — Computer boots from recipe and has expected tools."""

    def test_boot_with_recipe_and_verify_tool(self, client: httpx.Client):
        # Build recipe with curl (already in base, but add jq as proof)
        r = client.post("/recipes", json={
            "dockerfile": "FROM mshkn-base\nRUN apt-get update && apt-get install -y jq",
        })
        recipe_id = r.json()["recipe_id"]
        result = wait_for_recipe(client, recipe_id)
        assert result["status"] == "ready"

        # Create computer from recipe, exec to verify jq exists
        r2 = client.post("/computers", json={
            "recipe_id": recipe_id,
            "exec": "jq --version",
            "self_destruct": True,
        })
        assert r2.status_code == 200
        data = r2.json()
        assert data["exec_exit_code"] == 0
        assert "jq" in data["exec_stdout"]

    def test_boot_bare_no_recipe(self, client: httpx.Client):
        """Computer without recipe_id boots from bare rootfs."""
        r = client.post("/computers", json={
            "exec": "uname -a",
            "self_destruct": True,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["exec_exit_code"] == 0
        assert "Linux" in data["exec_stdout"]


class TestMultiToolRecipe:
    """T3.6' — Multi-tool Dockerfile (node + python)."""

    def test_multi_tool(self, client: httpx.Client):
        r = client.post("/recipes", json={
            "dockerfile": (
                "FROM mshkn-base\n"
                "RUN apt-get update && apt-get install -y python3 nodejs\n"
            ),
        })
        recipe_id = r.json()["recipe_id"]
        result = wait_for_recipe(client, recipe_id)
        assert result["status"] == "ready"

        r2 = client.post("/computers", json={
            "recipe_id": recipe_id,
            "exec": "python3 --version && node --version",
            "self_destruct": True,
        })
        assert r2.status_code == 200
        data = r2.json()
        assert data["exec_exit_code"] == 0
        assert "Python" in data["exec_stdout"]
        assert "v" in data["exec_stdout"]  # node version starts with v
```

- [ ] **Step 7: Run E2E tests**

Run: `MSHKN_API_URL=http://135.181.6.215:8000 .venv/bin/pytest tests/e2e/test_capability.py -v --tb=short`
Expected: All PASS (recipe builds may take 30-60s each)

- [ ] **Step 8: Run full E2E suite to check for regressions**

Run: `MSHKN_API_URL=http://135.181.6.215:8000 .venv/bin/pytest tests/e2e/ -v --tb=short`
Expected: All non-skipped tests pass

- [ ] **Step 9: Commit**

```bash
git add tests/e2e/test_capability.py
git commit -m "test: rewrite capability E2E tests for Docker recipe system"
```

---

## Task Dependencies

```
Task 1 (migration) ─┐
Task 2 (models)    ─┤
Task 3 (db ops)    ─┼── Task 5 (builder) ── Task 6 (API) ──┐
Task 4 (Dockerfile) ┘                                       ├── Task 9 (cleanup) ── Task 10 (deploy+E2E)
                                            Task 7 (manager) ┤
                                            Task 8 (endpoints)┘
```

Tasks 1-4 can be parallelized. Tasks 5-8 can be partially parallelized (5+6 together, 7+8 together). Task 9 must come after all integration. Task 10 is the final validation.
