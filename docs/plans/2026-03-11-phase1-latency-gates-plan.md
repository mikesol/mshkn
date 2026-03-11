# Phase 1 Latency Gates Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Tighten Phase 1 live latency thresholds and convert stable print-only latency checks into enforced `p95` benchmark tests.

**Architecture:** Keep all changes within `tests/e2e/test_phase1_latency.py`. Introduce named constants for sample sizes and threshold values, then convert the eligible tests to repeated measurements so their assertions are based on meaningful percentiles. Use fresh live-server measurements as the policy source of truth.

**Tech Stack:** Python 3.12, pytest, httpx, live E2E against Firecracker-backed `mshkn`

---

### Task 1: Add failing stricter assertions in the Phase 1 latency tests

**Files:**
- Modify: `tests/e2e/test_phase1_latency.py`

**Step 1: Add constants for sample sizes and tighter thresholds**

Define explicit constants near the top of the file for:
- bare create samples and threshold
- warm cache create samples and threshold
- empty checkpoint samples and threshold
- small-state checkpoint samples and threshold
- many-small-files checkpoint samples and threshold
- resume samples and threshold
- fork minimal samples and threshold

**Step 2: Convert eligible print-only tests to repeated measurements**

Change:
- `test_small_state_checkpoint`
- `test_many_small_files_checkpoint`
- `test_resume_latency`

from single-sample reporting to repeated runs with collected timings and a `p95` assertion.

**Step 3: Tighten the existing enforced tests**

Update:
- `test_bare_create_latency`
- `test_warm_cache_capability_create_latency`
- `test_empty_state_checkpoint`
- `test_fork_minimal_state`

to use the new sample counts and thresholds.

**Step 4: Run a focused local syntax check**

Run: `ruff check tests/e2e/test_phase1_latency.py`

Expected: PASS

### Task 2: Verify the changed tests locally at collection/import level

**Files:**
- Modify: none unless failures require a fix

**Step 1: Run the touched Phase 1 file in a narrow local mode if feasible**

Run: `/home/mikesol/Documents/GitHub/mshkn/.venv/bin/pytest tests/e2e/test_phase1_latency.py -q --collect-only`

Expected: PASS

**Step 2: Commit**

```bash
git add tests/e2e/test_phase1_latency.py
git commit -m "test: tighten phase1 latency benchmarks"
```

### Task 3: Verify the new gates on the live server

**Files:**
- Modify: none unless live failures require threshold or sample-count revision

**Step 1: Run the tightened benchmark subset against the live server**

Run the updated tests with:

```bash
MSHKN_API_URL=http://135.181.6.215:8000 \
MSHKN_API_KEY=mk-test-key-2026 \
/home/mikesol/Documents/GitHub/mshkn/.venv/bin/pytest \
tests/e2e/test_phase1_latency.py \
-vv -s
```

Focus on the changed tests:
- bare create
- warm cache capability create
- empty checkpoint
- small-state checkpoint
- many-small-files checkpoint
- resume latency
- fork minimal

**Step 2: Adjust only if evidence demands it**

If a gate fails:
- inspect whether this is a real regression, a flaky server-side fault, or an over-tight threshold
- only revise thresholds when the live evidence supports it

**Step 3: Commit any threshold corrections if needed**

```bash
git add tests/e2e/test_phase1_latency.py
git commit -m "test: calibrate tightened phase1 latency gates"
```
