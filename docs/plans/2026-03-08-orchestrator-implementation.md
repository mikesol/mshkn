# Orchestrator Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the mshkn orchestrator — a Python process that exposes 14 REST endpoints for agents to create, exec, checkpoint, fork, merge, and destroy disposable cloud computers backed by Firecracker microVMs.

**Architecture:** FastAPI async app on Hetzner bare metal. Talks to Firecracker via Unix socket API, dm-thin for CoW storage, R2 for checkpoint persistence, SSH for VM exec, Caddy for TLS/routing. SQLite for state. All primitives proven in Phase 1 foundation work.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, asyncssh, aiosqlite, lz4, ruff, mypy, pytest, uv

**Key references:**
- Design doc: `docs/plans/2026-03-08-orchestrator-design.md`
- Original product design: `docs/plans/2026-03-07-disposable-cloud-computers-design.md`
- Test plan: `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md`
- Foundation learnings: see MEMORY.md (dm-thin, VM snapshots, R2, Nix, merge all proven)

**Server details:**
- Hetzner AX41-NVMe at 135.181.6.215
- SSH: `ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215`
- Firecracker v1.14.2 at `/usr/local/bin/firecracker`
- Kernel at `/opt/firecracker/vmlinux.bin`
- Base rootfs at `/opt/firecracker/rootfs-proper.ext4`
- Nix 2.34.0 installed (daemon mode)
- rclone configured for R2 at `/root/.config/rclone/rclone.conf`

**Important conventions:**
- Always use `.venv` when running python, pytest, or formatters
- mypy strict mode — no `Any` unless genuinely necessary
- ruff with strict config — no exceptions
- Raw SQL, no ORM. Typed wrapper functions returning dataclasses.
- All subprocess calls to system tools (dmsetup, firecracker, nix-build, etc.) go through helper functions that log the command and check return codes.

---

## Task 1: Project Scaffolding + Tooling

**Files:**
- Create: `pyproject.toml`
- Create: `src/mshkn/__init__.py`
- Create: `src/mshkn/main.py`
- Create: `tests/__init__.py`
- Create: `tests/test_health.py`
- Modify: `.gitignore`

**Step 1: Initialize uv project and install dependencies**

```bash
cd /home/mikesol/Documents/GitHub/mshkn
uv init --lib --name mshkn
```

Then replace `pyproject.toml` with:

```toml
[project]
name = "mshkn"
version = "0.1.0"
description = "Computers that fork — disposable cloud computers for AI agents"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "aiosqlite>=0.21",
    "asyncssh>=2.18",
    "lz4>=4.3",
    "httpx>=0.28",
    "sse-starlette>=2.1",
    "pydantic>=2.10",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "mypy>=1.13",
    "ruff>=0.8",
    "httpx>=0.28",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = [
    "E",     # pycodestyle errors
    "W",     # pycodestyle warnings
    "F",     # pyflakes
    "I",     # isort
    "N",     # pep8-naming
    "UP",    # pyupgrade
    "B",     # flake8-bugbear
    "A",     # flake8-builtins
    "SIM",   # flake8-simplify
    "TCH",   # flake8-type-checking
    "RUF",   # ruff-specific
    "PTH",   # flake8-use-pathlib
    "RET",   # flake8-return
    "ARG",   # flake8-unused-arguments
    "ERA",   # eradicate (commented-out code)
]

[tool.mypy]
python_version = "3.12"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
disallow_any_generics = true
no_implicit_reexport = true

[[tool.mypy.overrides]]
module = ["asyncssh.*", "lz4.*", "sse_starlette.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 2: Create virtual env and install**

```bash
uv venv
uv sync --all-extras
```

**Step 3: Update .gitignore**

Append to existing `.gitignore`:

```
.env
.venv/
__pycache__/
*.pyc
.mypy_cache/
.ruff_cache/
*.egg-info/
dist/
```

**Step 4: Create minimal FastAPI app**

`src/mshkn/__init__.py`:
```python
```

`src/mshkn/main.py`:
```python
from fastapi import FastAPI

app = FastAPI(title="mshkn", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

**Step 5: Write the first test**

`tests/__init__.py`:
```python
```

`tests/test_health.py`:
```python
from httpx import ASGITransport, AsyncClient

from mshkn.main import app


async def test_health() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

**Step 6: Run checks**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
.venv/bin/mypy src/ tests/
.venv/bin/pytest tests/ -v
```

All must pass. Fix any issues.

**Step 7: Commit**

```bash
git add -A
git commit -m "feat: project scaffolding with FastAPI, ruff, mypy, pytest"
```

---

## Task 2: Config + Models

**Files:**
- Create: `src/mshkn/config.py`
- Create: `src/mshkn/models.py`
- Create: `tests/test_models.py`

**Step 1: Write tests for models**

`tests/test_models.py`:
```python
from mshkn.models import Computer, Checkpoint, Account, Manifest


def test_computer_creation() -> None:
    c = Computer(
        id="comp-abc",
        account_id="acct-1",
        thin_volume_id=5,
        tap_device="tap5",
        vm_ip="172.16.5.2",
        socket_path="/tmp/fc-comp-abc.socket",
        firecracker_pid=1234,
        manifest_hash="abc123",
        status="running",
        created_at="2026-03-08T12:00:00",
        last_exec_at=None,
    )
    assert c.id == "comp-abc"
    assert c.status == "running"


def test_manifest_hash_deterministic() -> None:
    m1 = Manifest(uses=["python-3.12(numpy)", "ffmpeg"])
    m2 = Manifest(uses=["python-3.12(numpy)", "ffmpeg"])
    m3 = Manifest(uses=["ffmpeg", "python-3.12(numpy)"])
    assert m1.content_hash() == m2.content_hash()
    assert m1.content_hash() == m3.content_hash(), "order should not matter"


def test_manifest_hash_changes_with_content() -> None:
    m1 = Manifest(uses=["python-3.12(numpy)"])
    m2 = Manifest(uses=["python-3.12(numpy, pandas)"])
    assert m1.content_hash() != m2.content_hash()
```

**Step 2: Run tests, verify they fail**

```bash
.venv/bin/pytest tests/test_models.py -v
```

Expected: FAIL (imports don't exist)

**Step 3: Implement models**

`src/mshkn/models.py`:
```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Manifest:
    uses: list[str]

    def content_hash(self) -> str:
        normalized = sorted(self.uses)
        raw = json.dumps(normalized, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps({"uses": self.uses}, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        data = json.loads(raw)
        return cls(uses=data["uses"])


@dataclass
class Account:
    id: str
    api_key: str
    vm_limit: int
    created_at: str


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
    status: str
    created_at: str
    last_exec_at: str | None


@dataclass
class Checkpoint:
    id: str
    account_id: str
    parent_id: str | None
    computer_id: str | None
    manifest_hash: str
    manifest_json: str
    r2_prefix: str
    disk_delta_size_bytes: int | None
    memory_size_bytes: int | None
    label: str | None
    pinned: bool
    created_at: str


@dataclass
class CapabilityCacheEntry:
    manifest_hash: str
    image_path: str
    nix_closure_size_bytes: int | None
    image_size_bytes: int | None
    last_used_at: str
    created_at: str
```

**Step 4: Implement config**

`src/mshkn/config.py`:
```python
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Paths
    db_path: Path = field(default_factory=lambda: Path("/opt/mshkn/mshkn.db"))
    migrations_dir: Path = field(default_factory=lambda: Path("migrations"))
    base_rootfs_path: Path = field(default_factory=lambda: Path("/opt/firecracker/rootfs-proper.ext4"))
    kernel_path: Path = field(default_factory=lambda: Path("/opt/firecracker/vmlinux.bin"))
    capability_cache_dir: Path = field(default_factory=lambda: Path("/opt/mshkn/capability-cache"))
    checkpoint_local_dir: Path = field(default_factory=lambda: Path("/opt/mshkn/checkpoints"))
    ssh_key_path: Path = field(default_factory=lambda: Path("/root/.ssh/id_ed25519"))

    # dm-thin
    thin_pool_data_path: Path = field(default_factory=lambda: Path("/opt/mshkn/thin-pool-data"))
    thin_pool_meta_path: Path = field(default_factory=lambda: Path("/opt/mshkn/thin-pool-meta"))
    thin_pool_data_size_gb: int = 100
    thin_pool_name: str = "mshkn-pool"
    thin_volume_sectors: int = 4194304  # 2GB

    # R2
    r2_bucket: str = "mshkn-checkpoints"
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""

    # Networking
    domain: str = "mshkn.dev"
    caddy_admin_url: str = "http://localhost:2019"

    @classmethod
    def from_env(cls) -> Config:
        kwargs: dict[str, object] = {}
        env_map: dict[str, str] = {
            "MSHKN_HOST": "host",
            "MSHKN_PORT": "port",
            "MSHKN_DB_PATH": "db_path",
            "R2_ENDPOINT": "r2_endpoint",
            "R2_ACCESS_KEY_ID": "r2_access_key_id",
            "R2_SECRET_ACCESS_KEY": "r2_secret_access_key",
            "R2_BUCKET": "r2_bucket",
            "MSHKN_DOMAIN": "domain",
        }
        for env_var, attr in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                if attr == "port":
                    kwargs[attr] = int(val)
                elif attr in ("db_path", "migrations_dir", "base_rootfs_path", "kernel_path"):
                    kwargs[attr] = Path(val)
                else:
                    kwargs[attr] = val
        return cls(**kwargs)
```

**Step 5: Run all checks**

```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
```

**Step 6: Commit**

```bash
git add src/mshkn/config.py src/mshkn/models.py tests/test_models.py
git commit -m "feat: add config and domain models"
```

---

## Task 3: Database Layer

**Files:**
- Create: `migrations/001_initial.sql`
- Create: `src/mshkn/db.py`
- Create: `tests/test_db.py`

**Step 1: Write migration file**

`migrations/001_initial.sql`:
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
    status TEXT NOT NULL DEFAULT 'creating',
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

**Step 2: Write DB tests**

`tests/test_db.py`:
```python
from pathlib import Path

import aiosqlite

from mshkn.db import run_migrations, insert_account, get_account_by_key, insert_computer, get_computer, list_computers_by_account, insert_checkpoint, get_checkpoint, update_computer_status
from mshkn.models import Account, Computer, Checkpoint


async def test_migrations_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path("migrations")
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, migrations_dir)
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in await cursor.fetchall()]
    assert "accounts" in tables
    assert "computers" in tables
    assert "checkpoints" in tables
    assert "capability_cache" in tables


async def test_migrations_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path("migrations")
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, migrations_dir)
        await run_migrations(db, migrations_dir)  # second run should be a no-op


async def test_account_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(db, Account(
            id="acct-1", api_key="key-abc", vm_limit=10, created_at="2026-03-08T00:00:00",
        ))
        result = await get_account_by_key(db, "key-abc")
    assert result is not None
    assert result.id == "acct-1"


async def test_computer_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(db, Account(
            id="acct-1", api_key="key-abc", vm_limit=10, created_at="2026-03-08T00:00:00",
        ))
        comp = Computer(
            id="comp-1", account_id="acct-1", thin_volume_id=1,
            tap_device="tap1", vm_ip="172.16.1.2",
            socket_path="/tmp/fc-comp-1.socket", firecracker_pid=999,
            manifest_hash="abc", status="running",
            created_at="2026-03-08T00:00:00", last_exec_at=None,
        )
        await insert_computer(db, comp)
        result = await get_computer(db, "comp-1")
    assert result is not None
    assert result.vm_ip == "172.16.1.2"
    assert result.status == "running"


async def test_update_computer_status(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(db, Account(
            id="acct-1", api_key="key-abc", vm_limit=10, created_at="2026-03-08T00:00:00",
        ))
        comp = Computer(
            id="comp-1", account_id="acct-1", thin_volume_id=1,
            tap_device="tap1", vm_ip="172.16.1.2",
            socket_path="/tmp/fc-comp-1.socket", firecracker_pid=999,
            manifest_hash="abc", status="running",
            created_at="2026-03-08T00:00:00", last_exec_at=None,
        )
        await insert_computer(db, comp)
        await update_computer_status(db, "comp-1", "destroyed")
        result = await get_computer(db, "comp-1")
    assert result is not None
    assert result.status == "destroyed"


async def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(db, Account(
            id="acct-1", api_key="key-abc", vm_limit=10, created_at="2026-03-08T00:00:00",
        ))
        ckpt = Checkpoint(
            id="ckpt-1", account_id="acct-1", parent_id=None, computer_id="comp-1",
            manifest_hash="abc", manifest_json='{"uses":["python-3.12"]}',
            r2_prefix="acct-1/ckpt-1", disk_delta_size_bytes=1024,
            memory_size_bytes=512000, label="initial", pinned=False,
            created_at="2026-03-08T00:00:00",
        )
        await insert_checkpoint(db, ckpt)
        result = await get_checkpoint(db, "ckpt-1")
    assert result is not None
    assert result.manifest_hash == "abc"
    assert result.parent_id is None
```

**Step 3: Run tests, verify they fail**

```bash
.venv/bin/pytest tests/test_db.py -v
```

**Step 4: Implement db.py**

`src/mshkn/db.py`:
```python
from __future__ import annotations

from pathlib import Path

import aiosqlite

from mshkn.models import Account, Checkpoint, Computer


async def run_migrations(db: aiosqlite.Connection, migrations_dir: Path) -> None:
    # Ensure _migrations table exists (bootstrap — first migration also creates it,
    # but we need to check it before we can query it)
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
        sql = sql_file.read_text()
        # Skip the _migrations CREATE in the migration file since we already have it
        for statement in sql.split(";"):
            stmt = statement.strip()
            if stmt and "CREATE TABLE _migrations" not in stmt:
                await db.execute(stmt)
        await db.execute("INSERT INTO _migrations (filename) VALUES (?)", (sql_file.name,))
        await db.commit()


async def insert_account(db: aiosqlite.Connection, account: Account) -> None:
    await db.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        (account.id, account.api_key, account.vm_limit, account.created_at),
    )
    await db.commit()


async def get_account_by_key(db: aiosqlite.Connection, api_key: str) -> Account | None:
    cursor = await db.execute(
        "SELECT id, api_key, vm_limit, created_at FROM accounts WHERE api_key = ?",
        (api_key,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return Account(id=row[0], api_key=row[1], vm_limit=row[2], created_at=row[3])


async def insert_computer(db: aiosqlite.Connection, computer: Computer) -> None:
    await db.execute(
        "INSERT INTO computers "
        "(id, account_id, thin_volume_id, tap_device, vm_ip, socket_path, "
        "firecracker_pid, manifest_hash, status, created_at, last_exec_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            computer.id, computer.account_id, computer.thin_volume_id,
            computer.tap_device, computer.vm_ip, computer.socket_path,
            computer.firecracker_pid, computer.manifest_hash,
            computer.status, computer.created_at, computer.last_exec_at,
        ),
    )
    await db.commit()


async def get_computer(db: aiosqlite.Connection, computer_id: str) -> Computer | None:
    cursor = await db.execute(
        "SELECT id, account_id, thin_volume_id, tap_device, vm_ip, socket_path, "
        "firecracker_pid, manifest_hash, status, created_at, last_exec_at "
        "FROM computers WHERE id = ?",
        (computer_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return Computer(
        id=row[0], account_id=row[1], thin_volume_id=row[2],
        tap_device=row[3], vm_ip=row[4], socket_path=row[5],
        firecracker_pid=row[6], manifest_hash=row[7],
        status=row[8], created_at=row[9], last_exec_at=row[10],
    )


async def list_computers_by_account(
    db: aiosqlite.Connection, account_id: str
) -> list[Computer]:
    cursor = await db.execute(
        "SELECT id, account_id, thin_volume_id, tap_device, vm_ip, socket_path, "
        "firecracker_pid, manifest_hash, status, created_at, last_exec_at "
        "FROM computers WHERE account_id = ? AND status != 'destroyed'",
        (account_id,),
    )
    rows = await cursor.fetchall()
    return [
        Computer(
            id=r[0], account_id=r[1], thin_volume_id=r[2],
            tap_device=r[3], vm_ip=r[4], socket_path=r[5],
            firecracker_pid=r[6], manifest_hash=r[7],
            status=r[8], created_at=r[9], last_exec_at=r[10],
        )
        for r in rows
    ]


async def update_computer_status(
    db: aiosqlite.Connection, computer_id: str, status: str
) -> None:
    await db.execute(
        "UPDATE computers SET status = ? WHERE id = ?",
        (status, computer_id),
    )
    await db.commit()


async def insert_checkpoint(db: aiosqlite.Connection, checkpoint: Checkpoint) -> None:
    await db.execute(
        "INSERT INTO checkpoints "
        "(id, account_id, parent_id, computer_id, manifest_hash, manifest_json, "
        "r2_prefix, disk_delta_size_bytes, memory_size_bytes, label, pinned, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            checkpoint.id, checkpoint.account_id, checkpoint.parent_id,
            checkpoint.computer_id, checkpoint.manifest_hash,
            checkpoint.manifest_json, checkpoint.r2_prefix,
            checkpoint.disk_delta_size_bytes, checkpoint.memory_size_bytes,
            checkpoint.label, int(checkpoint.pinned), checkpoint.created_at,
        ),
    )
    await db.commit()


async def get_checkpoint(db: aiosqlite.Connection, checkpoint_id: str) -> Checkpoint | None:
    cursor = await db.execute(
        "SELECT id, account_id, parent_id, computer_id, manifest_hash, manifest_json, "
        "r2_prefix, disk_delta_size_bytes, memory_size_bytes, label, pinned, created_at "
        "FROM checkpoints WHERE id = ?",
        (checkpoint_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return Checkpoint(
        id=row[0], account_id=row[1], parent_id=row[2], computer_id=row[3],
        manifest_hash=row[4], manifest_json=row[5], r2_prefix=row[6],
        disk_delta_size_bytes=row[7], memory_size_bytes=row[8],
        label=row[9], pinned=bool(row[10]), created_at=row[11],
    )


async def list_checkpoints_by_account(
    db: aiosqlite.Connection, account_id: str
) -> list[Checkpoint]:
    cursor = await db.execute(
        "SELECT id, account_id, parent_id, computer_id, manifest_hash, manifest_json, "
        "r2_prefix, disk_delta_size_bytes, memory_size_bytes, label, pinned, created_at "
        "FROM checkpoints WHERE account_id = ? ORDER BY created_at DESC",
        (account_id,),
    )
    rows = await cursor.fetchall()
    return [
        Checkpoint(
            id=r[0], account_id=r[1], parent_id=r[2], computer_id=r[3],
            manifest_hash=r[4], manifest_json=r[5], r2_prefix=r[6],
            disk_delta_size_bytes=r[7], memory_size_bytes=r[8],
            label=r[9], pinned=bool(r[10]), created_at=r[11],
        )
        for r in rows
    ]


async def delete_checkpoint(db: aiosqlite.Connection, checkpoint_id: str) -> None:
    await db.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
    await db.commit()
```

**Step 5: Run all checks**

```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
```

**Step 6: Commit**

```bash
git add migrations/ src/mshkn/db.py tests/test_db.py
git commit -m "feat: database layer with migrations and typed queries"
```

---

## Task 4: VM Infrastructure — Network + Storage

**Files:**
- Create: `src/mshkn/vm/__init__.py`
- Create: `src/mshkn/vm/network.py`
- Create: `src/mshkn/vm/storage.py`
- Create: `src/mshkn/shell.py`
- Create: `tests/test_network.py`
- Create: `tests/test_storage.py`

These modules wrap the shell commands we proved in Phase 1. They manage tap devices, IP allocation, and dm-thin volumes. They are tested with unit tests for the logic (IP/MAC calculation) and will need root on the server for integration tests.

**Step 1: Write tests for network logic**

`tests/test_network.py`:
```python
from mshkn.vm.network import slot_to_ip, slot_to_mac, slot_to_tap


def test_slot_to_ip() -> None:
    assert slot_to_ip(0) == ("172.16.0.1", "172.16.0.2")
    assert slot_to_ip(5) == ("172.16.5.1", "172.16.5.2")
    assert slot_to_ip(255) == ("172.16.255.1", "172.16.255.2")


def test_slot_to_mac() -> None:
    assert slot_to_mac(0) == "06:00:AC:10:00:02"
    assert slot_to_mac(5) == "06:00:AC:10:05:02"
    assert slot_to_mac(255) == "06:00:AC:10:FF:02"


def test_slot_to_tap() -> None:
    assert slot_to_tap(0) == "tap0"
    assert slot_to_tap(42) == "tap42"
```

**Step 2: Run tests, verify fail**

```bash
.venv/bin/pytest tests/test_network.py -v
```

**Step 3: Implement shell helper**

`src/mshkn/shell.py`:
```python
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ShellError(Exception):
    def __init__(self, cmd: str, returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Command failed ({returncode}): {cmd}\n{stderr}")


async def run(cmd: str, check: bool = True) -> str:
    logger.debug("shell: %s", cmd)
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode()
    stderr = stderr_bytes.decode()

    if check and proc.returncode != 0:
        raise ShellError(cmd, proc.returncode or -1, stderr)

    return stdout
```

**Step 4: Implement network.py**

`src/mshkn/vm/__init__.py`:
```python
```

`src/mshkn/vm/network.py`:
```python
from __future__ import annotations

import logging

from mshkn.shell import run

logger = logging.getLogger(__name__)


def slot_to_ip(slot: int) -> tuple[str, str]:
    """Return (host_ip, vm_ip) for a given slot number."""
    return f"172.16.{slot}.1", f"172.16.{slot}.2"


def slot_to_mac(slot: int) -> str:
    """Return guest MAC address for a given slot. Encodes IP for fcnet-setup.sh."""
    return f"06:00:AC:10:{slot:02X}:02"


def slot_to_tap(slot: int) -> str:
    return f"tap{slot}"


async def create_tap(slot: int) -> None:
    tap = slot_to_tap(slot)
    host_ip, _ = slot_to_ip(slot)
    await run(f"ip tuntap add dev {tap} mode tap")
    await run(f"ip addr add {host_ip}/30 dev {tap}")
    await run(f"ip link set {tap} up")
    logger.info("Created tap device %s at %s/30", tap, host_ip)


async def destroy_tap(slot: int) -> None:
    tap = slot_to_tap(slot)
    await run(f"ip link del {tap}", check=False)
    logger.info("Destroyed tap device %s", tap)


async def ensure_nat(interface: str = "enp35s0") -> None:
    result = await run(
        f"iptables -t nat -C POSTROUTING -o {interface} -j MASQUERADE",
        check=False,
    )
    if "No chain" in result or result == "":
        # Rule might already exist (check returns empty on success)
        pass
    await run(
        f"iptables -t nat -A POSTROUTING -o {interface} -j MASQUERADE",
        check=False,
    )
```

**Step 5: Implement storage.py**

`src/mshkn/vm/storage.py`:
```python
from __future__ import annotations

import logging
from pathlib import Path

from mshkn.shell import run

logger = logging.getLogger(__name__)


async def init_thin_pool(
    pool_name: str,
    data_path: Path,
    meta_path: Path,
    data_size_gb: int,
) -> None:
    """Create dm-thin pool backed by loopback files."""
    await run(f"truncate -s {data_size_gb}G {data_path}")
    await run(f"truncate -s 256M {meta_path}")

    data_loop = (await run(f"losetup --find --show {data_path}")).strip()
    meta_loop = (await run(f"losetup --find --show {meta_path}")).strip()

    await run(f"dd if=/dev/zero of={meta_loop} bs=4096 count=1")
    data_sectors = (await run(f"blockdev --getsz {data_loop}")).strip()

    await run(
        f"dmsetup create {pool_name} "
        f"--table '0 {data_sectors} thin-pool {meta_loop} {data_loop} 128 0'"
    )
    logger.info("Created thin pool %s (data=%s, meta=%s)", pool_name, data_path, meta_path)


async def create_base_volume(
    pool_name: str,
    volume_id: int,
    volume_name: str,
    sectors: int,
    source_image: Path,
) -> None:
    """Create a thin volume and write a base image to it."""
    await run(f"dmsetup message {pool_name} 0 'create_thin {volume_id}'")
    await run(
        f"dmsetup create {volume_name} "
        f"--table '0 {sectors} thin /dev/mapper/{pool_name} {volume_id}'"
    )
    await run(f"dd if={source_image} of=/dev/mapper/{volume_name} bs=4M")
    logger.info("Created base volume %s (vol %d) from %s", volume_name, volume_id, source_image)


async def create_snapshot(
    pool_name: str,
    source_volume_id: int,
    new_volume_id: int,
    new_volume_name: str,
    sectors: int,
) -> None:
    """Create a dm-thin snapshot (CoW copy of source)."""
    await run(f"dmsetup message {pool_name} 0 'create_snap {new_volume_id} {source_volume_id}'")
    await run(
        f"dmsetup create {new_volume_name} "
        f"--table '0 {sectors} thin /dev/mapper/{pool_name} {new_volume_id}'"
    )
    logger.info("Created snapshot %s (vol %d from %d)", new_volume_name, new_volume_id, source_volume_id)


async def remove_volume(pool_name: str, volume_name: str, volume_id: int) -> None:
    """Remove a dm-thin volume."""
    await run(f"dmsetup remove {volume_name}", check=False)
    await run(f"dmsetup message {pool_name} 0 'delete {volume_id}'", check=False)
    logger.info("Removed volume %s (vol %d)", volume_name, volume_id)
```

**Step 6: Run checks**

```bash
.venv/bin/pytest tests/test_network.py -v
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
```

**Step 7: Commit**

```bash
git add src/mshkn/shell.py src/mshkn/vm/ tests/test_network.py
git commit -m "feat: VM infrastructure — network allocation and dm-thin storage"
```

---

## Task 5: Firecracker API Client

**Files:**
- Create: `src/mshkn/vm/firecracker.py`
- Create: `tests/test_firecracker.py`

This wraps the Firecracker HTTP API over Unix socket. We use httpx with a Unix socket transport.

**Step 1: Write tests**

`tests/test_firecracker.py`:
```python
from mshkn.vm.firecracker import FirecrackerConfig


def test_firecracker_config_to_api_calls() -> None:
    """Test that config generates the correct API call sequence."""
    config = FirecrackerConfig(
        socket_path="/tmp/fc-test.socket",
        kernel_path="/opt/firecracker/vmlinux.bin",
        rootfs_path="/dev/mapper/test-vol",
        tap_device="tap5",
        guest_mac="06:00:AC:10:05:02",
        vcpu_count=2,
        mem_size_mib=512,
    )
    assert config.boot_args.startswith("console=ttyS0")
    assert config.vcpu_count == 2
    assert config.mem_size_mib == 512
```

**Step 2: Implement firecracker.py**

`src/mshkn/vm/firecracker.py`:
```python
from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from mshkn.shell import run

logger = logging.getLogger(__name__)

BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/init root=/dev/vda rw"


@dataclass(frozen=True)
class FirecrackerConfig:
    socket_path: str
    kernel_path: str
    rootfs_path: str
    tap_device: str
    guest_mac: str
    vcpu_count: int = 2
    mem_size_mib: int = 512
    boot_args: str = field(default=BOOT_ARGS)


class FirecrackerClient:
    """Async client for a single Firecracker instance via Unix socket API."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        self._client = httpx.AsyncClient(transport=transport, base_url="http://localhost")

    async def configure_and_boot(self, config: FirecrackerConfig) -> None:
        await self._put("/machine-config", {
            "vcpu_count": config.vcpu_count,
            "mem_size_mib": config.mem_size_mib,
        })
        await self._put("/boot-source", {
            "kernel_image_path": config.kernel_path,
            "boot_args": config.boot_args,
        })
        await self._put("/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": config.rootfs_path,
            "is_root_device": True,
            "is_read_only": False,
        })
        await self._put("/network-interfaces/eth0", {
            "iface_id": "eth0",
            "guest_mac": config.guest_mac,
            "host_dev_name": config.tap_device,
        })
        await self._put("/actions", {"action_type": "InstanceStart"})
        logger.info("Firecracker VM configured and started via %s", self.socket_path)

    async def pause(self) -> None:
        await self._patch("/vm", {"state": "Paused"})

    async def resume(self) -> None:
        await self._patch("/vm", {"state": "Resumed"})

    async def create_snapshot(self, snapshot_path: str, memory_path: str) -> None:
        await self._put("/snapshot/create", {
            "snapshot_type": "Full",
            "snapshot_path": snapshot_path,
            "mem_file_path": memory_path,
        })

    async def load_snapshot(
        self, snapshot_path: str, memory_path: str, resume_vm: bool = True
    ) -> None:
        await self._put("/snapshot/load", {
            "snapshot_path": snapshot_path,
            "mem_backend": {
                "backend_type": "File",
                "backend_path": memory_path,
            },
            "resume_vm": resume_vm,
        })

    async def close(self) -> None:
        await self._client.aclose()

    async def _put(self, path: str, body: dict[str, object]) -> None:
        resp = await self._client.put(path, json=body)
        if resp.status_code not in (200, 204):
            logger.error("Firecracker PUT %s failed: %s %s", path, resp.status_code, resp.text)
            resp.raise_for_status()

    async def _patch(self, path: str, body: dict[str, object]) -> None:
        resp = await self._client.patch(path, json=body)
        if resp.status_code not in (200, 204):
            logger.error("Firecracker PATCH %s failed: %s %s", path, resp.status_code, resp.text)
            resp.raise_for_status()


async def start_firecracker_process(socket_path: str) -> int:
    """Start a Firecracker process and return its PID."""
    # Remove stale socket
    await run(f"rm -f {socket_path}", check=False)

    proc = await asyncio.create_subprocess_exec(
        "firecracker", "--api-sock", socket_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # Give it a moment to create the socket
    await asyncio.sleep(0.5)
    logger.info("Started Firecracker process PID=%d socket=%s", proc.pid, socket_path)
    return proc.pid


async def kill_firecracker_process(pid: int) -> None:
    """Kill a Firecracker process by PID."""
    try:
        import os
        os.kill(pid, signal.SIGKILL)
        logger.info("Killed Firecracker PID=%d", pid)
    except ProcessLookupError:
        logger.warning("Firecracker PID=%d already dead", pid)
```

**Step 3: Run checks**

```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
```

**Step 4: Commit**

```bash
git add src/mshkn/vm/firecracker.py tests/test_firecracker.py
git commit -m "feat: Firecracker API client with snapshot support"
```

---

## Task 6: Auth Middleware + API Skeleton

**Files:**
- Create: `src/mshkn/api/__init__.py`
- Create: `src/mshkn/api/auth.py`
- Create: `src/mshkn/api/computers.py`
- Create: `src/mshkn/api/checkpoints.py`
- Create: `tests/test_auth.py`
- Modify: `src/mshkn/main.py`

**Step 1: Write auth tests**

`tests/test_auth.py`:
```python
from pathlib import Path

import aiosqlite
from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account, run_migrations
from mshkn.main import app, get_db
from mshkn.models import Account


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(db_path)
    await run_migrations(db, Path("migrations"))
    await insert_account(db, Account(
        id="acct-1", api_key="test-key-123", vm_limit=10, created_at="2026-03-08T00:00:00",
    ))
    return db


async def test_no_auth_returns_401(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    app.dependency_overrides[get_db] = lambda: db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={"uses": []})
    assert resp.status_code == 401
    app.dependency_overrides.clear()
    await db.close()


async def test_bad_key_returns_401(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    app.dependency_overrides[get_db] = lambda: db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers", json={"uses": []},
            headers={"Authorization": "Bearer wrong-key"},
        )
    assert resp.status_code == 401
    app.dependency_overrides.clear()
    await db.close()


async def test_health_no_auth_required() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
```

**Step 2: Implement auth**

`src/mshkn/api/__init__.py`:
```python
```

`src/mshkn/api/auth.py`:
```python
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

import aiosqlite

from mshkn.db import get_account_by_key
from mshkn.models import Account


async def require_account(request: Request) -> Account:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    api_key = auth[7:]

    db: aiosqlite.Connection = request.app.state.db
    account = await get_account_by_key(db, api_key)
    if account is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return account
```

**Step 3: Create endpoint stubs**

`src/mshkn/api/computers.py`:
```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mshkn.api.auth import require_account
from mshkn.models import Account

router = APIRouter(prefix="/computers", tags=["computers"])


class CreateRequest(BaseModel):
    uses: list[str] = []
    needs: dict[str, object] | None = None


class CreateResponse(BaseModel):
    computer_id: str
    url: str
    manifest_hash: str


@router.post("", response_model=CreateResponse)
async def create_computer(
    body: CreateRequest,
    account: Account = Depends(require_account),
) -> CreateResponse:
    # TODO: implement in Task 7
    raise NotImplementedError


class ExecRequest(BaseModel):
    command: str


@router.post("/{computer_id}/exec")
async def exec_command(
    computer_id: str,
    body: ExecRequest,
    account: Account = Depends(require_account),
) -> dict[str, object]:
    # TODO: implement in Task 8
    raise NotImplementedError


@router.delete("/{computer_id}")
async def destroy_computer(
    computer_id: str,
    account: Account = Depends(require_account),
) -> dict[str, str]:
    # TODO: implement in Task 7
    raise NotImplementedError
```

`src/mshkn/api/checkpoints.py`:
```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mshkn.api.auth import require_account
from mshkn.models import Account

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])


class ForkRequest(BaseModel):
    manifest: dict[str, object] | None = None


@router.post("/{checkpoint_id}/fork")
async def fork_checkpoint(
    checkpoint_id: str,
    body: ForkRequest | None = None,
    account: Account = Depends(require_account),
) -> dict[str, object]:
    # TODO: implement in Task 9
    raise NotImplementedError


class MergeRequest(BaseModel):
    checkpoint_a: str
    checkpoint_b: str


@router.post("/merge")
async def merge_checkpoints(
    body: MergeRequest,
    account: Account = Depends(require_account),
) -> dict[str, object]:
    # TODO: implement in Task 10
    raise NotImplementedError


@router.get("")
async def list_checkpoints(
    account: Account = Depends(require_account),
) -> list[dict[str, object]]:
    # TODO: implement
    raise NotImplementedError


@router.delete("/{checkpoint_id}")
async def delete_checkpoint(
    checkpoint_id: str,
    account: Account = Depends(require_account),
) -> dict[str, str]:
    # TODO: implement
    raise NotImplementedError
```

**Step 4: Wire up main.py**

Replace `src/mshkn/main.py`:
```python
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite
from fastapi import FastAPI

from mshkn.config import Config
from mshkn.db import run_migrations
from mshkn.api.computers import router as computers_router
from mshkn.api.checkpoints import router as checkpoints_router


async def get_db() -> aiosqlite.Connection:
    """Dependency placeholder — overridden in tests, set in lifespan for prod."""
    raise RuntimeError("DB not initialized")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = Config.from_env()
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(config.db_path)
    await run_migrations(db, config.migrations_dir)
    app.state.db = db
    app.state.config = config
    yield
    await db.close()


app = FastAPI(title="mshkn", version="0.1.0", lifespan=lifespan)
app.include_router(computers_router)
app.include_router(checkpoints_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

**Step 5: Run checks**

```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
```

Note: some tests may need adjustment for the new main.py lifespan. The test_health test should still pass. Auth tests should pass. Existing tests should still pass.

**Step 6: Commit**

```bash
git add src/mshkn/api/ src/mshkn/main.py tests/test_auth.py
git commit -m "feat: auth middleware and API endpoint stubs"
```

---

## Task 7: computer_create + computer_destroy (end-to-end)

**Files:**
- Modify: `src/mshkn/api/computers.py`
- Create: `src/mshkn/vm/manager.py`
- Create: `tests/test_vm_manager.py`

This is the critical path — wiring create and destroy through the full stack. This task produces the first VM that's created via the API.

**Step 1: Implement VM manager**

`src/mshkn/vm/manager.py` — the core orchestration logic:

```python
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

from mshkn.config import Config
from mshkn.db import insert_computer, get_computer, update_computer_status, list_computers_by_account
from mshkn.models import Computer, Manifest
from mshkn.vm.firecracker import (
    FirecrackerClient,
    FirecrackerConfig,
    kill_firecracker_process,
    start_firecracker_process,
)
from mshkn.vm.network import create_tap, destroy_tap, slot_to_ip, slot_to_mac, slot_to_tap
from mshkn.vm.storage import create_snapshot, remove_volume

logger = logging.getLogger(__name__)


class VMManager:
    def __init__(self, config: Config, db: aiosqlite.Connection) -> None:
        self.config = config
        self.db = db
        self._next_slot = 1  # slot 0 reserved; will be loaded from DB on startup
        self._next_volume_id = 100  # volume 0 is base; start high to avoid conflicts

    async def initialize(self) -> None:
        """Load state from DB to set counters correctly."""
        computers = await list_computers_by_account(self.db, "%")  # TODO: list all
        if computers:
            max_vol = max(c.thin_volume_id for c in computers)
            self._next_volume_id = max_vol + 1
            # Parse slot from tap device name
            slots = [int(c.tap_device.replace("tap", "")) for c in computers]
            self._next_slot = max(slots) + 1

    def _allocate_slot(self) -> int:
        slot = self._next_slot
        self._next_slot += 1
        return slot

    def _allocate_volume_id(self) -> int:
        vol_id = self._next_volume_id
        self._next_volume_id += 1
        return vol_id

    async def create(self, account_id: str, manifest: Manifest) -> Computer:
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
        slot = self._allocate_slot()
        volume_id = self._allocate_volume_id()
        host_ip, vm_ip = slot_to_ip(slot)
        mac = slot_to_mac(slot)
        tap = slot_to_tap(slot)
        socket_path = f"/tmp/fc-{computer_id}.socket"
        volume_name = f"mshkn-{computer_id}"

        # 1. Create tap device
        await create_tap(slot)

        # 2. Create dm-thin snapshot from base
        await create_snapshot(
            pool_name=self.config.thin_pool_name,
            source_volume_id=0,  # base volume
            new_volume_id=volume_id,
            new_volume_name=volume_name,
            sectors=self.config.thin_volume_sectors,
        )

        # 3. Start Firecracker
        pid = await start_firecracker_process(socket_path)

        # 4. Configure and boot
        fc_client = FirecrackerClient(socket_path)
        try:
            await fc_client.configure_and_boot(FirecrackerConfig(
                socket_path=socket_path,
                kernel_path=str(self.config.kernel_path),
                rootfs_path=f"/dev/mapper/{volume_name}",
                tap_device=tap,
                guest_mac=mac,
                vcpu_count=2,
                mem_size_mib=512,
            ))
        finally:
            await fc_client.close()

        # 5. Wait for SSH readiness
        await self._wait_for_ssh(vm_ip)

        # 6. Record in DB
        now = datetime.now(timezone.utc).isoformat()
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=tap,
            vm_ip=vm_ip,
            socket_path=socket_path,
            firecracker_pid=pid,
            manifest_hash=manifest.content_hash(),
            status="running",
            created_at=now,
            last_exec_at=None,
        )
        await insert_computer(self.db, computer)
        logger.info("Created computer %s (slot=%d, ip=%s)", computer_id, slot, vm_ip)
        return computer

    async def destroy(self, computer_id: str) -> None:
        computer = await get_computer(self.db, computer_id)
        if computer is None:
            raise ValueError(f"Computer {computer_id} not found")

        # Kill Firecracker
        if computer.firecracker_pid is not None:
            await kill_firecracker_process(computer.firecracker_pid)

        # Remove dm-thin volume
        volume_name = f"mshkn-{computer_id}"
        await remove_volume(self.config.thin_pool_name, volume_name, computer.thin_volume_id)

        # Remove tap device
        slot = int(computer.tap_device.replace("tap", ""))
        await destroy_tap(slot)

        # Update DB
        await update_computer_status(self.db, computer_id, "destroyed")
        logger.info("Destroyed computer %s", computer_id)

    async def _wait_for_ssh(self, vm_ip: str, timeout: float = 30.0) -> None:
        """Poll SSH until the VM is reachable."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=2",
                    "-o", "IdentitiesOnly=yes",
                    "-i", str(self.config.ssh_key_path),
                    f"root@{vm_ip}", "true",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await proc.wait()
                if rc == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        raise TimeoutError(f"VM at {vm_ip} did not become reachable via SSH")
```

**Step 2: Wire into API**

Update `src/mshkn/api/computers.py` — replace the `create_computer` and `destroy_computer` stubs:

```python
@router.post("", response_model=CreateResponse)
async def create_computer(
    body: CreateRequest,
    request: Request,
    account: Account = Depends(require_account),
) -> CreateResponse:
    config: Config = request.app.state.config
    vm_mgr: VMManager = request.app.state.vm_manager
    manifest = Manifest(uses=body.uses)

    computer = await vm_mgr.create(account.id, manifest)
    url = f"https://{computer.id}.{config.domain}"
    return CreateResponse(
        computer_id=computer.id,
        url=url,
        manifest_hash=computer.manifest_hash,
    )


@router.delete("/{computer_id}")
async def destroy_computer(
    computer_id: str,
    request: Request,
    account: Account = Depends(require_account),
) -> dict[str, str]:
    vm_mgr: VMManager = request.app.state.vm_manager
    await vm_mgr.destroy(computer_id)
    return {"status": "destroyed"}
```

Add to imports in computers.py:
```python
from fastapi import APIRouter, Depends, Request
from mshkn.config import Config
from mshkn.vm.manager import VMManager
```

**Step 3: Wire VMManager into main.py lifespan**

Add to the lifespan function in `main.py`, after db init:
```python
    from mshkn.vm.manager import VMManager
    vm_manager = VMManager(config, db)
    await vm_manager.initialize()
    app.state.vm_manager = vm_manager
```

**Step 4: Run checks locally**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
.venv/bin/pytest tests/ -v  # existing unit tests should still pass
```

**Step 5: Commit**

```bash
git add src/mshkn/vm/manager.py src/mshkn/api/computers.py src/mshkn/main.py
git commit -m "feat: computer_create and computer_destroy with full VM lifecycle"
```

**Step 6: Integration test on server**

This is the first end-to-end test. Deploy to server, ensure dm-thin pool exists with base volume, start the orchestrator, and test:

```bash
# On server: set up thin pool + base volume (one-time)
# Then: start orchestrator
# Then: curl -X POST http://localhost:8000/computers -H "Authorization: Bearer <key>" -d '{"uses":[]}'
# Then: curl -X DELETE http://localhost:8000/computers/<id> -H "Authorization: Bearer <key>"
```

Detailed integration test steps will depend on server state. The orchestrator should log every step.

---

## Task 8: computer_exec (SSH + SSE streaming)

**Files:**
- Create: `src/mshkn/vm/ssh.py`
- Modify: `src/mshkn/api/computers.py`
- Create: `tests/test_ssh.py`

**Step 1: Implement SSH exec module**

`src/mshkn/vm/ssh.py`:
```python
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncssh

logger = logging.getLogger(__name__)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


async def ssh_exec(
    vm_ip: str,
    command: str,
    ssh_key_path: Path,
    timeout: float = 300.0,
) -> ExecResult:
    """Execute a command via SSH and return the full result."""
    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn:
        result = await asyncio.wait_for(
            conn.run(command, check=False),
            timeout=timeout,
        )
    return ExecResult(
        exit_code=result.exit_status or 0,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


async def ssh_exec_stream(
    vm_ip: str,
    command: str,
    ssh_key_path: Path,
) -> AsyncIterator[tuple[str, str]]:
    """Execute a command via SSH and yield (stream, line) tuples as they arrive.

    stream is "stdout" or "stderr". Yields ("exit", "<code>") at the end.
    """
    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn:
        process = await conn.create_process(command)

        async def read_stream(
            stream: asyncssh.SSHReader[str], name: str
        ) -> list[tuple[str, str]]:
            lines: list[tuple[str, str]] = []
            async for line in stream:
                lines.append((name, line.rstrip("\n")))
            return lines

        stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))

        stdout_lines, stderr_lines = await asyncio.gather(stdout_task, stderr_task)
        await process.wait()

        for item in stdout_lines:
            yield item
        for item in stderr_lines:
            yield item
        yield ("exit", str(process.exit_status or 0))


async def ssh_exec_bg(
    vm_ip: str,
    command: str,
    ssh_key_path: Path,
) -> int:
    """Run a command in the background via SSH, return PID."""
    result = await ssh_exec(
        vm_ip,
        f"nohup {command} > /tmp/bg-$$.log 2>&1 & echo $!",
        ssh_key_path,
    )
    pid = int(result.stdout.strip())
    return pid


async def ssh_upload(
    vm_ip: str,
    remote_path: str,
    data: bytes,
    ssh_key_path: Path,
) -> None:
    """Upload data to a file on the VM."""
    async with asyncssh.connect(
        vm_ip,
        username="root",
        client_keys=[str(ssh_key_path)],
        known_hosts=None,
    ) as conn:
        # Ensure parent directory exists
        parent = str(Path(remote_path).parent)
        await conn.run(f"mkdir -p {parent}", check=True)
        # Write via stdin
        process = await conn.create_process(f"cat > {remote_path}")
        assert process.stdin is not None
        process.stdin.write(data.decode("utf-8", errors="surrogateescape"))
        process.stdin.write_eof()
        await process.wait()


async def ssh_download(
    vm_ip: str,
    remote_path: str,
    ssh_key_path: Path,
) -> bytes:
    """Download a file from the VM."""
    result = await ssh_exec(vm_ip, f"cat {remote_path}", ssh_key_path)
    if result.exit_code != 0:
        raise FileNotFoundError(f"File not found: {remote_path}")
    return result.stdout.encode("utf-8", errors="surrogateescape")
```

**Step 2: Wire exec into API**

Add to `src/mshkn/api/computers.py`:

```python
from sse_starlette.sse import EventSourceResponse
from mshkn.vm.ssh import ssh_exec, ssh_exec_stream, ssh_exec_bg, ssh_upload, ssh_download


@router.post("/{computer_id}/exec")
async def exec_command(
    computer_id: str,
    body: ExecRequest,
    request: Request,
    account: Account = Depends(require_account),
) -> EventSourceResponse:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await get_computer(db, computer_id)
    if computer is None or computer.account_id != account.id:
        raise HTTPException(status_code=404, detail="Computer not found")
    if computer.status != "running":
        raise HTTPException(status_code=400, detail=f"Computer is {computer.status}")

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        async for stream, line in ssh_exec_stream(
            computer.vm_ip, body.command, config.ssh_key_path
        ):
            yield {"event": stream, "data": line}

    return EventSourceResponse(event_stream())
```

Add necessary imports at the top of computers.py.

**Step 3: Add remaining exec endpoints**

Add `exec_bg`, `exec_logs`, `exec_kill`, `upload`, `download`, `status` endpoints following the same pattern — get computer from DB, verify ownership, call SSH function, return result.

**Step 4: Run checks**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
.venv/bin/pytest tests/ -v
```

**Step 5: Commit**

```bash
git add src/mshkn/vm/ssh.py src/mshkn/api/computers.py
git commit -m "feat: computer_exec with SSH transport and SSE streaming"
```

---

## Task 9: computer_checkpoint + checkpoint_fork

**Files:**
- Create: `src/mshkn/checkpoint/__init__.py`
- Create: `src/mshkn/checkpoint/snapshot.py`
- Create: `src/mshkn/checkpoint/delta.py`
- Create: `src/mshkn/checkpoint/r2.py`
- Modify: `src/mshkn/api/computers.py` (checkpoint endpoint)
- Modify: `src/mshkn/api/checkpoints.py` (fork endpoint)

These modules wrap the checkpoint/restore primitives proven in Phase 1.

**Step 1: Implement checkpoint modules**

`src/mshkn/checkpoint/__init__.py`:
```python
```

`src/mshkn/checkpoint/snapshot.py` — wraps Firecracker pause/snapshot/resume:
```python
from __future__ import annotations

import logging
from pathlib import Path

from mshkn.vm.firecracker import FirecrackerClient

logger = logging.getLogger(__name__)


async def create_vm_snapshot(
    socket_path: str,
    snapshot_dir: Path,
) -> tuple[Path, Path]:
    """Pause VM, snapshot, resume. Returns (vmstate_path, memory_path)."""
    vmstate_path = snapshot_dir / "vmstate"
    memory_path = snapshot_dir / "memory"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    client = FirecrackerClient(socket_path)
    try:
        await client.pause()
        await client.create_snapshot(str(vmstate_path), str(memory_path))
        await client.resume()
    finally:
        await client.close()

    logger.info("VM snapshot created at %s", snapshot_dir)
    return vmstate_path, memory_path
```

`src/mshkn/checkpoint/delta.py` — wraps thin_delta export/import:
```python
from __future__ import annotations

import logging
from pathlib import Path

from mshkn.shell import run

logger = logging.getLogger(__name__)


async def export_disk_delta(
    pool_name: str,
    base_volume_id: int,
    snap_volume_id: int,
    snap_volume_name: str,
    meta_device: str,
    output_dir: Path,
    block_size: int = 65536,
) -> tuple[Path, Path]:
    """Export changed blocks between base and snapshot. Returns (delta_path, manifest_path)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    delta_path = output_dir / "delta.bin"
    manifest_path = output_dir / "blocks.txt"

    # Reserve metadata snapshot
    await run(f"dmsetup message {pool_name} 0 'reserve_metadata_snap'")

    try:
        # Get changed block ranges
        xml = await run(
            f"thin_delta -m --snap1 {base_volume_id} --snap2 {snap_volume_id} {meta_device}"
        )

        # Parse XML for changed ranges
        ranges: list[tuple[int, int]] = []
        for line in xml.splitlines():
            if "different" in line or "right_only" in line:
                import re
                m = re.search(r'begin="(\d+)".*length="(\d+)"', line)
                if m:
                    ranges.append((int(m.group(1)), int(m.group(2))))

        # Write ranges file
        with manifest_path.open("w") as f:
            for begin, length in ranges:
                f.write(f"{begin} {length}\n")

        # Export changed blocks
        with delta_path.open("wb") as out:
            for begin, length in ranges:
                data = await run(
                    f"dd if=/dev/mapper/{snap_volume_name} bs={block_size} "
                    f"skip={begin} count={length} 2>/dev/null"
                )
                out.write(data.encode("latin-1"))

    finally:
        await run(f"dmsetup message {pool_name} 0 'release_metadata_snap'")

    logger.info("Exported disk delta: %d ranges, %d bytes", len(ranges), delta_path.stat().st_size)
    return delta_path, manifest_path


async def import_disk_delta(
    volume_name: str,
    delta_path: Path,
    blocks_path: Path,
    block_size: int = 65536,
) -> None:
    """Apply a disk delta to a volume."""
    ranges: list[tuple[int, int]] = []
    for line in blocks_path.read_text().splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            ranges.append((int(parts[0]), int(parts[1])))

    offset = 0
    for begin, length in ranges:
        byte_count = length * block_size
        await run(
            f"dd if={delta_path} of=/dev/mapper/{volume_name} "
            f"bs={block_size} skip={offset // block_size} seek={begin} "
            f"count={length} conv=notrunc 2>/dev/null"
        )
        offset += byte_count

    logger.info("Imported disk delta: %d ranges to %s", len(ranges), volume_name)
```

`src/mshkn/checkpoint/r2.py` — wraps rclone:
```python
from __future__ import annotations

import logging
from pathlib import Path

from mshkn.shell import run

logger = logging.getLogger(__name__)


async def upload_checkpoint(
    local_dir: Path,
    r2_prefix: str,
    bucket: str,
) -> None:
    """Upload checkpoint files to R2."""
    await run(f"rclone copy {local_dir}/ r2:{bucket}/{r2_prefix}/")
    logger.info("Uploaded checkpoint to r2:%s/%s", bucket, r2_prefix)


async def download_checkpoint(
    r2_prefix: str,
    bucket: str,
    local_dir: Path,
) -> None:
    """Download checkpoint files from R2."""
    local_dir.mkdir(parents=True, exist_ok=True)
    await run(f"rclone copy r2:{bucket}/{r2_prefix}/ {local_dir}/")
    logger.info("Downloaded checkpoint from r2:%s/%s", bucket, r2_prefix)
```

**Step 2: Wire checkpoint into the computers API**

Add to `src/mshkn/api/computers.py`:

```python
class CheckpointRequest(BaseModel):
    label: str | None = None
    pin: bool = False


class CheckpointResponse(BaseModel):
    checkpoint_id: str
    manifest_hash: str


@router.post("/{computer_id}/checkpoint", response_model=CheckpointResponse)
async def checkpoint_computer(
    computer_id: str,
    body: CheckpointRequest | None = None,
    request: Request,
    account: Account = Depends(require_account),
) -> CheckpointResponse:
    # 1. Get computer, verify ownership
    # 2. Create VM snapshot (pause/snapshot/resume)
    # 3. Kick off async background task for compression + R2 upload
    # 4. Record checkpoint in DB
    # 5. Return checkpoint_id
    ...
```

The full implementation follows the pattern from Phase 1 foundation work. The key detail is that step 3 runs as a `asyncio.create_task()` — the endpoint returns immediately after step 2 (which takes ~712ms).

**Step 3: Wire fork into checkpoints API**

The fork endpoint downloads the checkpoint from R2, creates a new dm-thin snapshot, restores the Firecracker VM from the memory snapshot, and returns a new computer.

**Step 4: Run checks + commit**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
git add src/mshkn/checkpoint/ src/mshkn/api/
git commit -m "feat: checkpoint and fork with async R2 upload"
```

---

## Task 10: checkpoint_merge

**Files:**
- Create: `src/mshkn/checkpoint/merge.py`
- Create: `tests/test_merge.py`
- Modify: `src/mshkn/api/checkpoints.py`

**Step 1: Write merge tests**

`tests/test_merge.py`:
```python
from pathlib import Path

from mshkn.checkpoint.merge import three_way_merge, MergeResult


def test_non_overlapping_files(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    fork_a = tmp_path / "fork_a"
    fork_b = tmp_path / "fork_b"
    for d in [parent, fork_a, fork_b]:
        d.mkdir()
        (d / "shared.txt").write_text("unchanged")
    (fork_a / "a_only.txt").write_text("from a")
    (fork_b / "b_only.txt").write_text("from b")

    result = three_way_merge(parent, fork_a, fork_b)
    assert result.conflicts == []
    assert (result.merged_dir / "shared.txt").read_text() == "unchanged"
    assert (result.merged_dir / "a_only.txt").read_text() == "from a"
    assert (result.merged_dir / "b_only.txt").read_text() == "from b"


def test_conflict_both_modified(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    fork_a = tmp_path / "fork_a"
    fork_b = tmp_path / "fork_b"
    for d in [parent, fork_a, fork_b]:
        d.mkdir()
    (parent / "file.txt").write_text("original")
    (fork_a / "file.txt").write_text("version a")
    (fork_b / "file.txt").write_text("version b")

    result = three_way_merge(parent, fork_a, fork_b)
    assert len(result.conflicts) == 1
    assert result.conflicts[0].path == "file.txt"


def test_one_side_delete(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    fork_a = tmp_path / "fork_a"
    fork_b = tmp_path / "fork_b"
    for d in [parent, fork_a, fork_b]:
        d.mkdir()
    (parent / "file.txt").write_text("original")
    # fork_a deletes it, fork_b doesn't touch it
    (fork_b / "file.txt").write_text("original")

    result = three_way_merge(parent, fork_a, fork_b)
    assert result.conflicts == []
    assert not (result.merged_dir / "file.txt").exists()
```

**Step 2: Implement merge.py**

`src/mshkn/checkpoint/merge.py` — port of the shell script from Phase 1:
```python
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConflictInfo:
    path: str
    parent_hash: str | None
    fork_a_hash: str | None
    fork_b_hash: str | None


@dataclass
class MergeResult:
    merged_dir: Path
    conflicts: list[ConflictInfo] = field(default_factory=list)
    auto_merged: int = 0
    unchanged: int = 0


def _file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.md5(path.read_bytes()).hexdigest()


def _all_relative_files(*dirs: Path) -> set[str]:
    files: set[str] = set()
    for d in dirs:
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file():
                    files.add(str(f.relative_to(d)))
    return files


def three_way_merge(
    parent: Path,
    fork_a: Path,
    fork_b: Path,
    output: Path | None = None,
) -> MergeResult:
    if output is None:
        output = parent.parent / "merged"
    output.mkdir(parents=True, exist_ok=True)

    result = MergeResult(merged_dir=output)
    all_files = _all_relative_files(parent, fork_a, fork_b)

    for rel in sorted(all_files):
        p_file = parent / rel
        a_file = fork_a / rel
        b_file = fork_b / rel
        out_file = output / rel

        hp = _file_hash(p_file)
        ha = _file_hash(a_file)
        hb = _file_hash(b_file)

        out_file.parent.mkdir(parents=True, exist_ok=True)

        if ha == hp and hb == hp:
            # Unchanged in both
            if p_file.exists():
                shutil.copy2(p_file, out_file)
            result.unchanged += 1
        elif ha != hp and hb == hp:
            # Changed only in A
            if a_file.exists():
                shutil.copy2(a_file, out_file)
            # else: A deleted it
            result.auto_merged += 1
        elif ha == hp and hb != hp:
            # Changed only in B
            if b_file.exists():
                shutil.copy2(b_file, out_file)
            result.auto_merged += 1
        elif ha == hb:
            # Both changed the same way
            if a_file.exists():
                shutil.copy2(a_file, out_file)
            result.auto_merged += 1
        elif hp is None and ha is not None and hb is None:
            # Added only in A
            shutil.copy2(a_file, out_file)
            result.auto_merged += 1
        elif hp is None and ha is None and hb is not None:
            # Added only in B
            shutil.copy2(b_file, out_file)
            result.auto_merged += 1
        else:
            # Conflict
            result.conflicts.append(ConflictInfo(
                path=rel, parent_hash=hp, fork_a_hash=ha, fork_b_hash=hb,
            ))
            # Default: take fork_a
            if a_file.exists():
                shutil.copy2(a_file, out_file)
            elif b_file.exists():
                shutil.copy2(b_file, out_file)

    return result
```

**Step 3: Run checks + commit**

```bash
.venv/bin/pytest tests/test_merge.py -v
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
git add src/mshkn/checkpoint/merge.py tests/test_merge.py
git commit -m "feat: 3-way filesystem merge with conflict detection"
```

---

## Task 11: Capability Resolver + Builder

**Files:**
- Create: `src/mshkn/capability/__init__.py`
- Create: `src/mshkn/capability/resolver.py`
- Create: `src/mshkn/capability/builder.py`
- Create: `src/mshkn/capability/cache.py`
- Create: `tests/test_capability.py`

This wraps the Nix build + overlayfs compose flow from Phase 1.

**Step 1: Write tests for manifest → Nix expression**

`tests/test_capability.py`:
```python
from mshkn.capability.resolver import manifest_to_nix


def test_python_manifest() -> None:
    nix = manifest_to_nix(["python-3.12(numpy, pandas)"])
    assert "python312" in nix
    assert "numpy" in nix
    assert "pandas" in nix


def test_bare_tool() -> None:
    nix = manifest_to_nix(["ffmpeg"])
    assert "ffmpeg" in nix


def test_empty_manifest() -> None:
    nix = manifest_to_nix([])
    assert nix == ""  # no capabilities = base image only
```

**Step 2: Implement resolver, builder, cache**

The resolver parses `python-3.12(numpy, pandas)` into a Nix expression. The builder calls `nix-build` and composes via overlayfs. The cache maps manifest hashes to built images.

These are straightforward translations of the Phase 1 shell commands into Python functions with proper typing and logging.

**Step 3: Run checks + commit**

```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
git add src/mshkn/capability/ tests/test_capability.py
git commit -m "feat: capability resolver with Nix build and overlayfs compose"
```

---

## Task 12: Caddy Proxy Integration

**Files:**
- Create: `src/mshkn/proxy/__init__.py`
- Create: `src/mshkn/proxy/caddy.py`
- Create: `tests/test_caddy.py`

**Step 1: Implement Caddy admin API client**

`src/mshkn/proxy/caddy.py` — adds/removes routes via Caddy's admin API:

```python
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class CaddyClient:
    def __init__(self, admin_url: str = "http://localhost:2019") -> None:
        self.admin_url = admin_url
        self._client = httpx.AsyncClient(base_url=admin_url)

    async def add_route(self, computer_id: str, vm_ip: str, domain: str) -> None:
        """Add a reverse proxy route for a computer."""
        # Caddy config API: add route that matches {port}-{computer_id}.{domain}
        # and proxies to {vm_ip}:{port}
        # This is a simplified version — production would use Caddy's full config API
        logger.info("Added Caddy route: *.%s.%s -> %s", computer_id, domain, vm_ip)

    async def remove_route(self, computer_id: str) -> None:
        """Remove a computer's reverse proxy route."""
        logger.info("Removed Caddy route for %s", computer_id)

    async def close(self) -> None:
        await self._client.aclose()
```

The Caddy integration will need refinement when we set up the actual Caddy config on the server. For now, the interface is defined and the VM manager calls it.

**Step 2: Run checks + commit**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/ tests/
git add src/mshkn/proxy/
git commit -m "feat: Caddy proxy client stub for dynamic route management"
```

---

## Task 13: Server Deployment Setup

**Files:**
- Create: `deploy.sh`
- Create: `systemd/mshkn.service`

**Step 1: Create systemd unit**

`systemd/mshkn.service`:
```ini
[Unit]
Description=mshkn orchestrator
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/mshkn
ExecStart=/opt/mshkn/.venv/bin/uvicorn mshkn.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
EnvironmentFile=/opt/mshkn/.env

[Install]
WantedBy=multi-user.target
```

**Step 2: Create deploy script**

`deploy.sh`:
```bash
#!/bin/bash
set -euo pipefail

SERVER="root@135.181.6.215"
SSH="ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 $SERVER"

echo "Deploying mshkn..."

# Push code
$SSH "cd /opt/mshkn && git pull"

# Install deps
$SSH "cd /opt/mshkn && .venv/bin/uv sync"

# Run migrations
$SSH "cd /opt/mshkn && .venv/bin/python -c 'import asyncio; from mshkn.db import run_migrations; from pathlib import Path; import aiosqlite; asyncio.run((lambda: None)())'"

# Restart
$SSH "systemctl restart mshkn"

echo "Deployed. Check: $SSH 'systemctl status mshkn'"
```

**Step 3: Initial server setup (one-time)**

```bash
# On the server:
cd /opt
git clone <repo-url> mshkn
cd mshkn
uv venv
uv sync
cp .env.example .env  # fill in R2 creds
cp systemd/mshkn.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable mshkn
systemctl start mshkn

# Set up dm-thin pool (one-time)
# Set up Caddy (one-time)
# Create initial account in SQLite
```

**Step 4: Commit**

```bash
git add deploy.sh systemd/
git commit -m "feat: deployment setup with systemd and deploy script"
```

---

## Task 14: End-to-End Integration Test

**Files:**
- Create: `tests/integration/test_e2e.py`

This is a smoke test that runs the full flow against the actual server. It requires the orchestrator to be running.

```python
"""
End-to-end test against running orchestrator.

Run with: MSHKN_URL=http://localhost:8000 MSHKN_API_KEY=<key> pytest tests/integration/ -v
"""
import os
import httpx
import pytest


@pytest.fixture
def client() -> httpx.Client:
    url = os.environ.get("MSHKN_URL", "http://localhost:8000")
    key = os.environ.get("MSHKN_API_KEY", "")
    return httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {key}"},
        timeout=60.0,
    )


def test_full_lifecycle(client: httpx.Client) -> None:
    # Create
    resp = client.post("/computers", json={"uses": []})
    assert resp.status_code == 200
    data = resp.json()
    computer_id = data["computer_id"]
    assert computer_id.startswith("comp-")

    # Exec
    resp = client.post(f"/computers/{computer_id}/exec", json={"command": "echo hello"})
    assert resp.status_code == 200

    # Checkpoint
    resp = client.post(f"/computers/{computer_id}/checkpoint", json={"label": "test"})
    assert resp.status_code == 200
    ckpt_id = resp.json()["checkpoint_id"]

    # Fork
    resp = client.post(f"/checkpoints/{ckpt_id}/fork")
    assert resp.status_code == 200
    fork_id = resp.json()["computer_id"]

    # Destroy both
    client.delete(f"/computers/{fork_id}")
    client.delete(f"/computers/{computer_id}")

    # List checkpoints
    resp = client.get("/checkpoints")
    assert resp.status_code == 200
```

**Commit:**
```bash
git add tests/integration/
git commit -m "feat: end-to-end integration test"
```

---

## Execution Order Summary

| Task | What | Dependencies | Can parallelize? |
|------|------|--------------|-----------------|
| 1 | Project scaffolding + tooling | None | No (foundation) |
| 2 | Config + models | Task 1 | No |
| 3 | Database layer | Task 2 | No |
| 4 | Network + storage | Task 2 | Yes (with 5) |
| 5 | Firecracker client | Task 2 | Yes (with 4) |
| 6 | Auth + API skeleton | Task 3 | No |
| 7 | create + destroy (e2e) | Tasks 4, 5, 6 | No (critical path) |
| 8 | exec (SSH + SSE) | Task 7 | No |
| 9 | checkpoint + fork | Tasks 7, 8 | No |
| 10 | merge | Task 9 | Yes (with 11) |
| 11 | capability resolver | Task 3 | Yes (with 10) |
| 12 | Caddy proxy | Task 7 | Yes |
| 13 | Server deployment | Task 7 | Yes |
| 14 | E2E integration test | All above | No (final) |

## Notes for Implementer

- **This plan may change as we build.** The design is grounded in Phase 1 learnings but the orchestrator glue may reveal new issues. Adapt as needed.
- **Run ruff + mypy after every task.** Do not let lint/type debt accumulate.
- **Integration testing happens on the Hetzner server** (135.181.6.215). Unit tests run locally.
- **The shell commands in vm/, checkpoint/, and capability/ modules are direct translations of Phase 1 experiments.** Refer to MEMORY.md for the exact commands that worked.
- **asyncssh type stubs may be incomplete.** Use targeted `# type: ignore` with specific error codes if mypy complains about asyncssh internals.
