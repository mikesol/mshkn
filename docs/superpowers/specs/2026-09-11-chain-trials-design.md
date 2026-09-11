# Chain trials: letting `try` show a verb whether its state persists

Date: 2026-09-11. Closes #118. From the measure's evidence (#111) and the post-cut
round of #123 (PR #125). Blocks #128, which must not land first.

## 1. Why

`try` builds a verb declaration and runs it **once** on a computer with no secrets,
no chain and no policy (`embryo/membrane/trials.py`, spec §3 and §5 of
`docs/superpowers/specs/2026-09-08-embryo-design.md`). For a `chain` verb that makes
the one property distinguishing it from an `ephemeral` one — that its disk survives an
invocation — exactly the property a trial cannot exhibit. Every trial starts from the
image.

Two live runs said so themselves. `2026-09-10-run-4`: "Persistence across invocations
is *precisely* the property the sandbox cannot show me — I've proved the arithmetic and
proved nothing about the chain." `2026-09-10-run-6` reached the same conclusion, spent a
turn's reasoning, a retracted verb and three blind invocations establishing by inference
what a second run would have shown it directly, and never proposed the verb it had
designed.

The chain verb that matters is not turn 9's counter. `2026-09-11-postcut-run-6` found
that a replay-protected identity hook — the fix for the liturgy's largest remaining gap,
and what #128 pushes every agent towards — needs "a nonce or monotonic counter inside the
signed `msg`, checked against a `state: chain` verb so each signature is spent once". An
identity hook is the last declaration anyone should propose with its persistence
unverifiable.

## 2. What changes, in one sentence

`try` takes an optional list of parameter sets and runs one invocation per entry, in
order; for a `chain` verb those invocations share one scratch chain that is discarded
with the trial.

Nothing about the authority model changes. A trial still holds no secret, passes no
policy check, enters no catalog, and can be invoked by nobody. What it gains is a chain
of its own, which is not the verb's.

## 3. The tool contract

`try(verb, params?, runs?)`.

| Input | Meaning |
|---|---|
| `runs` | Optional array of parameter objects, one per invocation, run in order. |
| `params` | As today. Absent `runs`, the trial is `[params or {}]` — one invocation, today's behaviour exactly. |

Refusals, both constructive and naming what would have been valid:

- `params` and `runs` together: "give `params` for one invocation or `runs` for several, not both".
- `runs: []`: "`runs` must name at least one invocation".

### `runs` is not restricted to `chain` verbs

#118 guessed it should be. It should not. One uniform rule — each entry is one
invocation, in order — removes a special case, removes a refusal the model must learn,
and lets the model read the difference between the state kinds rather than be told it.
For `ephemeral`, each invocation is a fresh computer from the recipe and nothing carries;
for `chain`, all of them run in order on the scratch chain. An ephemeral verb answering
`1` twice beside a chain verb answering `1` then `2` is the clearest available lesson,
and it costs one extra computer.

### The result

One shape for every trial, single or multiple:

```json
{"trial": "t-3", "status": "done", "build_log": "…",
 "runs": [{"status": "ok", "computer_id": "…", "exit_code": 0,
           "stdout": "1", "stderr": "", "chain_head": "ckpt-…"},
          {"status": "ok", "computer_id": "…", "exit_code": 0,
           "stdout": "2", "stderr": "", "chain_head": "ckpt-…"}]}
```

The top-level `status` is one of `invalid` (the declaration is not a declaration),
`failed` (the recipe did not build, with the log tail), `building` (built too late in the
turn to run; the next turn's poll runs the whole list), `done`, or `out of time` (§5).
Each entry of `runs` carries its own `status`, `ok` or `error`, as `invoke` already
reports one.

`chain_head` appears only for a `chain` verb, mirroring `invoke` in
`embryo/membrane/verbs.py`. It is the field `2026-09-10-run-6` read to infer that the
platform was chaining at all; a trial should show it too.

There is no flat single-run form. One shape is one thing to learn, and the project keeps
no backwards compatibility (pre-alpha, zero users).

### What stops a sequence

A non-zero exit code does **not**. It is a legitimate reading, and for the replay-protected
hook that motivates this work it is the expected reading of run 2: the same signature
presented twice must be accepted once and refused once. The sequence runs to the end and
reports every exit code.

An entry whose `status` is `error` does stop it — a mshkn error, a `Deferred` reply, or a
rendering failure means the next invocation would measure nothing. The runs collected so
far are returned with the error among them.

## 4. The scratch chain

Label `verb/trial/<trial id>`, e.g. `verb/trial/t-3`.

- It falls under `verb/`, the brain key's only label prefix (`embryo/hatch.sh`), so the
  scoped key may create, fork, list and delete it.
- Verb names match `[a-z0-9_]+` and contain no slash, so this label can never equal any
  `verb/<name>`. The verb's own `chain` field is ignored by the trial. Trialling a
  supersede of a catalogued verb therefore cannot touch the live chain, which answers
  #118's collision question without a uniqueness check.
- Trials cannot share one chain: a fork reuses the filesystem of the checkpoint it forks,
  not a new recipe, so a second trial's code would never run. One label per trial is
  forced, not chosen.

The invocations are the two calls `invoke` already makes:

1. Run 1: `POST /computers` with `recipe_id`, the rendered entrypoint, `needs`,
   `self_destruct: true` and `label` set to the scratch label.
2. Runs 2..n: `POST /checkpoints/fork` with that label, `exclusive: defer_on_conflict`
   and `self_destruct: true`.

A `Deferred` reply should be impossible on a label only this trial knows. If one arrives
it is recorded as that run's result and the sequence stops, the way `invoke` reports one.

### The sweep

#93 retention keeps the newest checkpoint of every label forever, so an unswept scratch
chain is a permanent leak. When the runs finish, the membrane lists the label's
checkpoints and deletes each one. This adds an eighth call to `embryo/membrane/mshkn.py`,
`delete_checkpoint`, whose module docstring says seven; a scoped key may call
`DELETE /checkpoints/{id}` when the checkpoint's label is under its prefixes
(`src/mshkn/api/scopes.py`). Deleting a parent whose child still exists is the path
`prune` already takes, so no ordering constraint applies.

A turn can die between the last run and the delete, and an out-of-time sequence skips the
sweep deliberately (§5). So `poll_trials`, at the start of the next turn, sweeps any trial
that has a chain and is not yet swept. The sweep is idempotent: a label with no
checkpoints is swept by doing nothing.

## 5. The clock

A turn's deadline is 240 s and `RUN_MARGIN` is 20 s. Nothing caps the length of `runs`
but the clock. The membrane runs invocations in order while at least `RUN_MARGIN`
remains, giving each the whole remaining budget as its timeout, then stops and returns
what it has:

```json
{"trial": "t-3", "runs": [{…}, {…}], "status": "out of time", "ran": 2, "of": 5}
```

Runs 1 and 2 still prove persistence — partial evidence is evidence — and the model reads
what its request cost instead of being told a number no experiment would reveal. A cap of
2 or 3 in `declarations.py` was the alternative and was rejected on that ground.

The out-of-time path attempts no sweep: it would spend time the turn does not have. The
next turn's poll does it.

## 6. State

`Trial` in `embryo/membrane/state.py` becomes:

| Field | Was |
|---|---|
| `runs: list[dict]` | `params: dict` |
| `results: list[dict]` | `result: dict \| None` |
| `build_log: str \| None` | carried inside `result` |
| `chain: str \| None` | — the scratch label, `None` until run 1 is attempted |
| `swept: bool` | — |

`to_doc` and `from_doc` follow. `state.json` lives on one brain's disk and the project
runs no migrations for it.

`_describe` renders one block per run for the inbox item, so a trial whose build outlasted
the turn reports every invocation on the next one.

## 7. The seed

The seed's clause becomes false the moment this lands, and a false line in the genome is
worse than a missing one. It is corrected, not extended:

> A fourth tool, `try`, builds a verb declaration and runs it on computers with no secrets
> and no policy. A `chain` verb's trial runs on a scratch chain that is discarded with the
> trial. Use it to test a declaration before you propose it. It installs nothing.

Under the #123 contract this is **invisible mechanism**: no experiment reveals that the
trial's chain is scratch and thrown away, and without the sentence a model must assume a
trial leaves state it is answerable for.

Nothing about `runs` enters the seed. The tool schema names the parameter and the result
shows what it did, so the mechanism teaches it — which is the case where #123 says the
seed stays silent.

`docs/embryo/README.md` records the amendment and says which rounds stop being comparable.

## 8. The spec

Edits to `docs/superpowers/specs/2026-09-08-embryo-design.md`:

- §2, decision 4: `try` tests a declaration "on a computer with no authority" — still
  true, now plural.
- §3, closing sentence: "a trial runs on a computer with no secrets, no chain and no
  policy, and installs nothing" is false and is replaced.
- §5, `try`: the paragraph describes one computer and lists "touch a chain" among what a
  trial cannot do. What a trial cannot touch is *the verb's* chain.
- §12, the `/brain/seed.md` inventory row: the invisible-mechanism list names "`try` runs
  with no secrets, no chain and no policy".
- §12, the flow-tier sentence: the deterministic proof gains a chain trial.

`tests/unit/test_docs.py` polices every path, module, route, metric and variable these
documents name.

## 9. Tests

**Unit** (`tests/unit/test_embryo_trials.py` unless noted):

- A `chain` verb's runs use `POST /computers` with `label=verb/trial/<id>` for run 1 and
  `POST /checkpoints/fork` for each later run.
- An `ephemeral` verb's runs use a fresh computer with no label every time.
- `chain_head` is present on a chain trial's runs and absent on an ephemeral one's.
- The sweep deletes every checkpoint under the scratch label and sets `swept`.
- An out-of-time sequence returns `ran k of n`, attempts no delete, and the next turn's
  `poll_trials` sweeps it.
- A trial that died after its runs and before its sweep is swept by the next poll.
- `params` and `runs` together, and `runs: []`, are refused with the reason.
- `tests/unit/test_embryo_mshkn.py`: `delete_checkpoint` against the fake client.
- `tests/unit/test_embryo_state.py`: the `Trial` round-trip through `to_doc`/`from_doc`.

**Flow** (`tests/flow/test_embryo_liturgy.py`): the scripted model trials a chain counter
with two runs before turn 9's proposal, reads `1` then `2`, and the scratch label lists
zero checkpoints afterwards. This is the deterministic proof of the thing run 6 could not
do.

**E2E** (`tests/e2e/test_phase14_embryo.py`): one test proving a chain trial on the live
host. The gate line moves from **170 passed, 6 skipped, 4 failed** over 180 to
**171 passed, 6 skipped, 4 failed** over 181; `CLAUDE.md` and
`docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` carry the number.

## 10. Out of scope

- #128 (turn 2 stating root's intent) stays blocked until this lands.
- Per-verb egress scoping for trials (spec §14) is unchanged.
- The vault (#91): a trial still holds no secret, and a verb whose `requires` is non-empty
  is still blocked at approval, not at trial.
- Host-side garbage collection (#75): the membrane sweeps its own scratch chains, so #75
  gains no work from this.
