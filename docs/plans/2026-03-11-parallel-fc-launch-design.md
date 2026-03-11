# Parallel Firecracker Launch Design

**Date:** 2026-03-11

**Issue:** #37

**Goal:** Reduce create/fork latency by overlapping dm-thin snapshot creation with Firecracker process startup.

## Problem

`VMManager.create()` and `VMManager.fork_from_checkpoint()` currently do these steps in sequence:

1. create tap
2. create dm-thin snapshot
3. start Firecracker process
4. configure and boot
5. wait for SSH

The dm-thin snapshot and Firecracker process startup are independent until `configure_and_boot()` needs the rootfs path to exist. Running them sequentially leaves a small but free latency win on the table.

## Chosen approach

Add one small helper in `src/mshkn/vm/manager.py` that:

1. starts `create_snapshot(...)` in an `asyncio.Task`
2. starts Firecracker immediately
3. waits for the snapshot to finish before calling `configure_and_boot(...)`

Both `create()` and `fork_from_checkpoint()` will use this helper so the overlap and cleanup logic is shared.

## Error handling

- If Firecracker process startup fails, cancel and await the snapshot task, then re-raise.
- If snapshot creation fails after Firecracker has started, kill the Firecracker process, then re-raise.
- Existing ordering for tap creation, SSH readiness, DB writes, and Caddy route registration stays unchanged.

## Testing

Add a focused unit test which proves `start_firecracker_process()` is invoked before the snapshot task completes. This verifies the overlap directly without relying on noisy wall-clock latency assertions.

Run targeted tests first, then broader non-E2E verification.
