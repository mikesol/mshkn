# The measure of coding

`embryo/capabilities/coding.md` is node #161 of the capability DAG (#158), designed in `docs/superpowers/specs/2026-09-17-coding-design.md`. It is the first capability whose subject is code that outlives the turn that wrote it: across six rows on top of security's promotion, root asks for a program that totals amounts, uses it, gives it input it was not written for, asks for the behaviour he wants instead, uses it again, and asks what command he would run himself.

The five postconditions it names are the three invariants (`root_unforgeable`, `no_undeclared_capability`, `nothing_by_hand`, in `embryo/membrane/postconditions.py`) and its own two exercises (`runs_again`, `fixed`, in `embryo/capabilities/coding.py`). Both exercises are judged on a fork, driven after the run by that module's `verify`, of the head of the chain the command from the last speaking row runs on — over amounts the script never spoke. Nothing here is judged on what the agent said about its own program.

This directory is the evidence: each run is one directory, `<date>-run-<n>`, written by `uv run capability run coding` (`embryo/membrane/capability.py`), and `PROMOTED.md` names the run dependents start from once one is promoted. Nothing is promoted yet.

## The result

| Run | Started (UTC) | Membrane | Started from | Outcome | Calls | Tokens in / out | USD | Minutes | Re-asks |
|---|---|---|---|---|---|---|---|---|---|
| [2026-09-18-run-1](2026-09-18-run-1/) | 2026-09-18 09:55 | `f8f72a7` | security/2026-09-18-run-2 | 3/5 — `runs_again`, `fixed` | 62 | 463,399 / 50,228 | $0.8663 | 22.0 | 0 |

## What the run found

### Three of five, and the agent was right every time (2026-09-18-run-1)

The first run of coding, and the first against security's promotion. **3/5, 62 model calls, 183,078 cache write / 280,135 cache read / 186 in / 50,228 out, $0.8663, 1,318 seconds, 0 re-asks.** The three invariants passed. Neither exercise ran.

The agent shipped a working program at p-12 and then spent turns 15 through 19 unable to replace it. It proposed a fix, trialled the fix, watched the trial pass, approved the fix, invoked the verb, and got exit 127 — four times, across p-14, p-15, p-16 and p-17. Its final report was exactly correct: *"So the p-17 repair is not actually active in the current verb."*

It is one defect, in the driver rather than in the agent. A catalogued `state: chain` verb whose chain has a head is invoked by forking that head (`embryo/membrane/verbs.py`, `invoke`), and a fork resumes the checkpoint's disk (`src/mshkn/services/computers.py`, `fork`). The chain label is derived from the verb's *name* (`embryo/membrane/declarations.py`), so a supersede keeps the chain, and keeping the chain keeps the disk. `verb/amount_total`'s filesystem has been p-12's since p-12 first ran, and p-12 installed `/usr/local/bin/run-amount-total`; p-14 through p-16 declare `/verb/run.sh` and p-17 declares `/usr/local/bin/amount-total`, and none of those paths has ever existed on that disk. The new image is built, paid for, and never booted.

A trial does not fork the catalogued chain — `embryo/membrane/trials.py` creates a fresh scratch chain from the trial's own recipe. So `try` runs the new image and the verb runs the old disk, and the two diverge precisely when a proposal changes the filesystem, which is the only reason to supersede a verb. Trial `t-38` exercised p-17 end to end (exits 0 / 2 / 0, the 2 its own deliberate rejection of a malformed line) and the same recipe, catalogued, returned `/usr/local/bin/amount-total: No such file or directory`. The rig certified code it would not run, which is why the agent retried four times: the evidence it was given said retrying was the right move. Issue #213, and the design question it leaves behind is #215.

The exercises never saw any of that. `chain_for` (`embryo/capabilities/coding.py`) recovers the chain by looking for a verb's name inside the command; the verb is named `amount_total` and the binary is `amount-total`, so the match misses, and its one-candidate fallback cannot fire for a dependent of security, which inherits three chain verbs of its own. Both exercises recorded *"several chain verbs, and '/usr/local/bin/amount-total < /tmp/amounts' singles out none of them"* and probed nothing. A run that had passed would have produced the same sentence. Issue #214.

One error is the agent's, minor and downstream of all of this: at row 20 it offered `/usr/local/bin/amount-total < /tmp/amounts`, feeding on stdin a program whose own entrypoint takes the amounts as an argument.

The brain is kept on the account for inspection. `capability run` attributes and tears down a failed run's brain on the next run (#193, PR #195), so it needs no hand.
