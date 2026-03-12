# Snapshot Restore Implementation Plan (#36 + #38)

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace cold boot with Firecracker LOAD_SNAPSHOT for both `create()` (via L3 capability memory cache) and `fork_from_checkpoint()`, reducing create/fork p95 from ~1000ms to ~150ms.

**Architecture:** A staging slot (slot 254) with fixed tap/drive names handles all snapshot restores. After LOAD_SNAPSHOT, two SSH commands reconfigure the guest IP to the final slot. An L3 cache stores FC memory snapshots per manifest hash. On L3 miss, a two-phase boot cold-boots a template, snapshots it, caches it, then restores from cache — ensuring ALL VMs are staging-compatible.

**Tech Stack:** asyncio, Firecracker API (LOAD_SNAPSHOT), dm-thin (dmsetup), asyncssh, aiosqlite

**Spec:** `docs/plans/2026-03-12-snapshot-restore-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/mshkn/vm/staging.py` (NEW) | Staging slot constants, restore pipeline (`restore_from_snapshot`), network reconfig |
| `src/mshkn/capability/template_cache.py` (NEW) | L3 cache: lookup, build template, evict |
| `migrations/007_snapshot_templates.sql` (NEW) | `snapshot_templates` table for L3 cache |
| `src/mshkn/vm/manager.py` (MODIFY) | Wire `create()` and `fork_from_checkpoint()` through staging restore |
| `src/mshkn/capability/eviction.py` (MODIFY) | Evict L3 templates when L2 capability volume is evicted |
| `tests/e2e/test_phase1_latency.py` (MODIFY) | Add new tests, adjust thresholds |

---

## Chunk 1: Staging Slot and Restore Pipeline

### Task 1: DB Migration for snapshot_templates

**Files:**
- Create: `migrations/007_snapshot_templates.sql`

- [ ] **Step 1: Write the migration**

```sql
-- L3 capability memory cache: FC memory snapshots per manifest hash
CREATE TABLE IF NOT EXISTS snapshot_templates (
    manifest_hash TEXT PRIMARY KEY,
    vmstate_path TEXT NOT NULL,
    memory_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- [ ] **Step 2: Verify migration applies**

Run: `.venv/bin/python -c "import asyncio; from mshkn.db import run_migrations; import aiosqlite; asyncio.run((lambda: None)())"`

No automated test yet — migration will be applied when the server starts.

- [ ] **Step 3: Commit**

```bash
git add migrations/007_snapshot_templates.sql
git commit -m "feat: add snapshot_templates migration for L3 cache"
```

### Task 2: Staging Slot Module

**Files:**
- Create: `src/mshkn/vm/staging.py`
- Read (reference): `src/mshkn/vm/network.py`, `src/mshkn/vm/firecracker.py`, `src/mshkn/vm/storage.py`

- [ ] **Step 1: Write unit tests for staging constants and helpers**

Create `tests/unit/test_staging.py`:

```python
from mshkn.vm.staging import STAGING_SLOT, STAGING_TAP, STAGING_HOST_IP, STAGING_VM_IP, STAGING_MAC, STAGING_DRIVE_NAME


def test_staging_constants():
    assert STAGING_SLOT == 254
    assert STAGING_TAP == "tap254"
    assert STAGING_HOST_IP == "172.16.254.1"
    assert STAGING_VM_IP == "172.16.254.2"
    assert STAGING_MAC == "06:00:AC:10:FE:02"
    assert STAGING_DRIVE_NAME == "mshkn-restore-staging"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_staging.py -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Write staging module with constants and restore pipeline**

Create `src/mshkn/vm/staging.py`:

```python
"""Staging slot for Firecracker snapshot restore.

All LOAD_SNAPSHOT restores go through a dedicated staging slot (254).
After restore, the VM's network is reconfigured to its final slot via SSH.
An asyncio.Lock serializes restores (~50ms each).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mshkn.shell import run
from mshkn.vm.firecracker import (
    FirecrackerClient,
    kill_firecracker_process,
    start_firecracker_process,
)
from mshkn.vm.network import create_tap, destroy_tap, slot_to_ip, slot_to_mac, slot_to_tap

if TYPE_CHECKING:
    from mshkn.vm.ssh import SSHPool

logger = logging.getLogger(__name__)

# Staging slot constants — must match the vmstate baked into templates
STAGING_SLOT = 254
STAGING_TAP = "tap254"
STAGING_HOST_IP = "172.16.254.1"
STAGING_VM_IP = "172.16.254.2"
STAGING_MAC = "06:00:AC:10:FE:02"
STAGING_DRIVE_NAME = "mshkn-restore-staging"

# Global lock — only one restore at a time through the staging slot
_restore_lock = asyncio.Lock()


@dataclass
class RestoreResult:
    """Result of a staging restore — the VM is running on its final slot."""
    pid: int
    socket_path: str
    vm_ip: str
    tap_device: str


async def restore_from_snapshot(
    vmstate_path: str,
    memory_path: str,
    disk_volume_id: int,
    final_slot: int,
    pool_name: str,
    thin_volume_sectors: int,
    ssh_pool: SSHPool | None = None,
) -> RestoreResult:
    """Restore a VM from a Firecracker snapshot via the staging slot.

    1. Map disk as staging drive name
    2. Create staging tap
    3. Start FC + LOAD_SNAPSHOT
    4. Wait for SSH at staging IP
    5. Add final IP to guest
    6. Rename tap + reconfigure host side
    7. Remove staging IP from guest
    8. Remove staging drive mapping

    The caller is responsible for:
    - Creating the dm-thin snapshot (disk_volume_id) before calling this
    - Recording the Computer in the DB after this returns
    """
    final_host_ip, final_vm_ip = slot_to_ip(final_slot)
    final_tap = slot_to_tap(final_slot)
    final_mac = slot_to_mac(final_slot)
    socket_path = f"/tmp/fc-staging-{final_slot}.socket"

    async with _restore_lock:
        try:
            # 1. Map disk volume as staging drive name
            await run(
                f"dmsetup create {STAGING_DRIVE_NAME} "
                f"--table '0 {thin_volume_sectors} thin /dev/mapper/{pool_name} {disk_volume_id}'"
            )

            # 2. Create staging tap (tap254 with 172.16.254.x)
            await create_tap(STAGING_SLOT)

            # 3. Start FC + LOAD_SNAPSHOT
            pid = await start_firecracker_process(socket_path)
            fc_client = FirecrackerClient(socket_path)
            try:
                await fc_client.load_snapshot(vmstate_path, memory_path, resume_vm=True)
            finally:
                await fc_client.close()

            # 4. Wait for SSH at staging IP
            await _wait_for_ssh_staging(STAGING_VM_IP)

            # 5. Add final IP to guest (keep staging IP alive for SSH)
            ssh_key = "/root/.ssh/id_ed25519"
            await _ssh_exec(
                STAGING_VM_IP,
                f"ip addr add {final_vm_ip}/30 dev eth0",
                ssh_pool=ssh_pool,
            )

            # 6. Rename tap + reconfigure host side
            await run(f"ip link set {STAGING_TAP} name {final_tap}")
            await run(f"ip addr flush dev {final_tap}")
            await run(f"ip addr add {final_host_ip}/30 dev {final_tap}")
            # Pre-populate ARP for the final IP
            # Guest MAC is STAGING_MAC (baked into vmstate, unchanged by LOAD_SNAPSHOT)
            await run(f"ip neigh replace {final_vm_ip} lladdr {STAGING_MAC} dev {final_tap} nud permanent")
            # Add iptables rules for the final tap (matching create_tap pattern)
            await run(
                f"iptables -I FORWARD -i {final_tap} -s {final_vm_ip} "
                f"! -d 172.16.0.0/12 -j ACCEPT"
            )
            await run(f"iptables -I FORWARD -i {final_tap} -s {final_vm_ip} -d 172.16.0.0/12 -j DROP")

            # 7. Remove staging IP from guest via final IP
            await _ssh_exec(
                final_vm_ip,
                f"ip addr del {STAGING_VM_IP}/30 dev eth0; "
                f"ip route replace default via {final_host_ip}",
                ssh_pool=ssh_pool,
            )

            # 8. Remove staging drive mapping (FC still holds the fd via /dev/vda)
            await run(f"dmsetup remove {STAGING_DRIVE_NAME}")

        except Exception:
            # Cleanup on failure
            await _cleanup_staging(pid=locals().get("pid"), socket_path=socket_path)
            raise

    return RestoreResult(
        pid=pid,
        socket_path=socket_path,
        vm_ip=final_vm_ip,
        tap_device=final_tap,
    )


async def _cleanup_staging(pid: int | None = None, socket_path: str | None = None) -> None:
    """Best-effort cleanup of staging resources after a failed restore."""
    if pid is not None:
        try:
            await kill_firecracker_process(pid)
        except Exception:
            logger.warning("Failed to kill staging FC process PID=%s", pid)

    try:
        await destroy_tap(STAGING_SLOT)
    except Exception:
        pass

    try:
        await run(f"dmsetup remove {STAGING_DRIVE_NAME}", check=False)
    except Exception:
        pass


async def _wait_for_ssh_staging(vm_ip: str, timeout: float = 5.0) -> None:
    """Poll until VM port 22 accepts TCP connections. Short timeout for snapshot restore."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(vm_ip, 22),
                timeout=0.05,
            )
            writer.close()
            await writer.wait_closed()
            return
        except (OSError, TimeoutError):
            pass
        await asyncio.sleep(0.01)
    raise TimeoutError(f"Staging VM at {vm_ip} did not become reachable on port 22")


async def _ssh_exec(vm_ip: str, command: str, ssh_pool: SSHPool | None = None) -> str:
    """Execute a command on the VM via SSH. Uses pool if available, else direct."""
    import asyncssh

    if ssh_pool is not None:
        conn = await ssh_pool.get(vm_ip)
        result = await conn.run(command, check=True)
        return result.stdout or ""

    async with asyncssh.connect(
        vm_ip,
        username="root",
        known_hosts=None,
        client_keys=["/root/.ssh/id_ed25519"],
    ) as conn:
        result = await conn.run(command, check=True)
        return result.stdout or ""
```

- [ ] **Step 4: Run tests to verify constants pass**

Run: `.venv/bin/pytest tests/unit/test_staging.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/vm/staging.py tests/unit/test_staging.py
git commit -m "feat: add staging slot module with restore pipeline"
```

### Task 3: Template Cache Module (L3)

**Files:**
- Create: `src/mshkn/capability/template_cache.py`
- Read (reference): `src/mshkn/capability/cache.py`, `src/mshkn/checkpoint/snapshot.py`

- [ ] **Step 1: Write the template cache module**

```python
"""L3 capability memory cache — FC memory snapshots per manifest hash.

Stores vmstate + memory files so that create() can use LOAD_SNAPSHOT
instead of cold boot when a capability volume already has a cached template.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def get_cached_template(
    db: aiosqlite.Connection, manifest_hash: str
) -> tuple[str, str] | None:
    """Return (vmstate_path, memory_path) for manifest_hash, or None on miss."""
    cursor = await db.execute(
        "SELECT vmstate_path, memory_path FROM snapshot_templates WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    vmstate_path, memory_path = row[0], row[1]
    # Verify files still exist
    if not Path(vmstate_path).exists() or not Path(memory_path).exists():
        logger.warning("L3 cache hit but files missing for %s, evicting", manifest_hash)
        await db.execute(
            "DELETE FROM snapshot_templates WHERE manifest_hash = ?",
            (manifest_hash,),
        )
        await db.commit()
        return None
    return vmstate_path, memory_path


async def cache_template(
    db: aiosqlite.Connection,
    manifest_hash: str,
    vmstate_path: str,
    memory_path: str,
) -> None:
    """Store a template snapshot in the L3 cache."""
    await db.execute(
        "INSERT OR REPLACE INTO snapshot_templates "
        "(manifest_hash, vmstate_path, memory_path) VALUES (?, ?, ?)",
        (manifest_hash, vmstate_path, memory_path),
    )
    await db.commit()
    logger.info("Cached L3 template for %s", manifest_hash)


async def evict_template(db: aiosqlite.Connection, manifest_hash: str) -> None:
    """Remove a template from the L3 cache and delete its files."""
    cursor = await db.execute(
        "SELECT vmstate_path, memory_path FROM snapshot_templates WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    row = await cursor.fetchone()
    if row is not None:
        for path in (row[0], row[1]):
            p = Path(path)
            if p.exists():
                p.unlink()
        # Also remove the parent directory if empty
        parent = Path(row[0]).parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    await db.execute(
        "DELETE FROM snapshot_templates WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    await db.commit()
    logger.info("Evicted L3 template for %s", manifest_hash)
```

- [ ] **Step 2: Commit**

```bash
git add src/mshkn/capability/template_cache.py
git commit -m "feat: add L3 template cache module"
```

---

## Chunk 2: Wire Restore into Manager

### Task 4: Build Template on L2 Cache Miss

**Files:**
- Modify: `src/mshkn/vm/manager.py` (lines 220-286, `_get_or_build_capability_volume`)

After building the L2 capability volume, trigger a background L3 template build. The template build:
1. Cold-boots a template VM on the staging slot from the capability volume
2. Waits for SSH
3. Pauses VM, creates FC snapshot (vmstate + memory)
4. Destroys template VM
5. Stores snapshot files in `{checkpoint_local_dir}/templates/{manifest_hash}/`

- [ ] **Step 1: Add `_build_l3_template` method to VMManager**

Add after `_get_or_build_capability_volume` (after line 286):

```python
async def _build_l3_template(
    self, manifest_hash: str, source_volume_id: int
) -> None:
    """Build an L3 template: cold-boot on staging slot, snapshot, cache."""
    from mshkn.capability.template_cache import cache_template
    from mshkn.vm.staging import (
        STAGING_DRIVE_NAME,
        STAGING_MAC,
        STAGING_SLOT,
        STAGING_TAP,
        STAGING_VM_IP,
        _restore_lock,
    )

    template_dir = self.config.checkpoint_local_dir / "templates" / manifest_hash
    template_dir.mkdir(parents=True, exist_ok=True)
    vmstate_path = template_dir / "vmstate"
    memory_path = template_dir / "memory"

    socket_path = f"/tmp/fc-template-{manifest_hash}.socket"
    pid: int | None = None

    async with _restore_lock:
        try:
            # Map capability volume as staging drive
            await run(
                f"dmsetup create {STAGING_DRIVE_NAME} "
                f"--table '0 {self.config.thin_volume_sectors} thin "
                f"/dev/mapper/{self.config.thin_pool_name} {source_volume_id}'"
            )

            # Create staging tap
            await create_tap(STAGING_SLOT)

            # Start FC and cold-boot on staging slot
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

            # Wait for SSH
            await self._wait_for_ssh(STAGING_VM_IP)

            # Pause and snapshot
            fc_client = FirecrackerClient(socket_path)
            try:
                await fc_client.pause()
                await fc_client.create_snapshot(str(vmstate_path), str(memory_path))
            finally:
                await fc_client.close()

            # Kill template VM
            await kill_firecracker_process(pid)
            pid = None

            # Destroy staging tap
            await destroy_tap(STAGING_SLOT)

            # Remove staging drive mapping
            await run(f"dmsetup remove {STAGING_DRIVE_NAME}")

            # Cache the template
            await cache_template(
                self.db, manifest_hash, str(vmstate_path), str(memory_path)
            )
            logger.info("Built L3 template for %s", manifest_hash)

        except Exception:
            logger.exception("Failed to build L3 template for %s", manifest_hash)
            # Cleanup
            if pid is not None:
                await kill_firecracker_process(pid)
            await destroy_tap(STAGING_SLOT)
            await run(f"dmsetup remove {STAGING_DRIVE_NAME}", check=False)
            raise
```

You MUST add this import at the top of manager.py (it is not currently imported):
```python
from mshkn.shell import run
```

- [ ] **Step 2: Modify `_get_or_build_capability_volume` to trigger L3 build**

After the existing L2 cache population (after line 285 `return cap_volume_id`), the L3 build needs to happen. But we don't want to block `create()` on L3 build the first time — the two-phase boot in `create()` handles L3 miss. So trigger the L3 build as a background task:

Actually, the two-phase boot design says on L3 miss, `create()` itself does the template build synchronously (cold-boot → snapshot → cache → restore). So `_build_l3_template` will be called from `create()`, not from `_get_or_build_capability_volume`. No changes to `_get_or_build_capability_volume`.

- [ ] **Step 3: Commit**

```bash
git add src/mshkn/vm/manager.py
git commit -m "feat: add _build_l3_template method to VMManager"
```

### Task 5: Wire `create()` Through Staging Restore

**Files:**
- Modify: `src/mshkn/vm/manager.py` (lines 288-365, `create` method)

The new flow:
1. Get capability volume (L2, unchanged)
2. Check L3 cache
3. On L3 hit: allocate slot + volume_id, create dm-thin snapshot, call `restore_from_snapshot()`
4. On L3 miss: call `_build_l3_template()` (two-phase boot), then restore from fresh cache
5. Record in DB, register Caddy route

- [ ] **Step 1: Rewrite `create()` method**

Replace `create()` (lines 288-365) with:

```python
async def create(
    self,
    account_id: str,
    manifest: Manifest,
    needs: dict[str, object] | None = None,
) -> Computer:
    mem_size_mib, vcpu_count = parse_needs(needs)
    computer_id = f"comp-{uuid.uuid4().hex[:12]}"

    # Get capability base volume (L1/L2 cache, builds if miss)
    source_volume_id = await self._get_or_build_capability_volume(manifest)
    manifest_hash = manifest.content_hash() if manifest.uses else "bare"

    # Check L3 cache
    from mshkn.capability.template_cache import get_cached_template

    template = await get_cached_template(self.db, manifest_hash)
    if template is None:
        # L3 miss — two-phase boot: build template, then restore
        logger.info("L3 cache miss for %s, building template...", manifest_hash)
        await self._build_l3_template(manifest_hash, source_volume_id)
        template = await get_cached_template(self.db, manifest_hash)
        if template is None:
            raise RuntimeError(f"L3 template build failed for {manifest_hash}")

    vmstate_path, memory_path = template

    # Allocate slot + volume, create dm-thin snapshot
    async with self._alloc_lock:
        slot = self._allocate_slot()
        volume_id = self._allocate_volume_id()
    volume_name = f"mshkn-{computer_id}"

    # Create dm-thin snapshot of capability base volume
    await create_snapshot(
        pool_name=self.config.thin_pool_name,
        source_volume_id=source_volume_id,
        new_volume_id=volume_id,
        new_volume_name=volume_name,
        sectors=self.config.thin_volume_sectors,
    )

    # Restore from snapshot via staging slot
    from mshkn.vm.staging import restore_from_snapshot

    result = await restore_from_snapshot(
        vmstate_path=vmstate_path,
        memory_path=memory_path,
        disk_volume_id=volume_id,
        final_slot=slot,
        pool_name=self.config.thin_pool_name,
        thin_volume_sectors=self.config.thin_volume_sectors,
        ssh_pool=self.ssh_pool,
    )

    # Warm SSH pool
    _host_ip, vm_ip = slot_to_ip(slot)
    if self.ssh_pool is not None:
        await self.ssh_pool.get(vm_ip)

    # Record in DB
    now = datetime.now(UTC).isoformat()
    computer = Computer(
        id=computer_id,
        account_id=account_id,
        thin_volume_id=volume_id,
        tap_device=result.tap_device,
        vm_ip=result.vm_ip,
        socket_path=result.socket_path,
        firecracker_pid=result.pid,
        manifest_hash=manifest.content_hash(),
        manifest_json=manifest.to_json(),
        status="running",
        created_at=now,
        last_exec_at=None,
    )
    await insert_computer(self.db, computer)

    # Register Caddy route
    if self.caddy is not None:
        await self.caddy.add_route(computer_id, vm_ip)

    logger.info("Created computer %s (slot=%d, ip=%s)", computer_id, slot, vm_ip)
    return computer
```

**IMPORTANT**: The `restore_from_snapshot` function expects `disk_volume_id` — the dm-thin volume ID, not the volume name. It creates the dm mapping `mshkn-restore-staging` pointing to that volume ID internally. But wait — we already created the volume with name `mshkn-{computer_id}` via `create_snapshot()`. The staging restore needs to map `STAGING_DRIVE_NAME` to the same volume ID. This means the dm-thin volume must NOT be activated under its final name before restore — the staging pipeline activates it under the staging name.

**Revised approach**: Don't call `create_snapshot` with the final volume name. Instead, only create the thin snapshot in the pool (without activating it as a device), then let `restore_from_snapshot` activate it as `mshkn-restore-staging`. After restore, remove the staging mapping, and activate under the final name.

Update `restore_from_snapshot` to also activate the final volume name after removing staging:

In `staging.py`, after step 8 (remove staging drive mapping), add:
```python
# 9. Activate disk under final volume name
await run(
    f"dmsetup create {final_volume_name} "
    f"--table '0 {thin_volume_sectors} thin /dev/mapper/{pool_name} {disk_volume_id}'"
)
```

And update the function signature to accept `final_volume_name: str`.

In `create()`, replace the `create_snapshot()` call with just the thin snapshot message (no device activation):
```python
# Create dm-thin snapshot in pool (no device activation — staging will activate it)
await run(
    f"dmsetup message {self.config.thin_pool_name} 0 "
    f"'create_snap {volume_id} {source_volume_id}'"
)
```

- [ ] **Step 2: Update staging.py to accept final_volume_name and activate after restore**

Add `final_volume_name: str` parameter to `restore_from_snapshot()` and add step 9:

```python
async def restore_from_snapshot(
    vmstate_path: str,
    memory_path: str,
    disk_volume_id: int,
    final_slot: int,
    pool_name: str,
    thin_volume_sectors: int,
    final_volume_name: str,
    ssh_pool: SSHPool | None = None,
) -> RestoreResult:
```

After step 8 (dmsetup remove staging), add:
```python
            # 9. Activate disk under final volume name
            await run(
                f"dmsetup create {final_volume_name} "
                f"--table '0 {thin_volume_sectors} thin /dev/mapper/{pool_name} {disk_volume_id}'"
            )
```

- [ ] **Step 3: Verify ruff + mypy pass**

Run: `.venv/bin/ruff check src/mshkn/vm/staging.py src/mshkn/vm/manager.py && .venv/bin/mypy src/mshkn/vm/staging.py src/mshkn/vm/manager.py`

- [ ] **Step 4: Commit**

```bash
git add src/mshkn/vm/manager.py src/mshkn/vm/staging.py
git commit -m "feat: wire create() through staging restore with L3 cache"
```

### Task 6: Wire `fork_from_checkpoint()` Through Staging Restore

**Files:**
- Modify: `src/mshkn/vm/manager.py` (lines 392-476, `fork_from_checkpoint` method)

The new flow: ALL forks go through `restore_from_snapshot()`. The checkpoint has vmstate + memory files (from `create_vm_snapshot`). These files reference the staging slot's tap/drive names because the original VM was created via staging restore.

- [ ] **Step 1: Rewrite `fork_from_checkpoint()` method**

Replace `fork_from_checkpoint()` (lines 392-476) with:

```python
async def fork_from_checkpoint(
    self, account_id: str, checkpoint: Checkpoint, manifest: Manifest | None = None,
) -> Computer:
    """Fork a new computer from a checkpoint via LOAD_SNAPSHOT.

    All checkpoints are staging-compatible because all VMs are created
    via the staging slot (two-phase boot ensures this).
    """
    if checkpoint.thin_volume_id is None:
        msg = f"Checkpoint {checkpoint.id} has no disk snapshot (created before this fix)"
        raise ValueError(msg)

    computer_id = f"comp-{uuid.uuid4().hex[:12]}"
    async with self._alloc_lock:
        slot = self._allocate_slot()
        volume_id = self._allocate_volume_id()
    volume_name = f"mshkn-{computer_id}"

    # Get checkpoint's vmstate + memory paths
    ckpt_dir = self.config.checkpoint_local_dir / checkpoint.id
    vmstate_path = str(ckpt_dir / "vmstate")
    memory_path = str(ckpt_dir / "memory")

    # If files not local, download from R2
    if not Path(vmstate_path).exists() or not Path(memory_path).exists():
        await self._download_checkpoint_snapshot(checkpoint)

    # Create dm-thin snapshot of checkpoint's disk (pool only, no device activation)
    await run(
        f"dmsetup message {self.config.thin_pool_name} 0 "
        f"'create_snap {volume_id} {checkpoint.thin_volume_id}'"
    )

    # Restore from snapshot via staging slot
    from mshkn.vm.staging import restore_from_snapshot

    result = await restore_from_snapshot(
        vmstate_path=vmstate_path,
        memory_path=memory_path,
        disk_volume_id=volume_id,
        final_slot=slot,
        pool_name=self.config.thin_pool_name,
        thin_volume_sectors=self.config.thin_volume_sectors,
        final_volume_name=volume_name,
        ssh_pool=self.ssh_pool,
    )

    # Warm SSH pool
    if self.ssh_pool is not None:
        await self.ssh_pool.get(result.vm_ip)

    # Record in DB
    now = datetime.now(UTC).isoformat()
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
    await insert_computer(self.db, computer)

    # Register Caddy route
    if self.caddy is not None:
        await self.caddy.add_route(computer_id, result.vm_ip)

    logger.info(
        "Forked computer %s from checkpoint %s (slot=%d, ip=%s)",
        computer_id, checkpoint.id, slot, result.vm_ip,
    )
    return computer
```

- [ ] **Step 2: Add `_download_checkpoint_snapshot` helper**

Add to VMManager class:

```python
async def _download_checkpoint_snapshot(self, checkpoint: Checkpoint) -> None:
    """Download vmstate + memory files from R2 if not cached locally."""
    ckpt_dir = self.config.checkpoint_local_dir / checkpoint.id
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    if not checkpoint.r2_prefix:
        raise ValueError(f"Checkpoint {checkpoint.id} has no R2 prefix")

    for filename in ("vmstate", "memory"):
        local_path = ckpt_dir / filename
        if not local_path.exists():
            r2_path = f"{self.config.r2_bucket}:{checkpoint.r2_prefix}/{filename}"
            await run(f"rclone copyto r2:{r2_path} {local_path}")
            logger.info("Downloaded %s for checkpoint %s", filename, checkpoint.id)
```

- [ ] **Step 3: Verify ruff + mypy pass**

Run: `.venv/bin/ruff check src/mshkn/vm/manager.py && .venv/bin/mypy src/mshkn/vm/manager.py`

- [ ] **Step 4: Commit**

```bash
git add src/mshkn/vm/manager.py
git commit -m "feat: wire fork_from_checkpoint() through staging restore"
```

---

## Chunk 3: Eviction and Cleanup

### Task 7: Wire L3 Eviction to L2 Eviction

**Files:**
- Modify: `src/mshkn/capability/eviction.py`

When an L2 capability volume is evicted, also evict its L3 template.

- [ ] **Step 1: Add L3 eviction call to `evict_lru_capabilities`**

After the existing `DELETE FROM capability_cache` (line 59-62), add:

```python
        # Also evict L3 template if present
        from mshkn.capability.template_cache import evict_template
        await evict_template(db, manifest_hash)
```

- [ ] **Step 2: Verify ruff pass**

Run: `.venv/bin/ruff check src/mshkn/capability/eviction.py`

- [ ] **Step 3: Commit**

```bash
git add src/mshkn/capability/eviction.py
git commit -m "feat: evict L3 templates when L2 capability volume is evicted"
```

### Task 8: Reserve Staging Slot in VMManager

**Files:**
- Modify: `src/mshkn/vm/manager.py` (lines 171-178, `_allocate_slot`)

Slot 254 is reserved for staging. Ensure it's never allocated to a regular VM.

- [ ] **Step 1: Update `_allocate_slot` to skip slot 254**

```python
def _allocate_slot(self) -> int:
    if self._free_slots:
        slot = self._free_slots.pop()
        if slot == 254:  # staging slot, skip
            return self._allocate_slot()
        return slot
    slot = self._next_slot
    if slot == 254:  # skip staging slot
        self._next_slot = 255
        slot = 255
    if slot > 255:
        raise RuntimeError("No free VM slots (all 255 in use)")
    self._next_slot += 1
    return slot
```

- [ ] **Step 2: Commit**

```bash
git add src/mshkn/vm/manager.py
git commit -m "feat: reserve slot 254 for staging restore"
```

---

## Chunk 4: E2E Tests and Threshold Adjustments

### Task 9: Update Existing Latency Thresholds

**Files:**
- Modify: `tests/e2e/test_phase1_latency.py` (lines 21-34, constants)

- [ ] **Step 1: Adjust thresholds**

The two-phase boot adds ~500ms to the first create of a new manifest (L3 miss). But warm-cache creates and forks are now LOAD_SNAPSHOT (~150ms). Adjust:

```python
BARE_CREATE_SAMPLES = 20
BARE_CREATE_P95_MS = 1600       # was 950 — L3 miss = two-phase boot (~1500ms)
WARM_CACHE_CREATE_SAMPLES = 10
WARM_CACHE_CREATE_P95_MS = 1600  # was 1150 — L3 miss with capability build
EMPTY_CHECKPOINT_SAMPLES = 10
EMPTY_CHECKPOINT_P95_MS = 1150   # unchanged
SMALL_STATE_CHECKPOINT_SAMPLES = 10
SMALL_STATE_CHECKPOINT_P95_MS = 1000  # unchanged
MANY_SMALL_FILES_CHECKPOINT_SAMPLES = 10
MANY_SMALL_FILES_CHECKPOINT_P95_MS = 1550  # unchanged
RESUME_SAMPLES = 10
RESUME_P95_MS = 350             # was 850 — now LOAD_SNAPSHOT
FORK_MINIMAL_SAMPLES = 10
FORK_MINIMAL_P95_MS = 350       # was 850 — now LOAD_SNAPSHOT
```

Note: The bare create test creates bare VMs (empty manifest). The first one will be an L3 miss (two-phase boot ~1500ms), but subsequent ones will be L3 hits (~150ms). The p95 over 20 samples should still pass at 1600ms since only the first sample pays the penalty. We may tighten after measuring.

For resume and fork: these always go through LOAD_SNAPSHOT since the checkpoint was created from a staging-restored VM. Target 350ms to be conservative (LOAD_SNAPSHOT ~50ms + SSH reconfig ~50ms + staging lock wait + network setup).

- [ ] **Step 2: Commit**

```bash
git add tests/e2e/test_phase1_latency.py
git commit -m "feat: adjust latency thresholds for snapshot restore"
```

### Task 10: Add New Latency Tests

**Files:**
- Modify: `tests/e2e/test_phase1_latency.py`

- [ ] **Step 1: Add warm L3 cache create latency test**

Add after the existing `TestT11CreateLatency` class (after line 115):

```python
# ---------------------------------------------------------------------------
# T1.6 — Warm L3 Cache Create Latency
# ---------------------------------------------------------------------------

WARM_L3_CREATE_SAMPLES = 10
WARM_L3_CREATE_P95_MS = 350  # LOAD_SNAPSHOT path, ~50ms + overhead


class TestT16WarmL3CreateLatency:
    """Create with warm L3 cache — should be LOAD_SNAPSHOT fast."""

    async def test_warm_l3_cache_create_latency(self, client):
        """First create warms L3 cache, subsequent creates should be fast."""
        # Warm the L3 cache with a throwaway create
        warmup_id = await create_computer(client, uses=[])
        await destroy_computer(client, warmup_id)

        # Now measure with warm L3 cache
        timings: list[float] = []
        for i in range(WARM_L3_CREATE_SAMPLES):
            computer_id: str | None = None
            try:
                start = time.perf_counter()
                computer_id = await create_computer(client, uses=[])
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                print(f"  warm L3 create #{i+1}: {elapsed_ms:.0f}ms")
            finally:
                if computer_id is not None:
                    await destroy_computer(client, computer_id)

        stats = LatencyStats(values_ms=timings)
        print(stats.report("T1.6 Warm L3 Cache Create", target_ms=WARM_L3_CREATE_P95_MS))
        assert stats.p95 <= WARM_L3_CREATE_P95_MS, (
            f"p95 warm L3 create latency {stats.p95:.0f}ms exceeds {WARM_L3_CREATE_P95_MS}ms"
        )
```

- [ ] **Step 2: Add fork snapshot restore latency test**

Add after the existing `TestT14ForkLatency` class:

```python
# ---------------------------------------------------------------------------
# T1.7 — Fork Snapshot Restore Latency
# ---------------------------------------------------------------------------

FORK_RESTORE_SAMPLES = 10
FORK_RESTORE_P95_MS = 350  # LOAD_SNAPSHOT fork, ~50ms + overhead


class TestT17ForkRestoreLatency:
    """Fork via LOAD_SNAPSHOT — verify state preservation + latency."""

    async def test_fork_snapshot_restore_latency(self, long_client):
        """Fork from checkpoint, verify state is preserved, assert latency."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            # Write a marker file
            await exec_command(
                long_client, computer_id,
                "echo 'snapshot-restore-test' > /tmp/marker.txt"
            )

            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="restore-test"
            )

            timings: list[float] = []
            forked_ids: list[str] = []
            try:
                for i in range(FORK_RESTORE_SAMPLES):
                    start = time.perf_counter()
                    forked_id = await fork_checkpoint(long_client, checkpoint_id)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    timings.append(elapsed_ms)
                    forked_ids.append(forked_id)
                    print(f"  fork restore #{i+1}: {elapsed_ms:.0f}ms")

                # Verify state on last forked VM
                result = await exec_command(
                    long_client, forked_ids[-1], "cat /tmp/marker.txt"
                )
                assert result.stdout.strip() == "snapshot-restore-test", (
                    f"State not preserved: got {result.stdout.strip()!r}"
                )
            finally:
                for fid in forked_ids:
                    await destroy_computer(long_client, fid)

            stats = LatencyStats(values_ms=timings)
            print(stats.report("T1.7 Fork Restore Latency", target_ms=FORK_RESTORE_P95_MS))
            assert stats.p95 <= FORK_RESTORE_P95_MS, (
                f"p95 fork restore latency {stats.p95:.0f}ms exceeds {FORK_RESTORE_P95_MS}ms"
            )
```

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_phase1_latency.py
git commit -m "feat: add warm L3 cache and fork restore E2E latency tests"
```

---

## Chunk 5: Deploy, Measure, Gate

### Task 11: Lint, Type-Check, and Unit Test

- [ ] **Step 1: Run full local validation**

```bash
.venv/bin/ruff check src/ && .venv/bin/mypy src/ && .venv/bin/pytest tests/ --ignore=tests/e2e --ignore=tests/integration -x
```

Fix any issues before proceeding.

- [ ] **Step 2: Commit any fixes**

### Task 12: Deploy and Run E2E

- [ ] **Step 1: Push and deploy**

```bash
git push origin issue-36-38
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215 "cd /opt/mshkn && git fetch && git checkout issue-36-38 && git pull && systemctl restart mshkn && systemctl restart litestream"
```

- [ ] **Step 2: Clean stale VMs**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215 "pkill -f firecracker; for tap in \$(ip -o link show type tun | awk -F: '{print \$2}' | tr -d ' '); do ip link del \"\$tap\" 2>/dev/null; done"
```

- [ ] **Step 3: Run E2E tests**

```bash
MSHKN_API_URL=http://135.181.6.215:8000 .venv/bin/pytest tests/e2e/test_phase1_latency.py -v --tb=short
```

- [ ] **Step 4: Compare p95 with main**

If any new or tightened threshold fails: investigate, adjust, and re-run. If the overall p95 profile is worse than main, do NOT merge — report to Mike.

- [ ] **Step 5: Tighten thresholds based on measured results**

After E2E passes, check actual p95 values. If they're significantly below the threshold, tighten:
- If warm L3 create p95 is ~100ms, tighten from 350ms to ~200ms
- If fork restore p95 is ~100ms, tighten from 350ms to ~200ms
- If resume p95 is ~100ms, tighten from 350ms to ~200ms

Re-run E2E to confirm tightened thresholds still pass.

- [ ] **Step 6: Final commit with measured thresholds**

```bash
git add tests/e2e/test_phase1_latency.py
git commit -m "feat: tighten latency thresholds based on measured results"
```

### Task 13: Create PR

- [ ] **Step 1: Create PR**

```bash
gh pr create --title "feat: snapshot restore for create and fork (#36 + #38)" --body "$(cat <<'EOF'
## Summary
- Replace cold boot with Firecracker LOAD_SNAPSHOT for both `create()` and `fork_from_checkpoint()`
- L3 capability memory cache stores FC memory snapshots per manifest hash
- Two-phase boot on L3 miss ensures ALL VMs are staging-compatible
- Staging slot (254) serializes all snapshot restores via asyncio.Lock

Closes #36
Closes #38

## Design alignment
- Follows `docs/plans/2026-03-12-snapshot-restore-design.md`
- Staging slot pattern from spike results
- SSH-based IP reconfig (no MMDS, no netns)
- Two-phase boot per Mike's design direction

## Validation performed
- Unit tests pass (ruff, mypy, pytest)
- E2E latency benchmarks pass on live server (135.181.6.215)
- p95 fork/resume latency reduced from ~850ms to ~Xms (measured)
- p95 warm L3 create latency ~Xms (measured)
- All existing E2E tests continue to pass

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Update the "Xms" placeholders with actual measured values before creating the PR.

- [ ] **Step 2: Wait for CI**

```bash
gh pr checks <N> --watch
```
