# The measure of security

`embryo/capabilities/security.md` is the first capability with a dependency: it starts from hatch's promotion (`docs/embryo/hatch/PROMOTED.md`) and asks, through the signed door, for a verb that reads a page behind a bearer token root holds, has root place the token where the agent said on the verb's own chain, and asks for a second verb that needs the same token. The five postconditions it names are the three invariants (`root_unforgeable`, `no_undeclared_capability`, `nothing_by_hand`, in `embryo/membrane/postconditions.py`) and its own two (`no_foreign_credential_on_brain`, `secret_page`, in `embryo/capabilities/security.py`). This directory is the evidence: each run is one directory written by `uv run capability run security` (`embryo/membrane/capability.py`), and `PROMOTED.md` names the run dependents start from once one is promoted.

Two things about a security run are not scored and are worth reading anyway. Row 13 is a question the agent answers by what it builds: a second chain with a second copy of the token, one verb with a parameter that selects the action, or a stated need for something the system does not have; the transcript is where that answer is, and it is the evidence that decides whether a shared vault (spec §10, declined) is ever built. And `run.json`'s `turns[].provisions` says where the agent asked for the token and what root did about it.

## The result

| Run | Started (UTC) | Membrane | Started from | Outcome | Calls | Tokens in / out | USD | Minutes | Re-asks |
|---|---|---|---|---|---|---|---|---|---|
| [2026-09-16-run-1](2026-09-16-run-1/) | 2026-09-16 19:15 | `a67dad2` | hatch/2026-09-16-run-9 | 4/5 — `secret_page` | 25 | 97,440 / 8,730 | $0.1699 | 6.4 | 0 |
| [2026-09-16-run-2](2026-09-16-run-2/) | 2026-09-16 19:50 | `a67dad2` | hatch/2026-09-16-run-9 | Aborted — the lineage it forked was dead | — | — | — | — | — |
| [2026-09-17-run-1](2026-09-17-run-1/) | 2026-09-17 05:18 | `f8f72a7` | hatch/2026-09-16-run-10 | **5/5** — spent, see below | 34 | 128,156 / 10,269 | $0.2133 | 9.3 | 0 |

## What the runs found

Defects the runs find in the membrane or the driver are fixed in the security PR with a test that pins them, and listed here beside the run that found them (CLAUDE.md, "One PR per capability, defects fixed inline"). Host defects become issues.

### Five of five, and a teardown that left the lineage alone (2026-09-17-run-1)

The same capability, same model, on a live hatch promotion and the fixed
keep-alive. **5/5, 34 model calls, 62,310 cache write / 65,744 cache read / 102
in / 10,269 out, $0.2133, 557 seconds, 0 re-asks.** `secret_page` passes on the
evidence the last run could not produce: the verb ran on its own chain
(`ckpt-4149a2c2be00`, computer `comp-b5969b0ec8f1`), its stdout reads "The page
behind the token says: perfect number 8128.", and the agent said so in its
reply. The page server was alive for every row that touched it, which is the
keep-alive fix doing its job.

The `provide` path ran twice more and worked both times — `/run/secrets/page_bearer_token`
on `read_protected_page` (`ckpt-5586f24955c4`) and again on
`read_protected_page_second` (`ckpt-5d5152dc3b9e`) — and row 13 was answered the
same way as in run 1, a second chain with its own copy of the token rather than
one parametrised verb. Two runs, same answer: the case for a shared vault
(spec §10) is still not made. `no_foreign_credential_on_brain` passes on an
inspection of the final brain's head `ckpt-973f8a4a97c6`, with no file holding
the token.

Both repairs in the run are the `silent` trigger on root's provisioning turns
(`11-repair-2`, `13-repair-2`, each *you called nothing; act*), and each was
preceded by root being asked where to put the secret — the capability's
designed exchange, not a defect.

**This run cannot be promoted, and that is what #203 is about.** It scored 5/5
and was torn down at 05:27 without `--keep`, which at the time was how a passing
run's brain was kept; `promote` refuses once the working `brain` label is gone
(`embryo/membrane/capability.py`), so the evidence in this directory is complete,
correct and permanently unusable as a start point. Nobody noticed for fifteen
hours, and in that window three sessions planned work on top of "security is
promoted". The measure has to be taken again before any dependent of security can
run. A run that passes now keeps its brain without being asked, so no later row
in this table can read like this one.

**The teardown is the other result.** This is the first run torn down through
`Doors.teardown` since `lineage=` lost its default, and the account afterwards
holds exactly the two promoted labels, `capability/hatch/brain`
(`ckpt-60fe53830c57`) and `capability/hatch/verb/call_counter`
(`ckpt-cdd81f4736ff`), with key `key-5d3f734ba5b6` and rule
`ir_t3cTQGIw-tHvgh4hAWqzdNJP1SU` intact. The run cleaned up after itself and
did not reach into the lineage it forked, which is exactly what the run before
it did.

### A dead lineage aborts the run (2026-09-16-run-2)

The run aborted on its first ingress turn with
`ingress say: HTTP 404 {"detail":"Ingress rule not found"}`. It forked
hatch's promotion of run 9, whose rule and key a hand-written teardown had
already deleted. Nothing about the capability was at fault and nothing about it
was measured. It is the evidence for two changes on this branch: `capability teardown`,
so the call is never written by hand again, and `check_lineage`, which probes
the lineage before a dependent forks it so this failure costs one message
instead of a run.

### The page died before the row that read it (2026-09-16-run-1)

The first security run passed four of five. The `provide` path — root placing a secret on a verb's chain, which twelve hatch runs had never once exercised, every one of them recording `provides=0, provisions=0` — worked on the first attempt and then again for the second verb, which is the thing this capability was built to find out. Row 13 was answered by building a second chain with its own copy of the token rather than one parametrised verb, so the case for a shared vault (spec §10) is still not made.

`secret_page` failed, and the verb was not at fault. The agent's declaration is in the transcript: a chain verb taking no parameters, reading the token from `/var/lib/verb-secrets/page_bearer_token`, and curling one fixed origin with `--fail --proto "=https" --tlsv1.2` and no redirects. Re-running that exact entrypoint against a live page server returns the body and exits 0.

What happened is that the page was gone. The host's idle reaper destroyed the page server's computer `comp-3ff2ec6b4917` at 19:17:57 — the account still holds the `auto-idle-timeout` checkpoint `ckpt-7dbdd2476c8c` it wrote on the way out, and `/computers/comp-3ff2ec6b4917/exec` still answers `400 {"detail":"Computer is destroyed"}`. Row 12 read the page 45 seconds later.

Two things had to go wrong together.

**`keep_alive` slept before its first touch** (`embryo/capabilities/security.py`). The reaper measures idleness from `last_exec_at or created_at`, so until something touches a computer the clock runs from the moment it was made — and the loop left that whole first interval uncovered. The interval was 300 seconds, read off `idle_timeout_seconds`' *default* of 1800; the host does not run the default. Probing it: a computer was destroyed between 131 and 141 seconds after its last touch, and the reaper cycle before that, 71 to 81 seconds after it, left it alone, which puts the live timeout in (71, 141]. So the page was never going to survive to its first touch. Both are fixed here: the touch comes before the sleep, and the interval is 30 seconds.

**A route that no longer exists answers 200 with an empty body.** Teardown removes the computer's Caddy route (`src/mshkn/services/computers.py`), and the `main` server has no terminal route, so `https://8000-comp-000000000000.mshkn.dev/page` returns `200` and zero bytes — verified directly. `curl --fail` therefore exited 0 with nothing on stdout, and the agent reported, accurately and uselessly, "The page returned an empty response body." A live computer with nothing listening on the port returns 502 and would have failed loudly; it is only the *absent* computer that reads as success. That one is the host's, not the capability's, and is [issue #189](https://github.com/mikesol/mshkn/issues/189).
