# mshkn

Disposable cloud computers for AI agents. See `docs/plans/2026-03-07-disposable-cloud-computers-design.md` for architecture and `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` for the definition-of-done test plan that encodes the full spec as 115 E2E tests.

## How to find work

```
gh issue list --milestone "<current phase>" --label "ready" --assignee ""
```

Pick an issue. Read it fully — especially **"Not in scope"**. Then:

1. Assign yourself: `gh issue edit <N> --add-assignee @me --remove-label ready --add-label in-progress`
2. Create a worktree: `git worktree add ../mshkn-<N> -b issue-<N>`
3. Work in that worktree
4. PR back to main: `gh pr create` referencing `Closes #<N>`

## How to submit work

PR body must include:

- `Closes #<N>`
- **What this does**: 2-3 sentences
- **Design alignment**: For each design doc principle referenced in the issue, confirm how the implementation matches. Any deviation must reference an approved `spec-change` issue.
- **Validation performed**: What you tested (unit tests, E2E on live server). Evidence, not claims.

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

After creating a PR, bot reviewers (CodeRabbit, Copilot) will leave comments. Triage them:

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

- **NEVER merge PRs without explicit user authorization.** Always wait for the user to say "merge it" (or equivalent). Creating a PR is fine; merging is not. No exceptions.
- **Spec seems wrong?** STOP. Open a GitHub Issue labeled `spec-change` with: the problem (with evidence), affected design doc sections, proposed change, downstream impact. Don't build on a wrong assumption.
- **Validate locally first**: `ruff check src/ && mypy src/ && .venv/bin/pytest tests/ --ignore=tests/e2e --ignore=tests/integration -x`. Always use `.venv` for poetry, pytest, python, and formatters.
- **E2E tests on live infra are the source of truth.** After deploying, run E2E against `135.181.6.215:8000`. Never accept regressions — if a test that passed before now fails, that's a real problem. Fix it or stop and discuss.
- **Deploy workflow**: commit → push → `ssh root@135.181.6.215 "cd /opt/mshkn && git pull && systemctl restart mshkn"` → clean stale VMs → E2E. Always clean up orphan dm-thin volumes, tap devices, and firecracker processes before running E2E after deploy.
- **No papering over failures.** If you can't solve something, say so. Don't mark tests as xfail, don't weaken assertions, don't add workarounds that hide the real issue. Failing tests are honest reminders of what's left.
- **Be mega-rigorous.** Don't code to the benchmark. Don't sweep stuff under the carpet. Evidence before assertions.
- **Wait for CI before merging.** After creating a PR, use `gh pr checks <N> --watch` to confirm all checks pass before requesting merge authorization. Never merge a red PR.

## Server reference

- **Host**: Hetzner AX41-NVMe at `135.181.6.215`, AMD Ryzen 5 3600, 64GB RAM, 2x512GB NVMe
- **SSH**: `ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@135.181.6.215`
- **Service**: `systemctl {restart,status,stop} mshkn`
- **Logs**: `journalctl -u mshkn --since '5 min ago' --no-pager`
- **Test account**: `acct-mike` / `mk-test-key-2026`
- **E2E**: `MSHKN_API_URL=http://135.181.6.215:8000 .venv/bin/pytest tests/e2e/ -v --tb=short`

## Current phase

Priority 1 (Bug Fixes) — see `docs/plans/2026-03-08-roadmap.md` for full prioritized backlog.
