# PR 3: Host Boundary — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put every host side effect (Firecracker, dm-thin, tap devices, SSH into guests, rclone to R2, Caddy routes) behind five small protocols with one Firecracker-backed implementation and one in-memory fake each, switch `VMManager` and the routers to use them, make SSH exec streaming actually stream, and add a `tests/flow/` tier that runs the real `VMManager` and API against the fake host.

**Architecture:** A new `mshkn.host` package holds `Hypervisor`, `BlockStore`, `Guest`, `ObjectStore`, `Proxy` protocols plus shared result types and a `Host` container. The implementations are the existing `vm/`, `proxy/`, and `checkpoint/{r2,snapshot}` code moved into classes; the staging-slot restore dance lives entirely inside `FirecrackerHypervisor`, whose lock is an instance attribute. `Runtime` carries a `Host`; `VMManager`, the routers, and the recipe builder take their host operations from it. `host/fake.py` ships in the package so flow tests (and later local development) can drive the whole system without a hypervisor. Behavior is unchanged except for the five PR-3-owned fixes named in Global Constraints.

**Tech Stack:** Python 3.12, asyncio, asyncssh 2.22, httpx, FastAPI, pytest 9 / pytest-asyncio 1.3, uv, ruff 0.15, mypy 1.19 strict.

**Spec:** `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` §4 (host boundary), §5 (Runtime host field), §11 (flow tier), §14 step 3. Signature deviations from §4.1, decided here: `Hypervisor.snapshot(socket_path: str, dest_dir: Path)` takes the socket path because that is what a `Computer` row carries; `Guest.warm(vm_ip)` replaces `wait_ready`; `BlockStore` gains `deactivate(name)` (the recipe builder needs to drop a device mapping without deleting the volume).

## Global Constraints

- Python `>=3.12`; uv only; every command runs as `uv run <tool>` inside the worktree.
- Local validation, identical to CI: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`. Green at the end of every task.
- Dependency direction: `api → services/vm → host, db`. Nothing under `host/` imports `api`, `vm`, `db`, or `runtime`. `host/` may import `config`, `errors`, `resources`, `observability`.
- Behavior on the live server is unchanged except these deliberate fixes (each named in the PR body):
  1. `Proxy.remove_route` never raises on transport errors (Caddy dropping a connection under concurrent deletes made `DELETE /computers` return 500).
  2. The staging cleanup no longer warns "Cannot find device tap254" on every restore (it checks existence first).
  3. The staging hop's SSH uses `config.ssh_key_path`, not a hard-coded `/root/.ssh/id_ed25519`.
  4. Recipe deletion removes the volume the builder actually created (`mshkn-recipe-<recipe_id>`, not `<content_hash[:16]>`).
  5. `POST /computers/{id}/exec` emits an `error` SSE event and an `exit` event when the SSH session cannot be established, instead of raising after headers were sent.
  Plus one structural change the spec mandates: exec streaming yields lines as they arrive instead of buffering until exit (§4.2). The E2E streaming test (T0.3) already tolerates both; PR 5 tightens it.
- No module-level mutable state remains under `src/mshkn/` (the staging lock becomes `FirecrackerHypervisor._staging_lock`).
- Live E2E gate: `MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000 scripts/e2e.sh`, detached, must report 151 passed, 6 skipped, 0 failed.
- Commit messages end with the trailer block (Co-Authored-By and Claude-Session lines). Never merge; open the PR and request authorization.

---

## File Structure

**Created**
- `src/mshkn/host/__init__.py` — protocols, result types, `Host`.
- `src/mshkn/host/shell.py` — `run`, `ShellError`, `RunFn` (moved from `mshkn/shell.py`).
- `src/mshkn/host/network.py` — slot helpers, `create_tap`, `destroy_tap`, `tap_exists` (moved from `vm/network.py`).
- `src/mshkn/host/dmthin.py` — `DmThinBlockStore`, `parse_pool_status`.
- `src/mshkn/host/firecracker.py` — `FirecrackerConfig`, `FirecrackerClient`, process helpers, staging constants, `FirecrackerHypervisor`.
- `src/mshkn/host/ssh.py` — `SshGuest`, `CONNECT_TIMEOUT_SECONDS`, `STREAM_GRACE_SECONDS`.
- `src/mshkn/host/r2.py` — `RcloneObjectStore`.
- `src/mshkn/host/caddy.py` — `CaddyProxy`.
- `src/mshkn/host/fake.py` — `FakeHypervisor`, `FakeBlockStore`, `FakeGuest`, `FakeObjectStore`, `FakeProxy`, `FakeHost()`.
- `src/mshkn/host/firecracker_host.py` — `firecracker_host(config) -> Host` factory.
- `tests/unit/test_host_types.py`, `test_dmthin.py`, `test_firecracker_hypervisor.py`, `test_ssh_guest.py`, `test_r2.py`, `test_caddy.py`, `test_fake_host.py`.
- `tests/flow/__init__.py`, `tests/flow/conftest.py`, `tests/flow/test_lifecycle.py`.

**Modified**
- `src/mshkn/vm/manager.py` — takes `host: Host`; all host calls go through it; template build unified; `_start_firecracker_with_snapshot` (dead) removed.
- `src/mshkn/runtime.py` — `host: Host` replaces `caddy`/`ssh_pool`.
- `src/mshkn/api/computers.py`, `checkpoints.py`, `ingress.py`, `recipes.py` — host operations via `rt.host`.
- `src/mshkn/recipe/builder.py` — takes `blocks: BlockStore`.
- `tests/unit/conftest.py` — `make_runtime(..., host=None)` defaults to `FakeHost()`.
- `tests/unit/test_vm_manager.py`, `test_recipe_builder.py`, `test_exec_on_create.py`, `test_self_destruct.py`, `test_status_timeout.py`, `test_staging.py`, `test_firecracker.py`, `test_network.py`, `test_ssh.py` — moved imports or fake-host scripting instead of patches.

**Deleted**
- `src/mshkn/shell.py`, `src/mshkn/vm/ssh.py`, `src/mshkn/vm/staging.py`, `src/mshkn/vm/firecracker.py`, `src/mshkn/vm/network.py`, `src/mshkn/vm/storage.py`, `src/mshkn/proxy/` (package), `src/mshkn/checkpoint/r2.py`, `src/mshkn/checkpoint/snapshot.py`. (`checkpoint/merge.py` stays until PR 4.)

---

### Task 1: Worktree and baseline

- [ ] **Step 1:** Use `superpowers:using-git-worktrees` to create `../mshkn-pr3` on branch `pr3-host-boundary` from `main` (0784ce6 or later). `cd ../mshkn-pr3 && uv sync`.
- [ ] **Step 2:** Record the baseline:

```bash
{ echo "Baseline before PR 3 (main @ $(git rev-parse --short HEAD))"; uv run ruff check . | tail -1; uv run ruff format --check . | tail -1; uv run mypy | tail -1; uv run pytest -q 2>&1 | tail -1; uv run pytest --cov -q 2>&1 | grep TOTAL; } | tee docs/superpowers/plans/2026-09-05-pr3-baseline.txt
git add docs/superpowers/plans/2026-09-05-pr3-baseline.txt && git commit -m "chore: record pre-PR3 baseline"
```

Expected: clean; `163 passed`; coverage TOTAL 45%.

---

### Task 2: Host package skeleton: protocols, types, shell and network moves

**Files:**
- Create: `src/mshkn/host/__init__.py`, `src/mshkn/host/shell.py`, `src/mshkn/host/network.py`, `tests/unit/test_host_types.py`
- Delete: `src/mshkn/shell.py`, `src/mshkn/vm/network.py`
- Modify: every importer of `mshkn.shell` and `mshkn.vm.network` (`vm/manager.py`, `vm/staging.py`, `vm/storage.py`, `recipe/builder.py`, `checkpoint/r2.py`, `tests/unit/test_network.py`, `tests/unit/test_recipe_builder.py`) to the new paths.

**Interfaces:**
- Produces: everything in `host/__init__.py` below; `host.shell.run(cmd, check=True) -> str`, `host.shell.ShellError`, `host.shell.RunFn`; `host.network.slot_to_ip/slot_to_mac/slot_to_tap`, `create_tap(slot, *, run=run)`, `destroy_tap(slot, *, run=run)`, `tap_exists(tap, *, run=run) -> bool`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_host_types.py`:

```python
from __future__ import annotations

from pathlib import Path

from mshkn.host import (
    ExecResult,
    Host,
    PoolUsage,
    RunningVM,
    SnapshotFiles,
    VmMetrics,
)
from mshkn.host.network import slot_to_ip, tap_exists


def test_result_types_are_frozen_and_hashable() -> None:
    vm = RunningVM(pid=1, socket_path="/tmp/s", slot=3, vm_ip="172.16.3.2", tap_device="tap3")
    assert hash(vm)
    assert SnapshotFiles(vmstate=Path("/a"), memory=Path("/b")).memory == Path("/b")
    assert ExecResult(exit_code=0, stdout="x", stderr="").exit_code == 0
    assert VmMetrics(cpu_pct=1.0, ram_usage_mb=1, ram_total_mb=2, disk_usage_mb=3, disk_total_mb=4).processes == []
    assert PoolUsage(data_used_ratio=0.5, metadata_used_ratio=0.1).data_used_ratio == 0.5


def test_host_is_a_plain_container() -> None:
    assert set(Host.__dataclass_fields__) == {"hypervisor", "blocks", "guest", "objects", "proxy"}


async def test_tap_exists_uses_injected_run() -> None:
    calls: list[str] = []

    async def fake_run(cmd: str, check: bool = True) -> str:
        calls.append(cmd)
        return "5: tap5: <BROADCAST> mtu 1500" if "tap5" in cmd else ""

    assert await tap_exists("tap5", run=fake_run)
    assert not await tap_exists("tap9", run=fake_run)
    assert all(c.startswith("ip link show ") for c in calls)
    assert slot_to_ip(5) == ("172.16.5.1", "172.16.5.2")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_host_types.py -q`
Expected: ImportError on `mshkn.host`.

- [ ] **Step 3: Implement**

`src/mshkn/host/__init__.py`:

```python
"""The host boundary.

Everything the orchestrator does to the machine goes through one of five
protocols. Production uses the Firecracker-backed implementations in this
package; tests use the in-memory fakes in host/fake.py. Nothing in this
package imports api, vm, db, or runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from mshkn.resources import Resources

StreamName = Literal["stdout", "stderr", "exit"]
OutputLine = tuple[StreamName, str]


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


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class VmMetrics:
    cpu_pct: float
    ram_usage_mb: int
    ram_total_mb: int
    disk_usage_mb: int
    disk_total_mb: int
    processes: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class PoolUsage:
    data_used_ratio: float
    metadata_used_ratio: float


class Hypervisor(Protocol):
    async def boot(
        self, *, slot: int, disk_volume_id: int, disk_name: str, resources: Resources
    ) -> RunningVM: ...
    async def restore(
        self, *, slot: int, disk_volume_id: int, disk_name: str, snapshot: SnapshotFiles
    ) -> RunningVM: ...
    async def snapshot(self, socket_path: str, dest_dir: Path) -> SnapshotFiles: ...
    async def build_template(self, *, disk_volume_id: int, dest_dir: Path) -> SnapshotFiles: ...
    async def kill(self, pid: int) -> None: ...
    def is_alive(self, pid: int) -> bool: ...
    async def teardown_slot(self, slot: int) -> None: ...


class BlockStore(Protocol):
    async def snap(self, *, source_volume_id: int, new_volume_id: int) -> None: ...
    async def activate(self, *, volume_id: int, name: str) -> None: ...
    async def deactivate(self, name: str) -> None: ...
    async def remove(self, *, volume_id: int, name: str) -> None: ...
    async def mkfs(self, name: str) -> None: ...
    def mounted(self, name: str, *, readonly: bool = False) -> AbstractAsyncContextManager[Path]: ...
    async def max_volume_id(self) -> int | None: ...
    async def usage(self) -> PoolUsage: ...


class Guest(Protocol):
    async def exec(self, vm_ip: str, command: str, *, timeout: float = 300.0) -> ExecResult: ...
    def stream(self, vm_ip: str, command: str, *, timeout: float = 60.0) -> AsyncIterator[OutputLine]: ...
    async def exec_bg(self, vm_ip: str, command: str) -> int: ...
    async def upload(self, vm_ip: str, remote_path: str, data: bytes) -> None: ...
    async def download(self, vm_ip: str, remote_path: str) -> bytes: ...
    async def metrics(self, vm_ip: str, *, timeout: float = 10.0) -> VmMetrics: ...
    async def warm(self, vm_ip: str) -> None: ...
    async def evict(self, vm_ip: str) -> None: ...
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

`src/mshkn/host/shell.py`: the current `src/mshkn/shell.py` verbatim, plus:

```python
class RunFn(Protocol):
    """Signature of shell.run, so implementations can take an injected runner."""

    async def __call__(self, cmd: str, check: bool = True) -> str: ...
```

(`from typing import Protocol` at the top.) Delete `src/mshkn/shell.py`; every `from mshkn.shell import ...` becomes `from mshkn.host.shell import ...`.

`src/mshkn/host/network.py`: the current `src/mshkn/vm/network.py` with `run` injectable and an existence check:

```python
async def tap_exists(tap: str, *, run: RunFn = shell_run) -> bool:
    return (await run(f"ip link show {tap} 2>/dev/null", check=False)).strip() != ""


async def create_tap(slot: int, *, run: RunFn = shell_run) -> None:
    ... (existing body, every `run(` call uses the parameter)


async def destroy_tap(slot: int, *, run: RunFn = shell_run) -> None:
    tap = slot_to_tap(slot)
    _, vm_ip = slot_to_ip(slot)
    await run(f"iptables -D FORWARD -i {tap} -s {vm_ip} ! -d 172.16.0.0/12 -j ACCEPT", check=False)
    await run(f"iptables -D FORWARD -i {tap} -s {vm_ip} -d 172.16.0.0/12 -j DROP", check=False)
    if not await tap_exists(tap, run=run):
        logger.debug("Tap %s already gone", tap)
        return
    try:
        await run(f"ip link del {tap}")
    except ShellError as e:
        logger.warning("Failed to delete tap %s: %s", tap, e.stderr.strip())
    else:
        logger.info("Destroyed tap device %s", tap)
```

with `from mshkn.host.shell import RunFn, ShellError, run as shell_run`. Delete `src/mshkn/vm/network.py`; update importers (`vm/manager.py`, `vm/staging.py`, `tests/unit/test_network.py`).

- [ ] **Step 4: Verify**

```bash
uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
test ! -e src/mshkn/shell.py && test ! -e src/mshkn/vm/network.py && echo "moved"
```

Expected: clean; `166 passed`; `moved`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(host): boundary protocols and result types; shell and network moved into host/"
```

---

### Task 3: DmThinBlockStore

**Files:**
- Create: `src/mshkn/host/dmthin.py`, `tests/unit/test_dmthin.py`
- (Old `vm/storage.py` stays until Task 8.)

**Interfaces:**
- Produces: `DmThinBlockStore(pool_name: str, sectors: int, *, run: RunFn = shell.run)` implementing `BlockStore`; `parse_pool_status(text: str) -> PoolUsage`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_dmthin.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mshkn.host.dmthin import DmThinBlockStore, parse_pool_status
from mshkn.host.shell import ShellError

STATUS = "0 209715200 thin-pool 0 4211/65536 14044/409600 - rw discard_passdown queue_if_no_space - 1024"


def test_parse_pool_status() -> None:
    usage = parse_pool_status(STATUS)
    assert usage.metadata_used_ratio == pytest.approx(4211 / 65536)
    assert usage.data_used_ratio == pytest.approx(14044 / 409600)


def test_parse_pool_status_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="thin-pool"):
        parse_pool_status("0 100 linear 8:1 0")


class Recorder:
    def __init__(self, responses: dict[str, str | Exception] | None = None) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.responses = responses or {}

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append((cmd, check))
        for key, resp in self.responses.items():
            if key in cmd:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return ""


async def test_snap_retries_after_orphaned_volume() -> None:
    first = ShellError("dmsetup message", 1, "device-mapper: message ioctl failed: File exists")
    run = Recorder()
    state = {"n": 0}

    async def flaky(cmd: str, check: bool = True) -> str:
        run.calls.append((cmd, check))
        if "create_snap" in cmd:
            state["n"] += 1
            if state["n"] == 1:
                raise first
        return ""

    store = DmThinBlockStore("mshkn-pool", 16777216, run=flaky)
    await store.snap(source_volume_id=0, new_volume_id=7)
    cmds = [c for c, _ in run.calls]
    assert cmds == [
        "dmsetup message mshkn-pool 0 'create_snap 7 0'",
        "dmsetup message mshkn-pool 0 'delete 7'",
        "dmsetup message mshkn-pool 0 'create_snap 7 0'",
    ]


async def test_activate_and_remove_issue_expected_commands() -> None:
    run = Recorder()
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    await store.activate(volume_id=7, name="mshkn-comp-x")
    await store.remove(volume_id=7, name="mshkn-comp-x")
    cmds = [c for c, _ in run.calls]
    assert cmds[0] == "dmsetup create mshkn-comp-x --table '0 16777216 thin /dev/mapper/mshkn-pool 7'"
    assert cmds[1] == "dmsetup remove mshkn-comp-x"
    assert cmds[2] == "dmsetup message mshkn-pool 0 'delete 7'"


async def test_mounted_mounts_and_unmounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / "mnt"))
    (tmp_path / "mnt").mkdir()
    run = Recorder()
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    async with store.mounted("mshkn-ckpt-a", readonly=True) as path:
        assert path == tmp_path / "mnt"
        assert run.calls[-1][0] == f"mount -o ro /dev/mapper/mshkn-ckpt-a {path}"
    assert run.calls[-1][0] == f"umount {tmp_path / 'mnt'}"
    assert not (tmp_path / "mnt").exists()


async def test_max_volume_id_parses_dmsetup_table() -> None:
    table = "mshkn-base: 0 16777216 thin 252:0 0\nmshkn-comp-a: 0 16777216 thin 252:0 745\n"
    run = Recorder({"dmsetup table": table})
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    assert await store.max_volume_id() == 745


async def test_usage_uses_dmsetup_status() -> None:
    run = Recorder({"dmsetup status": STATUS})
    store = DmThinBlockStore("mshkn-pool", 16777216, run=run)
    usage = await store.usage()
    assert usage.data_used_ratio == pytest.approx(14044 / 409600)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_dmthin.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

`src/mshkn/host/dmthin.py`:

```python
"""dm-thin block store: copy-on-write volumes on the mshkn thin pool."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.host import PoolUsage
from mshkn.host.shell import RunFn, ShellError
from mshkn.host.shell import run as shell_run

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

_REMOVE_RETRIES = 5
_UMOUNT_RETRIES = 3


def parse_pool_status(text: str) -> PoolUsage:
    """Parse `dmsetup status <pool>`: `0 <sectors> thin-pool <txn> <mu>/<mt> <du>/<dt> ...`."""
    parts = text.split()
    if len(parts) < 6 or parts[2] != "thin-pool":
        raise ValueError(f"not a thin-pool status line: {text!r}")
    meta_used, meta_total = (int(x) for x in parts[4].split("/"))
    data_used, data_total = (int(x) for x in parts[5].split("/"))
    return PoolUsage(
        data_used_ratio=data_used / data_total if data_total else 0.0,
        metadata_used_ratio=meta_used / meta_total if meta_total else 0.0,
    )


class DmThinBlockStore:
    def __init__(self, pool_name: str, sectors: int, *, run: RunFn = shell_run) -> None:
        self._pool = pool_name
        self._sectors = sectors
        self._run = run

    async def snap(self, *, source_volume_id: int, new_volume_id: int) -> None:
        """create_snap in the pool, retrying once if the target id is an orphan."""
        cmd = f"dmsetup message {self._pool} 0 'create_snap {new_volume_id} {source_volume_id}'"
        try:
            await self._run(cmd)
        except ShellError as e:
            if "File exists" not in e.stderr and "already exists" not in e.stderr:
                raise
            logger.warning("Orphaned thin volume %d in pool, deleting and retrying", new_volume_id)
            await self._run(f"dmsetup message {self._pool} 0 'delete {new_volume_id}'")
            await self._run(cmd)

    async def activate(self, *, volume_id: int, name: str) -> None:
        """Map a thin volume to /dev/mapper/<name>, replacing a stale mapping."""
        cmd = f"dmsetup create {name} --table '0 {self._sectors} thin /dev/mapper/{self._pool} {volume_id}'"
        try:
            await self._run(cmd)
        except ShellError as e:
            if "File exists" not in e.stderr and "already exists" not in e.stderr:
                raise
            logger.warning("Stale device %s exists, removing and retrying", name)
            await self._run(f"dmsetup remove {name}", check=False)
            await self._run(cmd)
        logger.info("Activated volume %s (vol %d)", name, volume_id)

    async def deactivate(self, name: str) -> None:
        await self._run(f"dmsetup remove {name}")

    async def remove(self, *, volume_id: int, name: str) -> None:
        """Unmap the device (retrying while the kernel still holds it) and delete the volume."""
        for attempt in range(_REMOVE_RETRIES):
            try:
                await self._run(f"dmsetup remove {name}")
                break
            except ShellError as e:
                if attempt < _REMOVE_RETRIES - 1:
                    logger.debug("dmsetup remove %s failed (attempt %d): %s", name, attempt + 1, e.stderr.strip())
                    await asyncio.sleep(0.5)
                else:
                    logger.warning("dmsetup remove %s failed after %d attempts: %s", name, _REMOVE_RETRIES, e.stderr.strip())
        try:
            await self._run(f"dmsetup message {self._pool} 0 'delete {volume_id}'")
        except ShellError as e:
            logger.warning("dmsetup delete vol %d failed: %s", volume_id, e.stderr.strip())
        logger.info("Removed volume %s (vol %d)", name, volume_id)

    async def mkfs(self, name: str) -> None:
        await self._run(f"mkfs.ext4 -F /dev/mapper/{name}")

    @contextlib.asynccontextmanager
    async def mounted(self, name: str, *, readonly: bool = False) -> AsyncIterator[Path]:
        mount_point = Path(tempfile.mkdtemp(prefix="mshkn-mnt-"))
        opts = " -o ro" if readonly else ""
        await self._run(f"mount{opts} /dev/mapper/{name} {mount_point}")
        try:
            yield mount_point
        finally:
            for attempt in range(_UMOUNT_RETRIES):
                try:
                    await self._run(f"umount {mount_point}")
                    break
                except ShellError:
                    if attempt < _UMOUNT_RETRIES - 1:
                        await asyncio.sleep(0.5)
                    else:
                        logger.warning("umount %s failed after %d attempts", mount_point, _UMOUNT_RETRIES)
            with contextlib.suppress(OSError):
                mount_point.rmdir()

    async def max_volume_id(self) -> int | None:
        """Highest thin volume id mapped on this host (catches orphans the DB forgot)."""
        try:
            output = await self._run("dmsetup table --target thin")
        except ShellError:
            return None
        max_id: int | None = None
        for line in output.strip().splitlines():
            parts = line.split()
            # "<name>: 0 <sectors> thin <pool_major:minor> <volume_id>"
            if len(parts) >= 6 and parts[3] == "thin":
                with contextlib.suppress(ValueError):
                    vol_id = int(parts[5])
                    max_id = vol_id if max_id is None or vol_id > max_id else max_id
        return max_id

    async def usage(self) -> PoolUsage:
        return parse_pool_status(await self._run(f"dmsetup status {self._pool}"))
```

The `mounted` test patches `tempfile.mkdtemp`; the module must call it as `tempfile.mkdtemp(...)` (attribute access), not via `from tempfile import mkdtemp`.

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_dmthin.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1`
Expected: 7 passed; clean; `173 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/host/dmthin.py tests/unit/test_dmthin.py && git commit -m "feat(host): DmThinBlockStore with pool usage parsing"
```

---

### Task 4: FirecrackerHypervisor

**Files:**
- Create: `src/mshkn/host/firecracker.py`, `tests/unit/test_firecracker_hypervisor.py`
- Modify: `tests/unit/test_staging.py`, `tests/unit/test_firecracker.py` (import from the new module)
- (Old `vm/firecracker.py` and `vm/staging.py` stay until Task 8.)

**Interfaces:**
- Produces: `FirecrackerConfig`, `FirecrackerClient`, `start_firecracker_process`, `kill_firecracker_process`, `wait_for_port(ip, port, *, timeout, interval)`, staging constants (`STAGING_SLOT`, `STAGING_TAP`, `STAGING_HOST_IP`, `STAGING_VM_IP`, `STAGING_MAC`, `STAGING_DRIVE_NAME`), and `FirecrackerHypervisor(config: Config, *, run: RunFn = shell.run)` implementing `Hypervisor`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_firecracker_hypervisor.py`:

```python
from __future__ import annotations

from pathlib import Path

from mshkn.config import Config
from mshkn.host.firecracker import (
    STAGING_DRIVE_NAME,
    STAGING_SLOT,
    STAGING_TAP,
    FirecrackerHypervisor,
)


class Recorder:
    def __init__(self, taps: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.taps = taps if taps is not None else set()

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append(cmd)
        if cmd.startswith("ip link show "):
            tap = cmd.split()[3]
            return f"7: {tap}: <UP>" if tap in self.taps else ""
        return ""


async def test_teardown_slot_skips_missing_tap() -> None:
    run = Recorder(taps=set())
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv.teardown_slot(5)
    assert not any(c.startswith("ip link del tap5") for c in run.calls)
    assert any(c.startswith("iptables -D FORWARD -i tap5") for c in run.calls)


async def test_teardown_slot_deletes_present_tap() -> None:
    run = Recorder(taps={"tap5"})
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv.teardown_slot(5)
    assert "ip link del tap5" in run.calls


async def test_staging_clean_is_quiet_when_nothing_to_clean() -> None:
    run = Recorder(taps=set())
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv._ensure_staging_clean()
    assert not any(c == f"ip link del {STAGING_TAP}" for c in run.calls)
    assert f"dmsetup remove {STAGING_DRIVE_NAME}" in run.calls
    assert STAGING_SLOT == 254


def test_is_alive_for_own_pid_and_dead_pid() -> None:
    import os

    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    assert hv.is_alive(os.getpid())
    assert not hv.is_alive(2**22 - 1)


def test_staging_lock_is_per_instance() -> None:
    a = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    b = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    assert a._staging_lock is not b._staging_lock
```

Update `tests/unit/test_staging.py` and `tests/unit/test_firecracker.py` to import from `mshkn.host.firecracker` (same names).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_firecracker_hypervisor.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

`src/mshkn/host/firecracker.py` — move `BOOT_ARGS`, `FirecrackerConfig`, `FirecrackerClient`, `start_firecracker_process`, `kill_firecracker_process` verbatim from `vm/firecracker.py`; move the six staging constants from `vm/staging.py`; add:

```python
async def wait_for_port(ip: str, port: int, *, timeout: float, interval: float = 0.01) -> None:
    """Poll until ip:port accepts TCP connections."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=0.05)
            writer.close()
            await writer.wait_closed()
            return
        except (OSError, TimeoutError):
            pass
        await asyncio.sleep(interval)
    raise TimeoutError(f"{ip}:{port} did not become reachable within {timeout}s")


class FirecrackerHypervisor:
    """Boots, restores, snapshots, and kills Firecracker microVMs.

    Every boot/restore goes through the staging slot (254): the VM comes up
    with the staging tap and IP baked into templates and checkpoints, then
    is moved to its final slot via SSH and a tap rename. One restore at a
    time; the lock is an instance attribute.
    """

    _RESTORE_SSH_TIMEOUT = 5.0
    _BOOT_SSH_TIMEOUT = 30.0

    def __init__(self, config: Config, *, run: RunFn = shell_run) -> None:
        self._config = config
        self._run = run
        self._staging_lock = asyncio.Lock()

    # -- Hypervisor protocol -------------------------------------------------

    async def boot(self, *, slot: int, disk_volume_id: int, disk_name: str, resources: Resources) -> RunningVM:
        async def activate(client: FirecrackerClient, socket_path: str) -> None:
            await client.configure_and_boot(
                FirecrackerConfig(
                    socket_path=socket_path,
                    kernel_path=str(self._config.kernel_path),
                    rootfs_path=f"/dev/mapper/{STAGING_DRIVE_NAME}",
                    tap_device=STAGING_TAP,
                    guest_mac=STAGING_MAC,
                    mem_size_mib=resources.mem_mib,
                    vcpu_count=resources.vcpus,
                )
            )

        return await self._stage(slot=slot, disk_volume_id=disk_volume_id, disk_name=disk_name,
                                 activate=activate, ssh_timeout=self._BOOT_SSH_TIMEOUT)

    async def restore(self, *, slot: int, disk_volume_id: int, disk_name: str, snapshot: SnapshotFiles) -> RunningVM:
        async def activate(client: FirecrackerClient, socket_path: str) -> None:
            await client.load_snapshot(str(snapshot.vmstate), str(snapshot.memory), resume_vm=True)

        return await self._stage(slot=slot, disk_volume_id=disk_volume_id, disk_name=disk_name,
                                 activate=activate, ssh_timeout=self._RESTORE_SSH_TIMEOUT)

    async def snapshot(self, socket_path: str, dest_dir: Path) -> SnapshotFiles:
        """Pause, write vmstate+memory into dest_dir, resume."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        files = SnapshotFiles(vmstate=dest_dir / "vmstate", memory=dest_dir / "memory")
        client = FirecrackerClient(socket_path)
        try:
            await client.pause()
            await client.create_snapshot(str(files.vmstate), str(files.memory))
            await client.resume()
        finally:
            await client.close()
        logger.info("VM snapshot created at %s", dest_dir)
        return files

    async def build_template(self, *, disk_volume_id: int, dest_dir: Path) -> SnapshotFiles:
        """Cold-boot the given disk on the staging slot, snapshot it there, and tear it down."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        files = SnapshotFiles(vmstate=dest_dir / "vmstate", memory=dest_dir / "memory")
        socket_path = f"/tmp/fc-template-{disk_volume_id}.socket"
        pid: int | None = None
        async with self._staging_lock:
            try:
                await self._ensure_staging_clean()
                await asyncio.gather(self._map_staging_disk(disk_volume_id), create_tap(STAGING_SLOT, run=self._run))
                pid = await start_firecracker_process(socket_path)
                client = FirecrackerClient(socket_path)
                try:
                    await client.configure_and_boot(
                        FirecrackerConfig(
                            socket_path=socket_path,
                            kernel_path=str(self._config.kernel_path),
                            rootfs_path=f"/dev/mapper/{STAGING_DRIVE_NAME}",
                            tap_device=STAGING_TAP,
                            guest_mac=STAGING_MAC,
                        )
                    )
                    await wait_for_port(STAGING_VM_IP, 22, timeout=self._BOOT_SSH_TIMEOUT, interval=0.025)
                    await client.pause()
                    await client.create_snapshot(str(files.vmstate), str(files.memory))
                finally:
                    await client.close()
                await kill_firecracker_process(pid)
                pid = None
                await destroy_tap(STAGING_SLOT, run=self._run)
                await self._run(f"dmsetup remove {STAGING_DRIVE_NAME}")
            except Exception:
                logger.exception("Template build on volume %d failed", disk_volume_id)
                await self._cleanup_staging(pid)
                raise
        logger.info("Built template from volume %d at %s", disk_volume_id, dest_dir)
        return files

    async def kill(self, pid: int) -> None:
        await kill_firecracker_process(pid)

    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def teardown_slot(self, slot: int) -> None:
        await destroy_tap(slot, run=self._run)

    # -- staging -------------------------------------------------------------

    async def _stage(
        self,
        *,
        slot: int,
        disk_volume_id: int,
        disk_name: str,
        activate: Callable[[FirecrackerClient, str], Awaitable[None]],
        ssh_timeout: float,
    ) -> RunningVM:
        final_host_ip, final_vm_ip = slot_to_ip(slot)
        final_tap = slot_to_tap(slot)
        socket_path = f"/tmp/fc-{disk_name}.socket"
        pid: int | None = None
        async with self._staging_lock:
            try:
                await self._ensure_staging_clean()
                fc_task = asyncio.create_task(start_firecracker_process(socket_path))
                try:
                    await asyncio.gather(self._map_staging_disk(disk_volume_id), create_tap(STAGING_SLOT, run=self._run))
                except Exception:
                    fc_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await fc_task
                    raise
                pid = await fc_task
                client = FirecrackerClient(socket_path)
                try:
                    await activate(client, socket_path)
                finally:
                    await client.close()
                await wait_for_port(STAGING_VM_IP, 22, timeout=ssh_timeout)
                await asyncio.gather(
                    self._ssh_add_ip(final_vm_ip, final_host_ip),
                    self._run(f"ip link del {final_tap}", check=False),
                )
                await self._run(
                    f"ip link set {STAGING_TAP} name {final_tap} && "
                    f"ip addr flush dev {final_tap} && "
                    f"ip addr add {final_host_ip}/30 dev {final_tap} && "
                    f"ip neigh replace {final_vm_ip} lladdr {STAGING_MAC} dev {final_tap} nud permanent && "
                    f"iptables -I FORWARD -i {final_tap} -s {final_vm_ip} ! -d 172.16.0.0/12 -j ACCEPT && "
                    f"iptables -I FORWARD -i {final_tap} -s {final_vm_ip} -d 172.16.0.0/12 -j DROP && "
                    f"dmsetup rename {STAGING_DRIVE_NAME} {disk_name}"
                )
            except Exception:
                await self._cleanup_staging(pid)
                raise
        return RunningVM(pid=pid, socket_path=socket_path, slot=slot, vm_ip=final_vm_ip, tap_device=final_tap)

    async def _map_staging_disk(self, disk_volume_id: int) -> None:
        await self._run(
            f"dmsetup create {STAGING_DRIVE_NAME} --table '0 {self._config.thin_volume_sectors} thin "
            f"/dev/mapper/{self._config.thin_pool_name} {disk_volume_id}'"
        )

    async def _ensure_staging_clean(self) -> None:
        """Remove stale staging resources from a previous failed restore, quietly."""
        with contextlib.suppress(Exception):
            await destroy_tap(STAGING_SLOT, run=self._run)
        with contextlib.suppress(Exception):
            await self._run(f"dmsetup remove {STAGING_DRIVE_NAME}", check=False)

    async def _cleanup_staging(self, pid: int | None) -> None:
        if pid is not None:
            try:
                await kill_firecracker_process(pid)
            except Exception:
                logger.warning("Failed to kill staging FC process PID=%s", pid)
        await self._ensure_staging_clean()

    async def _ssh_add_ip(self, final_vm_ip: str, final_host_ip: str) -> None:
        """Give the guest its final IP and default route, through the staging IP."""
        conn = await asyncio.wait_for(
            asyncssh.connect(
                STAGING_VM_IP,
                username="root",
                known_hosts=None,
                client_keys=[str(self._config.ssh_key_path)],
            ),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        async with conn:
            await conn.run(
                f"ip addr add {final_vm_ip}/30 dev eth0 2>/dev/null; "
                f"ip route replace default via {final_host_ip} && ip neigh flush dev eth0",
                check=True,
            )
```

Imports: `asyncio, contextlib, logging, os, signal`, `asyncssh`, `httpx`, `from collections.abc import Awaitable, Callable` (under `TYPE_CHECKING` with `Path`, `Config`, `Resources`), `from mshkn.host import RunningVM, SnapshotFiles`, `from mshkn.host.network import create_tap, destroy_tap, slot_to_ip, slot_to_tap`, `from mshkn.host.shell import RunFn, run as shell_run`. `CONNECT_TIMEOUT_SECONDS` is defined in this module for now as `10.0` with the same comment as in `vm/ssh.py` (Task 5 makes `host/ssh.py` import it from here, so there is exactly one definition).

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_firecracker_hypervisor.py tests/unit/test_staging.py tests/unit/test_firecracker.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1`
Expected: pass; clean; `178 passed`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(host): FirecrackerHypervisor with the staging slot as an instance concern"
```

---

### Task 5: SshGuest with real streaming

**Files:**
- Create: `src/mshkn/host/ssh.py`, `tests/unit/test_ssh_guest.py`
- Modify: `tests/unit/test_ssh.py` → delete (its exports test is obsolete; the connect-bound test moves into `test_ssh_guest.py`).

**Interfaces:**
- Produces: `SshGuest(key_path: Path, *, connect: ConnectFn = asyncssh.connect)` implementing `Guest`; `STREAM_GRACE_SECONDS = 2.0`; `CONNECT_TIMEOUT_SECONDS` re-exported from `host.firecracker`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ssh_guest.py`:

```python
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import asyncssh
import pytest

import mshkn.host.ssh as ssh_module
from mshkn.host import ExecResult
from mshkn.host.ssh import SshGuest


class FakeReader:
    def __init__(self, lines: list[tuple[float, str]]) -> None:
        self._lines = lines

    def __aiter__(self) -> FakeReader:
        return self

    async def __anext__(self) -> str:
        if not self._lines:
            raise StopAsyncIteration
        delay, line = self._lines.pop(0)
        await asyncio.sleep(delay)
        return line


class FakeProcess:
    def __init__(self, stdout: list[tuple[float, str]], stderr: list[tuple[float, str]], exit_after: float, code: int = 0) -> None:
        self.stdout = FakeReader(stdout)
        self.stderr = FakeReader(stderr)
        self._exit_after = exit_after
        self.exit_status = code
        self.killed = False

    async def wait(self) -> None:
        await asyncio.sleep(self._exit_after)

    def kill(self) -> None:
        self.killed = True


class FakeConn:
    def __init__(self, process: FakeProcess) -> None:
        self._process = process
        self.closed = False

    async def create_process(self, command: str) -> FakeProcess:
        return self._process

    async def run(self, command: str, check: bool = False) -> Any:
        class R:
            exit_status = 0
            stdout = "ok\n"
            stderr = ""

        return R()

    def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


def make_guest(process: FakeProcess) -> SshGuest:
    async def connect(host: str, **kwargs: Any) -> FakeConn:
        return FakeConn(process)

    return SshGuest(Path("/tmp/k"), connect=connect)


async def test_stream_yields_lines_before_the_process_exits() -> None:
    process = FakeProcess(stdout=[(0.0, "a\n"), (0.05, "b\n")], stderr=[], exit_after=0.3)
    guest = make_guest(process)
    seen: list[tuple[float, tuple[str, str]]] = []
    t0 = time.monotonic()
    async for item in guest.stream("172.16.1.2", "cmd"):
        seen.append((time.monotonic() - t0, item))
    names = [item for _, item in seen]
    assert names == [("stdout", "a"), ("stdout", "b"), ("exit", "0")]
    assert seen[0][0] < 0.2, "first line must arrive before process exit (0.3s)"
    assert seen[1][0] < 0.25


async def test_stream_kills_on_timeout_and_still_reports_exit() -> None:
    process = FakeProcess(stdout=[(0.0, "x\n")], stderr=[], exit_after=10, code=0)
    guest = make_guest(process)
    items = [item async for item in guest.stream("172.16.1.2", "cmd", timeout=0.1)]
    assert process.killed
    assert items[-1][0] == "exit"


async def test_stream_grace_drains_lines_after_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_module, "STREAM_GRACE_SECONDS", 0.2)
    process = FakeProcess(stdout=[(0.05, "late\n")], stderr=[(0.0, "err\n")], exit_after=0.0)
    guest = make_guest(process)
    items = [item async for item in guest.stream("172.16.1.2", "cmd")]
    assert ("stdout", "late") in items
    assert ("stderr", "err") in items
    assert items[-1] == ("exit", "0")


async def test_exec_uses_pooled_connection_and_parses_result() -> None:
    process = FakeProcess([], [], 0)
    guest = make_guest(process)
    result = await guest.exec("172.16.1.2", "true")
    assert result == ExecResult(exit_code=0, stdout="ok\n", stderr="")


async def test_connect_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_module, "CONNECT_TIMEOUT_SECONDS", 0.05)

    async def hanging(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    guest = SshGuest(Path("/tmp/k"), connect=hanging)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await guest.warm("172.16.1.2")
    assert time.monotonic() - start < 1.0
    assert asyncssh is not None
```

Delete `tests/unit/test_ssh.py`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_ssh_guest.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

`src/mshkn/host/ssh.py`:

```python
"""Guest access over SSH with a per-VM connection pool and real streaming."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import asyncssh

from mshkn.host import ExecResult, OutputLine, VmMetrics
from mshkn.host.firecracker import CONNECT_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

# After the main process exits, keep draining output for this long: background
# children that inherited the shell's fds can keep the streams open.
STREAM_GRACE_SECONDS = 2.0
_HEALTH_CHECK_INTERVAL = 30.0
_METRICS_CMD = (
    "top -bn1 -d0.5 | grep '%Cpu' | awk '{print $8}'; "
    "free -m | awk '/^Mem:/{print $2,$3}'; "
    'df -BM / | awk \'NR==2{gsub(/M/,"",$2); gsub(/M/,"",$3); print $2,$3}\'; '
    "ps -eo pid,comm --no-headers | head -50"
)


class ConnectFn(Protocol):
    def __call__(self, host: str, **kwargs: Any) -> Any: ...


class SshGuest:
    def __init__(self, key_path: Path, *, connect: ConnectFn = asyncssh.connect) -> None:
        self._key_path = str(key_path)
        self._connect = connect
        self._conns: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}

    # -- connections ---------------------------------------------------------

    async def _fresh(self, vm_ip: str, **extra: Any) -> Any:
        return await asyncio.wait_for(
            self._connect(vm_ip, username="root", client_keys=[self._key_path], known_hosts=None, **extra),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

    async def _pooled(self, vm_ip: str) -> Any:
        """Get or create the persistent connection for a VM (health-checked when idle)."""
        lock = self._locks.setdefault(vm_ip, asyncio.Lock())
        async with lock:
            conn = self._conns.get(vm_ip)
            now = asyncio.get_running_loop().time()
            if conn is not None:
                if now - self._last_used.get(vm_ip, 0.0) < _HEALTH_CHECK_INTERVAL:
                    self._last_used[vm_ip] = now
                    return conn
                try:
                    result = await asyncio.wait_for(conn.run("true", check=False), timeout=3.0)
                    if result.exit_status == 0:
                        self._last_used[vm_ip] = now
                        return conn
                except Exception:
                    pass
                with contextlib.suppress(Exception):
                    conn.close()
                del self._conns[vm_ip]
            conn = await self._fresh(vm_ip, keepalive_interval=15, login_timeout=10)
            self._conns[vm_ip] = conn
            self._last_used[vm_ip] = now
            return conn

    async def warm(self, vm_ip: str) -> None:
        await self._pooled(vm_ip)

    async def evict(self, vm_ip: str) -> None:
        conn = self._conns.pop(vm_ip, None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        self._locks.pop(vm_ip, None)
        self._last_used.pop(vm_ip, None)

    async def close(self) -> None:
        for conn in self._conns.values():
            with contextlib.suppress(Exception):
                conn.close()
        self._conns.clear()
        self._locks.clear()
        self._last_used.clear()

    # -- Guest protocol ------------------------------------------------------

    async def exec(self, vm_ip: str, command: str, *, timeout: float = 300.0) -> ExecResult:
        conn = await self._pooled(vm_ip)
        try:
            return await self._run_on(conn, command, timeout)
        except (asyncssh.ChannelOpenError, asyncssh.ConnectionLost) as exc:
            logger.warning("SSH channel error for %s, reconnecting: %s", vm_ip, exc)
            await self.evict(vm_ip)
            conn = await self._pooled(vm_ip)
            return await self._run_on(conn, command, timeout)

    @staticmethod
    async def _run_on(conn: Any, command: str, timeout: float) -> ExecResult:
        result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
        return ExecResult(
            exit_code=result.exit_status or 0,
            stdout=str(result.stdout) if result.stdout else "",
            stderr=str(result.stderr) if result.stderr else "",
        )

    async def stream(self, vm_ip: str, command: str, *, timeout: float = 60.0) -> AsyncIterator[OutputLine]:
        """Yield (stream, line) as lines arrive; ends with ("exit", code).

        Uses the pooled connection; if the pooled connection cannot open
        another channel (sshd MaxSessions), falls back to a dedicated one.
        """
        conn = await self._pooled(vm_ip)
        owned = False
        try:
            process = await conn.create_process(command)
        except asyncssh.ChannelOpenError:
            conn = await self._fresh(vm_ip)
            owned = True
            process = await conn.create_process(command)
        try:
            async for item in self._pump(process, timeout):
                yield item
        finally:
            if owned:
                conn.close()

    @staticmethod
    async def _pump(process: Any, timeout: float) -> AsyncIterator[OutputLine]:
        queue: asyncio.Queue[OutputLine | None] = asyncio.Queue()

        async def read(reader: Any, name: str) -> None:
            try:
                async for line in reader:
                    await queue.put((name, line.rstrip("\n")))  # type: ignore[arg-type]
            finally:
                await queue.put(None)

        pumps = [
            asyncio.create_task(read(process.stdout, "stdout")),
            asyncio.create_task(read(process.stderr, "stderr")),
        ]
        exit_task = asyncio.create_task(process.wait())
        loop = asyncio.get_running_loop()
        hard_deadline = loop.time() + timeout
        grace_deadline: float | None = None
        finished_readers = 0
        try:
            while finished_readers < 2:
                now = loop.time()
                if grace_deadline is None and exit_task.done():
                    grace_deadline = now + STREAM_GRACE_SECONDS
                budget = (grace_deadline if grace_deadline is not None else hard_deadline) - now
                if budget <= 0:
                    if grace_deadline is None:
                        logger.warning("stream: process did not exit within %.1fs, killing", timeout)
                        process.kill()
                        grace_deadline = now + STREAM_GRACE_SECONDS
                        continue
                    break
                getter = asyncio.create_task(queue.get())
                waiters: set[asyncio.Task[Any]] = {getter} if exit_task.done() else {getter, exit_task}
                done, _pending = await asyncio.wait(waiters, timeout=budget, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    item = getter.result()
                    if item is None:
                        finished_readers += 1
                    else:
                        yield item
                else:
                    getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
        finally:
            for task in pumps:
                task.cancel()
            if not exit_task.done():
                exit_task.cancel()
            await asyncio.gather(*pumps, exit_task, return_exceptions=True)
        yield ("exit", str(process.exit_status or 0))

    async def exec_bg(self, vm_ip: str, command: str) -> int:
        escaped = command.replace("'", "'\\''")
        result = await self.exec(
            vm_ip,
            f"nohup bash -c '{escaped}' > /tmp/bg-tmp-$$.log 2>&1 & "
            f"BG=$!; ln -sf /tmp/bg-tmp-$$.log /tmp/bg-$BG.log; echo $BG",
        )
        pid_str = result.stdout.strip()
        if not pid_str:
            raise RuntimeError(f"Failed to get PID for background command: stderr={result.stderr!r}")
        return int(pid_str)

    async def upload(self, vm_ip: str, remote_path: str, data: bytes) -> None:
        conn = await self._pooled(vm_ip)
        await conn.run(f"mkdir -p {Path(remote_path).parent!s}", check=True)
        async with conn.start_sftp_client() as sftp, sftp.open(remote_path, "wb") as f:
            await f.write(data)

    async def download(self, vm_ip: str, remote_path: str) -> bytes:
        conn = await self._pooled(vm_ip)
        async with conn.start_sftp_client() as sftp:
            try:
                async with sftp.open(remote_path, "rb") as f:
                    data: bytes = await f.read()
                    return data
            except asyncssh.SFTPNoSuchFile:
                raise FileNotFoundError(f"File not found: {remote_path}") from None

    async def metrics(self, vm_ip: str, *, timeout: float = 10.0) -> VmMetrics:
        result = await self.exec(vm_ip, _METRICS_CMD, timeout=timeout)
        return parse_metrics(result.stdout)


def parse_metrics(stdout: str) -> VmMetrics:
    """Parse the four-part _METRICS_CMD output."""
    lines = stdout.strip().splitlines()
    cpu_pct = 0.0
    if lines:
        with contextlib.suppress(ValueError):
            cpu_pct = round(100.0 - float(lines[0].strip().replace(",", ".")), 1)
    ram_total_mb = ram_usage_mb = 0
    if len(lines) > 1 and len(parts := lines[1].split()) >= 2:
        with contextlib.suppress(ValueError):
            ram_total_mb, ram_usage_mb = int(parts[0]), int(parts[1])
    disk_total_mb = disk_usage_mb = 0
    if len(lines) > 2 and len(parts := lines[2].split()) >= 2:
        with contextlib.suppress(ValueError):
            disk_total_mb, disk_usage_mb = int(parts[0]), int(parts[1])
    processes: list[dict[str, object]] = []
    for line in lines[3:]:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            with contextlib.suppress(ValueError):
                processes.append({"pid": int(parts[0]), "command": parts[1]})
    return VmMetrics(
        cpu_pct=cpu_pct, ram_usage_mb=ram_usage_mb, ram_total_mb=ram_total_mb,
        disk_usage_mb=disk_usage_mb, disk_total_mb=disk_total_mb, processes=processes,
    )
```

If mypy objects to the `# type: ignore[arg-type]` on the queue put (the `name` parameter is `str`, not `StreamName`), type `read(reader: Any, name: StreamName)` and call it with the literals instead; drop the ignore.

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_ssh_guest.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1`
Expected: 5 passed; clean; the total is 178 minus the 4 deleted `test_ssh.py` tests plus 5 = `179 passed`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(host): SshGuest with a connection pool and line-by-line streaming"
```

---

### Task 6: RcloneObjectStore and CaddyProxy

**Files:**
- Create: `src/mshkn/host/r2.py`, `src/mshkn/host/caddy.py`, `tests/unit/test_r2.py`, `tests/unit/test_caddy.py`

**Interfaces:**
- `RcloneObjectStore(bucket: str, *, remote: str = "r2", run: RunFn = shell.run)`; `CaddyProxy(admin_url: str, domain: str, *, transport: httpx.AsyncBaseTransport | None = None)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_r2.py`:

```python
from __future__ import annotations

from pathlib import Path

from mshkn.host.r2 import RcloneObjectStore


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append((cmd, check))
        return ""


async def test_upload_download_delete_commands(tmp_path: Path) -> None:
    run = Recorder()
    store = RcloneObjectStore("mshkn-checkpoints", run=run)
    await store.upload_dir(tmp_path / "ckpt-1", "acct-1/ckpt-1")
    await store.download_dir("acct-1/ckpt-1", tmp_path / "dl")
    await store.delete_prefix("acct-1/ckpt-1")
    assert run.calls == [
        (f"rclone copy {tmp_path / 'ckpt-1'}/ r2:mshkn-checkpoints/acct-1/ckpt-1/", True),
        (f"rclone copy r2:mshkn-checkpoints/acct-1/ckpt-1/ {tmp_path / 'dl'}/", True),
        ("rclone purge r2:mshkn-checkpoints/acct-1/ckpt-1/", False),
    ]
    assert (tmp_path / "dl").is_dir()
```

`tests/unit/test_caddy.py`:

```python
from __future__ import annotations

import httpx
import pytest

from mshkn.errors import HostError
from mshkn.host.caddy import CaddyProxy


def make_proxy(handler: httpx.MockTransport) -> CaddyProxy:
    return CaddyProxy("http://caddy", "mshkn.dev", transport=handler)


async def test_add_route_posts_regexp_route() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    proxy = make_proxy(httpx.MockTransport(handler))
    await proxy.add_route("comp-1", "172.16.1.2")
    assert seen[0].method == "POST" and seen[0].url.path == "/config/apps/http/servers/main/routes"
    body = seen[0].content.decode()
    assert '"@id": "route-comp-1"' in body and "172.16.1.2:{http.regexp.port_match.1}" in body


async def test_add_route_raises_host_error_after_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    proxy = make_proxy(httpx.MockTransport(handler))
    with pytest.raises(HostError):
        await proxy.add_route("comp-1", "172.16.1.2")


async def test_remove_route_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected")

    proxy = make_proxy(httpx.MockTransport(handler))
    await proxy.remove_route("comp-1")  # logs, does not raise


async def test_healthy_reflects_admin_api() -> None:
    proxy = make_proxy(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert await proxy.healthy()
    down = make_proxy(httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    assert not await down.healthy()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_r2.py tests/unit/test_caddy.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

`src/mshkn/host/r2.py`:

```python
"""Checkpoint object storage via rclone (Cloudflare R2)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mshkn.host.shell import RunFn
from mshkn.host.shell import run as shell_run

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class RcloneObjectStore:
    def __init__(self, bucket: str, *, remote: str = "r2", run: RunFn = shell_run) -> None:
        self._bucket = bucket
        self._remote = remote
        self._run = run

    def _url(self, prefix: str) -> str:
        return f"{self._remote}:{self._bucket}/{prefix}/"

    async def upload_dir(self, local_dir: Path, prefix: str) -> None:
        await self._run(f"rclone copy {local_dir}/ {self._url(prefix)}")
        logger.info("Uploaded %s to %s", local_dir, self._url(prefix))

    async def download_dir(self, prefix: str, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        await self._run(f"rclone copy {self._url(prefix)} {local_dir}/")
        logger.info("Downloaded %s to %s", self._url(prefix), local_dir)

    async def delete_prefix(self, prefix: str) -> None:
        await self._run(f"rclone purge {self._url(prefix)}", check=False)
        logger.info("Deleted %s", self._url(prefix))
```

`src/mshkn/host/caddy.py`: the current `CaddyClient` renamed `CaddyProxy`, with these changes: constructor `(admin_url, domain, *, transport=None)` building `httpx.AsyncClient(base_url=admin_url, timeout=10.0, transport=transport)`; `add_route` raises `HostError(...)` (from `mshkn.errors`) instead of `RuntimeError` in both places; `remove_route` wraps the request in `try: ... except httpx.HTTPError as exc: logger.warning("Failed to remove Caddy route for %s: %s", computer_id, exc); return`; new `healthy()`:

```python
    async def healthy(self) -> bool:
        try:
            resp = await self._client.get("/config/")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200
```

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_r2.py tests/unit/test_caddy.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1`
Expected: 5 passed; clean; `184 passed`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(host): RcloneObjectStore and CaddyProxy (route removal never raises)"
```

---

### Task 7: Fakes and the Firecracker host factory

**Files:**
- Create: `src/mshkn/host/fake.py`, `src/mshkn/host/firecracker_host.py`, `tests/unit/test_fake_host.py`

**Interfaces:**
- `FakeHost() -> Host` with `FakeHypervisor`, `FakeBlockStore`, `FakeGuest`, `FakeObjectStore`, `FakeProxy` as below; `firecracker_host(config: Config) -> Host`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_fake_host.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mshkn.errors import HostError
from mshkn.host import ExecResult, SnapshotFiles
from mshkn.host.fake import FakeHost
from mshkn.resources import DEFAULT_RESOURCES


async def test_blocks_track_volumes_and_fail_next() -> None:
    host = FakeHost()
    blocks = host.blocks
    await blocks.snap(source_volume_id=0, new_volume_id=100)
    await blocks.activate(volume_id=100, name="mshkn-comp-a")
    assert blocks.volumes == {0: None, 100: 0}
    assert "mshkn-comp-a" in blocks.active
    blocks.fail_next("snap")
    with pytest.raises(HostError):
        await blocks.snap(source_volume_id=0, new_volume_id=101)
    await blocks.remove(volume_id=100, name="mshkn-comp-a")
    assert 100 not in blocks.volumes and "mshkn-comp-a" not in blocks.active
    async with blocks.mounted("x") as path:
        assert path.is_dir()


async def test_hypervisor_boots_restores_snapshots_kills(tmp_path: Path) -> None:
    host = FakeHost()
    hv = host.hypervisor
    vm = await hv.boot(slot=3, disk_volume_id=100, disk_name="mshkn-comp-a", resources=DEFAULT_RESOURCES)
    assert vm.slot == 3 and vm.vm_ip == "172.16.3.2" and vm.tap_device == "tap3"
    assert hv.is_alive(vm.pid)
    files = await hv.snapshot(vm.socket_path, tmp_path / "ckpt")
    assert files.vmstate.exists() and files.memory.exists()
    vm2 = await hv.restore(slot=4, disk_volume_id=101, disk_name="mshkn-comp-b", snapshot=files)
    assert hv.restored == [(101, files)]
    await hv.kill(vm.pid)
    assert not hv.is_alive(vm.pid) and hv.is_alive(vm2.pid)
    await hv.teardown_slot(3)
    assert 3 in hv.torn_down
    assert isinstance(await hv.build_template(disk_volume_id=0, dest_dir=tmp_path / "tpl"), SnapshotFiles)


async def test_guest_scripts_and_records() -> None:
    host = FakeHost()
    guest = host.guest
    guest.script["python3 --version"] = ExecResult(0, "Python 3.12.3\n", "")
    r = await guest.exec("172.16.3.2", "python3 --version")
    assert r.stdout.startswith("Python 3")
    assert await guest.exec("172.16.3.2", "sync") == ExecResult(0, "", "")
    assert guest.commands == [("172.16.3.2", "python3 --version"), ("172.16.3.2", "sync")]
    guest.stream_script["ls"] = [("stdout", "a"), ("stdout", "b")]
    items = [i async for i in guest.stream("172.16.3.2", "ls")]
    assert items == [("stdout", "a"), ("stdout", "b"), ("exit", "0")]
    await guest.upload("172.16.3.2", "/tmp/f", b"data")
    assert await guest.download("172.16.3.2", "/tmp/f") == b"data"
    with pytest.raises(FileNotFoundError):
        await guest.download("172.16.3.2", "/nope")
    assert (await guest.metrics("172.16.3.2")).ram_total_mb > 0


async def test_objects_and_proxy_record(tmp_path: Path) -> None:
    host = FakeHost()
    src = tmp_path / "up"
    src.mkdir()
    (src / "vmstate").write_bytes(b"v")
    await host.objects.upload_dir(src, "acct/ckpt")
    dl = tmp_path / "dl"
    await host.objects.download_dir("acct/ckpt", dl)
    assert (dl / "vmstate").read_bytes() == b"v"
    await host.objects.delete_prefix("acct/ckpt")
    assert host.objects.prefixes == {}
    await host.proxy.add_route("comp-a", "172.16.3.2")
    assert host.proxy.routes == {"comp-a": "172.16.3.2"}
    await host.proxy.remove_route("comp-a")
    assert host.proxy.routes == {}
    assert await host.proxy.healthy()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_fake_host.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

`src/mshkn/host/fake.py`:

```python
"""In-memory implementations of the host protocols for flow tests and local runs.

Each fake records what was asked of it, keeps just enough state to make the
orchestrator's bookkeeping observable, and can be told to fail its next call
of a given method with `fail_next("<method>")`.
"""

from __future__ import annotations

import contextlib
import itertools
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.errors import HostError
from mshkn.host import (
    ExecResult,
    Host,
    OutputLine,
    PoolUsage,
    RunningVM,
    SnapshotFiles,
    VmMetrics,
)
from mshkn.host.network import slot_to_ip, slot_to_tap

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mshkn.resources import Resources


class _Failable:
    def __init__(self) -> None:
        self._fail: set[str] = set()

    def fail_next(self, method: str) -> None:
        self._fail.add(method)

    def _maybe_fail(self, method: str) -> None:
        if method in self._fail:
            self._fail.discard(method)
            raise HostError(f"fake {type(self).__name__}.{method} failed on request")


class FakeBlockStore(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.volumes: dict[int, int | None] = {0: None}  # id -> parent id
        self.active: dict[str, int] = {}  # device name -> volume id
        self.pool_usage = PoolUsage(data_used_ratio=0.1, metadata_used_ratio=0.05)
        self.calls: list[tuple[str, object]] = []

    async def snap(self, *, source_volume_id: int, new_volume_id: int) -> None:
        self._maybe_fail("snap")
        self.calls.append(("snap", (source_volume_id, new_volume_id)))
        self.volumes[new_volume_id] = source_volume_id

    async def activate(self, *, volume_id: int, name: str) -> None:
        self._maybe_fail("activate")
        self.active[name] = volume_id

    async def deactivate(self, name: str) -> None:
        self.active.pop(name, None)

    async def remove(self, *, volume_id: int, name: str) -> None:
        self._maybe_fail("remove")
        self.calls.append(("remove", (volume_id, name)))
        self.active.pop(name, None)
        self.volumes.pop(volume_id, None)

    async def mkfs(self, name: str) -> None:
        self.calls.append(("mkfs", name))

    @contextlib.asynccontextmanager
    async def mounted(self, name: str, *, readonly: bool = False) -> AsyncIterator[Path]:
        path = Path(tempfile.mkdtemp(prefix=f"fake-mnt-{name}-"))
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    async def max_volume_id(self) -> int | None:
        ids = [v for v in self.volumes if v != 0]
        return max(ids) if ids else None

    async def usage(self) -> PoolUsage:
        return self.pool_usage


class FakeHypervisor(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self._pids = itertools.count(1000)
        self.alive: dict[int, RunningVM] = {}
        self.booted: list[tuple[int, Resources]] = []
        self.restored: list[tuple[int, SnapshotFiles]] = []
        self.snapshots: list[tuple[str, Path]] = []
        self.torn_down: list[int] = []

    def _vm(self, slot: int, disk_name: str) -> RunningVM:
        pid = next(self._pids)
        _, vm_ip = slot_to_ip(slot)
        vm = RunningVM(pid=pid, socket_path=f"/tmp/fake-{disk_name}.socket", slot=slot, vm_ip=vm_ip, tap_device=slot_to_tap(slot))
        self.alive[pid] = vm
        return vm

    async def boot(self, *, slot: int, disk_volume_id: int, disk_name: str, resources: Resources) -> RunningVM:
        self._maybe_fail("boot")
        self.booted.append((disk_volume_id, resources))
        return self._vm(slot, disk_name)

    async def restore(self, *, slot: int, disk_volume_id: int, disk_name: str, snapshot: SnapshotFiles) -> RunningVM:
        self._maybe_fail("restore")
        self.restored.append((disk_volume_id, snapshot))
        return self._vm(slot, disk_name)

    async def snapshot(self, socket_path: str, dest_dir: Path) -> SnapshotFiles:
        self._maybe_fail("snapshot")
        dest_dir.mkdir(parents=True, exist_ok=True)
        files = SnapshotFiles(vmstate=dest_dir / "vmstate", memory=dest_dir / "memory")
        files.vmstate.write_bytes(b"fake-vmstate")
        files.memory.write_bytes(b"fake-memory")
        self.snapshots.append((socket_path, dest_dir))
        return files

    async def build_template(self, *, disk_volume_id: int, dest_dir: Path) -> SnapshotFiles:
        self._maybe_fail("build_template")
        return await self.snapshot(f"/tmp/fake-template-{disk_volume_id}.socket", dest_dir)

    async def kill(self, pid: int) -> None:
        self.alive.pop(pid, None)

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    async def teardown_slot(self, slot: int) -> None:
        self.torn_down.append(slot)


class FakeGuest(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.script: dict[str, ExecResult] = {}
        self.stream_script: dict[str, list[OutputLine]] = {}
        self.commands: list[tuple[str, str]] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.warmed: list[str] = []
        self.evicted: list[str] = []
        self.default = ExecResult(exit_code=0, stdout="", stderr="")
        self.default_metrics = VmMetrics(cpu_pct=1.5, ram_usage_mb=64, ram_total_mb=230, disk_usage_mb=200, disk_total_mb=7800, processes=[{"pid": 1, "command": "systemd"}])
        self._bg_pids = itertools.count(4000)

    async def exec(self, vm_ip: str, command: str, *, timeout: float = 300.0) -> ExecResult:
        self._maybe_fail("exec")
        self.commands.append((vm_ip, command))
        return self.script.get(command, self.default)

    async def stream(self, vm_ip: str, command: str, *, timeout: float = 60.0) -> AsyncIterator[OutputLine]:
        self._maybe_fail("stream")
        self.commands.append((vm_ip, command))
        for item in self.stream_script.get(command, []):
            yield item
        yield ("exit", "0")

    async def exec_bg(self, vm_ip: str, command: str) -> int:
        self._maybe_fail("exec_bg")
        self.commands.append((vm_ip, command))
        return next(self._bg_pids)

    async def upload(self, vm_ip: str, remote_path: str, data: bytes) -> None:
        self._maybe_fail("upload")
        self.files[(vm_ip, remote_path)] = data

    async def download(self, vm_ip: str, remote_path: str) -> bytes:
        self._maybe_fail("download")
        try:
            return self.files[(vm_ip, remote_path)]
        except KeyError:
            raise FileNotFoundError(f"File not found: {remote_path}") from None

    async def metrics(self, vm_ip: str, *, timeout: float = 10.0) -> VmMetrics:
        self._maybe_fail("metrics")
        return self.default_metrics

    async def warm(self, vm_ip: str) -> None:
        self.warmed.append(vm_ip)

    async def evict(self, vm_ip: str) -> None:
        self.evicted.append(vm_ip)

    async def close(self) -> None:
        return None


class FakeObjectStore(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.prefixes: dict[str, dict[str, bytes]] = {}

    async def upload_dir(self, local_dir: Path, prefix: str) -> None:
        self._maybe_fail("upload_dir")
        self.prefixes[prefix] = {p.name: p.read_bytes() for p in local_dir.iterdir() if p.is_file()}

    async def download_dir(self, prefix: str, local_dir: Path) -> None:
        self._maybe_fail("download_dir")
        local_dir.mkdir(parents=True, exist_ok=True)
        for name, data in self.prefixes.get(prefix, {}).items():
            (local_dir / name).write_bytes(data)

    async def delete_prefix(self, prefix: str) -> None:
        self.prefixes.pop(prefix, None)


class FakeProxy(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[str, str] = {}
        self.is_healthy = True

    async def add_route(self, computer_id: str, vm_ip: str) -> None:
        self._maybe_fail("add_route")
        self.routes[computer_id] = vm_ip

    async def remove_route(self, computer_id: str) -> None:
        self.routes.pop(computer_id, None)

    async def healthy(self) -> bool:
        return self.is_healthy

    async def close(self) -> None:
        return None


@dataclass
class FakeHostParts:
    hypervisor: FakeHypervisor = field(default_factory=FakeHypervisor)
    blocks: FakeBlockStore = field(default_factory=FakeBlockStore)
    guest: FakeGuest = field(default_factory=FakeGuest)
    objects: FakeObjectStore = field(default_factory=FakeObjectStore)
    proxy: FakeProxy = field(default_factory=FakeProxy)


def FakeHost() -> Host:  # noqa: N802 — reads as a constructor at call sites
    parts = FakeHostParts()
    return Host(hypervisor=parts.hypervisor, blocks=parts.blocks, guest=parts.guest, objects=parts.objects, proxy=parts.proxy)
```

The tests access fake-specific attributes (`host.blocks.volumes`, `host.hypervisor.restored`, ...) on a `Host` typed with the protocols; mypy will reject that in tests. Give the tests (and the flow conftest) the concrete types: in `test_fake_host.py`, `from typing import cast` and `blocks = cast(FakeBlockStore, host.blocks)` (same for the others), or have `FakeHost()` return a `Host` subclass `FakeHostInstance(Host)` whose fields are annotated with the fake types. Use the subclass: define `@dataclass class FakeHostInstance(Host): hypervisor: FakeHypervisor; blocks: FakeBlockStore; guest: FakeGuest; objects: FakeObjectStore; proxy: FakeProxy` and have `FakeHost()` return it; drop `FakeHostParts`.

`src/mshkn/host/firecracker_host.py`:

```python
"""Production Host wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import Host
from mshkn.host.caddy import CaddyProxy
from mshkn.host.dmthin import DmThinBlockStore
from mshkn.host.firecracker import FirecrackerHypervisor
from mshkn.host.r2 import RcloneObjectStore
from mshkn.host.ssh import SshGuest

if TYPE_CHECKING:
    from mshkn.config import Config


def firecracker_host(config: Config) -> Host:
    return Host(
        hypervisor=FirecrackerHypervisor(config),
        blocks=DmThinBlockStore(config.thin_pool_name, config.thin_volume_sectors),
        guest=SshGuest(config.ssh_key_path),
        objects=RcloneObjectStore(config.r2_bucket),
        proxy=CaddyProxy(config.caddy_admin_url, config.domain),
    )
```

- [ ] **Step 4: Verify**

`uv run pytest tests/unit/test_fake_host.py -q && uv run ruff check . && uv run mypy && uv run pytest -q 2>&1 | tail -1`
Expected: 4 passed; clean; `188 passed`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests && git commit -m "feat(host): in-memory fakes and the Firecracker host factory"
```

---

### Task 8: Switch VMManager, Runtime, routers, and the builder to the Host; delete the old modules

**Files:**
- Modify: `src/mshkn/vm/manager.py`, `src/mshkn/runtime.py`, `src/mshkn/api/computers.py`, `checkpoints.py`, `ingress.py`, `recipes.py`, `src/mshkn/recipe/builder.py`, `tests/unit/conftest.py`, `tests/unit/test_vm_manager.py`, `test_recipe_builder.py`, `test_exec_on_create.py`, `test_self_destruct.py`, `test_status_timeout.py`
- Delete: `src/mshkn/vm/ssh.py`, `vm/staging.py`, `vm/firecracker.py`, `vm/storage.py`, `src/mshkn/proxy/`, `src/mshkn/checkpoint/r2.py`, `checkpoint/snapshot.py`

**Interfaces:**
- `VMManager(config, db, *, host: Host, tasks: BackgroundTasks | None = None)`; `Runtime.host: Host` (fields `caddy`, `ssh_pool` removed); `Runtime.from_env` uses `firecracker_host(config)`; `Runtime.close` calls `host.guest.close()` and `host.proxy.close()`.
- `build_recipe(db, config, blocks: BlockStore, recipe_id, dockerfile, content_hash, allocate_volume_id)`.
- `tests/unit/conftest.py::make_runtime(db, *, vm_manager=None, config=None, host=None)` — `host` defaults to `FakeHost()`; the `ssh_pool` parameter is removed.

- [ ] **Step 1: VMManager**

Constructor stores `self.host = host`; the `_start_firecracker_with_snapshot` method is deleted (no callers). Replace host calls:

| Old | New |
|---|---|
| `pool_create_snap(pool, vol, src)` | `await self.host.blocks.snap(source_volume_id=src, new_volume_id=vol)` |
| `create_snapshot(pool, source, new, name, sectors)` (in `snapshot_disk_for_checkpoint`) | `await self.host.blocks.snap(source_volume_id=..., new_volume_id=...)` then `await self.host.blocks.activate(volume_id=..., name=...)` |
| `remove_volume(pool, name, vol)` | `await self.host.blocks.remove(volume_id=vol, name=name)` |
| `self._scan_pool_max_volume_id()` | `await self.host.blocks.max_volume_id()` (delete the method) |
| `restore_from_snapshot(vmstate, memory, disk_volume_id, final_slot, pool, sectors, final_volume_name, socket_path)` | `await self.host.hypervisor.restore(slot=slot, disk_volume_id=volume_id, disk_name=volume_name, snapshot=SnapshotFiles(vmstate=Path(vmstate), memory=Path(memory)))` |
| `cold_boot_from_disk(..., mem_size_mib, vcpu_count, ...)` | `await self.host.hypervisor.boot(slot=slot, disk_volume_id=volume_id, disk_name=volume_name, resources=resources)` (`DEFAULT_RESOURCES` where the old call omitted them) |
| `RestoreResult.pid/socket_path/vm_ip/tap_device` | the same attributes on `RunningVM` |
| `kill_firecracker_process(pid)` | `await self.host.hypervisor.kill(pid)` |
| `self._is_pid_alive(pid)` | `self.host.hypervisor.is_alive(pid)` (delete the method) |
| `destroy_tap(slot)` | `await self.host.hypervisor.teardown_slot(slot)` |
| `self.ssh_pool.get(vm_ip)` (warm) | `await self.host.guest.warm(vm_ip)` |
| `self.ssh_pool.remove(vm_ip)` | `await self.host.guest.evict(vm_ip)` |
| `ssh_exec(vm_ip, "sync", key, timeout=10.0, pool=...)` | `await self.host.guest.exec(vm_ip, "sync", timeout=10.0)` (keep the outer `wait_for(..., 15.0)`) |
| `create_vm_snapshot(socket_path, snapshot_dir)` | `await self.host.hypervisor.snapshot(socket_path, snapshot_dir)` |
| `upload_checkpoint(dir, prefix, bucket)` via `_upload_checkpoint_bg` | `self.host.objects.upload_dir(dir, prefix)` inside `_upload_checkpoint_bg` (drop the `upload_fn`/`bucket` parameters) |
| `delete_checkpoint_r2(prefix, bucket)` | `await self.host.objects.delete_prefix(prefix)` |
| `_download_checkpoint_snapshot`: per-file `rclone copyto` | `await self.host.objects.download_dir(checkpoint.r2_prefix, ckpt_dir)` (keep the `r2_prefix` check) |
| `self.caddy.add_route/remove_route` | `await self.host.proxy.add_route/remove_route` (no `None` checks; `Host` is required) |
| `_build_l3_template_for_recipe` + `_build_bare_l3_template` | one method: `async def _ensure_template(self, recipe: Recipe | None) -> SnapshotFiles | None` that returns cached paths from `recipe.template_*` / `get_bare_template`, else calls `await self.host.hypervisor.build_template(disk_volume_id=recipe.base_volume_id or 0, dest_dir=self.config.checkpoint_local_dir / "templates" / (recipe.id if recipe else "bare"))`, caches via `update_recipe_template` / `cache_bare_template`, and returns the files; on failure logs a warning and returns `None` (caller cold-boots). |

`create()` becomes: resolve recipe → allocate → `blocks.snap` → if `resources.is_default`: `files = await self._ensure_template(recipe)`; `vm = restore(...)` if files else `boot(...)` — else `boot(...)` → `guest.warm` → insert row → `proxy.add_route`. All function-local `from mshkn.vm.* / mshkn.checkpoint.* / mshkn.shell import` lines go away; the remaining function-local `from mshkn.db import ...` and the `_process_deferred` import stay for PR 4. Imports at the top: `from mshkn.host import Host, SnapshotFiles` (under `TYPE_CHECKING` for `Host`; `SnapshotFiles` is constructed at runtime).

- [ ] **Step 2: Runtime**

```python
@dataclass
class Runtime:
    config: Config
    db: aiosqlite.Connection
    host: Host
    vm_manager: VMManager
    tasks: BackgroundTasks
    rate_limiter: RateLimiter
    rule_limiters: dict[str, RateLimiter] = field(default_factory=dict)
    build_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
```

`from_env`: `host = firecracker_host(config)`; `vm_manager = VMManager(config, db, host=host, tasks=tasks)`. `close`: after `drain`, `await self.host.guest.close()` and `await self.host.proxy.close()`, then `db.close()`. Remove the `CaddyClient`/`SSHPool` imports.

- [ ] **Step 3: Routers and builder**

`api/computers.py`: delete the `mshkn.vm.ssh`, `mshkn.checkpoint.*` imports; `_self_destruct` and `_process_deferred` take `host: Host` instead of `pool`; replacements: `ssh_exec(ip, cmd, key, timeout=t, pool=p)` → `await rt.host.guest.exec(ip, cmd, timeout=t)` (or `host.guest.exec` inside helpers); `create_vm_snapshot(socket, dir)` → `await host.hypervisor.snapshot(socket, dir)`; `pool.remove(ip)` → `await host.guest.evict(ip)`; `upload_checkpoint(dir, prefix, bucket)` in `tasks.spawn(...)` → `host.objects.upload_dir(dir, prefix)`; `ssh_exec_bg` → `rt.host.guest.exec_bg(ip, cmd)`; `ssh_upload` → `rt.host.guest.upload(ip, path, data)`; `ssh_download` → `rt.host.guest.download(ip, path)`; `ssh_gather_metrics(ip, key, timeout=10.0, pool=...)` → `rt.host.guest.metrics(ip, timeout=10.0)` (keep the outer `wait_for`). The exec endpoint's generator becomes:

```python
    async def event_stream() -> AsyncIterator[dict[str, str]]:
        t0 = time.monotonic()
        try:
            async for stream, line in rt.host.guest.stream(computer.vm_ip, body.command):
                yield {"event": stream, "data": line}
        except Exception as exc:
            logger.warning("exec stream for %s failed: %s", computer_id, type(exc).__name__)
            yield {"event": "error", "data": f"{type(exc).__name__}: {exc}"}
            yield {"event": "exit", "data": "255"}
        finally:
            exec_duration_seconds.observe(time.monotonic() - t0)
```

`api/checkpoints.py`: the fork-exec path uses `rt.host.guest.exec`; `_self_destruct(..., host=rt.host, ...)`; merge uses `await rt.host.blocks.snap(source_volume_id=parent_vol, new_volume_id=merged_volume_id)`, `await rt.host.blocks.activate(volume_id=merged_volume_id, name=merged_volume_name)`, and `async with rt.host.blocks.mounted(vol_parent, readonly=True) as mount_parent, rt.host.blocks.mounted(vol_a, readonly=True) as mount_a, rt.host.blocks.mounted(vol_b, readonly=True) as mount_b, rt.host.blocks.mounted(merged_volume_name) as mount_output:` replacing the manual mount/unmount and `tempfile.mkdtemp`/`rmtree` (the merge result dir still uses a `tempfile.TemporaryDirectory`); `delete_checkpoint` uses `rt.host.blocks.remove(volume_id=..., name=...)` and `rt.host.objects.delete_prefix(ckpt.r2_prefix)`.

`api/ingress.py`: `ssh_exec(ip, cmd, key, pool=None)` → `await vm_manager.host.guest.exec(ip, cmd)` in `_do_create`/`_do_fork` (they receive `vm_manager`, which carries the host); `_self_destruct(..., host=vm_manager.host, ...)`.

`api/recipes.py`: `build_recipe(db, config, rt.host.blocks, recipe_id, ...)`; delete path uses `await rt.host.blocks.remove(volume_id=recipe.base_volume_id, name=f"mshkn-recipe-{recipe.id}")` (fix 4).

`recipe/builder.py`: `build_recipe(db, config, blocks: BlockStore, ...)`; `create_snapshot(...)` → `await blocks.snap(source_volume_id=0, new_volume_id=allocate_volume_id)` + `await blocks.activate(volume_id=allocate_volume_id, name=volume_name)`; `run("mkfs.ext4 ...")` → `await blocks.mkfs(volume_name)`; the mount/tar/post-process/umount block → `async with blocks.mounted(volume_name) as mount_point: await run(f"tar xf {tar_path} -C {mount_point}"); await _post_process_rootfs(str(mount_point), config)`; `run(f"dmsetup remove {volume_name}")` → `await blocks.deactivate(volume_name)`; in the `finally`, the leftover-device cleanup becomes `with contextlib.suppress(Exception): await blocks.deactivate(volume_name)` (the `mount_point` bookkeeping goes away). Docker commands keep using `run` from `mshkn.host.shell`.

Delete the old modules listed in Files. `grep -rn "mshkn.vm.ssh\|mshkn.vm.staging\|mshkn.vm.firecracker\|mshkn.vm.storage\|mshkn.proxy\|checkpoint.r2\|checkpoint.snapshot\|mshkn.shell\b" src tests` must be empty.

- [ ] **Step 4: Tests**

`tests/unit/conftest.py`: `make_runtime(db, *, vm_manager: Any = None, config: Config | None = None, host: Host | None = None) -> Runtime` with `host = host if host is not None else FakeHost()` and `Runtime(config=..., db=db, host=host, vm_manager=..., tasks=BackgroundTasks(), rate_limiter=RateLimiter(80, 10.0))`.

Convert the patched tests:
- `test_exec_on_create.py`: replace `patch("mshkn.api.computers.ssh_exec", return_value=mock_result)` and `patch("mshkn.vm.ssh.ssh_exec", ...)` with `host = FakeHost(); host.guest.script["echo hello world"] = ExecResult(0, "hello world\n", "")` and `make_runtime(db, vm_manager=vm_mgr, host=host)`; assertions on `mock_ssh.call_args` become assertions on `host.guest.commands`.
- `test_self_destruct.py`: same for `ssh_exec`; `create_vm_snapshot` and `upload_checkpoint` patches are simply dropped (the fake hypervisor writes snapshot files under `config.checkpoint_local_dir`, so `make_runtime` needs `config=Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")`; the fake object store records the prefix); `deliver_callback` patch stays. Assertions that a checkpoint was uploaded become `assert "acct-1/<ckpt>" in host.objects.prefixes` where the test previously asserted the upload mock was called.
- `test_status_timeout.py`: patch `host.guest.metrics` by subclassing or by monkeypatching the fake instance's method with the hanging coroutine (`monkeypatch.setattr(host.guest, "metrics", hanging)`), instead of patching `mshkn.api.computers.ssh_gather_metrics`.
- `test_vm_manager.py`: keep `test_slot_allocation` (set `manager.host = FakeHost()`); delete the `_start_firecracker_with_snapshot` test.
- `test_recipe_builder.py`: `ensure_base_image` test unchanged (patch target `mshkn.recipe.builder.run` still exists).

- [ ] **Step 5: Verify**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q 2>&1 | tail -1
grep -rnE "mshkn\.vm\.(ssh|staging|firecracker|storage|network)|mshkn\.proxy|checkpoint\.(r2|snapshot)|from mshkn\.shell|^_[a-z_]+ = asyncio\.Lock\(\)" src tests || echo "old modules gone, no module-level locks"
```

Expected: clean; `187 passed` (188 minus the deleted `_start_firecracker_with_snapshot` test); the grep prints its message.

- [ ] **Step 6: Commit**

```bash
git add -A src tests && git commit -m "refactor: VMManager, Runtime, routers, and the recipe builder use the Host boundary

Old vm/proxy/checkpoint host modules removed. Fixes folded in: Caddy route
removal never raises; staging cleanup checks tap existence; staging hop uses
config.ssh_key_path; recipe deletion removes the volume the builder created;
exec streams emit error+exit events instead of raising mid-response."
```

---

### Task 9: Flow-test tier

**Files:**
- Create: `tests/flow/__init__.py`, `tests/flow/conftest.py`, `tests/flow/test_lifecycle.py`

**Interfaces:**
- Fixture `flow` yields a `Flow(app, runtime, host, client)` dataclass: real `VMManager` on a `FakeHost`, temp DB with the test account (`acct-1` / `test-key`), `runtime.vm_manager.initialize()` run, background tasks drained on teardown.

- [ ] **Step 1: Write the tests**

`tests/flow/conftest.py`:

```python
"""Flow tier: the real app and VMManager against the in-memory fake host."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, insert_account, run_migrations
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import Account
from mshkn.ratelimit import RateLimiter
from mshkn.runtime import BackgroundTasks, Runtime
from mshkn.vm.manager import VMManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

AUTH = {"Authorization": "Bearer test-key"}


@dataclass
class Flow:
    app: FastAPI
    runtime: Runtime
    host: FakeHostInstance
    client: AsyncClient


@pytest.fixture
async def flow(tmp_path: Path) -> AsyncIterator[Flow]:
    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "checkpoints", idle_timeout_seconds=0)
    db = await connect(tmp_path / "flow.db")
    await run_migrations(db, Path("migrations"))
    await insert_account(db, Account(id="acct-1", api_key="test-key", vm_limit=10, created_at="2026-09-05T00:00:00"))
    host = FakeHost()
    tasks = BackgroundTasks()
    vm_manager = VMManager(config, db, host=host, tasks=tasks)
    runtime = Runtime(config=config, db=db, host=host, vm_manager=vm_manager, tasks=tasks, rate_limiter=RateLimiter(80, 10.0))
    await vm_manager.initialize()
    app = create_app(runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://flow", headers=AUTH) as client:
        try:
            yield Flow(app=app, runtime=runtime, host=host, client=client)
        finally:
            await tasks.drain(timeout=2.0)
            await db.close()
```

`tests/flow/test_lifecycle.py`:

```python
from __future__ import annotations

from mshkn.db import get_checkpoint, get_computer
from mshkn.host import ExecResult
from mshkn.models import ComputerStatus

from .conftest import Flow


async def _exec(flow: Flow, computer_id: str, command: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    current = "stdout"
    async with flow.client.stream("POST", f"/computers/{computer_id}/exec", json={"command": command}) as resp:
        assert resp.status_code == 200
        async for raw in resp.aiter_lines():
            line = raw.strip()
            if line.startswith("event: "):
                current = line[7:]
            elif line.startswith("data: "):
                events.append((current, line[6:]))
    return events


async def test_create_exec_checkpoint_fork_destroy(flow: Flow) -> None:
    host = flow.host

    # create: a CoW volume off the base image, a booted VM on a slot, a proxy route, a DB row
    resp = await flow.client.post("/computers", json={})
    assert resp.status_code == 200, resp.text
    cid = resp.json()["computer_id"]
    row = await get_computer(flow.runtime.db, cid)
    assert row is not None and row.status is ComputerStatus.RUNNING
    assert host.blocks.volumes[row.thin_volume_id] == 0
    assert host.hypervisor.is_alive(row.firecracker_pid or -1)
    assert host.proxy.routes == {cid: row.vm_ip}
    assert host.guest.warmed == [row.vm_ip]
    # first bare create builds the template once, then restores from it
    assert len(host.hypervisor.restored) == 1

    # exec streams through the guest
    host.guest.stream_script["echo hi"] = [("stdout", "hi")]
    assert await _exec(flow, cid, "echo hi") == [("stdout", "hi"), ("exit", "0")]

    # checkpoint: sync, snapshot files, evict, frozen disk, row with no parent
    host.guest.script["sync"] = ExecResult(0, "", "")
    resp = await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "base"})
    assert resp.status_code == 200, resp.text
    ckpt_id = resp.json()["checkpoint_id"]
    ckpt = await get_checkpoint(flow.runtime.db, ckpt_id)
    assert ckpt is not None and ckpt.parent_id is None and ckpt.label == "base"
    assert any(cmd == "sync" for _, cmd in host.guest.commands)
    assert host.guest.evicted == [row.vm_ip]
    assert host.blocks.volumes[ckpt.thin_volume_id or -1] == row.thin_volume_id
    assert (flow.runtime.config.checkpoint_local_dir / ckpt_id / "vmstate").exists()
    await flow.runtime.tasks.wait(f"upload:{ckpt_id}")
    assert f"acct-1/{ckpt_id}" in host.objects.prefixes

    # fork: a new VM restored from the checkpoint's disk and snapshot files
    resp = await flow.client.post(f"/checkpoints/{ckpt_id}/fork", json={})
    assert resp.status_code == 200, resp.text
    fork_id = resp.json()["computer_id"]
    fork_row = await get_computer(flow.runtime.db, fork_id)
    assert fork_row is not None and fork_row.source_checkpoint_id == ckpt_id
    assert host.blocks.volumes[fork_row.thin_volume_id] == ckpt.thin_volume_id
    assert host.hypervisor.restored[-1][0] == fork_row.thin_volume_id
    assert fork_row.vm_ip != row.vm_ip

    # destroy both: no VMs, no routes, volumes gone, rows destroyed
    for target in (cid, fork_id):
        resp = await flow.client.delete(f"/computers/{target}")
        assert resp.status_code == 200, resp.text
    assert host.hypervisor.alive == {}
    assert host.proxy.routes == {}
    assert row.thin_volume_id not in host.blocks.volumes
    assert fork_row.thin_volume_id not in host.blocks.volumes
    assert ckpt.thin_volume_id in host.blocks.volumes  # checkpoints persist
    for target in (cid, fork_id):
        r = await get_computer(flow.runtime.db, target)
        assert r is not None and r.status is ComputerStatus.DESTROYED
    assert sorted(host.hypervisor.torn_down) == sorted(int(r.tap_device[3:]) for r in (row, fork_row))


async def test_unknown_recipe_is_404_and_leaves_no_host_state(flow: Flow) -> None:
    resp = await flow.client.post("/computers", json={"recipe_id": "rcp-nope"})
    assert resp.status_code == 404
    assert flow.host.hypervisor.alive == {}
    assert flow.host.blocks.volumes == {0: None}
```

- [ ] **Step 2: Run to verify the harness and the tests**

`uv run pytest tests/flow -q -m flow`
Expected: 2 passed. If the lifecycle test fails, that is a real defect in the wiring from Task 8 (the fake records exactly what the manager asked); fix the manager or the fake's recording, never the assertion, and say which in the report.

- [ ] **Step 3: Verify the whole tree**

`uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q 2>&1 | tail -1 && uv run pytest --cov -q 2>&1 | grep TOTAL`
Expected: clean; `189 passed`; coverage TOTAL above 45% (report the number).

- [ ] **Step 4: Commit**

```bash
git add tests/flow && git commit -m "test: flow tier — real VMManager and API against the fake host, with the first lifecycle test"
```

---

### Task 10: Final verification, PR, CI, live E2E

- [ ] **Step 1:** Full local validation (verification-before-completion): `uv sync --frozen && uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov 2>&1 | grep -E "passed|TOTAL"`; clean tree.
- [ ] **Step 2:** Push `pr3-host-boundary`, open the PR with this body skeleton (fill the `<...>`):

```
Part 3 of 6 of the quality overhaul (spec §4, §5, §11; plan docs/superpowers/plans/2026-09-05-pr3-host-boundary.md).

**What this does**
Introduces `mshkn.host`: five protocols (Hypervisor, BlockStore, Guest, ObjectStore, Proxy) with Firecracker/dm-thin/asyncssh/rclone/Caddy implementations extracted from the old vm/, proxy/, and checkpoint/ modules, plus in-memory fakes. VMManager, the routers, and the recipe builder take their host operations from `Runtime.host`. SSH exec streaming now yields lines as they arrive. Adds the `tests/flow` tier: the real app and VMManager against the fake host.

**Deliberate behavior fixes**
- Caddy route removal never raises (concurrent deletes could 500).
- Staging cleanup checks tap existence (no more "Cannot find device tap254" on every restore).
- Staging hop uses config.ssh_key_path.
- Recipe deletion removes `mshkn-recipe-<recipe_id>` (the builder's name), not `<hash[:16]>`.
- `POST /computers/{id}/exec` emits `error` + `exit` events when the SSH session cannot be opened.
- Exec streams: line-by-line instead of buffered.

**Design alignment**
- §4.1 protocols: implemented; signature deviations recorded in the plan header (`snapshot(socket_path, dest_dir)`, `warm` instead of `wait_ready`, added `deactivate`).
- §4.2 implementations: staging lock is an instance attribute; no module-level mutable state remains.
- §4.3 fakes: implemented with fault injection.
- §5: Runtime carries `host`.
- §11 flow tier: harness plus the first lifecycle test; the rest of the flow suite is PR 5.

**Validation performed**
- Baseline before: <paste docs/superpowers/plans/2026-09-05-pr3-baseline.txt>
- After: ruff/format/mypy clean; `uv run pytest` <N> passed; coverage TOTAL <n>%.
- CI: <link>
- Live E2E (`scripts/e2e.sh` against 65.21.22.161 at <sha>): <151 passed, 6 skipped, 0 failed>; PR 2 baseline was 151/6/0.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01CPKyFZiT4pPi4v5gkph5KZ
```

- [ ] **Step 3:** `gh pr checks --watch` to green.
- [ ] **Step 4:** Live E2E, detached: `setsid nohup env MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000 scripts/e2e.sh -p no:cacheprovider > /tmp/e2e-pr3.log 2>&1 < /dev/null & disown`, poll the log for the summary line. Expected 151/6/0. Also confirm in `ssh mshkn journalctl -u mshkn --since '30 min ago' --no-pager | grep -c "Cannot find device tap254"` that the count is 0.
- [ ] **Step 5:** Triage bot reviews; report with the CI link and E2E summary; do not merge.

---

## Self-review

**Spec coverage:** §4.1 protocols and types → Task 2; §4.2 Firecracker hypervisor with the staging lock as an instance attribute and the staging hop using config → Task 4; dm-thin with `usage()` → Task 3; `SshGuest.stream` real streaming → Task 5; rclone and Caddy → Task 6; §4.3 fakes with fault injection → Task 7; §5 `Runtime.host` and the removal of `caddy`/`ssh_pool` → Task 8; §11 flow harness and first lifecycle test → Task 9; §14 step 3 as a whole. The five PR-3-owned bug fixes from PR 2's E2E logs are in Tasks 2 (tap existence), 6 (Caddy), 4 (key path), 8 (recipe name, SSE error event).

**Placeholder scan:** no `TBD`/`TODO` remain; PR-body `<...>` fields are filled at submission.

**Type consistency:** `Hypervisor.snapshot(socket_path, dest_dir)` is what `VMManager._auto_checkpoint_and_destroy`, the checkpoint endpoint, and `_self_destruct` call in Task 8 and what `FakeHypervisor` implements in Task 7. `BlockStore.snap/activate/remove/deactivate/mkfs/mounted/max_volume_id/usage` names match `DmThinBlockStore` (Task 3), `FakeBlockStore` (Task 7), and every call in Task 8. `Guest.exec/stream/exec_bg/upload/download/metrics/warm/evict/close` match `SshGuest`, `FakeGuest`, and the router rewrites. `FakeHost()` returns `FakeHostInstance` with concrete field types, which `tests/flow/conftest.py` imports. `firecracker_host(config)` (Task 7) is what `Runtime.from_env` calls (Task 8).
