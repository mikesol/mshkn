# The measure of security

`embryo/capabilities/security.md` is the first capability with a dependency: it starts from hatch's promotion (`docs/embryo/hatch/PROMOTED.md`) and asks, through the signed door, for a verb that reads a page behind a bearer token root holds, has root place the token where the agent said on the verb's own chain, and asks for a second verb that needs the same token. The five postconditions it names are the three invariants (`root_unforgeable`, `no_undeclared_capability`, `nothing_by_hand`, in `embryo/membrane/postconditions.py`) and its own two (`no_foreign_credential_on_brain`, `secret_page`, in `embryo/capabilities/security.py`). This directory is the evidence: each run is one directory written by `uv run capability run security` (`embryo/membrane/capability.py`), and `PROMOTED.md` names the run dependents start from once one is promoted.

Two things about a security run are not scored and are worth reading anyway. Row 13 is a question the agent answers by what it builds: a second chain with a second copy of the token, one verb with a parameter that selects the action, or a stated need for something the system does not have; the transcript is where that answer is, and it is the evidence that decides whether a shared vault (spec §10, declined) is ever built. And `run.json`'s `turns[].provisions` says where the agent asked for the token and what root did about it.

## The result

| Run | Started (UTC) | Membrane | Started from | Outcome | Calls | Tokens in / out | USD | Minutes | Re-asks |
|---|---|---|---|---|---|---|---|---|---|
| [2026-09-16-run-1](2026-09-16-run-1/) | 2026-09-16 19:15 | `a67dad2` | hatch/2026-09-16-run-9 | 4/5 — `secret_page` | 25 | 97,440 / 8,730 | $0.1699 | 6.4 | 0 |

## What the runs found

Defects the runs find in the membrane or the driver are fixed in the security PR with a test that pins them, and listed here beside the run that found them (CLAUDE.md, "One PR per capability, defects fixed inline"). Host defects become issues.

### The page died before the row that read it (2026-09-16-run-1)

The first security run passed four of five. The `provide` path — root placing a secret on a verb's chain, which twelve hatch runs had never once exercised, every one of them recording `provides=0, provisions=0` — worked on the first attempt and then again for the second verb, which is the thing this capability was built to find out. Row 13 was answered by building a second chain with its own copy of the token rather than one parametrised verb, so the case for a shared vault (spec §10) is still not made.

`secret_page` failed, and the verb was not at fault. The agent's declaration is in the transcript: a chain verb taking no parameters, reading the token from `/var/lib/verb-secrets/page_bearer_token`, and curling one fixed origin with `--fail --proto "=https" --tlsv1.2` and no redirects. Re-running that exact entrypoint against a live page server returns the body and exits 0.

What happened is that the page was gone. The host's idle reaper destroyed the page server's computer `comp-3ff2ec6b4917` at 19:17:57 — the account still holds the `auto-idle-timeout` checkpoint `ckpt-7dbdd2476c8c` it wrote on the way out, and `/computers/comp-3ff2ec6b4917/exec` still answers `400 {"detail":"Computer is destroyed"}`. Row 12 read the page 45 seconds later.

Two things had to go wrong together.

**`keep_alive` slept before its first touch** (`embryo/capabilities/security.py`). The reaper measures idleness from `last_exec_at or created_at`, so until something touches a computer the clock runs from the moment it was made — and the loop left that whole first interval uncovered. The interval was 300 seconds, read off `idle_timeout_seconds`' *default* of 1800; the host does not run the default. Probing it: a computer was destroyed between 131 and 141 seconds after its last touch, and the reaper cycle before that, 71 to 81 seconds after it, left it alone, which puts the live timeout in (71, 141]. So the page was never going to survive to its first touch. Both are fixed here: the touch comes before the sleep, and the interval is 30 seconds.

**A route that no longer exists answers 200 with an empty body.** Teardown removes the computer's Caddy route (`src/mshkn/services/computers.py`), and the `main` server has no terminal route, so `https://8000-comp-000000000000.mshkn.dev/page` returns `200` and zero bytes — verified directly. `curl --fail` therefore exited 0 with nothing on stdout, and the agent reported, accurately and uselessly, "The page returned an empty response body." A live computer with nothing listening on the port returns 502 and would have failed loudly; it is only the *absent* computer that reads as success. That one is the host's, not the capability's, and is [issue #189](https://github.com/mikesol/mshkn/issues/189).
