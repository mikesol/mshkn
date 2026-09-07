# Plans index

Every design and implementation document in this repository, with its status as of 2026-09-07 (main after PR #64). Status is judged from the code, not from what the document says about itself. The documents are historical records and are not edited; this index is what changes.

Statuses: **implemented**, **partially implemented**, **not implemented** (in the test plan, no code, tracked in an issue), **in progress** (the plan being executed), **superseded by …**, **retired** (never built, not replaced), **reference** (not a plan).

## Product

| Document | What it proposed | Status | Evidence |
|---|---|---|---|
| `docs/plans/2026-03-07-disposable-cloud-computers-design.md` | The product: disposable Firecracker VMs identified by checkpoints, fork and merge, Nix capability layers, sleep-for-free economics. | partially implemented: the computer, checkpoint, fork and merge model is live; the Nix capability layer was replaced by Docker recipes | `src/mshkn/services/computers.py`, `src/mshkn/services/checkpoints.py`, `src/mshkn/services/recipes.py` |
| `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` | The definition of done: 157 end-to-end tests, T0 to T13. | reference; the suite is `tests/e2e/`. Seven tests (T8.6, T9.1, T10.1, T10.2, T10.5, T11.2 and the audit-log check) fail as `Not implemented` until built (#65). | `tests/e2e/` collects 157 |
| `docs/plans/2026-03-08-roadmap.md` | Prioritised backlog from the first E2E run. | partially implemented: see the breakdown below | |
| `docs/plans/2026-03-08-orchestrator-design.md` | One FastAPI process over Firecracker, dm-thin, R2, Nix and SQLite. | partially implemented: everything but Nix; the module layout became `host/`, `services/`, `db/` in the quality overhaul | `src/mshkn/app.py`, `src/mshkn/host/`, `src/mshkn/services/` |
| `docs/plans/2026-03-08-orchestrator-implementation.md` | Task plan for the orchestrator. | implemented, later restructured by PRs 2 to 4 of the quality overhaul | `src/mshkn/runtime.py` |
| `docs/plans/2026-03-09-nix-capability-system-design.md` | Two-level Nix capability cache. | superseded by `docs/plans/2026-03-13-recipe-system-design.md` | no code references the `capability_cache` table; it remains in the schema, unused (vestigial schema is not rebuilt, spec §15) |
| `docs/plans/2026-03-09-nix-capability-implementation.md` | Task plan for the Nix cache. | superseded by `docs/plans/2026-03-13-recipe-system-implementation.md` | no `capability` package exists |
| `docs/plans/2026-03-10-codex-agent-integration.md` | Drive computers from a Codex subscription. | retired: never built, nothing references it | |
| `docs/plans/2026-03-11-parallel-fc-launch-design.md` | Overlap Firecracker start with disk and tap setup. | implemented | `_stage` in `src/mshkn/host/firecracker.py` gathers the disk map and tap while the process starts |
| `docs/plans/2026-03-11-parallel-fc-launch-plan.md` | Task plan for the above. | implemented | same |
| `docs/plans/2026-03-11-phase1-latency-gates-design.md` | Turn print-only latency checks into p95 assertions. | implemented | `tests/e2e/test_phase1_latency.py` |
| `docs/plans/2026-03-11-phase1-latency-gates-plan.md` | Task plan for the above. | implemented | same |
| `docs/plans/2026-03-12-snapshot-restore-design.md` | Restore snapshots through a staging slot; L3 template cache. | implemented | `restore` and `build_template` in `src/mshkn/host/firecracker.py`; `src/mshkn/db/templates.py` |
| `docs/plans/2026-03-12-snapshot-restore-plan.md` | Task plan for the above. | implemented | same |
| `docs/plans/2026-03-13-recipe-system-design.md` | Replace Nix with Dockerfile recipes. | implemented | `src/mshkn/services/recipes.py`, `Dockerfile.mshkn-base` |
| `docs/plans/2026-03-13-recipe-system-implementation.md` | Task plan for recipes; remove `capability_cache` and manifests from the API. | implemented | `src/mshkn/api/schemas.py` has `recipe_id`, no manifest |
| `docs/plans/2026-03-13-telegram-agent-design.md` | A Telegram back-office agent on mshkn computers. | retired: the bridge was moved out of the repo (Claude remote control replaced it) | issue #34 |
| `docs/superpowers/plans/2026-03-12-ingress-mapping.md` | Webhook ingress with Starlark transforms. | implemented | `src/mshkn/services/ingress.py`, `src/mshkn/services/starlark.py` |

## Quality overhaul (2026-09)

Spec: `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md`. Six PRs; each plan has a `-baseline.txt` beside it recording the test count and coverage before the PR.

| Plan | PR | Status |
|---|---|---|
| `docs/superpowers/plans/2026-09-04-pr1-tooling-ci-hygiene.md` | #60 | implemented (merged 2026-09-04) |
| `docs/superpowers/plans/2026-09-04-pr2-foundations.md` | #61 | implemented (merged 2026-09-05) |
| `docs/superpowers/plans/2026-09-05-pr3-host-boundary.md` | #62 | implemented (merged 2026-09-06) |
| `docs/superpowers/plans/2026-09-06-pr4-services.md` | #63 | implemented (merged 2026-09-06) |
| `docs/superpowers/plans/2026-09-06-pr5-tests.md` | #64 | implemented (merged 2026-09-07); made seven stub E2E tests fail honestly (#65) |
| `docs/superpowers/plans/2026-09-07-pr6-docs-devtools.md` | this PR | in progress (this PR) |

## Roadmap breakdown (`docs/plans/2026-03-08-roadmap.md`)

| Item | Status |
|---|---|
| P1 destroy ownership check, startup recovery, allocation locking | implemented: `ComputerService.get_owned`, `Runtime.start`, `SlotAllocator` |
| P2 Caddy dynamic routing | implemented: `src/mshkn/host/caddy.py` |
| P3 Nix capability system | superseded by `docs/plans/2026-03-13-recipe-system-design.md` |
| P4 merge end to end | implemented: `POST /checkpoints/{parent_id}/merge` |
| P5 VM limits, rate limiting, idle timeout, retention, `needs`, stale cleanup | implemented: `ComputerService.create`, `src/mshkn/ratelimit.py`, `src/mshkn/services/reaper.py`, `src/mshkn/resources.py` |
| P6 metrics, JSON logs, status enrichment, checkpoint DAG, alerts | implemented: `src/mshkn/observability/`, `GET /alerts`; Grafana dashboards are not automatable and were never built |
| P7 Litestream | implemented: `systemd/litestream.service`, `DEPLOY.md` §12 |
| Economics validation (T9.x), dumb agent (T10.5), S3 isolation (T8.6) | not implemented; part of #65 |

## Open follow-ups

Filed from PR 5's final review and live runs: #65 (seven unimplemented E2E tests), #66 (an abandoned bring-up can orphan a spawned Firecracker), #67 (socket registry is process-local), #68 (`_post_process_rootfs` and a pre-existing `fcnet.service` symlink), #69 (test-harness hygiene), #70 (REST destroy and the dead-VM reaper can tear down the same computer). Earlier: #55 to #57 (restore-path experiments), #58 (exec log retention), #59 (`/forward`).
