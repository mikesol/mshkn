# The measure of the first real agent

Spec §11 of `docs/superpowers/specs/2026-09-08-embryo-design.md` defines the measure: the liturgy (`embryo/liturgy.md`) spoken N times to a real model, and how often, and at what cost in turns and tokens, the embryo reaches every postcondition. This directory is the evidence. Each run is one directory, written by `uv run measure` (`embryo/membrane/measure.py`; the decisions are in `docs/superpowers/plans/2026-09-09-measure.md`). Approvals in every run below were automatic (`--approve auto`): the membrane's invariants were the guard, and every proposal is in the transcript for a reader to judge after the fact.

## The result, 2026-09-09

Eight runs against `claude-opus-5` at the API's default effort. **No run reached every postcondition; the best reached 3 of 7.** The runs were not one embryo measured eight times: each run found a defect, the defect was fixed, and the next run hatched from the fixed code, so the table names the membrane commit each run hatched from. The exercise was paused after run 8 on the finding that the turn's clock, not any defect, is the limit (below).

| Run | Started (UTC) | Membrane | Outcome | Model calls | Tokens in / out | USD |
|---|---|---|---|---|---|---|
| `2026-09-09-run-1` | 07:41 | `e3e9d90` | 2/7. Turn 2: 8192 output tokens of thinking, no text, no call, reported as done (#106). The door never opened. | 4 | 16839 / 11494 | 0.37 |
| `2026-09-09-run-2` | 07:46 | `e3e9d90` | 2/7. As run 1. | 4 | 15135 / 10861 | 0.35 |
| `2026-09-09-run-4` | 07:51 | `acbf71f` | Aborted at turn 2: the host's reaper destroyed the brain's computer 160 s into the turn as idle (#108). | 2 | 5781 / 2092 | 0.08 |
| `2026-09-09-run-3` | 08:00 | `fe0edf0` | 3/7. Turn 2 proposed a careful hook (six trial cases, a replay ledger on its own chain). The driver approved the policy before the hook and it was refused until turn 6's pass; the hook then named nobody, because the model had guessed the payload shape and said so in its reply. Turns 7 and 9 were refused as anonymous, which is the approved policy working. | 12 | 118725 / 31165 | 1.37 |
| `2026-09-09-run-5` | 08:15 | `52be7b0` | 3/7. Turn 2: four calls, the trial's build outlived the 240 s turn, nothing proposed. | 7 | 60928 / 22253 | 0.86 |
| `2026-09-09-run-6` | 08:23 | `f3ceb07` | Aborted: turn 1 and two follow-ups ran out of time exploring the trial sandbox, one answer hit the 16000-token budget, then a `try` near the deadline timed out and crashed the turn (#109). | 12 | 162629 / 51393 | 2.10 |
| `2026-09-09-run-7` | 08:46 | `dc7b4ef` | Aborted at turn 2: a streamed message echoed back with the SDK's `parsed_output` field, API 400 (fixed in `4cc5db0`). | 4 | 15456 / 2768 | 0.15 |
| `2026-09-09-run-8` | 08:55 | `4cc5db0` | Interrupted by hand (the pause), no `run.json`; the commands are recorded. Turn 2 and its two follow-ups each spent the whole 240 s turn on a single streamed response and proposed nothing; the words so far were kept and each turn ended as `deadline`. | 6 | 22785 / 2565, under-counted | 0.18, under-counted |

Directory numbers are the order the directories were created, not the order the runs started; the table is chronological. The cost column is the model's own usage at $5 per million input tokens and $25 per million output tokens (`claude-opus-5`, first-party list price on 2026-06-24), with no caching in play. Two things are not in these counts: mem0's extraction calls and the OpenAI embeddings, and the output of a completion cut off at the deadline, whose token count arrives at the end of the stream and so never reaches the audit line (run 8's three cut-off turns). The Anthropic console is the authority; the runs cost about $6.50 there.

| Postcondition | Reached, of the 5 runs that reached turn 10 |
|---|---|
| authentication (signed is `ssh:mike`, unsigned is `anonymous`) | 0 |
| root unforgeable | 5 |
| authorization | 0 |
| `page_title` from a self-destructed computer | 0 |
| the counter: 1, 2, a chain of two | 0 |
| no undeclared capability | 2 |
| nothing written by a human after hatching | 5 |

## What the runs found

Each defect is its own issue, as #101 said it would be; the ones marked fixed were fixed in PR #104 because the next run could not proceed without them.

- **#105** (host, open): one contended write under T5.7's twenty concurrent destroys left the service's SQLite connection inside an open transaction; litestream commits every second, so every later write failed with `database is locked` until a restart. Found by the branch's first live E2E run.
- **#106** (fixed): the loop's 8192-token output budget was spent on thinking and the `max_tokens` stop was reported as `done`. Completions are now streamed with a 64000-token budget and bounded by the time left in the turn; a turn that runs out says so and keeps the words it has. `MEMBRANE_EFFORT` sets `output_config.effort`; every run above used the default.
- **#107** (open): mem0's fact extraction failed to parse the model's response in every run, so memory may have been written empty.
- **#108** (fixed, deployed): the reaper destroyed a computer whose exec was still running; a running command now keeps a computer alive.
- **#109** (fixed): a transport error during a trial crashed the turn; it is a tool result now, and a trial with no time left waits for the next turn.
- The seed did not say what a hook receives. Run 3's model wrote a correct hook, guessed the payload shape, wrote "I guessed the payload shape" in its reply, and named nobody. The seed now states the `{msg, sig}` payload and how root signs.
- The driver approved proposals in id order; a door policy named a hook not yet in the catalog and was refused. Verbs are approved before policies now, and turn 3 also follows a turn that ran out before proposing.

## The finding that paused the exercise

A turn of the embryo is one fork exec, mshkn gives a fork's exec 300 seconds, and the membrane stops at 240 to save its state. At the default effort, `claude-opus-5` spends minutes thinking on the hard turns (turn 2: design a verb that verifies an SSH signature, plus a policy). Runs 5, 6 and 8 show single streamed responses that fill the whole 240 seconds and end with nothing proposed; the follow-ups do the same. No budget, ceiling or margin changes that: the turn's clock and the model's deliberation are not the same size. The levers left are less thinking per call (`--effort medium`, built, not measured), a longer exec budget (600 seconds is the API's ceiling), or a turn that does not keep a computer alive while the model thinks: the brain hands the request to a relay that calls the model and wakes the brain when the answer arrives. The last is the design change, #110; the measure resumes on it, #111.

#110 landed as PR #114: a turn is a chain of forks through the host's relay, so the model's deliberation is bounded by the relay's patience rather than by a fork's 300 s exec budget. The measure resumes on that turn as #111.

## What a run directory holds

| File | What |
|---|---|
| `run.json` | The model, the effort, the membrane commit, the times, every turn's principal, model calls, usage, tools and proposals, the approvals, the token totals, the cost, and the seven postconditions each with the evidence it was judged on. An aborted run has the error instead of the verdict. |
| `transcript.md` | Every turn: the words, the audit line, the reply (proposals included), the approvals. |
| `final-list.json` | Turn 10: root's `list` after the liturgy. |
| `commands/NNN-<door>-<command>.json` | Every command the measure sent, in order, with its raw stdout and stderr and the computer it ran on: every `say`, every `list` (the build polls included), every `approve`. Nothing else was sent; that is the last postcondition. |
