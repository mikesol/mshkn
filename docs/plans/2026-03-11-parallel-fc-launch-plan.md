# Parallel Firecracker Launch Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Overlap dm-thin snapshot creation with Firecracker process startup in VM create/fork paths.

**Architecture:** Add one helper in `VMManager` that owns the shared overlap behavior and cleanup. Update both `create()` and `fork_from_checkpoint()` to use it, then add a focused regression test proving the snapshot and process-start steps are actually overlapped.

**Tech Stack:** Python 3.12, asyncio, pytest, Firecracker, dm-thin

---

### Task 1: Add a failing regression test for overlapped startup

**Files:**
- Modify: `tests/test_vm_manager.py`
- Modify: `src/mshkn/vm/manager.py`

**Step 1: Write the failing test**

Add a unit test that:
- patches `create_snapshot()` to block on an `asyncio.Event`
- patches `start_firecracker_process()` to record that it was called
- invokes the shared startup helper or the smallest reachable manager path
- asserts Firecracker start is reached before the snapshot is released

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_vm_manager.py -k parallel -v`

Expected: FAIL because the helper does not yet exist or the call order is still sequential.

**Step 3: Commit**

```bash
git add tests/test_vm_manager.py
git commit -m "test: cover parallel snapshot and firecracker startup"
```

### Task 2: Implement shared overlap helper in VMManager

**Files:**
- Modify: `src/mshkn/vm/manager.py`

**Step 1: Write minimal implementation**

Add a helper which:
- creates the snapshot task
- starts Firecracker
- awaits the snapshot before returning the pid
- cancels the snapshot task if Firecracker startup fails
- kills Firecracker if the snapshot task fails after startup

Use the helper from both `create()` and `fork_from_checkpoint()`.

**Step 2: Run the targeted test**

Run: `.venv/bin/pytest tests/test_vm_manager.py -k parallel -v`

Expected: PASS

**Step 3: Commit**

```bash
git add src/mshkn/vm/manager.py tests/test_vm_manager.py
git commit -m "perf: overlap dm-thin snapshot with firecracker startup"
```

### Task 3: Verify no regressions in local non-E2E coverage

**Files:**
- Modify: none unless failures require targeted fixes

**Step 1: Run relevant local tests**

Run: `.venv/bin/pytest tests/test_vm_manager.py tests/test_checkpoint_parent.py -v`

Expected: PASS

**Step 2: Run broader local validation**

Run: `.venv/bin/pytest tests/ --ignore=tests/e2e --ignore=tests/integration -x`

Expected: PASS

### Task 4: Verify live latency path

**Files:**
- Modify: none unless live verification reveals a regression

**Step 1: Deploy branch**

Run the normal deploy workflow for the branch.

**Step 2: Run targeted live latency tests**

Run: `MSHKN_API_URL=http://135.181.6.215:8000 .venv/bin/pytest tests/e2e/test_phase1_latency.py -v --tb=short`

Expected: PASS with no latency regression and ideally measurable create/fork improvement.

**Step 3: Commit only if follow-up fixes were required**

```bash
git add <files>
git commit -m "fix: address verification regressions for parallel launch"
```
