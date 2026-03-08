# Disposable Cloud Computers for AI Agents

**Date:** 2026-03-07
**Status:** Design approved
**One-liner:** "Computers that fork."

## Overview

An agent asks for a computer with specific tools installed. It appears in under 2 seconds. The agent works, checkpoints its progress, and the computer is destroyed. Later, the agent (or another one) resumes from that checkpoint — or forks it to explore two approaches in parallel, then merges the results.

There are no persistent machines and no sleeping VMs. Just checkpoints on object storage and ephemeral compute that materializes on demand.

## The Competition

For the time being, we only know of one competitor - sprites.dev. The main difference is that they park and resume boxes, whereas we recreate them everytime with declarative capabilities (more on this later).

## Core Concepts

### The Model

There are no persistent machines. There are only **checkpoints** and **ephemeral compute**.

- A checkpoint is a (manifest, state) tuple stored on object storage.
- A computer is a disposable Firecracker microVM that materializes from a manifest or checkpoint.
- The checkpoint is the identity. The computer is a shell.

### Why This Model

- **Retry:** Agents fail constantly. `resume(last_good_checkpoint)` gives pristine state every time. No dirty machines.
- **Fork/merge:** Only coherent when checkpoints are the primitive, not machines.
- **Shareability:** Checkpoints are portable data. Share like Docker images but with full runtime state.
- **Debuggability:** Every session starts from a checkpoint and optionally produces one. Full DAG of state evolution.
- **$0 sleep:** No sleeping machines to pay for. Checkpoints are just bytes on S3.

### Wake Latency Tradeoff

Our model means resume is ~500ms-1s (hot, NVMe-cached) vs [Sprites](https://sprites.dev/)' ~100-300ms (suspended VM unpause). We accept this because:

1. The gap is hundreds of milliseconds, not seconds.
2. Suspend-on-host can be added as a transparent performance cache without changing the conceptual model — the orchestrator keeps recently-used VMs paused on the host as an optimization, invisible to the agent.
3. The advantages (retry, fork, merge, $0 sleep, host independence) justify the tax.

## Computer Lifecycle

```
create(manifest) -> computer(ssh) -> checkpoint() -> destroy
                                   -> fork(checkpoint_id) -> computer(ssh) -> ...
                                   -> merge(checkpoint_a, checkpoint_b) -> checkpoint_merged
resume(checkpoint_id, manifest?) -> computer(ssh) -> ...
```

### Primitives

| Operation | What happens | Target latency |
|---|---|---|
| `create(manifest)` | Fresh minimal Linux + capabilities materialized. Returns SSH handle + URL. | <= 2s |
| `exec(command)` | Run a command. Streaming stdout/stderr. | instant |
| `checkpoint()` | Snapshot (manifest, filesystem delta, memory) to S3. Returns checkpoint ID. | <= 1s |
| `fork(checkpoint_id)` | Resume a checkpoint as a new computer. Copy-on-write, O(1). | <= 2s |
| `merge(ckpt_a, ckpt_b)` | Produce a new checkpoint combining two forks' deltas. Conflicts reported. | seconds |
| `resume(checkpoint_id, manifest?)` | Resume from checkpoint, optionally with modified manifest. | <= 2s |
| `destroy()` | Kill the computer. No implicit checkpoint. | instant |

No implicit anything. The agent controls the lifecycle explicitly. Only automatic behaviors are:
- **Checkpoint retention:** keep last N, expire after X days, unless pinned.
- **VM leak prevention:** per-account concurrent VM limit (e.g., 10). New creates fail with clear error if limit reached. Idle timeout (configurable, e.g., 30 min with no exec) triggers auto-checkpoint and destroy, with warning. Billing is per-minute while a computer exists, so leaking VMs costs money — self-correcting incentive.
- **Rate limiting:** per-account API rate limits to prevent abuse.

## Storage Architecture

Three layers, all backed by object storage:

### Layer 1: Base Image

Single minimal Linux filesystem. Read-only. Pre-positioned on the host. Rarely changes.

### Layer 2: Capability Layers

Each capability is a Nix-built, immutable filesystem layer stored on object storage, cached on local NVMe. Composed via overlayfs on top of the base.

### Layer 3: User State

Copy-on-write layer on top. Everything the agent does (writes files, creates data) lands here. Only mutable layer.

### Runtime Filesystem Structure

```
/  (overlayfs)
|-- lower: base image (read-only)
|-- lower: /nix/store closures (read-only)
+-- upper: user layer (CoW, read-write)
```

### Checkpoint

A checkpoint = snapshot of layer 3 + pointer to which layers 1+2 were active (the manifest) + VM memory snapshot. Only the delta goes to S3.

### Fork

New CoW layer on top of the same checkpoint. O(1) — no data copying.

### Merge

Diff two forks' layer 3 deltas against common ancestor. File-level merge:
- Files modified in only one fork: take that version
- Files modified in both: conflict, report to agent
- Files created in both with same path: conflict
- File deleted in one fork, unmodified in other: delete
- File deleted in one fork, modified in other: conflict
- Everything else: union

Concurrent merges referencing the same parent checkpoint must be safe — merges produce new checkpoints and don't mutate the parent, so no locking is needed.

Merged checkpoint has filesystem state but no process state (can't merge memory snapshots). Resume from merge is a cold boot into merged filesystem.

### Overlay Depth Compaction

Overlayfs performance degrades with deeply stacked layers. A fork-of-a-fork-of-a-fork accumulates lower layers. The system must automatically flatten (compact) the overlay stack when depth exceeds a threshold — collapsing intermediate layers into a single read-only layer. This is transparent to the agent but essential for performance in deep fork chains.

## Capability System

### Nix as Backend

Capabilities are Nix derivations. The registry is nixpkgs plus custom overlays. Binary caches ensure pre-built artifacts. The agent never sees Nix.

### Capabilities Are Environments, Not Packages

The manifest doesn't list individual packages. It lists ecosystem-level declarations:

```
uses: [
  ffmpeg,
  python-3.12(numpy>=1.26, pandas, torch==2.1),
  node-22(next@14, react, tailwindcss)
]
```

`python-3.12(numpy, pandas, torch)` is a single capability — one immutable layer. The system resolves the dependency spec, builds deterministically, caches as content-addressed layer.

### Ecosystem Support (Launch)

| Ecosystem | Declarative | User state |
|---|---|---|
| Python | Runtime + pip deps (from requirements spec) | .py files, models, data |
| Node | Runtime + npm deps (from package.json/lockfile) | Source code, build output |
| Go | Runtime + modules (from go.sum) | Source code, binaries |
| Rust | Toolchain + crates (from Cargo.lock) | Source code, binaries |
| System tools | Binary + libraries | Config files, output |

### Enforced Purity

Package manager directories are read-only (immutable Nix store). `pip install` in the mutable layer fails. The system returns a structured error with the exact command to reconstitute the computer with the missing dependency:

```
computer_exec(id, "pip install requests")
-> {
    exit_code: 1,
    stderr: "Package installation not permitted in mutable layer.",
    suggested_action: {
      tool: "checkpoint_fork",
      args: {
        checkpoint_id: "auto-current",
        manifest: {uses: ["python-3.12(numpy, pandas, requests)", "ffmpeg", "node-22"]}
      }
    }
  }
```

Agent blindly executes the suggested action. Any model can do it.

### Registry

- Open — anyone can publish capability definitions
- We seed with common capabilities at launch
- Capabilities are immutable once published (version pinned)
- Escape hatch: `uses: [tarball("https://...")]` for anything not in the registry

### Manifest Compatibility on Resume

- Identical manifest: resume as-is
- Additive (new capabilities): add layers, fine
- Breaking (removed/changed capabilities): warn agent, require explicit force

## Compute Layer

Firecracker microVMs on bare metal Hetzner.

### Create Flow

1. Resolve capabilities -> Nix closures (cached on NVMe from binary cache)
2. Compose rootfs: base image + Nix store mount + empty CoW user layer
3. Boot Firecracker microVM (~125ms)
4. Return SSH handle + URL

### Resource Allocation

`needs:` field maps to Firecracker VM config. Defaults: 1 vCPU, 512MB RAM. Agent scales up if needed. GPU passthrough: future (Firecracker doesn't support it).

Resource limits are enforced by Firecracker — the VM cannot exceed its allocated RAM or CPU. Requests exceeding host capacity are rejected with a clear error showing available resources.

### Density

Hetzner AX42 (~EUR40/mo): 64GB RAM, 8 cores, 2x512GB NVMe. At default sizing (512MB/computer): ~100 concurrent computers.

### The 2s Target

Holds as long as capability layers are cache-warm on NVMe. Cold pulls from S3 bust the budget. Mitigation: pre-warm top ~100 capabilities on every worker.

## Networking

### Per-Computer HTTPS

Every computer gets a wildcard subdomain: `{computer-id}.{our-domain}`. Any port the computer listens on is automatically accessible at `{port}-{computer-id}.{our-domain}` on 443. No `expose()` call needed. Agent gets base URL on `create()`.

Implementation: Caddy with a pre-issued wildcard Let's Encrypt certificate + wildcard DNS record pointing to the box. The wildcard cert is issued once at setup, so per-computer TLS is instant — no cert issuance delay.

### URL Identity Across Resume

Computers are disposable, so resume creates a new computer with a new ID and URL. If an agent needs a stable URL (e.g., sharing a preview link), it can use a URL alias — a stable name that the orchestrator re-points on resume. This is a convenience feature, not a core primitive. For v1, agents should treat URLs as ephemeral.

### Inter-Computer Networking

Explicitly undesigned for launch. Computers are isolated. If two computers need to talk, they use each other's public HTTPS URLs.

### SSH-Like Access

Not literal SSH. The orchestrator API exposes exec/upload/download endpoints with SSH semantics. Streaming output on exec.

## Orchestrator

Single process on the Hetzner box alongside the Firecracker VMs.

### Responsibilities

- Accept API calls (create, exec, checkpoint, fork, merge, resume, destroy)
- Manage Firecracker VM lifecycle
- Manage overlayfs composition
- Talk to S3 for checkpoint storage
- Run reverse proxy config
- Handle checkpoint retention (expiry, cleanup)
- Authenticate agents (API keys per account)

### State

- S3: checkpoints, capability layers, account metadata
- Local SQLite: active VMs, cache index — made durable to S3 via Litestream

## Agent-Facing API (14 Tools)

```
computer_create(uses, needs?) -> {computer_id, url, manifest}
computer_exec(computer_id, command) -> stream {stdout, stderr} -> {exit_code}
computer_exec_bg(computer_id, command) -> {pid}
computer_exec_logs(computer_id, pid) -> stream {stdout, stderr}
computer_exec_kill(computer_id, pid) -> {ok}
computer_upload(computer_id, path, data) -> {ok}
computer_download(computer_id, path) -> {data}
computer_status(computer_id) -> {uptime_s, cpu_pct, ram_usage_mb, disk_usage_mb, processes: [{pid, command}]}
computer_checkpoint(computer_id, pin?, label?) -> {checkpoint_id, manifest}
computer_destroy(computer_id) -> {ok}

checkpoint_fork(checkpoint_id, manifest?) -> {computer_id, url, manifest}
checkpoint_merge(checkpoint_a, checkpoint_b) -> {checkpoint_id, conflicts}
checkpoint_resolve_conflicts(checkpoint_id, resolutions) -> {checkpoint_id}
checkpoint_list(filter?) -> [{checkpoint_id, manifest, timestamp, label, pinned, parent, size_bytes}]
checkpoint_delete(checkpoint_id) -> {ok}
```

Errors from package installation return structured `suggested_action` with a ready-to-invoke tool call.

## Observability

### Operator Side

Structured JSON logs from the orchestrator + a Prometheus metrics endpoint. Single Grafana instance for dashboards. All runs on the same box.

Key metrics:
- Host resources: CPU, RAM, NVMe usage, network bandwidth
- Latency distributions: p50/p95/p99 for create, checkpoint, fork, resume
- Active VM count and churn rate
- S3 write/read latency and error rate
- Litestream replication lag

Alerts (via webhook to Slack/email):
- NVMe > 80% full
- RAM > 90%
- S3 write failures
- Litestream replication lag > threshold
- p95 create latency > 2s

### Agent Side

`computer_status` (tool #14 above) lets agents inspect resource usage and running processes before making decisions. This replaces the need for agents to run `free -m` or `ps aux` — structured data instead of parsing shell output.

### Account/Billing Side

Not an agent tool — a separate API for the human account owner:

```
account_usage() -> {
  active_computers: 3,
  compute_minutes_this_month: 847,
  checkpoint_storage_gb: 12.4,
  estimated_cost: "$4.23"
}
```

### Built-in: The Checkpoint DAG

`checkpoint_list` already returns `{parent, timestamp, label}` for every checkpoint. This is a full lineage DAG — the complete history of how state evolved, where the agent forked, what it kept, what it discarded. Better observability into agent decision-making than most platforms offer, for free.

## Non-Negotiables (Benchmarked Against sprites.dev)

| Metric | Sprites | Us | How |
|---|---|---|---|
| Create time | 1-2s | <= 2s | Firecracker ~125ms + cached Nix layers |
| Checkpoint | "instant" | <= 1s | Upper layer -> S3 + memory snapshot |
| Restore | ~1s | <= 2s | Cached upper layer + Firecracker boot + memory restore |
| Fork | No | O(1) | CoW overlay on checkpoint |
| Merge | No | Yes | File-level delta merge with conflict resolution |
| Sleep cost | Low (auto-sleep) | $0 | No sleeping machines, checkpoints on S3 |
| Capabilities | apt-get | Declarative | Nix-backed, ecosystem-aware |
| Storage | 100GB | 100GB | Overlayfs upper layer |
| Time limits | None | None | VM runs until destroyed |
| HTTPS URLs | Anycast | Auto per-port | Wildcard subdomain + Caddy |
| Streaming exec | Unknown | Yes | SSH-like streaming output |
| Failure recovery | Dirty machine state | Pristine resume | Checkpoint-as-identity model |
| Purity | Mutable, accumulates cruft | Enforced immutable | Read-only Nix store + helpful errors |

## Where Sprites Beats Us (For Now)

- **Global presence** — multi-region anycast vs our single Hetzner box
- **Wake latency** — ~100-300ms unpause vs our ~500ms-2s resume
- **Brand/ecosystem** — Fly's existing customer base

**Our bet:** agents don't care about global latency (100ms extra is irrelevant for multi-second operations) and the fork/merge/purity advantages outweigh the wake latency gap.

## Economics

### Fixed Costs (Zero Users)

| Item | Cost |
|---|---|
| Hetzner AX42 (64GB RAM, 8 cores, 2x512GB NVMe) | ~EUR40/mo |
| Domain + wildcard DNS | ~$1/mo |
| S3/R2 (empty) | $0 |
| Nix binary cache on R2 | ~$1-2/mo |
| **Total** | **~$43/mo** |

Bootstrap: start on Hetzner CAX11 (~EUR4/mo, 4GB RAM, 2 cores) for first few clients.

### Per-Computer Costs

| Resource | Our cost | Charge |
|---|---|---|
| Compute | ~$0.005/hr per VM | $0.02-0.05/hr |
| Checkpoint storage (R2) | $0.015/GB/mo | $0.05/GB/mo |
| Bandwidth (R2 egress) | Free | Free |

### Unit Economics at Scale

| Scenario | Revenue | Cost | Margin |
|---|---|---|---|
| 10 users, 2 hrs/day, 5 ckpts each | ~$30-75/mo | ~$44/mo | Breakeven |
| 50 users, 2 hrs/day, 10 ckpts each | ~$150-375/mo | ~$50/mo | 65-85% |
| 100 users, 2 boxes | ~$300-750/mo | ~$90/mo | 70-88% |

### Pricing Model

- Compute: per-minute while computer exists
- Storage: per-GB/mo for checkpoints (only bytes written)
- Capabilities: free
- Free tier: X minutes + Y GB/month

## Explicit Scope Cuts (v1)

| Cut | Rationale |
|---|---|
| Multi-region | One box. Agents don't care about 100ms latency. |
| GPU support | Firecracker doesn't support passthrough. |
| Inter-computer networking | Use public URLs. |
| Web dashboard | API-only. Agents don't use dashboards. |
| Team/org features | Single API key per account. |
| Host autoscaling | Manual upgrade. Watch utilization. |
| Custom base images | Minimal Linux only. |
| Windows/macOS | Linux only. |

## The Stack

- Firecracker on Hetzner bare metal
- Nix for capability resolution + binary cache on R2
- Overlayfs (base + capability layers + CoW user layer)
- S3/R2 for checkpoint storage
- Litestream + SQLite for orchestrator state
- Caddy for TLS/reverse proxy
- Single orchestrator process
