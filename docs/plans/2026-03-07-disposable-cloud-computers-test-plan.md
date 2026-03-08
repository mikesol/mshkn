# Disposable Cloud Computers — Crusty Sea-Dog Test Plan

**Date:** 2026-03-07
**Author:** The Incredulous Sea-Dog
**Mood:** "I've seen seventeen 'revolutionary' infra products sink to the bottom of the ocean. Prove it."

---

## Philosophy

This test plan exists because every claim in the design doc is guilty until proven innocent. We test the happy path last. We test the ugly path first. We test the numbers with a stopwatch, not with vibes. If something says "<=2s" we're going to measure it 1,000 times and look at p99, not p50. If something says "O(1)" we're going to plot it against input size and see if the line is flat or if somebody's lying.

---

## Phase 0: "Does It Even Boot?" (Smoke)

The absolute bare minimum before we waste another minute.

### T0.1 — Cold Create, No Capabilities

```
computer_create(uses: [])
```

- Does it return a `computer_id` and `url`?
- Can we `computer_exec(id, "echo hello")` and get `hello` back?
- Can we `computer_destroy(id)` without error?
- **If this fails, stop. Go home. Nothing else matters.**

### T0.2 — Create With a Single Capability

```
computer_create(uses: [python-3.12()])
```

- `computer_exec(id, "python3 --version")` returns `3.12.x`?
- `computer_destroy(id)` clean?

### T0.3 — SSH-Like Exec Basics

- Does `computer_exec` stream stdout line by line, or does it buffer the whole thing?
- Run `for i in $(seq 1 10); do echo $i; sleep 0.1; done` — do we see lines arrive incrementally?
- Does stderr come through separately?
- Does a failing command return the correct non-zero `exit_code`?

---

## Phase 1: "Show Me the Stopwatch" (Latency Benchmarks)

Every latency claim in the design doc, measured properly. Not once — 100+ iterations, report min/p50/p95/p99/max.

### T1.1 — Create Latency (Target: <= 2s)

- **Warm cache:** `computer_create(uses: [python-3.12(numpy)])` x100, all layers pre-cached on NVMe.
- **Cold cache:** Evict layers from NVMe, repeat. How bad does it get? The doc says "Cold pulls from S3 bust the budget" — I want to see exactly how busted.
- **Bare minimum:** `computer_create(uses: [])` x100. How close to the 125ms Firecracker boot time do we actually get?
- **Kitchen sink:** `computer_create(uses: [python-3.12(numpy, pandas, torch==2.1), node-22(next@14, react, tailwindcss), ffmpeg])` x50. Does the multi-layer overlay composition blow the budget?

### T1.2 — Checkpoint Latency (Target: <= 1s)

- **Empty state:** Create computer, write nothing, checkpoint. Should be near-instant.
- **Small state:** Write 10MB of files, checkpoint. Measure.
- **Large state:** Write 1GB of files, checkpoint. Does it still hold <= 1s? I doubt it.
- **Lots of small files:** Write 100,000 1KB files, checkpoint. Metadata-heavy workloads kill naive snapshot.
- **After heavy mutation:** Create 1GB, delete 900MB, checkpoint. How smart is the delta? Does it ship 1GB or 100MB?

### T1.3 — Resume/Restore Latency (Target: <= 2s)

- Resume from a tiny checkpoint. Measure.
- Resume from a 1GB checkpoint. Measure.
- Resume from a checkpoint taken 5 minutes ago (still hot on NVMe?). Measure.
- Resume from a checkpoint taken 24 hours ago (evicted from NVMe?). Measure.
- **Critical:** After resume, is the process state actually restored? Run a long-running process, checkpoint mid-execution, resume — does the process continue?

### T1.4 — Fork Latency (Target: <= 2s, "O(1)")

- Fork a checkpoint with 10MB user state. Measure.
- Fork a checkpoint with 10GB user state. Measure.
- **If fork is truly O(1), these two numbers must be statistically indistinguishable.** If 10GB fork is slower, the "O(1)" claim is a lie and the CoW isn't working.
- Fork the same checkpoint 50 times concurrently. Do they all come up?

### T1.5 — Merge Latency (Target: "seconds")

- Merge two forks that touched zero overlapping files. Should be fast.
- Merge two forks that touched 1,000 non-overlapping files each. Measure.
- Merge two forks where one touched 1 file and the other touched 10,000. Measure.
- "Seconds" is vague. Is it 2 seconds or 30? Pin it down.

---

## Phase 2: "Break the Fork/Merge" (Correctness)

Fork and merge are the flagship features. If they're broken, this product is Sprites with extra steps.

### T2.1 — Fork Isolation

- Create computer, write `/tmp/original.txt` with content "A".
- Checkpoint -> `ckpt_base`.
- Fork `ckpt_base` -> `computer_a`. Write `/tmp/original.txt` with content "B".
- Fork `ckpt_base` -> `computer_b`. Read `/tmp/original.txt` — must still say "A".
- **Forks must not bleed into each other.** If computer_b sees "B", the CoW is broken.

### T2.2 — Merge: Non-Overlapping Files

- Fork from common checkpoint.
- Fork A creates `/data/a.txt`.
- Fork B creates `/data/b.txt`.
- Merge -> new checkpoint must contain both files. No conflicts.

### T2.3 — Merge: Same File Modified in Both Forks

- Fork A writes "hello from A" to `/data/shared.txt`.
- Fork B writes "hello from B" to `/data/shared.txt`.
- Merge -> must report a conflict on `/data/shared.txt`.
- Conflict must be resolvable via `checkpoint_resolve_conflicts`.
- After resolution, resumed computer must have the chosen content.

### T2.4 — Merge: File Created in Both Forks at Same Path

- Fork A creates `/data/new.txt` with content X.
- Fork B creates `/data/new.txt` with content Y.
- Merge -> conflict. Same as above.

### T2.5 — Merge: Deletion in One Fork

- Base checkpoint has `/data/file.txt`.
- Fork A deletes `/data/file.txt`.
- Fork B does not touch it.
- Merge -> file should be deleted (one-sided change).

### T2.6 — Merge: Deletion vs Modification

- Base has `/data/file.txt` = "original".
- Fork A deletes `/data/file.txt`.
- Fork B modifies `/data/file.txt` = "modified".
- Merge -> conflict. Must be surfaced, not silently resolved.

### T2.7 — Merge Has No Process State

- The doc says "Merged checkpoint has filesystem state but no process state."
- Merge two checkpoints that both had running processes. Resume from merged checkpoint.
- No zombie processes. Clean boot. Filesystem is correct.

### T2.8 — Deep Fork Chains

- Checkpoint -> Fork -> Checkpoint -> Fork -> Checkpoint -> Fork (5 levels deep).
- Resume from the deepest checkpoint. Is state correct?
- Does latency degrade with fork depth? (Stacked overlays concern.)
- At what depth does automatic overlay compaction trigger?
- After compaction, is state identical? Is performance restored?

### T2.9 — Diamond Merge

- Checkpoint A -> Fork B, Fork C -> both checkpoint -> Merge B+C -> Fork D, Fork E -> Merge D+E.
- Full DAG. Does the system track lineage correctly?
- Does the merge of merges produce correct state?

### T2.10 — Concurrent Merges on Shared Parent

- Checkpoint P -> Fork A, Fork B, Fork C.
- Merge(A, B) and Merge(A, C) at the same time.
- Both merges produce valid checkpoints? No corruption? No lock contention?
- Parent checkpoint P is unmodified after both merges?

---

## Phase 3: "Purity or Theater?" (Capability System)

### T3.1 — pip install Is Blocked

```
computer_create(uses: [python-3.12(numpy)])
computer_exec(id, "pip install requests")
```

- Must fail with exit_code 1.
- Must return structured `suggested_action` with the correct manifest.
- The suggested manifest must include ALL existing capabilities plus the new one.
- **Then actually execute the suggested action** — does `checkpoint_fork` with the new manifest work?
- After fork, `import requests` works? `import numpy` still works?

### T3.2 — npm install Is Blocked

Same as above but for Node.

```
computer_create(uses: [node-22(react)])
computer_exec(id, "npm install express")
```

- Blocked? Suggested action correct? Fork works?

### T3.3 — apt-get / apk Is Blocked

- Try installing system packages. Must be blocked with helpful error.

### T3.4 — Writing to /nix/store Fails

```
computer_exec(id, "touch /nix/store/evil")
```

- Must fail. Read-only filesystem.

### T3.5 — Capability Layer Caching

- Create computer with `python-3.12(numpy)`. Destroy.
- Create another with `python-3.12(numpy)`. Should reuse cached layer.
- Measure: second create should be faster (no Nix build, no S3 pull).

### T3.6 — Capability Composition

```
computer_create(uses: [
  python-3.12(numpy>=1.26, pandas, torch==2.1),
  node-22(next@14, react, tailwindcss),
  ffmpeg
])
```

- All three ecosystems work simultaneously?
- `python3 -c "import numpy; import pandas; import torch"`
- `node -e "require('react'); require('next')"`
- `ffmpeg -version`
- No conflicts between Nix closures?

### T3.7 — Version Pinning

- `python-3.12(numpy==1.26.0)` — does it install exactly 1.26.0, not 1.26.4?
- Create, destroy, create again — same version?
- Create on a different day — same version? (Nix determinism claim.)

### T3.8 — Tarball Escape Hatch

```
computer_create(uses: [tarball("https://example.com/tool.tar.gz")])
```

- Does it actually work?
- Is the tarball cached after first pull?
- What happens if the URL is unreachable?

### T3.9 — Manifest Compatibility on Resume

- Create with `uses: [python-3.12(numpy)]`. Checkpoint.
- Resume with `uses: [python-3.12(numpy, pandas)]` (additive). Should work.
- Resume with `uses: [node-22()]` (breaking — removed Python). Must warn/require force.
- Resume with identical manifest. Must be seamless.

---

## Phase 4: "Networking or It's a Toy" (HTTPS/Connectivity)

### T4.1 — Auto-HTTPS Works

- Create computer. `computer_exec(id, "python3 -m http.server 8080")` in background.
- Hit `https://8080-{computer-id}.{domain}` from the outside. Does it respond?
- Is the TLS certificate valid? (Not self-signed, not expired.)
- TLS should be instant — wildcard cert is pre-issued at setup. If there's any per-computer cert delay, something is wrong.

### T4.2 — Multiple Ports

- Start servers on port 3000, 5000, 8080 simultaneously.
- All three URLs work? No interference?

### T4.3 — WebSocket Support

- Start a WebSocket server. Connect from outside. Does upgrade work through Caddy?

### T4.4 — URL Changes on Checkpoint/Resume

- Start server, checkpoint, destroy, resume.
- Computer gets a new ID and URL (computers are disposable — this is expected).
- Old URL must be dead immediately. No dangling proxy routes.
- If URL alias feature exists: alias re-points to new computer? Verify.

### T4.5 — Isolation

- Computer A cannot reach Computer B's internal network.
- Only public HTTPS URLs work for inter-computer communication.

---

## Phase 5: "Death by a Thousand Agents" (Concurrency & Density)

### T5.1 — 100 Concurrent Computers

- The doc claims ~100 concurrent computers on AX42 (64GB RAM, 512MB each).
- Spin up 100 `computer_create(uses: [])` concurrently.
- Do all 100 come up? In what time?
- `computer_exec` on all 100 — do they all respond?
- What's the 101st do? Error? Queue? OOM-kill a random victim?

### T5.2 — Concurrent Checkpoints

- 50 computers all checkpoint at the same time.
- S3 bandwidth bottleneck? NVMe write contention?
- Do all 50 succeed within the 1s target?

### T5.3 — Concurrent Creates and Destroys

- While 50 computers are running, create 20 more and destroy 10. Churn.
- Orchestrator keeps state consistent? SQLite doesn't lock up?

### T5.4 — Memory Pressure

- Create 100 computers at 512MB default. That's 50GB of the 64GB budget.
- The orchestrator + host OS + Caddy need RAM too.
- Does the system OOM? Does it degrade gracefully?

### T5.5 — NVMe Pressure

- 100 computers each write 1GB to their user layer. That's 100GB on NVMe.
- 2x512GB NVMe, but the capability cache also lives there.
- Does the system handle NVMe exhaustion? Error messages? Eviction?

### T5.6 — Resource Allocation Enforcement

- `computer_create(uses: [], needs: {ram: "8GB", cores: 4})` — does the VM actually get 8GB and 4 cores?
- Run `free -m` inside the VM — reports ~8GB?
- Run a CPU-bound workload — uses 4 cores, not more?
- `computer_create(uses: [], needs: {ram: "128GB"})` — exceeds host capacity. Clear error?
- Can a VM allocate more memory than its `needs` spec? (Must not — Firecracker enforces the limit.)

### T5.7 — Per-Account VM Limit

- Create 10 computers (assuming limit is 10). 11th must fail with clear error.
- Destroy one. 11th create now succeeds?
- Different API key can still create computers while the first account is at limit.

### T5.8 — Idle Timeout

- Create computer. Don't exec anything for the idle timeout period (e.g., 30 min).
- System auto-checkpoints and destroys? Verify checkpoint exists.
- Agent can resume from the auto-checkpoint?

### T5.9 — API Rate Limiting

- Hammer `computer_create` 1,000 times in 1 second from one API key.
- Rate limiter kicks in? Returns 429? Doesn't crash the orchestrator?
- Different API key is unaffected by the first key's rate limit?

---

## Phase 6: "The Long Night" (Durability & Recovery)

### T6.1 — Orchestrator Crash Recovery

- Create 10 computers. Kill the orchestrator process (SIGKILL).
- Restart orchestrator.
- Are the 10 computers still running? Can we exec on them?
- Is the SQLite state consistent (Litestream)?

### T6.2 — Host Reboot

- Create 10 computers with checkpoints. Reboot the host machine.
- After reboot, can we resume from the checkpoints?
- The computers themselves should be gone (ephemeral). Only checkpoints survive.

### T6.3 — S3 Unavailable During Checkpoint

- Create computer, write data. Block S3 access (iptables or similar).
- Try to checkpoint. Must fail with clear error, not hang forever.
- Unblock S3. Retry checkpoint. Must succeed.
- Original computer must still be usable during S3 outage.

### T6.4 — Checkpoint Retention

- Create 20 checkpoints. Set retention to "keep last 5".
- Are the oldest 15 eventually cleaned up?
- Pin checkpoint #3. Is it retained even though it's old?
- Unpinned checkpoints expire after X days? Verify.

### T6.5 — Litestream Replication

- Kill host. Spin up a new host from scratch.
- Restore SQLite from Litestream S3 backup.
- Resume from S3 checkpoints. Does the system come back?

### T6.6 — Stale VM Cleanup

- Create a computer but never destroy it. Walk away.
- Does the system ever clean it up? (The doc says "VM runs until destroyed" — so no auto-cleanup?)
- If truly no auto-cleanup: what happens when someone creates 1,000 computers and forgets?

---

## Phase 7: "Every Endpoint, Every Edge Case" (API Completeness)

### T7.1 — All 15 Tools Exercised

Walk through every single API endpoint:

| # | Endpoint | Test |
|---|---|---|
| 1 | `computer_create` | Covered above |
| 2 | `computer_exec` | Covered above |
| 3 | `computer_exec_bg` | Run a background process. Get PID back. |
| 4 | `computer_exec_logs` | Stream logs from the background PID. |
| 5 | `computer_exec_kill` | Kill the background PID. Confirm it's dead. |
| 6 | `computer_upload` | Upload a 1KB file. Download it. Content matches? |
| 7 | `computer_download` | Download a file created via exec. Content correct? |
| 8 | `computer_checkpoint` | Covered above |
| 9 | `computer_destroy` | Covered above |
| 10 | `checkpoint_fork` | Covered above |
| 11 | `checkpoint_merge` | Covered above |
| 12 | `checkpoint_resolve_conflicts` | Covered in merge tests |
| 13 | `checkpoint_list` | Create 5 checkpoints with labels. List them. Filters work? |
| 14 | `computer_status` | Create computer, run work, call status. Returns structured `{uptime_s, cpu_pct, ram_usage_mb, disk_usage_mb, processes}`? |
| 15 | `checkpoint_delete` | Delete a checkpoint. Confirm it's gone from list and S3. |

### T7.2 — Upload/Download Stress

- Upload a 100MB file. Download it. SHA256 matches?
- Upload a binary file (not text). Round-trip intact?
- Upload to a path that doesn't exist yet (`/deep/nested/dir/file.bin`). Auto-create dirs?
- Download a file that doesn't exist. Clean error?

### T7.3 — Background Process Lifecycle

- `computer_exec_bg(id, "sleep 3600")` -> get PID.
- `computer_exec_logs(id, pid)` -> should show nothing (sleep is silent).
- `computer_exec_bg(id, "for i in $(seq 1 100); do echo $i; sleep 0.01; done")` -> stream logs, verify all 100 lines.
- `computer_exec_kill(id, pid)` -> kill it. Exec_logs should end.
- Kill a PID that doesn't exist -> clean error.

### T7.4 — Double Destroy

- `computer_destroy(id)` twice. Second call: error? Idempotent OK? Not a crash.

### T7.5 — Exec on Destroyed Computer

- Destroy computer. Then `computer_exec(id, "echo hi")`.
- Must return clear error, not hang or crash.

### T7.6 — Checkpoint a Destroyed Computer

- Destroy first, then checkpoint. Clear error.

### T7.7 — Fork a Nonexistent Checkpoint

- `checkpoint_fork("bogus-id")` — clear error.

### T7.8 — Merge a Checkpoint With Itself

- `checkpoint_merge(ckpt_a, ckpt_a)` — should either no-op or return the same checkpoint. Not crash.

---

## Phase 8: "Security, Because I Don't Trust You" (Isolation & Auth)

### T8.1 — VM Escape

- From inside a computer, try to access the host filesystem.
- `mount`, `dmesg`, `/proc/1/cmdline` — all must be contained by Firecracker.

### T8.2 — Cross-Tenant Isolation

- Create computer with API key A. Create computer with API key B.
- Key A cannot exec on B's computer. Cannot list B's checkpoints. Cannot fork B's checkpoints.

### T8.3 — Invalid API Key

- All endpoints reject requests with invalid/missing API keys. 401, not 500.

### T8.4 — Resource Limits Inside VM

- `:(){ :|:&};:` (fork bomb) — does the VM contain it? Does it kill the host?
- `dd if=/dev/zero of=/fill bs=1M` — does the 100GB storage limit hold?
- `stress --vm 1 --vm-bytes 2G` — does the VM stay within its RAM allocation?

### T8.5 — Network Egress from VM

- Can the VM reach the public internet? (Needed for many agent tasks.)
- Can the VM reach the host's orchestrator port directly? (Must not — only through the API.)
- Can the VM reach other VMs' internal IPs? (Must not.)

### T8.6 — Checkpoint Data Isolation on S3

- Checkpoints from tenant A are not accessible by downloading raw S3 URLs from tenant B.
- S3 paths include tenant scoping.

---

## Phase 9: "The Economics Are a Fantasy" (Cost Validation)

### T9.1 — Measure Actual S3 Costs

- Run 10 computers for 2 hours, 5 checkpoints each. What's the actual R2 bill?
- Compare to the "$0.015/GB/mo" claim.

### T9.2 — Measure NVMe Wear

- 1,000 checkpoints/day for a week. What's the NVMe write endurance consumption?
- NVMe dies in 6 months? 2 years? This matters for the "$43/mo" claim.

### T9.3 — Verify the "$0 Sleep" Claim

- Create a checkpoint. Wait 30 days. No compute charges accrued?
- Only S3 storage charges? Verify.

---

## Phase 10: "The Agent Doesn't Care About Your Feelings" (Integration)

### T10.1 — Real Agent Workflow: Web App Development

Simulate an actual agent building a Next.js app:

1. `computer_create(uses: [node-22(next@14, react, tailwindcss)])`
2. Upload a project scaffold.
3. `computer_exec(id, "cd /app && npm run dev")` in background.
4. Hit the HTTPS URL — does the dev server respond?
5. Make changes, verify hot reload works via the public URL.
6. Checkpoint. Destroy. Resume. Dev server starts again from checkpoint?

### T10.2 — Real Agent Workflow: Data Science

1. `computer_create(uses: [python-3.12(numpy, pandas, scikit-learn)])`
2. Upload a CSV dataset (50MB).
3. Run a training script. Get results.
4. Agent realizes it needs `matplotlib`. Gets structured error.
5. Agent forks with new manifest including `matplotlib`.
6. Continues from checkpoint with `matplotlib` available. Previous work intact.

### T10.3 — Real Agent Workflow: Parallel Exploration

1. Create computer, set up base state, checkpoint.
2. Fork 3 times — try 3 different approaches.
3. Each fork does different work.
4. Agent picks the best fork's checkpoint.
5. Discards the other two. Checkpoints cleaned up.

### T10.4 — Real Agent Workflow: Failure Recovery

1. Create computer, do work, checkpoint.
2. Agent runs a command that corrupts state (`rm -rf /app/node_modules` or similar).
3. Agent resumes from last good checkpoint. Clean state, no corruption.
4. Repeat — this is the core "retry" value prop.

### T10.5 — Dumb Agent Test

- Give the 15 tools to the cheapest, dumbest LLM available (Haiku or equivalent).
- Can it successfully: create a computer, run a command, checkpoint, fork, destroy?
- The structured `suggested_action` on `pip install` — can a dumb model parse and execute it?
- **If this only works with Opus, the "any model can do it" claim is false.**

---

## Phase 11: "Flying Blind Is Not a Feature" (Observability)

The design doc now promises structured logging, Prometheus metrics, Grafana dashboards, alerting, an agent-facing `computer_status` tool, an `account_usage` billing API, and the checkpoint DAG as implicit observability. I've been burned by "we'll add monitoring later" exactly as many times as I've heard the phrase. So we test all of it.

### T11.1 — Prometheus Endpoint Exists and Is Scraped

- Hit the orchestrator's `/metrics` endpoint (or whatever it exposes). Does it return valid Prometheus format?
- Are these metrics present and non-zero after running a basic workflow:
  - `create_latency_seconds` (histogram with p50/p95/p99)
  - `checkpoint_latency_seconds`
  - `fork_latency_seconds`
  - `resume_latency_seconds`
  - `active_vm_count` (gauge)
  - `host_cpu_usage`, `host_ram_usage`, `host_nvme_usage`
  - `s3_write_latency_seconds`, `s3_read_latency_seconds`, `s3_errors_total`
  - `litestream_replication_lag_seconds`
- Scrape every 15s for 5 minutes during active use. Are the values updating? Not stale?

### T11.2 — Latency Histograms Match Reality

- Run 50 `computer_create` calls. Measure client-side p95.
- Compare to the `create_latency_seconds` p95 from Prometheus.
- **If they disagree by more than 10%, the metrics are lying.** Could be measuring the wrong thing (e.g., excluding queue time, or measuring only the Firecracker boot portion).
- Repeat for checkpoint, fork, and resume.

### T11.3 — Structured JSON Logs

- Tail the orchestrator logs during a full lifecycle (create -> exec -> checkpoint -> fork -> merge -> destroy).
- Every log line is valid JSON? (Not mixed with unstructured garbage from Firecracker or Caddy.)
- Each log entry includes: timestamp, level, operation, computer_id/checkpoint_id, duration, error (if any)?
- Can we reconstruct the full timeline of a single computer's life from the logs alone?

### T11.4 — Alerts Fire When They Should

- **NVMe > 80%:** Fill the NVMe to 80%. Does the alert fire within 1 minute?
- **RAM > 90%:** Spin up enough VMs to push host RAM to 90%. Alert fires?
- **S3 write failure:** Block S3 with iptables. Attempt a checkpoint. Alert fires?
- **Litestream lag:** Kill Litestream or block its S3 access. Does the lag alert fire?
- **p95 create > 2s:** Evict capability caches to force cold pulls. p95 blows past 2s. Alert fires?
- **False positives:** Run a healthy system for 1 hour. Zero alerts? (If the system cries wolf at idle, the alerts are useless.)

### T11.5 — `computer_status` Returns Accurate Data

- Create computer with `needs: {ram: "2GB", cores: 2}`.
- `computer_status(id)` -> `ram_usage_mb` is a sane number (not 0, not more than 2048)?
- Run `stress --cpu 2` in background. `computer_status(id)` -> `cpu_pct` climbs toward ~200% (2 cores)?
- Write 500MB of files. `computer_status(id)` -> `disk_usage_mb` reflects ~500?
- Run 3 background processes. `computer_status(id)` -> `processes` array has 3+ entries with correct PIDs and commands?
- Destroy computer. `computer_status(id)` -> clear error, not stale data.

### T11.6 — `computer_status` vs `exec("free -m")` Consistency

- Call `computer_status(id)` and `computer_exec(id, "free -m")` simultaneously.
- Do the RAM numbers roughly agree? (Within ~10% — the structured tool should match reality.)
- Same for disk: `computer_status` disk_usage_mb vs `exec("df -BM /")`.
- **If they disagree significantly, the status tool is decorative, not functional.**

### T11.7 — `account_usage` API

- This is a separate human-facing API, not an agent tool. Hit it with account credentials.
- `active_computers` — matches the number we actually created?
- `compute_minutes_this_month` — create a computer, run it for 5 minutes, destroy. Does the count increase by ~5?
- `checkpoint_storage_gb` — create a checkpoint with known size. Does it show up?
- `estimated_cost` — is it a plausible number based on the pricing model? (Not $0, not $10,000.)
- Call with invalid credentials. 401, not 500.

### T11.8 — Checkpoint DAG as Observability

- Create -> checkpoint A -> fork -> checkpoint B -> fork -> checkpoint C.
- `checkpoint_list()` returns all three with correct `parent` pointers: C -> B -> A -> null.
- Fork from A twice: checkpoint D and E. Both show parent = A.
- Merge D + E -> checkpoint F. F's parent(s) — does the data model support two parents? Or is it stored differently?
- **The DAG must be fully reconstructible from `checkpoint_list` output alone.** If any parent pointer is missing or wrong, the "full lineage DAG" claim is marketing.

### T11.9 — Grafana Dashboards Load

- Open Grafana. Do the dashboards actually render?
- Are the panels connected to real data sources, or are they placeholder "no data" panels?
- During a load test (Phase 5), do the dashboards show the spike in real time?
- This sounds trivial, but I've seen more Grafana dashboards that show "No Data" than ones that work.

### T11.10 — Observability Under Failure

- Kill a computer with SIGKILL from the host (not via API — simulate a crash).
- Does the orchestrator log an abnormal termination?
- Does the active VM count metric decrement?
- Does `computer_status` for that ID return an error (not stale data from before the crash)?
- Is there any alert for "unexpected VM termination"?

---

## Pass/Fail Criteria

| Category | Pass | Fail |
|---|---|---|
| Smoke (Phase 0) | All tests pass | Any failure = showstopper |
| Latency (Phase 1) | p95 within stated targets | p95 exceeds targets by >50% |
| Fork/Merge (Phase 2) | All correctness tests pass | Any data corruption or silent merge error |
| Purity (Phase 3) | All blocked operations return structured errors | Any silent mutation of immutable layers |
| Networking (Phase 4) | HTTPS instant via wildcard cert, no dangling routes | Broken TLS, dangling DNS, or unreachable URLs |
| Concurrency (Phase 5) | 80+ of 100 computers stable, resource limits enforced, rate limiting works | <80 or OOM crashes, resource escape, or no rate limiting |
| Durability (Phase 6) | Checkpoints survive host reboot | Data loss on any failure scenario |
| API (Phase 7) | All 15 endpoints work, edge cases return errors not crashes | Any crash or hang on bad input |
| Security (Phase 8) | Full VM isolation, tenant separation | Any escape, cross-tenant access, or host access |
| Economics (Phase 9) | Actual costs within 2x of projections | Costs >3x projections |
| Integration (Phase 10) | Real agent workflows complete end-to-end | Any workflow that can't complete |
| Observability (Phase 11) | Metrics accurate within 10% of reality, alerts fire within 1 min, status tool matches shell output, DAG fully reconstructible | Metrics lie, alerts don't fire, status is decorative, or DAG has broken parent pointers |

---

## Final Note

If this thing actually passes all of the above, I'll eat my mass-produced pirate bandana from Amazon. But I've been wrong before — once, in '09 — and I suppose it could happen again.
