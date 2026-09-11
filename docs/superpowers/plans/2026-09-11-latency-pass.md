# Latency Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lower create/fork and checkpoint p95 on the live host by removing the per-connection PAM cost in the guest, the fsync of the memory file, unthrottled uploads, serial bring-up and teardown steps, and the coarse kill wait, while deciding each of #143 to #151 on evidence.

**Architecture:** Every change stays behind the existing host boundary: the guest image (`_post_process_rootfs`), the hypervisor (`kill_firecracker_process`, `_stage`), the object store command, and the two services that sequence host calls (`ComputerService`, `CheckpointService`). The checkpoint memory file is written to a tmpfs staging directory and moved to the durable directory by the upload task. No schema or route changes.

**Tech Stack:** Python 3.12 asyncio, Firecracker v1.14.2, dm-thin, asyncssh, rclone.

**Spec:** GitHub issues #143 to #151; the measurements they cite come from the live journal on 2026-09-11.

## Global Constraints

- The gate: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov` (coverage floor 98 %, zero warnings).
- `tests/unit/test_docs.py`: every path, module, route, metric and variable a listed document names must exist.
- No backwards compatibility: replace, do not version.
- Live E2E is the judge: `170 passed, 6 skipped, 4 failed` (the four `Not implemented` in #65).

## Decisions

| Issue | Decision | Why |
|---|---|---|
| #143 PAM | accept | 50 ms per new connection, 23 k samples; `UsePAM no` in `_post_process_rootfs` |
| #144 fsync | accept | Firecracker `sync_all()` on 256 MiB; snapshot on `/dev/shm`, move in the upload task |
| #145 uploads | accept nice/ionice + one at a time; reject zstd | zstd changes the R2 layout for a second-order gain |
| #146 udev | reject | on the live host `--noudevsync` and `--noudevrules` both leave `/dev/mapper/<name>` absent when `dmsetup` returns, and `remove --noudevsync` failed with EBUSY; Firecracker opens the node at once |
| #147 overlap | accept gather(warm, add_route); skip happy-path staging cleanup; keep `warm`; keep `activate`; **reject** overlapping the dm snap with the dump | the live run captured empty files: Firecracker's flush inside create_snapshot is what lands guest writes on the volume (drive cache Unsafe); lazy activation would hide checkpoint volumes from `blocks.max_volume_id` |
| #148 destroy | accept | pidfd wait; route removal alongside the kill; tap teardown alongside volume removal |
| #149 cold boot | accept boot args and masking the timers; keep `console=ttyS0` | the timers also fire inside restored guests when `date -s` moves the clock |
| #150 evict | accept, subject to the live run | a 300 ms pause does not break TCP; revert if `ConnectionLost` appears |
| #151 Caddy | reject | routing redesign with a resolver-cache failure mode; #147 hides the cost |

---

### Task 1: Guest image (#143, #149)

**Files:** Modify `src/mshkn/services/recipes.py` (`_post_process_rootfs`), `src/mshkn/host/firecracker.py` (`BOOT_ARGS`); Test `tests/unit/test_image_pipeline.py`, `tests/unit/test_firecracker_client.py`.

- [ ] Test: after `inject_tar`, `etc/ssh/sshd_config` contains `UsePAM no`, `etc/update-motd.d` is empty, and `etc/systemd/system/apt-daily.timer` is a symlink to `/dev/null` (one per masked unit).
- [ ] Implement: `UsePAM` handled like `PermitRootLogin`; `_MASKED_UNITS` tuple; remove `update-motd.d` entries.
- [ ] `BOOT_ARGS` gains `quiet loglevel=3 systemd.show_status=0 random.trust_cpu=on`; the client test pins the string.
- [ ] Gate, commit.

### Task 2: Staging cleanup only when dirty; warm alongside add_route (#147)

**Files:** Modify `src/mshkn/host/firecracker.py` (`_stage`, `_cleanup_staging`), `src/mshkn/services/computers.py` (`_bring_up`); Test `tests/unit/test_firecracker_stage.py`, `tests/unit/test_computer_service.py`.

- [ ] Test: two successful boots run `dmsetup remove mshkn-restore-staging` once; a failed boot then a boot runs it before the second boot's map.
- [ ] Implement `_staging_dirty` (True at construction and after `_cleanup_staging`).
- [ ] Test: `add_route` failure still abandons; the route is added even when `warm` is slow (gather).
- [ ] Gate, commit.

### Task 3: Checkpoint: dm snap alongside the dump, no evict (#147, #150)

**Files:** Modify `src/mshkn/services/checkpoints.py`; Test `tests/unit/test_checkpoint_service.py`, `tests/unit/test_self_destruct.py`, `tests/flow/test_lifecycle.py`.

- [ ] Tests updated: `evicted` no longer includes the checkpoint; the volume snap and the snapshot both happen.
- [ ] Implement `asyncio.gather(self.host.hypervisor.snapshot(...), self._snap_disk(computer, volume_name))`.
- [ ] Gate, commit.

### Task 4: Destroy (#148)

**Files:** Modify `src/mshkn/host/firecracker.py` (`kill_firecracker_process`), `src/mshkn/services/computers.py` (`_CleanupPass.steps`, `_teardown`, `_abandon`); Test `tests/unit/test_firecracker_process.py`, `tests/unit/test_computer_service.py`.

- [ ] Test: kill returns within 50 ms of the child exiting.
- [ ] Implement pidfd wait with a polling fallback.
- [ ] Test: cancellation mid-kill still runs the later phases and releases the slot last (existing test stays green).
- [ ] Implement `steps()` running a phase's steps concurrently; phases: (route, kill+evict), (volume, tap), then status, gauge, release.
- [ ] Gate, commit.

### Task 5: Snapshot on tmpfs, persist in the upload task (#144, #145)

**Files:** Modify `src/mshkn/config.py`, `src/mshkn/services/checkpoints.py`, `src/mshkn/services/computers.py` (`_snapshot_files_for`), `src/mshkn/host/r2.py`; Test `tests/unit/test_checkpoint_service.py`, `tests/unit/test_computer_service.py`, `tests/unit/test_r2.py`, `tests/unit/test_config.py`, `tests/flow/test_lifecycle.py`.

- [ ] `Config.checkpoint_staging_dir` default `/dev/shm/mshkn`.
- [ ] `create` snapshots into `staging/<id>`; the task under `upload:<id>` copies to `local/<id>` (atomic rename from `local/<id>.tmp`), uploads under a one-slot semaphore, then removes the staging copy after `_STAGING_LINGER_SECONDS`.
- [ ] `_snapshot_files_for`: local, then staging, then R2.
- [ ] `delete` removes both directories.
- [ ] `upload_dir` runs `nice -n 19 ionice -c 3 rclone copy …`.
- [ ] Gate, commit.

### Task 6: Docs and issue closure

- [ ] `docs/ARCHITECTURE.md`: create step 5, checkpoint create, destroy, staging pass, durable disk, config table.
- [ ] `docs/plans/README.md` row for this plan.
- [ ] Deploy, rebuild the base volume, run the live suite, record T1 numbers on the PR; close #143 to #151 with accept/reject notes.
