# Plans index

Every design and implementation document in this repository, with its status as of 2026-09-08 (the `embryo` branch). Status is judged from the code, not from what the document says about itself. The documents are historical records and are not edited, with one exception: the test plan is the definition of done, and a `spec-change` issue may revise it (#76 replaced Phase 10 and added T7.9 on 2026-09-07; #58 added T7.10 and T13.14 on 2026-09-08). Otherwise this index is what changes.

Statuses: **implemented**, **partially implemented**, **not implemented** (in the test plan, no code, tracked in an issue), **in progress** (the plan being executed), **superseded by …**, **retired** (never built, not replaced), **reference** (not a plan).

## Product

| Document | What it proposed | Status | Evidence |
|---|---|---|---|
| `docs/plans/2026-03-07-disposable-cloud-computers-design.md` | The product: disposable Firecracker VMs identified by checkpoints, fork and merge, Nix capability layers, sleep-for-free economics. | partially implemented: the computer, checkpoint, fork and merge model is live; the Nix capability layer was replaced by Docker recipes | `src/mshkn/services/computers.py`, `src/mshkn/services/checkpoints.py`, `src/mshkn/services/recipes.py` |
| `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` | The definition of done: 180 end-to-end tests, T0 to T15. | reference; the suite is `tests/e2e/`. Four tests (T8.6, T9.1, T11.2 and the audit-log check) fail as `Not implemented` until built (#65). Phase 10 and T7.9 were revised by #76 (`docs/superpowers/specs/2026-09-07-generative-agent-workload-proof-design.md`) and landed with PR 7; T7.10 and T13.14 (exec log retention) landed with #58; T8.7 (scoped keys) with #88; T12.1 (fork by label) with #89. Phase 14 (the embryo, seven tests) was added on 2026-09-08 from `docs/superpowers/specs/2026-09-08-embryo-design.md` §11; Phase 14 was rewritten and Phase 15 (the relay, four tests) added from `docs/superpowers/specs/2026-09-09-async-turn-relay-design.md` §8 (#110). | `tests/e2e/` collects 180 |
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
| `docs/superpowers/plans/2026-09-07-pr6-docs-devtools.md` | #72 | implemented (merged 2026-09-07) |

## Generative-agent workload proof (2026-09)

Spec: `docs/superpowers/specs/2026-09-07-generative-agent-workload-proof-design.md` (from the #76 brainstorm). Two PRs: PR 7a makes the bare base the export of `mshkn-base`; PR 7 adds the eight Phase 10 tests and T7.9, enforces the base-image rule (#73), adds a caller-chosen exec timeout, reports killed commands honestly, and retains recipe images so rebuilds are incremental. Both plans are written; each PR records its baseline beside its plan when it starts.

| Plan | PR | Status |
|---|---|---|
| `docs/superpowers/plans/2026-09-07-pr7a-one-base.md` | #79 | implemented (merged 2026-09-07) |
| `docs/superpowers/plans/2026-09-07-pr7-workload-proof.md` | #81 | implemented (merged 2026-09-07) |

## The embryo (2026-09)

Spec: `docs/superpowers/specs/2026-09-08-embryo-design.md`. Plan: `docs/superpowers/plans/2026-09-08-embryo.md`. The first real agent built on mshkn: a membrane in a `brain` checkpoint chain with a scoped key, an authenticated door and a public one, and a catalog of verbs that grows only by proposal and approval. PR #98. Status: **implemented**. Evidence: the `membrane` package and the priors in `embryo/`, the liturgy end to end over the fake host in `tests/flow/test_embryo_liturgy.py`, and Phase 14 of the test plan on the live host in `tests/e2e/test_phase14_embryo.py`.

The measure (spec §11, #101): `docs/superpowers/plans/2026-09-09-measure.md`. `uv run measure` (`embryo/membrane/measure.py`) speaks the liturgy to a real model and judges the seven postconditions; the runs and their tally are in `docs/embryo/`. Status: **implemented; the exercise is paused**. Eight runs on 2026-09-09 reached at most 3 of 7 postconditions and found five defects (#105 to #109); they stopped on a design limit, not a defect: a turn is one 300 s fork exec and the model's thinking does not fit it. #110 landed as PR #114; #111 resumes the measure on the new turn.

## Asynchronous turns through the relay (2026-09)

Spec: `docs/superpowers/specs/2026-09-09-async-turn-relay-design.md` (from the #110 brainstorm, which decided to fold `lampas`'s request-to-webhook relay into mshkn as a host-side service). Plan: `docs/superpowers/plans/2026-09-09-async-turn-relay.md`. Rewrites the embryo's turn (§6 of `docs/superpowers/specs/2026-09-08-embryo-design.md`) so a turn is a chain of forks through `mshkn.services.relay.RelayService` instead of one fork's exec: `say` posts the model request to `POST /relay` with its own wake-up as the delivery and answers with an acknowledgement, and `membrane resume <job_id>` continues the turn when the relay's fork arrives. Adds the `relay_jobs` table, the `relay` scope, and Phase 15 of the test plan. Status: **implemented** (PR #114). Evidence so far: `src/mshkn/services/relay.py`, `src/mshkn/services/ssrf.py`, `src/mshkn/services/sse.py`, `tests/e2e/test_phase15_relay.py`, `tests/e2e/test_phase14_embryo.py`.

## Cutting the seed back to a seed (2026-09)

Spec: `docs/superpowers/specs/2026-09-10-seed-reduction-design.md` (#123). Plan: `docs/superpowers/plans/2026-09-10-seed-reduction.md`. Cuts `embryo/seed.md` to the two categories the standing rule in `CLAUDE.md` admits — irreducible bootstrap and invisible mechanism — moving everything else out to a constructive refusal the model can read or to turn 2 of the liturgy, which now states root's signing facts instead of the seed handing them over. Delivers a refused approval, a `requires`-blocked proposal and a rejected Dockerfile to the model's inbox for the first time (previously visible only on root's stdout), and adds refusals for a policy that names `root` among its principals and for a hook verb that does not declare exactly one parameter. Status: **implemented** on the `seed-reduction` branch; the measure round against the reduced seed (spec §10) is pending. Evidence: `embryo/seed.md`, `embryo/liturgy.md`, `embryo/membrane/declarations.py`, `embryo/membrane/invariants.py`, `embryo/membrane/liturgy.py`, `embryo/membrane/proposals.py`, `tests/unit/test_embryo_priors.py`, `tests/unit/test_embryo_declarations.py`, `tests/unit/test_embryo_proposals.py`.

## Chain trials (2026-09)

Spec: `docs/superpowers/specs/2026-09-11-chain-trials-design.md` (#118). Plan: `docs/superpowers/plans/2026-09-11-chain-trials.md`. `try` takes an optional `runs` list of parameter sets and runs one invocation per entry in order; a `chain` verb's invocations share a scratch chain `verb/trial/<trial id>`, swept when the trial ends and re-swept by the next turn's poll if the trial ran out of time or mshkn refused the delete. A non-zero exit does not stop the sequence (a replay-protected hook must be accepted once and refused once); an `error` does. `parse_verb` now refuses a verb that declares a `chain` under `verb/trial/`, so a declared chain can never collide with a trial's scratch one. The seed's clause that `try` "runs it once on a computer with no secrets, no chain and no policy" was false under this and was corrected, not extended; nothing about `runs` entered the seed. Status: **implemented** on the `chain-trials` branch. Evidence: `embryo/membrane/trials.py`, `embryo/membrane/declarations.py`, `embryo/membrane/turn.py`, `embryo/membrane/state.py`, `embryo/membrane/mshkn.py`, `embryo/membrane/scripted.py`, `embryo/seed.md`, `src/mshkn/host/fake.py`, `tests/unit/test_embryo_trials.py`, `tests/flow/test_embryo_liturgy.py`, `tests/e2e/test_phase14_embryo.py`.

## Roadmap breakdown (`docs/plans/2026-03-08-roadmap.md`)

| Item | Status |
|---|---|
| P1 destroy ownership check, startup recovery, allocation locking | implemented: `ComputerService.get_owned`, `Runtime.start`, `SlotAllocator` |
| P2 Caddy dynamic routing | implemented: `src/mshkn/host/caddy.py` |
| P3 Nix capability system | superseded by `docs/plans/2026-03-13-recipe-system-design.md` |
| P4 merge end to end | implemented: `POST /checkpoints/{parent_id}/merge` |
| P5 VM limits, rate limiting, idle timeout, retention, `needs`, stale cleanup | implemented: `ComputerService.create`, `src/mshkn/ratelimit.py`, `src/mshkn/services/reaper.py`, `src/mshkn/resources.py` |
| P6 metrics, JSON logs, status enrichment, checkpoint DAG, alerts | implemented: `src/mshkn/observability/`, `GET /alerts`; Grafana dashboards are not automatable and were never built |
| P7 Litestream | implemented: `systemd/litestream.service`, `DEPLOY.md` §11 |
| Economics validation (T9.x), S3 isolation (T8.6) | not implemented; part of #65 |

## Open follow-ups

Filed from PR 5's final review and live runs: #65 (four unimplemented E2E checks), #66 (an abandoned bring-up can orphan a spawned Firecracker), #67 (socket registry is process-local), #68 (`_post_process_rootfs` and a pre-existing `fcnet.service` symlink), #69 (test-harness hygiene), #70 (REST destroy and the dead-VM reaper can tear down the same computer). Earlier: #55 to #57 (restore-path experiments). Closed since: #70 (PR #84), #58 (exec log retention: the `exec_log` table, `GET /computers/{computer_id}/exec_log`, T7.10 and T13.14), #59 (`/forward`: superseded by the relay, `docs/ARCHITECTURE.md` §9a).
