# The measure of the first real agent

Spec §11 of `docs/superpowers/specs/2026-09-08-embryo-design.md` defines the measure: the liturgy (`embryo/liturgy.md`) spoken N times to a real model, and how often, and at what cost in turns and tokens, the embryo reaches every postcondition. This directory is the evidence. Each run is one directory, written by `uv run measure` (`embryo/membrane/measure.py`; the decisions are in `docs/superpowers/plans/2026-09-09-measure.md`). Approvals in every run below were automatic (`--approve auto`): the membrane's invariants were the guard, and every proposal is in the transcript for a reader to judge after the fact.

Two rounds are recorded. **2026-09-09** is the first real agent measured on the turn as it then was, one fork exec of 240 seconds: no run reached every postcondition, and the exercise paused on the finding that the turn's clock, not any defect, was the limit. **2026-09-10** is the measure resumed on the asynchronous turn (#110): the best run reached all seven, and medium effort beat the API's default on cost, time and outcome at once.

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

## The result, post-cut (#123)

`embryo/seed.md` was cut back to irreducible bootstrap and invisible mechanism (#123, PR #125), so
these runs attempted a harder task than either round above and **are not comparable to them**. Six
runs against `claude-opus-5` at `--effort medium`, approvals automatic. Each membrane differs: a run
found a defect, the defect was fixed, and the next run hatched from the fixed code.

| Run | Membrane | Outcome | Calls | USD |
|---|---|---|---|---|
| `2026-09-10-postcut-run-1` | `1d7d5d2` | 3/7. Would not write a policy blind — a full replacement might drop root's own access. The clause saying root's rights are not policy's to grant was restored. | 15 | 2.23 |
| `2026-09-10-postcut-run-2` | `431c3a6` | 3/7. Proposed both documents. Its hook read stdin and declared no parameters; the refusal caught it, and no repair turn existed to deliver the reason. | 9 | 1.02 |
| `2026-09-10-postcut-run-3` | `ac56165` | Stopped by hand. A relay DNS failure ate the refusal, because a turn that fails never gave its inbox back; and the new repair trigger fired at every settle. Both fixed. | — | — |
| `2026-09-10-postcut-run-4` | `14f11fd` | 3/7. **The door opened.** Authentication lost to `sig` carrying base64 over ASCII armor — a second encoding the seed used to disclose. The encoding was deleted rather than disclosed. | 22 | 2.77 |
| `2026-09-11-postcut-run-5` | `57b4f0b` | 3/7. Learned the one-parameter hook rule from a refusal, mid-turn, and said so. Then declined to guess the policy schema. | 12 | 0.81 |
| `2026-09-11-postcut-run-6` | `ee32cd2` | **4/7, the best post-cut result.** The live policy joined the turn's environment; the agent wrote a correct policy after two constructive refusals, and authentication worked for the first time. | 22 | 1.81 |

**What the round establishes.** #123 assumed everything cut from the seed would be reached by a
constructive refusal. That holds wherever the action is safe to attempt — the verb schema, the
effect enum, the one-parameter hook rule were all learned exactly that way, and run 5's model
narrated the moment it happened. It does not hold for the policy: there is no `try` for one, a
valid-but-wrong document is applied irrevocably, and a careful agent therefore declines to
experiment. That gap was closed by letting the agent read its own policy rather than by returning
anything to the seed (design §9b).

**What stopped it at 4/7.** Every remaining postcondition needs the verified principal to hold
`propose`, and run 6 withheld it on a correct argument: an `ephemeral` hook cannot detect replay, so
a captured signature would be a standing grant of self-modification. Spec §9 assumes the opposite
choice without the liturgy ever asking for it. That is a `spec-change` question, in the shape of
#117, and is deliberately not resolved by weakening a postcondition.

## The result, 2026-09-10

The measure resumed on the asynchronous turn (#110, PR #114), as #111 said it would. Six runs against `claude-opus-5`, four of which spoke to the model: three at `--effort medium` and one at the API's default. **The best reached every postcondition, for $3.39 and 23 minutes.** Approvals were automatic again (`--approve auto`).

| Run | Started (UTC) | Membrane | Effort | Outcome | Calls | Tokens in / out | USD | Minutes |
|---|---|---|---|---|---|---|---|---|
| `2026-09-10-run-1` | 09:36 | `c6c4f10` | medium | Aborted on turn 1: mem0 sends `temperature` for any `haiku`-family model and `anthropic` 1.4.0's `messages.create` has no such parameter, so every extraction raised. Fixed in `6c99519`. | 0 | — | 0.02 | — |
| `2026-09-10-run-2` | 09:43 | `6c99519` | medium | **7/7. The first run to reach every postcondition.** | 33 | 405707 / 54496 | 3.39 | 23.0 |
| `2026-09-10-run-3` | 10:15 | `6c99519` | medium | Aborted at turn 10, nine turns closed: a wake-up fork was killed mid-exec with no output, so the turn could never close (#116). | 34 | 514306 / 62741 | 4.14 | 31.9 |
| `2026-09-10-run-4` | 10:54 | `6c99519` | medium | 6/7 as judged. The only miss was `counter`, and the judge was wrong rather than the embryo: the model invoked its own counter twice to prove state crossed the chain, so the invocations returned 1, 2, 3 where the postcondition demanded 1 then 2 (#117). Under the corrected judge its recorded evidence passes. | 35 | 404073 / 44859 | 3.14 | 18.0 |
| `2026-09-10-run-5` | 11:23 | `a462b01` | default | Never hatched: run 4's brain was still on the account because it was kept for a post-mortem. Operator error, no model calls. | 0 | — | 0.00 | — |
| `2026-09-10-run-6` | 11:27 | `a462b01` | **default** | 4/7, and the most instructive run of the set (below). | 35 | 786785 / 168374 | 8.14 | 46.2 |

Costs are the model's own usage at $5 per million input tokens and $25 per million output, with no caching; the Anthropic console is the authority. Runs 1 and 5 were aborts, so the round bought four real runs for **$19.33**, including about $0.50 spent reproducing #107 outside any run.

| Postcondition | medium (of the 2 runs that reached turn 10) | default (1 run) |
|---|---|---|
| authentication | 2 | 0 |
| root unforgeable | 2 | 1 |
| authorization | 2 | 0 |
| `page_title` from a self-destructed computer | 2 | 1 |
| the counter: a chain that counts | 1, and 2 under the judge as corrected by #117 | 0 |
| no undeclared capability | 2 | 1 |
| nothing written by a human after hatching | 2 | 1 |

### The turn is no longer the limit

This is what #110 was for, and the runs settle it as a number rather than an argument. A turn used to be one fork exec: mshkn gave it 300 seconds and the membrane stopped at 240 to save its state. Turn 2 — design a verb that verifies an SSH signature, and a policy to go with it — took **432 s, 732 s, 264 s and 432 s** in runs 2, 3, 4 and 6.

Every one of those exceeds the old deadline. Not one of these four runs could have completed under the old turn shape, and the three 2026-09-09 runs that spent whole turns thinking and proposed nothing were not unlucky; they were structurally unable to finish.

### Medium effort beat the API's default, on every axis

| | medium (run 2) | default (run 6) |
|---|---|---|
| Postconditions | **7/7** | 4/7 |
| Cost | **$3.39** | $8.14 |
| Wall clock | **23.0 min** | 46.2 min |
| Output tokens | **54 496** | 168 374 |

Twice the time, 2.4 times the money, three fewer postconditions. The reason is worth stating carefully, because "more deliberation is worse" is not quite what happened.

Of run 6's three misses, **one is a real failure, and it is not the one it first appears to be**. Asked for a verb that counts its invocations, it built one that refuses to guess where chain state lives: it reads the mount table, excludes volatile filesystems and every "system path" — `/` among them — and considers only candidate directories that already exist. On `mshkn-base` the root filesystem is the only writable persistent one, and the verb never creates its candidates, so it wrote nothing, read nothing, and returned `count: 1` three times with `verdict: UNCONFIRMED`. Run 4's model wrote `mkdir -p /state` and its counter went 1, 2, 3 across three computers, carrying a timestamp written on a computer that no longer existed.

But run 6 did not mistake its instrument for the world. By the last counted turn it had diagnosed the bug itself — "v1 only uses a candidate directory *if it already exists*. All nine are absent, so it wrote nothing, so it read nothing. A third UNCONFIRMED from an instrument that never takes a reading is not a third data point... If I reported this as 'confirmed again,' I'd be laundering my own bug into evidence" — and had inferred the answer from the platform's own behaviour: its chain verb's result carried a `chain_head` where the `ephemeral` `page_title` had none, "consistent with the disk simply being the root ext4 filesystem". It designed the corrected verb, trialled it, and told root what it needed: "What I need from you is one approval, not another invocation."

**It then never proposed it.** Turn `9-count-1` records `proposals: []`: five `remember` calls and a `try`, no `propose`. Both remaining turns discuss `p-9` as a pending proposal, and `final-list.json` ends at `p-8`. The reasoning was right, the artifact was never produced, and the model's account of the world diverged from the membrane's record — which the membrane could have caught, since it owns the list of proposals that exist.

Two things follow. The first is that "default effort over-engineers" is the wrong lesson from this run; the design instinct was over-cautious, but the diagnosis was better than run 4's, which never had to diagnose anything because it guessed right. The second is that the liturgy could not have rescued it either way: `9-count-1` and `9-count-2` are the only turns the measure drives without an approval pass (`embryo/membrane/measure.py`, the two `public_turn(... COUNT ...)` calls carry no `settle`), so a proposal made there is never approved even when it is made.

**The other two misses are one choice, and it is defensible.** Run 6 named the verified principal `ssh:owner` rather than `ssh:mike`, declining to take identity from an SSH key's comment field — which is, after all, unauthenticated text that anyone can write. Authentication worked: signed messages resolved to a stable principal and unsigned ones to `anonymous`. But spec §11 names `ssh:mike`, and the policy it wrote has no entry for that principal, so both postconditions record a miss. The postcondition was not loosened to accommodate this. The spec is explicit, the liturgy hands over a key whose comment is `mike`, and both medium runs read it that way; relaxing a postcondition each time a run misses it is how a measure stops meaning anything.

So the honest summary is narrower than "default effort is worse at the task": on this liturgy, at N=1, default effort produced one design that could not work and one conformance failure that a stricter reading would call good security instinct. What run 6 could not do was *test* the property that mattered. Its own proposal said so before it ran: "`try` runs with no chain, so persistence itself cannot be proven in a sandbox." It reasoned for 168 000 output tokens about where state lives, and the one experiment that would have answered the question was unavailable to it. More deliberation did not substitute for a missing feedback loop, and arguably could not.

### Where the time goes

Nothing summarised timing before, though every command has always recorded its `seconds` and `at`. Derived from the evidence of run 2 (123 commands, 23.0 min):

| Where | Measured |
|---|---|
| Waiting on the model | **11.9 min (52%)** — 33 relay jobs, mean 21.6 s, median 14.9 s, two over 60 s |
| Everything else | ~11 min of forks and driver polling, overlapping the above |

Model calls are not the fat: 20 of 33 finished under 20 seconds. The forks are. One run of the liturgy costs **180 forks of the brain chain**, and 108 of them — 60% — are the driver asking `list` whether a turn has closed yet. Each such poll restores a Firecracker VM and boots the membrane for about 8 seconds, so the poll loop occupies the chain for 14.3 minutes against the model's 11.9. `GET /relay/{job_id}` is a host-side row read that could gate those polls; the driver forks a whole VM instead. It costs no tokens, which is why it went unnoticed.

| Fork kind, run 2 | Count |
|---|---|
| `root list` — the driver's poll | 108 |
| `membrane resume` — a model answer waking the turn | 33 |
| trials, verb invocations, hooks | 24 |
| `say` | 10 |
| `root approve` | 5 |

The 8-second cost of a bare fork is itself worth attention: a turn of six model calls pays it seven times, about a quarter of a run. How much of it is Firecracker restoring and how much is the membrane's Python boot has not been measured.

### What these runs found

- **#107** (fixed, `c6c4f10`): mem0 spends one token budget on the model's deliberation and on the JSON it asks for. At `claude-opus-5` and mem0's default 2000, a long deliberation truncated the document, mem0 caught the parse failure and stored nothing, and the audit line still said `memory_written: true`. Extraction now runs on `claude-haiku-4-5` with its own 4000-token budget, and `memory_written` reports what the store did rather than what the turn intended. Proven live: run 2 wrote memory on nine turns, the one `false` being the anonymous turn, with zero extraction failures — against six of the eight runs of 2026-09-09 failing.
- **The `temperature` defect** (fixed, `6c99519`): moving extraction off Opus uncovered that mem0 sends sampling parameters for any `haiku`-family model, which `anthropic` 1.4.0's `messages.create` does not accept. The test now drives mem0's own code path against the installed SDK's signature, so the next model or SDK change fails locally instead of on a paid run.
- **#116** (open): a wake-up fork was killed mid-exec with no output and the turn could never close, costing run 3. The cause is undetermined and the guest's evidence was destroyed with the brain. Measured on a kept brain afterwards: 482 MB total, ~303 MB used at rest, the membrane with the mem0 stack open at 134 MB resident — about 44 MB of headroom. The brain is hatched with 1 GB now, which raised available memory from 178 MB to 307 MB and is a mitigation, not a fix. Recorded on the issue: used-at-rest scales with total RAM (63%, then 69%), and roughly 450 MB of a 1 GB brain is unaccounted for by processes, cache and slab.
- **#117** (open, `spec-change`): the counter postcondition assumed one invocation per counted turn, so run 4 scored 6/7 for verifying its own verb. The judge now reads every chain invocation across both counted turns and requires 1..N in order with one checkpoint per invocation — stricter than what it replaced, which would have passed two calls returning 1 and 2 even if a third had been lost.
- **A proposal that existed only in prose.** Run 6 wrote two turns' worth of reasoning about `p-9`, including asking root to approve it, without ever calling `propose`. The membrane knows which proposal ids exist and could say so; nothing checks a reply's claims against the state the membrane owns.
- **A proposal made during the counted turns can never be approved.** The measure drives `9-count-1` and `9-count-2` without the `settle` pass every other liturgy turn gets, so an embryo that self-corrects at that point has no way to land the correction.
- **The counter evidence is extracted badly.** Run 6 recorded `counts: [7375, null, 7375]` because the judge takes the first integer in the verb's stdout and that verb led with a `run_id`. The verdict was right — the counter never counted — but the numbers in the record are noise, and the judge should read the verb's own `count:` line.

### What this round does not establish

- **N is small.** Three medium runs, of which one was lost to #116, and a single default-effort run. "Medium reaches 7/7" rests on two runs; "default over-engineers" rests on one.
- **One liturgy, one model.** Every conclusion here is about `claude-opus-5` speaking `embryo/liturgy.md`. Where medium effort's directness stops being an asset and starts being a liability is exactly what this liturgy cannot show: its tasks are small, well specified, and checkable within the turn.
- **Approvals were automatic.** This measures the embryo under a root that approves whatever the membrane's invariants permit, not a discerning one.
- **The two rounds are not fully comparable, and the seed is why.** After `2026-09-09-run-3` guessed the public payload's shape, `embryo/seed.md` was amended to state it: root signs with `ssh-keygen -Y sign -n mshkn` and sends `{"msg", "sig"}`, and a hook's stdout `mike` becomes the principal `ssh:mike`. Turn 2 of the liturgy asks the agent to invent a way to know who is speaking, so that amendment hands over part of the answer to a question the measure scores — and the `authentication` and `authorization` postconditions both turn on it. The 2026-09-10 runs therefore attempted an easier task than the 2026-09-09 runs did. The seed also carries operational scar tissue of the same kind (that the builder has no heredoc syntax, so scripts are written with `printf`). Both are recorded here rather than quietly enjoyed, and the standing rule in `CLAUDE.md` ("Keep the seed a seed") exists to stop the drift: only irreducible bootstrap and invisible mechanism belong there, and everything else must be reached by the liturgy or by a refusal that teaches.
- **A third boundary, and the seed is why again.** #123 cut `embryo/seed.md` back to the two admissible categories: out came the `effect` enum and the `local`/`read` restriction, the `timeout_seconds` ceiling, the `name` charset and reserved names, the policy schema, the proposal field list, and the root-signature protocol — the signing command, the `{"msg", "sig"}` envelope and the `mike` → `ssh:mike` clause. Each is now taught by a constructive refusal the model can read (approval-time refusals reached only root's stdout before #123 and now ride the inbox), by `PROPOSE_TOOL`'s description, or by turn 2 of the liturgy, which states the facts only the sender can state. One of those refusals is new since #123 landed: `refuse_approval` in `embryo/membrane/invariants.py` now refuses a policy whose hook verb does not declare exactly one parameter, naming the verb's actual parameters. It closes a failure that was silent before and predates #123 — `embryo/membrane/hooks.py:38-40` skips such a hook without invoking it, recording it, or signalling anything — so the seed used to carry "with the decoded payload as their single parameter" as a substitute, and now a refusal does instead. One thing #123 called for did not come out: the builder's lack of heredoc syntax and the `printf` workaround, which the design's §7 made conditional on a live check before removal. #123 assumed a single failed build would teach it loudly; a live probe on the host (recipe `rcp-60eae850c368`) found the opposite. The recipe built to `status: ready` with a clean log showing `Step 2/2 : RUN <<EOF` collapsed into a single no-op instruction, and a computer booted from that image reported `FILE_MISSING` and `ls: cannot access '/verb/'`. A build that "succeeds" while silently producing a broken verb is exactly the invisible-mechanism case §3 describes, so the line stays and #123 was wrong about that item. Runs after this cut attempted a harder task than the 2026-09-10 runs did, and are not comparable to them.
- **Two runs were kept and then inspected** (`--keep`), and the kept brains were forked afterwards to measure memory. That happened after each run's verdict was judged, so no postcondition is affected, but those forks are not in the runs' command records.
- **2026-09-11 (#118).** `try` gained a list of invocations, and a `chain` verb's trial now runs them on a scratch chain discarded with the trial. The seed's clause "runs it once on a computer with no secrets, no chain and no policy" was false under this and was corrected, not extended; nothing about `runs` entered the seed, since the tool schema names it and the result shows what it did. Rounds before this date are not comparable on any postcondition involving a chain verb: `2026-09-10-run-6` spent a turn's reasoning and three blind invocations on a property a single second run now shows directly.

## What a run directory holds

| File | What |
|---|---|
| `run.json` | The model, the run's default effort and the `output_config.effort` each turn's model calls carried -- `null` where the request carried none, which is the API's default arm and the one the 2026-09-10 round calls "default" -- the membrane commit, the times, every turn's principal, model calls, usage, tools and proposals, the approvals, the token totals, the cost, and the seven postconditions each with the evidence it was judged on. An aborted run has the error instead of the verdict. |
| `transcript.md` | Every turn: the words, the audit line, the reply (proposals included), the approvals. |
| `final-list.json` | Turn 10: root's `list` after the liturgy. |
| `commands/NNN-<door>-<command>.json` | Every command the measure sent, in order, with its raw stdout and stderr and the computer it ran on: every `say`, every `list` (the build polls included), every `approve`. Nothing else was sent; that is the last postcondition. |
