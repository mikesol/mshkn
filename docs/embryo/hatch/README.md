# The measure of hatch

`embryo/capabilities/hatch.md` is the first capability: the ten rows that take an embryo from an egg to an open door, a verified principal, a policy, an ephemeral verb and a chain verb, with the seven postconditions `embryo/membrane/postconditions.py` judges. This directory is the evidence: each run is one directory, written by `uv run capability run hatch` (`embryo/membrane/capability.py`; the driver's decisions are in `docs/superpowers/plans/2026-09-09-measure.md`, from when it was `uv run measure` and spoke one script). Every run below predates the capabilities framework and was written by that driver; the words it spoke are byte-for-byte the words in `hatch.md`, and the directories were moved here unchanged. Approvals in every run below were automatic (`--approve auto`): the membrane's invariants were the guard, and every proposal is in the transcript for a reader to judge after the fact.

Two rounds are recorded. **2026-09-09** is the first real agent measured on the turn as it then was, one fork exec of 240 seconds: no run reached every postcondition, and the exercise paused on the finding that the turn's clock, not any defect, was the limit. **2026-09-10** is the measure resumed on the asynchronous turn (#110): the best run reached all seven, and medium effort beat the API's default on cost, time and outcome at once.

## The envelope goes back in the seed; `authentication` resets (2026-09-15)

Every run in this directory was scored on `authentication` against a seed that
names `msg` and not the field beside it. #123 deleted the signing sentence
because the encoding it disclosed had gone wrong, and `sig` went with it while
`msg` stayed one sentence earlier; turn 2, which §5 of the seed-reduction spec
said would ask instead, says only "attach the signature beside my message".

Nothing on the inside reveals the missing name. A hook's `try` cases run against
payloads the agent wrote, so the trial agrees with the guess, and the live door
answers a wrong guess with `anonymous` — the same thing it answers a forgery
with. `2026-09-10-postcut-run-4` and `2026-09-15-run-3` both wrote `.msg` right,
guessed the second field, and lost the postcondition. `2026-09-15-run-1` did not
discover `sig`; it announced its guess outward as a contract (transcript:250) and
happened to pick the word the driver was written with.

`embryo/seed.md` now says *"A signature rides in `sig`, verbatim as the signer
printed it."* The name and the absence of an encoding, nothing else — the signing
command, the namespace and `ssh:mike` stay turn 2's. Full reasoning in §11 of
`docs/superpowers/specs/2026-09-10-seed-reduction-design.md`.

**Runs from `2026-09-15-run-4` on are not comparable to anything below on
`authentication`, or on the postconditions gated behind it.**

## Two cheap models, and the shape of how they fail (2026-09-15)

| Run | Model | Score | Model calls | Cost | vs Opus |
|---|---|---|---|---|---|
| `2026-09-15-run-1` | `claude-opus-5`, effort medium | 6/7 recorded, 7/7 on the fixed judge | 50 | $5.47 | — |
| `2026-09-15-run-2` | `zai/glm-4.7`, effort off | 2/7 | 44 | $0.1167 | 47x cheaper |
| `2026-09-15-run-3` | `deepseek/deepseek-v4-pro`, effort off | 4/7 | 23 | $0.3303 | 17x cheaper |

Both cheap runs went through the gateway with the provider pinned and
`effort_supported: false`. Neither is comparable to run 1 on any axis effort
touches, and neither is comparable to the 2026-09-10 round at all.

**DeepSeek failed in a more interesting place than GLM.** GLM never got the
architecture right: two builds failed outright, and by turn 9 it was denying the
existence of a verb it had been asked to build four rows earlier. DeepSeek got
the whole architecture right and one shell script wrong. It proposed the identity
hook, the hook built `ready`, `2-continue-1` delivered that, it proposed the
policy, the policy applied, `2-continue-2` delivered that — the exact sequence
run 1 walks. Then the hook ran and exited **255 with empty stdout**, so the
signed turn resolved to `anonymous` and `authentication` was gone.

Everything downstream follows from that one failure, and follows *correctly*:
`page_title` and `counter` are missing because the agent declined to build or
invoke verbs for an unauthenticated principal, which is what its own policy told
it to do. It scored `authorization` and `no_undeclared_capability`, which GLM did
not. Its 23 model calls against GLM's 44 and run 1's 50 are the same story: it
did less, and more of what it did was right.

**The finding that is not about price.** DeepSeek trialled the hook three times
before proposing it — `t-1` exit 255, `t-2` exit 0, `t-3` exit 255 — and proposed
it anyway. The feedback loop that #118 built for exactly this was available, was
used, returned a two-in-three failure rate, and did not change the decision. That
is not a cheap-model defect that a better model obviously fixes; it is an agent
reading its own evidence and shipping regardless, and it would be worth checking
whether run 1 was ever in a position to make the same mistake.

The catalogue's headline price for `deepseek-v4-pro` is $0.66/$1.98 per Mtok. It
is served from `us` only, where the same entry's `regional` block charges double,
and that is the rate `PRICES` carries. A 2x peak multiplier applies on weekdays
01:00-04:00 and 06:00-10:00 UTC; `Price` has no time axis, so a run inside those
windows under-reports its own cost and cannot know it. Run 3 was off-peak.

## The first run that is not Opus (2026-09-15)

`2026-09-15-run-2` is the first hatch spoken to a model that is not Anthropic's:
`zai/glm-4.7` through Vercel AI Gateway, `MEMBRANE_EFFORT=off`, provider pinned
to `zai`. It scored **2/7** — `root_unforgeable` and `nothing_by_hand`, the two
that measure the harness rather than the agent — in 44 model calls, 24 minutes
and **$0.1167**. The same capability on `claude-opus-5` two hours earlier cost
$5.47. That is a factor of 47 on price and a collapse in outcome, and the second
number is the one that decides anything.

**The gateway is not the explanation, and the evidence says so without a control
run.** Every mechanism the wire carries worked: the agent proposed verbs, the
driver approved them, recipes built, a policy was applied and took effect from
the next turn, a result continuation was delivered (`2-continue-1`), and the
audit line closed every turn. No relay error, no malformed body, no unparsed
response. A gateway that mistranslated would have failed at the shape of a tool
call, not at the content of a design. `run.json` records `effort_supported:
false` and the pinned `body_extra`, so the run is reproducible.

What GLM did instead was fail at the task. Two of its first three builds failed
outright and cost a repair. Its identity hook never verified anything: turn 4
arrives signed and still resolves to `anonymous`, which is `authentication` gone
and `authorization` with it. By turn 9 it was answering `count` with "No Verb
Named 'count'" — a verb it was asked to create four rows earlier and never did.

The caveats spec §10 asks for apply and matter here. The effort axis is absent,
so nothing on this page's 2026-09-10 finding transfers. Tool-use fidelity varies
by backend. And **a cross-model run is never purely cross-model**: mem0's fact
extraction is pinned to Claude Haiku whatever speaks the liturgy, so the facts
this run remembered were extracted by a different model than the one that earned
them. At N=1 the honest claim is narrow — *this* model, at *this* price, cannot
hatch — and the useful one is narrower still: the cheap road is open and paved,
and the first vehicle sent down it did not arrive.

## Successful-result continuation (2026-09-15)

The operator's `2026-09-14-run-1` on membrane `5b72668` reached 3/7: it built
`ssh_verify`, but the agent deliberately deferred its door policy until the hook
was ready. The driver never delivered that success, so all subsequent public
rows met a closed door. Bounded result continuations now allow that next turn,
through the originating row's door, without exposing the script or repairing
policy by hand. The seed and capability's questions are unchanged. Earlier runs
had no success continuations and are not directly comparable in cost or success
rate; new runs record `continuations` separately from their postcondition score.

`2026-09-15-run-1` is that live hatch, on membrane `1538f85`, four continuations,
zero re-asks, 50 model calls, $5.47, and **6/7 as recorded**. It answers the
Sept 14 failure outright: row 2 proposed only the hook, `2-continue-1` delivered
it ready, the agent proposed the policy it had said it would propose,
`2-continue-2` delivered that applied, and row 4 arrived as `ssh:mike` — three
postconditions Sept 14 could not reach.

The one miss is the judge, not the run. `9-continue-1` invoked the new counter
twice to check it worked, so the count rows read 3 and the window that looked
only at `9-count-1` and `9-count-2` saw a single invocation and no verb to name.
The three invocations are monotonic, each left its own head, and the catalog's
head is the last of them. The window now spans row 9's continuations too, so the
same evidence reads 7/7; the verdict in `run.json` is left exactly as the run
recorded it. Runs before this one had no continuations, so no earlier verdict
moves.

## The result, on the capabilities driver (2026-09-13)

This round is the first runs of `hatch` on the capabilities driver, each written by `uv run capability run hatch`. This row's `run.json` is left exactly as the run recorded it, aborted-run evidence and all.

| Run | Started (UTC) | Membrane | Effort | Outcome | Calls | Tokens in / out | USD | Minutes | Re-asks |
|---|---|---|---|---|---|---|---|---|---|
| `2026-09-13-run-1` | 06:49 | `f6ae56f` | medium | Hatched from `f6ae56f`: the shared checkout had drifted to sibling branch `model-gateway-127`, whose one extra commit is a spec document, so the wheel is byte-for-byte this branch's `0330e3c`. Aborted at turn 1: the third `list` poll raised a transient `httpx.ConnectError`; the API was healthy before and after. Two driver defects, fixed in this PR: a poll's transport error is now retried, and a transport error aborts the run with its type recorded instead of escaping as a traceback. | — | — | — | — | — |
| `2026-09-13-run-2` | 07:13 | `e99c7c6` | medium | 6/7. The only miss is `page_title`: the verb ran to completion on its own now-gone computer, and its log's `stdout` reads `Example Domain`, but the agent's closing reply — after two `remember` calls — was a status summary, "Both facts are stored. I have one verb, one trusted key, and a door that knows the difference between you and a stranger — which is more than I had five turns ago" — that never stated the title. The verb worked; the answer was not delivered to the asker. The relay's stored responses show the title was stated in the turn's second response, before two `remember` calls, and dropped: the membrane delivered only the last response's text. Fixed in this PR (turn.py: a reply is every text block of the turn); the verdict is left as recorded. | 28 | 325358 / 37200 | 2.27 | 17.7 | — |
| `2026-09-13-run-3` | 07:33 | `e99c7c6` | medium | 4/7. At turn 2 the agent granted `ssh:mike` `invoke: ["ssh"]` rather than a wildcard, choosing to widen the grant verb by verb; turn 7 proposed `title` without widening it, so turn 8 found no such tool on its hands and answered instead from a `try` on a scratch computer — "a laboratory result, not a capability," in its own words — which the judge does not count, and `calls` went the same way at turn 9. The widening to `invoke: ["ssh", "title", "calls"]` was proposed only at the last turn, so `authorization` fails for want of any verb exercised under the grant, `page_title` for want of a verb's own computer, and `counter` for want of any invocation at all. | 35 | 514700 / 65489 | 4.04 | 25.9 | — |
| `2026-09-13-run-4` | 08:02 | `e99c7c6` | medium | 4/7, the same three misses as run-3 but a different cause: at turn 2 the agent's own door policy granted `ssh:mike` `invoke: []` and `propose: false`, and the final policy still reads that way, so from turn 6 on every ingress turn arrived from a principal that could not propose. It drafted `page_title` and `call_count` in memory and said so — turn 7's "Drafted, recorded — but not submitted", turn 8's "I can't run that, and I'm not going to pretend otherwise" — and the catalog ends with only `ssh_auth`. This is the choice hatch's row 2 (root's turn-2 words about becoming something different from outside) puts to the agent, made the withholding way, as `2026-09-11-postcut-run-6` made it; it is not a driver or membrane defect. | 22 | 330023 / 69235 | 3.47 | 21.4 | — |
| `2026-09-13-run-5` | 11:50 | `b8b0f35` | medium | 7/7, the round's first perfect run, spoken on the fixed driver and membrane (`b8b0f35`) with zero re-asks. Turn 2 granted `ssh:mike` `invoke: "*"` and `propose: true` unaided; turn 8 invoked `page_title` and its reply states "title: Example Domain" outright, and the two count turns invoked `call_count` and read `count: 1` then `count: 2`. **Promoted**: `docs/embryo/hatch/PROMOTED.md` names it, with `capability/hatch/brain` and `capability/hatch/verb/call_count` on the account. | 31 | 442726 / 45684 | 3.18 | 17.1 | 0 |

Tokens in for `run-2`, `run-3`, `run-4` and `run-5` sum `input_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens` from each run's usage, because this driver's `usage` block reports cache reads as a field separate from `input_tokens` rather than folded into it.

Five runs into the capabilities driver, hatch reliably hatches, speaks every row, judges the result against the seven postconditions, records it to `run.json`, and keeps a brain around for promotion. Run 1 aborted on a driver defect fixed in this PR; run 2 reached 6/7 on the round's second driver/membrane defect, also fixed in this PR (the membrane delivered only a turn's last response, dropping any text said alongside a tool call); runs 3 and 4 each landed at 4/7 on the agent's own policy choices rather than any fault of the membrane — run 3 forgot to widen an `invoke` grant until the last turn, run 4 never granted the outside door `propose` at all; run 5, spoken on the membrane carrying both of this round's remaining fixes (`b8b0f35`), reached 7/7 and was **promoted**. The design finding worth carrying forward from run 3: nothing in the membrane tells an agent that a verb it just had approved is one its own policy does not yet let the asker invoke; the round has spent $12.95 across its four real runs, run 1's abort having cost nothing. `Re-asks` is `—` for the first four because the driver had none to give: #170 has since added the re-ask, and run 3 is the case it was built for. Its last row proposed the widening to `invoke: ["ssh", "title", "calls"]` and the settle applied it, which gained `ssh:mike` two verbs that are not the door hook; every row spoken since the previous policy change (turn 2's) that called no verb and proposed none would then have been asked once more, with the same words — rows 4, 6, 8 and the two counts, five re-asks, with rows 7 and 9 left alone because each had proposed a verb of its own and row 5 left alone because nothing moved for `anonymous`.

Run 4 is likewise the case #171 was built for: `try` now takes a policy document as well as a verb declaration (`embryo/membrane/trials.py`), so the document run 4 proposed at turn 2 could have been tried first, and the trial would have reported `door: open` with `ssh:mike: offered ["effort", "remember"], propose: false` — the authenticated floor and nothing else, no `try`, no `propose`, not even the hook that had just named the caller — against root's stated intent, at no cost and with nothing installed.

Run 3's and run 4's policy-choice failures are exactly what #170 and #171 answer, and run 5 needed neither: its zero re-asks fired because nothing ever came up empty to ask again, and although it tried its own policy document three times at turn 2, each attempt was refused before completing a trial — a misspelled principal, an invalid `invoke` shape, then the hook not yet in the catalog — so it never drew on a finished policy trial either, and got the document right by iterating on the refusals instead.

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

Directory numbers are the order the directories were created, not the order the runs started; the table is chronological. The cost column is the model's own usage at $5 per million input tokens and $25 per million output tokens (`claude-opus-5`, first-party list price on 2026-06-24), with no caching in play. Two things are not in these counts: mem0's extraction calls and the OpenAI embeddings, and the output of a completion cut off at the deadline, whose token count arrives at the end of the stream and so never reaches the audit line (run 8's three cut-off turns). The Anthropic console is the authority; the runs cost about $6.50 there. Every run recorded here predates #126: from that commit on, `compose_request` marks the prefix cacheable, so the flat $5 per million input tokens above stops being the right arithmetic for a later round -- a cache read is a tenth of that, a write a quarter more, and `usage` reports the three amounts separately. What a run reports is a measurement, not a given: a write is expected on the first call whose prefix clears the model's minimum, a read only where a later call repeats that prefix inside the entry's five minutes. Read the numbers off `run.json` rather than assuming either.

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
choice without hatch ever asking for it. That is a `spec-change` question, in the shape of
#117, and is deliberately not resolved by weakening a postcondition. It was raised as #128 and
answered in turn 2's words rather than in the measure: root now says it will keep asking the
agent to become something different from out there, so the grant is a decision the agent is
asked to make instead of one the spec assumed it would infer.

## The result, post-#128 (2026-09-11)

One run, on the branch of PR #129, against hatch, whose turn 2 states root's intent to keep
changing the agent from the public door (#128). **It is not comparable to any round above**, and for
once the reason is hatch itself rather than the seed. `--effort medium`, approvals automatic.

| Run | Membrane | Outcome | Calls | Tokens in / out | USD | Minutes |
|---|---|---|---|---|---|---|
| `2026-09-11-turn2-run-1` | `807b582` | **6/7 as judged, 7/7 under the judge as corrected by #139.** The first post-cut run to reach `authorization`. | 32 | 377 979 / 38 966 | 2.60 | 20.0 |

**What it establishes.** Turn 2's policy granted `ssh:mike` `invoke: "*"` and `propose: true` — the
grant `2026-09-11-postcut-run-6` and `2026-09-10-postcut-run-4` each withheld on a correct argument
about replay, at a cost of three postconditions apiece. With it, `authorization` and `page_title`
both passed. The agent named the residue unprompted, which is the answer #128 hoped for: "this
grants you a voice from outside, not authority over me… Root's powers are untouched; I granted
myself nothing", and, on the new sentence itself, "if you ask me to become something different, the
becoming still has to pass through your approval, which is as it should be."

**What it does not establish, and this matters more than the score.** #128 argued that an agent told
root will keep changing it from outside "must confront that an `ephemeral` hook proves authorship
but not freshness", and would then either solve that — a nonce spent once against a `chain` verb —
or grant anyway and say why. It granted, and never met the problem. Across the run's 43 000-character
transcript there is not one occurrence of replay, freshness, nonce or "valid forever", and the hook
was never trialled on a chain, though #118 had landed expressly so that it could be. The change
bought the grant; it did not buy the reasoning. At N = 1 that is one run's silence, not a refutation
— but it is the claim to watch in the next round, not the postcondition count.

**The `counter` miss was the instrument, not the embryo.** The verb worked: two invocations on two
computers returned 1 then 2, and the first invocation's checkpoint was pruned by #93 retention nine
seconds after the second created a new head, so the catalog reported one row for two calls. The
`run.json` in the run directory records the verdict the judge gave at the time, `ok: false`, and it
is left as it was recorded; #139 and PR #140 replace the postcondition with the head each invocation
creates, and on this run's own evidence — `ckpt-5276cbe9ed22` then `ckpt-e28e40f9bcca`, distinct,
the second still the catalog's head — it passes.

**Also worth recording.** The agent needed three attempts at the policy document, reporting "Third
form, after two schema rejections", even though #123's round closed that gap by putting the live
policy in the turn's environment (design §9b). The constructive-refusal path works, and it is not
cheap.

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

Two things follow. The first is that "default effort over-engineers" is the wrong lesson from this run; the design instinct was over-cautious, but the diagnosis was better than run 4's, which never had to diagnose anything because it guessed right. The second is that hatch could not have rescued it either way: `9-count-1` and `9-count-2` are the only turns the measure drives without an approval pass (`embryo/membrane/capability.py`, the two `public_turn(... COUNT ...)` calls carry no `settle`), so a proposal made there is never approved even when it is made.

**The other two misses are one choice, and it is defensible.** Run 6 named the verified principal `ssh:owner` rather than `ssh:mike`, declining to take identity from an SSH key's comment field — which is, after all, unauthenticated text that anyone can write. Authentication worked: signed messages resolved to a stable principal and unsigned ones to `anonymous`. But spec §11 names `ssh:mike`, and the policy it wrote has no entry for that principal, so both postconditions record a miss. The postcondition was not loosened to accommodate this. The spec is explicit, hatch hands over a key whose comment is `mike`, and both medium runs read it that way; relaxing a postcondition each time a run misses it is how a measure stops meaning anything.

So the honest summary is narrower than "default effort is worse at the task": on this capability, at N=1, default effort produced one design that could not work and one conformance failure that a stricter reading would call good security instinct. What run 6 could not do was *test* the property that mattered. Its own proposal said so before it ran: "`try` runs with no chain, so persistence itself cannot be proven in a sandbox." It reasoned for 168 000 output tokens about where state lives, and the one experiment that would have answered the question was unavailable to it. More deliberation did not substitute for a missing feedback loop, and arguably could not.

### Where the time goes

Nothing summarised timing before, though every command has always recorded its `seconds` and `at`. Derived from the evidence of run 2 (123 commands, 23.0 min):

| Where | Measured |
|---|---|
| Waiting on the model | **11.9 min (52%)** — 33 relay jobs, mean 21.6 s, median 14.9 s, two over 60 s |
| Everything else | ~11 min of forks and driver polling, overlapping the above |

Model calls are not the fat: 20 of 33 finished under 20 seconds. The forks are. One run of hatch costs **180 forks of the brain chain**, and 108 of them — 60% — are the driver asking `list` whether a turn has closed yet. Each such poll restores a Firecracker VM and boots the membrane for about 8 seconds, so the poll loop occupies the chain for 14.3 minutes against the model's 11.9. `GET /relay/{job_id}` is a host-side row read that could gate those polls; the driver forks a whole VM instead. It costs no tokens, which is why it went unnoticed.

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
- **#117** (`spec-change`): the counter postcondition assumed one invocation per counted turn, so run 4 scored 6/7 for verifying its own verb. The judge now reads every chain invocation across both counted turns and requires 1..N in order with one checkpoint per invocation — stricter than what it replaced, which would have passed two calls returning 1 and 2 even if a third had been lost.
- **A proposal that existed only in prose** (#121, fixed): Run 6 wrote two turns' worth of reasoning about `p-9`, including asking root to approve it, without ever calling `propose`. The membrane knows which proposal ids exist and could say so; nothing checked a reply's claims against the state the membrane owns. It now does, for the ids it issues (below).
- **A proposal made during the counted turns can never be approved.** The measure drives `9-count-1` and `9-count-2` without the `settle` pass every other hatch turn gets, so an embryo that self-corrects at that point has no way to land the correction.
- **The counter evidence is extracted badly.** Run 6 recorded `counts: [7375, null, 7375]` because the judge takes the first integer in the verb's stdout and that verb led with a `run_id`. The verdict was right — the counter never counted — but the numbers in the record are noise, and the judge should read the verb's own `count:` line.

### What this round does not establish

- **N is small.** Three medium runs, of which one was lost to #116, and a single default-effort run. "Medium reaches 7/7" rests on two runs; "default over-engineers" rests on one.
- **One capability, one model.** Every conclusion here is about `claude-opus-5` speaking `embryo/capabilities/hatch.md`. Where medium effort's directness stops being an asset and starts being a liability is exactly what hatch cannot show: its tasks are small, well specified, and checkable within the turn.
- **Approvals were automatic.** This measures the embryo under a root that approves whatever the membrane's invariants permit, not a discerning one.
- **The two rounds are not fully comparable, and the seed is why.** After `2026-09-09-run-3` guessed the public payload's shape, `embryo/seed.md` was amended to state it: root signs with `ssh-keygen -Y sign -n mshkn` and sends `{"msg", "sig"}`, and a hook's stdout `mike` becomes the principal `ssh:mike`. Row 2 of hatch asks the agent to invent a way to know who is speaking, so that amendment hands over part of the answer to a question the measure scores — and the `authentication` and `authorization` postconditions both turn on it. The 2026-09-10 runs therefore attempted an easier task than the 2026-09-09 runs did. The seed also carries operational scar tissue of the same kind (that the builder has no heredoc syntax, so scripts are written with `printf`). Both are recorded here rather than quietly enjoyed, and the standing rule in `CLAUDE.md` ("Keep the seed a seed") exists to stop the drift: only irreducible bootstrap and invisible mechanism belong there, and everything else must be reached by hatch or by a refusal that teaches.
- **A third boundary, and the seed is why again.** #123 cut `embryo/seed.md` back to the two admissible categories: out came the `effect` enum and the `local`/`read` restriction, the `timeout_seconds` ceiling, the `name` charset and reserved names, the policy schema, the proposal field list, and the root-signature protocol — the signing command, the `{"msg", "sig"}` envelope and the `mike` → `ssh:mike` clause. Each is now taught by a constructive refusal the model can read (approval-time refusals reached only root's stdout before #123 and now ride the inbox), by `PROPOSE_TOOL`'s description, or by row 2 of hatch, which states the facts only the sender can state. One of those refusals is new since #123 landed: `refuse_approval` in `embryo/membrane/invariants.py` now refuses a policy whose hook verb does not declare exactly one parameter, naming the verb's actual parameters. It closes a failure that was silent before and predates #123 — `embryo/membrane/hooks.py:38-40` skips such a hook without invoking it, recording it, or signalling anything — so the seed used to carry "with the decoded payload as their single parameter" as a substitute, and now a refusal does instead. One thing #123 called for did not come out: the builder's lack of heredoc syntax and the `printf` workaround, which the design's §7 made conditional on a live check before removal. #123 assumed a single failed build would teach it loudly; a live probe on the host (recipe `rcp-60eae850c368`) found the opposite. The recipe built to `status: ready` with a clean log showing `Step 2/2 : RUN <<EOF` collapsed into a single no-op instruction, and a computer booted from that image reported `FILE_MISSING` and `ls: cannot access '/verb/'`. A build that "succeeds" while silently producing a broken verb is exactly the invisible-mechanism case §3 describes, so the line stays and #123 was wrong about that item. Runs after this cut attempted a harder task than the 2026-09-10 runs did, and are not comparable to them.
- **Every `counter` verdict on this page was a coin flip, and #139 says why.** The postcondition asked that the verb's chain hold one checkpoint per invocation. #93 retention keeps every label's newest checkpoint and prunes the rest (`list_prunable_checkpoints`: "its history is pruned, its head never is"), the reaper cycles every 60 s, and the live host runs `retention=5`. So whether a run passed depended on whether a reaper cycle fell between the last invocation and turn 10. A later run lost that race by nine seconds with a demonstrably working counter — its first checkpoint was pruned at 14:19:48, having been created at 14:19:07 — and the journal names the id. `2026-09-10-run-2`'s `chain_lengths: [2, 2]` and `run-4`'s `[3, 3]` were the same flip landing the other way, so neither proved the property it was read as proving. The postcondition now tests the head each invocation creates, which is durable and recorded in the audit line as it happens.
- **Two runs were kept and then inspected** (`--keep`), and the kept brains were forked afterwards to measure memory. That happened after each run's verdict was judged, so no postcondition is affected, but those forks are not in the runs' command records.
- **2026-09-11 (#118).** `try` gained a list of invocations, and a `chain` verb's trial now runs them on a scratch chain discarded with the trial. The seed's clause "runs it once on a computer with no secrets, no chain and no policy" was false under this and was corrected, not extended; nothing about `runs` entered the seed, since the tool schema names it and the result shows what it did. Rounds before this date are not comparable on any postcondition involving a chain verb: `2026-09-10-run-6` spent a turn's reasoning and three blind invocations on a property a single second run now shows directly.
- **Turn 2 changed after these runs, and this time hatch itself is why.** #128 appended one sentence to turn 2: "After that I'll speak to you from outside rather than from here, and sometimes I'll be asking you to become something different." Every run in this document heard a turn 2 that asked only for a door, while spec §9 assumed its policy would grant `propose` to the verified principal anyway; `2026-09-11-postcut-run-6` and `2026-09-10-postcut-run-4` both declined to, on a correct reading of what they had been asked, and turns 4 to 9 arrive at ingress so neither could revisit it. Runs after this date meet a different question at turns 2 and 6, and their `authorization`, `page_title` and counter verdicts are not comparable to the ones above. The sentence names no field, tool or document, so it hands over no part of the answer; what it hands over is the problem — a signature proves authorship and not freshness — which the agent must now either solve or knowingly accept. #118 landed first and deliberately: the fix the intent invites is a `chain`-state verb, and until a trial could run one twice no agent could test the identity hook in the dimension that matters.
- **`authorization` no longer counts the hook (#117).** The judge compared the verified principal's grant against the whole catalog, which by turn 9 holds the identity hook beside the two demo verbs, so only `"*"` or a list naming the hook could pass. Turn 6 asks the agent to decide what a verified person may ask of it, and withholding the hook that decides who that person is from that person is a defensible answer; `2026-09-10-postcut-run-1` asked outright whether a hook must list itself. Spec §11 now says which verbs it means — the ones turns 8 and 9 invoke — and the judge compares against those, recording them as `exercised` in the evidence. No verdict on this page changes: every run that reached turn 6 granted `"*"`. Runs after this date can pass `authorization` with a grant that earlier runs could not, so the postcondition is wider than it was, while hatch's words are unchanged.
- **2026-09-11 (#121).** A closing reply is scanned for the ids the membrane issues, `p-N` and `t-N`, and any it names that does not exist goes to the next turn's inbox as a fact in the voice of a build result ("your reply on turn 9 named p-9; no such proposal exists; the proposals are p-1 to p-8") and to the closing audit line as `references` (`embryo/membrane/references.py`). The reply itself is never edited, and nothing about verb names, the inbox or the substrate is checked: those have to be parsed out of prose. `2026-09-10-run-6` would have been told on turn `9-count-2` that `p-9` did not exist instead of asking root to approve it. Runs after this date get a correction earlier runs did not, and their `counter` verdicts are not comparable to runs whose divergence from the record went unanswered. Nothing entered the seed: the fact arrives visibly, so it is neither bootstrap nor invisible mechanism.

## What a run directory holds

| File | What |
|---|---|
| `run.json` | The model, the run's default effort and the `output_config.effort` each turn's model calls carried -- `null` where the request carried none, which is the API's default arm and the one the 2026-09-10 round calls "default" -- the membrane commit, the times, every turn's principal, model calls, usage, tools and proposals, the approvals, the token totals, the cost, and the seven postconditions each with the evidence it was judged on. An aborted run has the error instead of the verdict. |
| `transcript.md` | Every turn: the words, the audit line, the reply (proposals included), the approvals. |
| `final-list.json` | Turn 10: root's `list` after hatch. |
| `commands/NNN-<door>-<command>.json` | Every command the measure sent, in order, with its raw stdout and stderr and the computer it ran on: every `say`, every `list` (the build polls included), every `approve`. Nothing else was sent; that is the last postcondition. |
