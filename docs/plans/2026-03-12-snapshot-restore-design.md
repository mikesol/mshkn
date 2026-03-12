# Snapshot Restore for Create and Fork (#36 + #38)

Combined design for L3 capability memory cache (#36) and Firecracker snapshot restore for fork/resume (#38).

## Problem

Every `computer_create` and `fork_from_checkpoint` cold-boots a Firecracker VM. Kernel + systemd init takes ~500-800ms (the SSH readiness poll). Firecracker's `LOAD_SNAPSHOT` API can restore a running VM from a memory snapshot in ~9ms, with SSH ready in ~13ms.

## Spike Results

Tested on production hardware (FC 1.14.2):

| Metric | Cold Boot | Snapshot Restore |
|--------|-----------|-----------------|
| SSH ready | ~800-1000ms | ~13ms |
| LOAD_SNAPSHOT API call | N/A | ~9ms |
| Total create (bare, p95) | ~950ms | ~50ms (estimated with SSH pool) |

Key FC constraints discovered:
- **No pre-configuration**: FC will not accept drive/network config before LOAD_SNAPSHOT. The snapshot restores all config from vmstate.
- **No post-load config updates**: FC rejects drive/network changes after loading ("operation not supported after starting the microVM").
- **Path matching required**: The drive path and tap device name in the vmstate must point to valid resources on the host.
- **Tap rename works**: Renaming a tap device while FC is running doesn't break connectivity (FC holds the fd).

## Design

### Staging Slot

All snapshot restores go through a dedicated **staging slot** (slot 254):
- Tap device: `tap254`
- Host IP: `172.16.254.1`
- Guest IP: `172.16.254.2`
- MAC: `06:00:AC:10:FE:02`
- Drive path: `/dev/mapper/mshkn-restore-staging`

The staging slot is a serialization point. An `asyncio.Lock` ensures only one restore happens at a time. Serialization overhead is ~50ms per restore, which is negligible compared to the ~1000ms cold boot it replaces.

### Restore Pipeline (shared by #36 and #38)

```
async def restore_from_snapshot(vmstate_path, memory_path, disk_volume_id, final_slot):
    async with restore_lock:
        # 1. Map disk volume as staging drive name
        dmsetup create mshkn-restore-staging → disk_volume_id

        # 2. Create staging tap (tap254 with 172.16.254.x)
        create_tap(254)

        # 3. Start FC + LOAD_SNAPSHOT (~15ms)
        pid = start_firecracker_process(staging_socket)
        fc_client.load_snapshot(vmstate_path, memory_path, resume_vm=True)

        # 4. Wait for SSH at staging IP (~3-13ms)
        wait_for_ssh("172.16.254.2")

        # 5. Add final IP to guest (keep staging IP alive)
        ssh_exec("ip addr add {final_ip}/30 dev eth0")

        # 6. Rename tap + reconfigure host side
        ip link set tap254 name tap{final_slot}
        ip addr flush dev tap{final_slot}
        ip addr add {final_host_ip}/30 dev tap{final_slot}

        # 7. Remove staging IP from guest via final IP
        ssh_exec("ip addr del 172.16.254.2/30 dev eth0; ip route replace default via {final_gw}")

        # 8. Remove staging drive mapping
        dmsetup remove mshkn-restore-staging
```

After restore, the VM is running on its final slot with the correct IP. The staging resources are released for the next restore.

### L3 Capability Memory Cache (#36)

Extends the capability cache from two levels to three:

| Level | What's Cached | Exists? |
|-------|--------------|---------|
| L1 | Nix store paths (host) | Yes |
| L2 | dm-thin capability base volume (disk) | Yes |
| **L3** | **FC memory snapshot per manifest** | **New** |

**Template build flow** (background, after L2 cache population):
1. Boot a template VM from the capability volume on the staging slot
2. Wait for SSH readiness
3. Pause VM, create FC snapshot (vmstate + memory files)
4. Destroy template VM
5. Store snapshot files in `{checkpoint_local_dir}/templates/{manifest_hash}/`

**Create with L3 hit**:
1. Look up L3 cache by manifest_hash
2. Create dm-thin snapshot of capability base volume
3. Run restore pipeline (LOAD_SNAPSHOT → SSH reconfig → ~50ms)

**Create with L3 miss**:
1. Cold boot as today (~950ms)
2. Trigger background L3 template build for the manifest

**Storage**: One snapshot per cached manifest. ~256MB per snapshot (mem_size_mib default). With 3-5 common manifests, ~0.75-1.25GB NVMe.

**Eviction**: L3 snapshots are evicted when the corresponding L2 capability volume is evicted.

### Snapshot Restore for Fork (#38)

Checkpoints already have vmstate + memory files from `create_vm_snapshot`. Fork can use LOAD_SNAPSHOT instead of cold boot — but only if the checkpoint's vmstate references the staging slot's drive/tap names.

**When does a checkpoint have staging-compatible vmstate?**
- VMs created via L3 cache (LOAD_SNAPSHOT) have staging config in their vmstate
- FC doesn't know about the tap rename or drive remap — it still has the original staging names
- Checkpoints from these VMs inherit the staging names → forkable via LOAD_SNAPSHOT

**When is a checkpoint NOT staging-compatible?**
- VMs created via cold boot (L3 miss) have VM-specific drive/tap names
- Checkpoints from cold-booted VMs → must fork via cold boot (current behavior)

**Implementation**:
- Add `staging_compatible: bool` column to checkpoints table
- Set `True` when checkpoint is from a staging-restored VM
- On fork: if `staging_compatible`, use restore pipeline; else cold boot
- Over time, as L3 warms, most forks benefit from snapshot restore

### Network Reconfiguration

After LOAD_SNAPSHOT, the guest has the staging slot's IP (172.16.254.2). Two SSH commands reconfigure it:

1. `ip addr add {final_ip}/30 dev eth0` — adds new IP, keeps staging IP (SSH connection stays alive)
2. (Host renames tap, changes host IP)
3. `ip addr del 172.16.254.2/30 dev eth0; ip route replace default via {final_gw}` — removes staging IP via new IP

This avoids rootfs changes, MMDS setup, or network namespace complexity.

### Changes to Existing Code

**firecracker.py**: `load_snapshot()` method already exists (line 75-85). Will be used as-is.

**manager.py**:
- New `_restore_from_snapshot()` method (the restore pipeline above)
- `create()`: check L3 cache before cold boot, use `_restore_from_snapshot()` on hit
- `fork_from_checkpoint()`: check `staging_compatible`, use `_restore_from_snapshot()` if True
- `_get_or_build_capability_volume()`: trigger background L3 template build after L2 build

**network.py**: No changes to existing functions. The tap rename and IP reconfig are done inline in the restore pipeline.

**storage.py**: No changes. The staging drive mapping uses existing `create_snapshot` and `remove_volume`.

**New**: `capability/template_cache.py` — L3 cache management (template build, lookup, eviction).

**DB**: New `snapshot_templates` table for L3 cache. New `staging_compatible` column on `checkpoints`.

### E2E Test Changes

**New tests**:
- `test_warm_l3_cache_create_latency` — create with L3 hit, assert p95 well below current threshold
- `test_fork_from_staging_compatible_checkpoint` — fork via LOAD_SNAPSHOT, verify state + latency

**Existing tests**: If p95 improves, tighten thresholds to cement gains. Only merge if p95 is lower than main.

## What This Does NOT Change

- Checkpoint flow (pause → snapshot → resume → disk snapshot) — unchanged
- Cold boot path — still works, used as fallback for L3 miss
- Capability build (L1/L2 cache) — unchanged, L3 is an addition
- Network model for cold-booted VMs — unchanged (MAC-derived IP via fcnet-setup.sh)

## Risks

- **Staging lock contention**: Under high concurrent create load, the staging lock serializes restores. At ~50ms per restore, throughput is ~20 restores/sec. For our scale, this is fine.
- **SSH reconfig failure**: If SSH fails during IP reconfig, the VM is in a half-configured state. Mitigation: kill the VM and fall back to cold boot.
- **Memory snapshot staleness**: If the rootfs changes (e.g., new rootfs build), L3 templates must be invalidated. Tie L3 eviction to rootfs version.
- **Disk consistency**: The memory snapshot must match the disk state. For L3 templates, we take both at the same time. For checkpoints, they're already consistent (taken while VM is paused).
