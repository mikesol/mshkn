# mshkn

Disposable cloud computers for AI agents: Firecracker microVMs you create, exec on, checkpoint, fork, merge and destroy. `README.md` says what exists, `docs/ARCHITECTURE.md` says how it works, `docs/plans/README.md` indexes every plan with its status, and `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` is the definition of done (157 E2E tests).

## The gate

Run this before every commit you would show anyone. It is the same set of checks CI runs (`.github/workflows/ci.yml`):

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov
```

uv is the only package manager; every tool runs through the project venv as `uv run <tool>`. `pytest --cov` runs the unit and flow tiers and enforces the coverage floor (98 %, `fail_under` in `pyproject.toml`); plain `pytest` skips the coverage measurement, which is fine while iterating on one file. The E2E tier is deselected by default; zero warnings is part of green. `tests/unit/test_docs.py` fails when a document names a path, module, route, metric or variable that does not exist: fix the document, not the test.

## The live E2E gate

E2E tests on the live host are the source of truth for the product. After deploying, run:

```bash
export MSHKN_SERVER=root@<ip>       # or an ssh config alias
scripts/e2e.sh                       # pushes, deploys, cleans orphaned VM resources, ensures the test account, runs tests/e2e
```

Run it detached (`setsid nohup … > log 2>&1 < /dev/null &`) and read the log; a full run takes about 14 minutes. The expected result is **144 passed, 6 skipped, 7 failed**, and the seven must be exactly the `Not implemented` tests in #65. Any other failure is a regression: fix it or stop and discuss. Never mark a test xfail, never weaken an assertion, never skip to get green. Failing tests are honest reminders of what is left; a test with no assertion is worse than a failing one.

After a run, check the service journal for tracebacks:

```bash
ssh $MSHKN_SERVER journalctl -u mshkn --since '20 min ago' --no-pager | grep -ci traceback
```

## How to find work

Open issues are the backlog: `gh issue list`. `docs/plans/README.md` shows what each historical plan delivered and what is still open. Read an issue fully before starting; if it names something out of scope, leave it out.

1. Create a worktree: `git worktree add ../mshkn-<name> -b <branch>` and work there.
2. Open a PR against `main` with `gh pr create`.

## How to submit work

The PR body must include:

- `Closes #<N>` when there is an issue.
- **What this does**: two or three sentences.
- **Design alignment**: for each principle in `docs/ARCHITECTURE.md` or the spec the change touches, how the implementation matches. A deviation references an approved `spec-change` issue.
- **Validation performed**: the gate output, the CI link, and the live E2E summary line with the failing set named. Evidence, not claims.

## Required skills for all workflow operations

If you are implementing a GitHub issue and, having studied the codebase, feel that it is relatively straightforward and mechanical to implement and needs just a few decisions here and there, you can go about implementing directly and ask questions as they come up.

Otherwise, for creative, open-ended, or large tickets, you MUST use the superpowers skills for brainstorming, planning, worktree management, and sub-agent dispatch. Do NOT hand-roll these operations with raw Task tool calls — the skills handle permissions, directory routing, and agent coordination correctly. Raw background agents WILL fail on file writes due to auto-denied permissions.

| Operation | Required skill |
|---|---|
| Creative/design work before implementation | `superpowers:brainstorming` |
| Writing implementation plans | `superpowers:writing-plans` |
| Creating/managing git worktrees | `superpowers:using-git-worktrees` |
| Dispatching parallel sub-agents | `superpowers:dispatching-parallel-agents` |
| Executing plans with sub-agents (same session) | `superpowers:subagent-driven-development` |
| Executing plans (separate session) | `superpowers:executing-plans` |
| Finishing a branch (merge/PR/cleanup) | `superpowers:finishing-a-development-branch` |
| Code review | `superpowers:requesting-code-review` |
| Verifying work before claiming done | `superpowers:verification-before-completion` |
| TDD workflow | `superpowers:test-driven-development` |

**Never** use `run_in_background: true` with the Task tool for implementation work. Background agents cannot prompt for permissions and will silently fail or write to wrong directories.

## How to handle PR reviews

After creating a PR, bot reviewers (CodeRabbit, Copilot) may leave comments. Triage them:

1. **Reply to every comment** with a concise rationale (fix, defer, or dismiss with reason)
2. **Resolve every thread** after replying — use the GraphQL `resolveReviewThread` mutation
3. **Fix only what's actually wrong** — bot reviewers lack project context and frequently suggest over-engineering

**API reference** (so you don't have to rediscover this):

```bash
# Get review comment IDs
gh api repos/mikesol/mshkn/pulls/<N>/comments --jq '.[] | {id, user: .user.login, path, line, body: .body[:80]}'

# Reply to a review comment (in_reply_to creates a thread reply)
gh api repos/mikesol/mshkn/pulls/<N>/comments -f body="Your reply" -F in_reply_to=<comment_id>

# Get thread IDs for resolving
gh api graphql -f query='{ repository(owner: "mikesol", name: "mshkn") { pullRequest(number: <N>) { reviewThreads(first: 50) { nodes { id isResolved } } } } }'

# Resolve a thread
gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "<thread_id>"}) { thread { isResolved } } }'
```

## Standing rules

- **NEVER merge PRs without explicit user authorization.** Wait for the user to say "merge it" (or equivalent). Creating a PR is fine; merging is not. No exceptions.
- **Wait for CI before requesting a merge.** `gh pr checks <N> --watch`. Never merge a red PR.
- **Spec seems wrong?** Stop. Open a GitHub issue labeled `spec-change` with the problem and its evidence, the affected sections, the proposed change and the downstream impact. Don't build on a wrong assumption.
- **No papering over failures.** If you can't solve something, say so. No xfail, no weakened assertions, no workarounds that hide the real issue.
- **Be mega-rigorous.** Don't code to the benchmark. Don't sweep stuff under the carpet. Evidence before assertions.
- **No backwards compatibility or versioning.** This is a pre-alpha research project with zero users. Don't version APIs, don't keep fallback paths, don't create a "v2" beside the old thing; replace it. The one exception is database migrations, which are sequential and additive.
- **Product behaviour changes need a test that found or pins them**, in the unit or flow tier; the E2E tier proves them on the live host.
- **Infrastructure comes before workarounds.** If a task needs a host or a service the project does not have, write the minimum into `docs/infrastructure.md` and ask for it; do not make the product optional or add indirection to work around missing infrastructure.

## Deployment

`DEPLOY.md` is the fresh-server procedure (Firecracker, dm-thin pool, Docker base image, Caddy, R2, Litestream), executed verbatim on the current host. `scripts/deploy.sh` pushes the current branch to the host and restarts the service; `scripts/e2e.sh` does that and runs the suite.

## Server reference

The live E2E server is a dedicated KVM host set up from `DEPLOY.md`. Export its address once per shell (`MSHKN_SERVER=root@<ip>` or an ssh config alias):

- **Deploy**: `scripts/deploy.sh`
- **E2E**: `scripts/e2e.sh`
- **Service**: `ssh $MSHKN_SERVER systemctl {restart,status,stop} mshkn`
- **Logs**: `ssh $MSHKN_SERVER journalctl -u mshkn --since '5 min ago' --no-pager`
- **Test account**: `acct-mike` / `mk-test-key-2026` (recreated by `scripts/e2e.sh` if the database was reset)
- **Secrets on the host**: `/opt/mshkn/.env` (R2), `/etc/caddy/env` (Cloudflare DNS token), `/etc/litestream.yml`. Never print them.
