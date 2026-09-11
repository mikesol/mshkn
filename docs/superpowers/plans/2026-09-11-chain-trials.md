# Chain Trials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `try` run a declaration several times in order, so a `chain` verb can be shown the one property that makes it a chain verb — that its disk survives an invocation.

**Architecture:** `try` grows an optional `runs` list of parameter objects, one invocation per entry. A `chain` verb's invocations share one scratch chain labelled `verb/trial/<trial id>`, created by `POST /computers` with that label and advanced by `POST /checkpoints/fork` — the same two calls `membrane/verbs.py:invoke` already makes. The membrane deletes the scratch chain when the trial ends, and sweeps any unswept one at the start of the next turn. Nothing about the authority model changes: a trial still holds no secret, passes no policy check and enters no catalog.

**Tech Stack:** Python 3.12, `httpx`, `pytest` + `pytest-asyncio`, `ruff`, `mypy`. Everything runs through the project venv as `uv run <tool>`.

**Spec:** `docs/superpowers/specs/2026-09-11-chain-trials-design.md`

## Global Constraints

- **Package manager is uv.** Every tool runs as `uv run <tool>`. Never call `pip`, `poetry`, `python` or `pytest` directly.
- **The gate, before every commit you would show anyone:** `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov`. Coverage floor is 98 % (`fail_under` in `pyproject.toml`). Zero warnings is part of green.
- **No backwards compatibility.** Pre-alpha, zero users. Replace the old shape; do not keep a fallback path or a v2 beside it.
- **No xfail, no weakened assertions, no skips to get green.** A failing test is an honest reminder; a test with no assertion is worse than a failing one.
- **`tests/unit/test_docs.py` fails when a document names a path, module, route, metric or variable that does not exist.** Fix the document, not the test.
- **Scratch label format is exactly `verb/trial/<trial id>`** — e.g. `verb/trial/t-3`. It must start with `verb/` (the brain key's only label prefix, `embryo/hatch.sh`) and must contain a `/` after that prefix so it can never equal a `verb/<name>` chain (verb names match `[a-z0-9_]+` and hold no slash).
- **`RUN_MARGIN` is 20.0 seconds** and is the per-invocation floor: an invocation starts only while at least that much of the turn remains.
- **The E2E gate line does not move.** It stays 170 passed, 6 skipped, 4 failed over 180. Do not add an E2E test; Task 8 strengthens an existing one.

---

### Task 1: `delete_checkpoint`, the eighth mshkn call

The membrane speaks to mshkn through `embryo/membrane/mshkn.py`, which declares a `MshknApi` Protocol, a real `Mshkn` client, and (in `tests/support_embryo.py`) a `FakeMshkn` the unit tier runs against. All three must learn the same call. `DELETE /checkpoints/{id}` already exists in the product (`src/mshkn/api/checkpoints.py`) and a scoped key may call it when the checkpoint's label falls under the key's prefixes (`src/mshkn/api/scopes.py:require_checkpoint_label`).

**Files:**
- Modify: `embryo/membrane/mshkn.py` (module docstring, the `MshknApi` Protocol, the `Mshkn` class)
- Modify: `tests/support_embryo.py` (`FakeMshkn`)
- Test: `tests/unit/test_embryo_mshkn.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `async def delete_checkpoint(self, checkpoint_id: str) -> None` on `MshknApi`, `Mshkn` and `FakeMshkn`. `FakeMshkn` also records `("delete_checkpoint", {"checkpoint_id": ...})` in `self.calls` and removes the id from every list in `self.chains`, dropping a label whose list becomes empty.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_embryo_mshkn.py`. That file already has a `_client(handler)` helper that wraps `httpx.MockTransport` — use it; do not build a second one. Add `FakeMshkn` to its imports (`from tests.support_embryo import FakeMshkn`).

```python
async def test_delete_checkpoint_calls_the_route() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "deleted"})

    api = _client(handler)
    await api.delete_checkpoint("ckpt-1")
    assert seen == [("DELETE", "/checkpoints/ckpt-1")]
    await api.aclose()


async def test_delete_checkpoint_raises_on_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(403, json={"detail": "Scope labels does not cover 'verb/trial/t-1'"})

    api = _client(handler)
    with pytest.raises(MshknError) as exc:
        await api.delete_checkpoint("ckpt-1")
    assert exc.value.status == 403 and "Scope labels" in exc.value.detail
    await api.aclose()


async def test_the_fake_forgets_a_deleted_checkpoint() -> None:
    api = FakeMshkn()
    api.chains["verb/trial/t-1"] = ["ckpt-a", "ckpt-b"]
    await api.delete_checkpoint("ckpt-a")
    assert api.chains["verb/trial/t-1"] == ["ckpt-b"]
    await api.delete_checkpoint("ckpt-b")
    assert "verb/trial/t-1" not in api.chains
    assert ("delete_checkpoint", {"checkpoint_id": "ckpt-b"}) in api.calls
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/mikesol/Documents/GitHub/mshkn-chain-trials
uv run pytest tests/unit/test_embryo_mshkn.py -q
```

Expected: FAIL — `AttributeError: 'Mshkn' object has no attribute 'delete_checkpoint'`.

- [ ] **Step 3: Add the call to the Protocol and the client**

In `embryo/membrane/mshkn.py`, change the module docstring's first line from "seven calls" to "eight calls", add to the `MshknApi` Protocol, after `list_checkpoints`:

```python
    async def delete_checkpoint(self, checkpoint_id: str) -> None: ...
```

and to the `Mshkn` class, after `list_checkpoints`:

```python
    async def delete_checkpoint(self, checkpoint_id: str) -> None:
        await self._request("DELETE", f"/checkpoints/{checkpoint_id}")
```

- [ ] **Step 4: Teach the fake**

In `tests/support_embryo.py`, add to `FakeMshkn` after `list_checkpoints`:

```python
    async def delete_checkpoint(self, checkpoint_id: str) -> None:
        self.calls.append(("delete_checkpoint", {"checkpoint_id": checkpoint_id}))
        for label, ids in list(self.chains.items()):
            if checkpoint_id in ids:
                ids.remove(checkpoint_id)
                if not ids:
                    del self.chains[label]
                return
        raise MshknError(404, "Checkpoint not found")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/test_embryo_mshkn.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add embryo/membrane/mshkn.py tests/support_embryo.py tests/unit/test_embryo_mshkn.py
git commit -m "feat(embryo): the membrane can delete a checkpoint (#118)"
```

---

### Task 2: `runs` on the trial declaration

`try`'s inputs are validated where every other declaration is: `embryo/membrane/declarations.py`, which turns JSON documents into frozen values and raises `DeclarationError` with the reason. The refusals must be **constructive** — they name what would have been valid — because that is how this project teaches a model instead of documenting at it.

This task adds a pure function. Nothing calls it yet.

**Files:**
- Modify: `embryo/membrane/declarations.py`
- Test: `tests/unit/test_embryo_declarations.py`

**Interfaces:**
- Consumes: `DeclarationError` from this same module.
- Produces: `def parse_runs(params: object, runs: object) -> list[dict[str, Any]]` in `embryo/membrane/declarations.py`. Returns a list of at least one parameter dict. Raises `DeclarationError` when both inputs are given, when `runs` is an empty list, when `runs` is not a list, or when any entry is not an object.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_embryo_declarations.py`:

```python
def test_parse_runs_defaults_to_one_invocation() -> None:
    assert parse_runs({"url": "u"}, None) == [{"url": "u"}]
    assert parse_runs(None, None) == [{}]


def test_parse_runs_takes_a_list_of_parameter_sets() -> None:
    assert parse_runs(None, [{"n": 1}, {"n": 2}]) == [{"n": 1}, {"n": 2}]


def test_parse_runs_refuses_both_and_names_the_choice() -> None:
    with pytest.raises(DeclarationError) as exc:
        parse_runs({"url": "u"}, [{"url": "u"}])
    assert "params" in str(exc.value) and "runs" in str(exc.value)


def test_parse_runs_refuses_an_empty_list() -> None:
    with pytest.raises(DeclarationError) as exc:
        parse_runs(None, [])
    assert "at least one invocation" in str(exc.value)


def test_parse_runs_refuses_a_non_list_and_a_non_object_entry() -> None:
    with pytest.raises(DeclarationError) as exc:
        parse_runs(None, {"n": 1})
    assert "list" in str(exc.value)
    with pytest.raises(DeclarationError) as exc:
        parse_runs(None, [{"n": 1}, "two"])
    assert "runs[1]" in str(exc.value)
```

Add `parse_runs` to the `from membrane.declarations import ...` line at the top of that file.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/test_embryo_declarations.py -q -k parse_runs
```

Expected: FAIL — `ImportError: cannot import name 'parse_runs'`.

- [ ] **Step 3: Write the implementation**

In `embryo/membrane/declarations.py`, after `parse_verb`:

```python
def parse_runs(params: object, runs: object) -> list[dict[str, Any]]:
    """The invocations of one trial (#118). `params` is one invocation; `runs` is
    several, in order. A `chain` verb's runs share the trial's scratch chain, so a
    second entry is how the model sees whether its state persisted."""
    if params is not None and runs is not None:
        raise DeclarationError(
            "give params for one invocation or runs for several, not both; "
            "runs is a list of parameter objects, one per invocation, in order"
        )
    if runs is None:
        return [dict(_obj(params, "params"))] if params is not None else [{}]
    if not isinstance(runs, list):
        raise DeclarationError("runs must be a list of parameter objects, one per invocation")
    if not runs:
        raise DeclarationError("runs must name at least one invocation")
    return [dict(_obj(entry, f"runs[{i}]")) for i, entry in enumerate(runs)]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/test_embryo_declarations.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add embryo/membrane/declarations.py tests/unit/test_embryo_declarations.py
git commit -m "feat(embryo): parse a trial's list of invocations (#118)"
```

---

### Task 3: `Trial` carries a sequence, a chain and a sweep flag

`Trial` lives in `embryo/membrane/state.py` and is serialised into `state.json`, the brain's one atomic document. The old fields (`params: dict`, `result: dict | None`) are replaced, not kept beside the new ones — the project runs no migrations for `state.json`, which lives on one brain's disk.

This task changes the dataclass only. `tests/unit/test_embryo_trials.py` and `tests/unit/test_embryo_turn.py` construct `Trial` with the old field names and **will be red when this task ends**; Task 4 repairs them, and that is this task's stated scope, not an oversight. Run only `tests/unit/test_embryo_state.py` here. Do not repair the other files, do not add a compatibility shim for the old field names, and do not weaken or skip anything to get the suite green.

**Files:**
- Modify: `embryo/membrane/state.py` (the `Trial` dataclass)
- Test: `tests/unit/test_embryo_state.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Trial(id: str, verb: Verb, runs: list[dict[str, Any]], recipe_id: str | None, status: TrialStatus, results: list[dict[str, Any]], build_log: str | None = None, chain: str | None = None, swept: bool = False)`, with `to_doc()`/`from_doc()` round-tripping every field.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_embryo_state.py`:

```python
def test_a_trial_round_trips_its_runs_chain_and_sweep() -> None:
    trial = Trial(
        id="t-3",
        verb=parse_verb(VERB),
        runs=[{"url": "a"}, {"url": "b"}],
        recipe_id="rcp-1",
        status="done",
        results=[{"status": "ok", "stdout": "1"}, {"status": "ok", "stdout": "2"}],
        build_log="ok",
        chain="verb/trial/t-3",
        swept=True,
    )
    back = Trial.from_doc(trial.to_doc())
    assert back.runs == trial.runs and back.results == trial.results
    assert back.chain == "verb/trial/t-3" and back.swept is True
    assert back.build_log == "ok" and back.verb.name == trial.verb.name


def test_a_trial_defaults_to_no_chain_and_unswept() -> None:
    trial = Trial(
        id="t-1",
        verb=parse_verb(VERB),
        runs=[{}],
        recipe_id=None,
        status="building",
        results=[],
    )
    assert trial.chain is None and trial.swept is False and trial.build_log is None
    assert Trial.from_doc(trial.to_doc()).results == []
```

Make sure `Trial`, `parse_verb` and `VERB` are imported at the top of that file; follow whatever that file already imports rather than adding a duplicate import line.

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/unit/test_embryo_state.py -q
```

Expected: FAIL — `TypeError: Trial.__init__() got an unexpected keyword argument 'runs'`.

- [ ] **Step 3: Rewrite the dataclass**

Replace the whole `Trial` dataclass in `embryo/membrane/state.py` with:

```python
@dataclass
class Trial:
    id: str
    verb: Verb
    runs: list[dict[str, Any]]
    recipe_id: str | None
    status: TrialStatus
    results: list[dict[str, Any]]
    build_log: str | None = None
    # The scratch chain a `chain` verb's invocations share, `verb/trial/<id>`, and
    # whether its checkpoints have been deleted (#118). `None` until run 1 is attempted.
    chain: str | None = None
    swept: bool = False

    def to_doc(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "verb": self.verb.to_doc(),
            "runs": self.runs,
            "recipe_id": self.recipe_id,
            "status": self.status,
            "results": self.results,
            "build_log": self.build_log,
            "chain": self.chain,
            "swept": self.swept,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Trial:
        return cls(
            id=doc["id"],
            verb=parse_verb(doc["verb"]),
            runs=doc["runs"],
            recipe_id=doc.get("recipe_id"),
            status=doc["status"],
            results=doc.get("results") or [],
            build_log=doc.get("build_log"),
            chain=doc.get("chain"),
            swept=bool(doc.get("swept")),
        )
```

- [ ] **Step 4: Run the state tests to verify they pass**

```bash
uv run pytest tests/unit/test_embryo_state.py -q
```

Expected: PASS. `tests/unit/test_embryo_trials.py` and `tests/unit/test_embryo_turn.py` are now red with `TypeError: Trial.__init__() got an unexpected keyword argument 'params'`; Task 4 fixes them. Leave them red.

- [ ] **Step 5: Commit**

```bash
git add embryo/membrane/state.py tests/unit/test_embryo_state.py
git commit -m "feat(embryo): a trial holds a sequence of runs, a scratch chain and a sweep flag (#118)"
```

---

### Task 4: the trial runs the sequence

The heart of the change, all in `embryo/membrane/trials.py`. `run_trial` stops being one computer and becomes the sequence; `try_verb` parses `runs` and hands it over; `_describe` renders one block per run for the inbox.

Read `embryo/membrane/verbs.py:invoke` before starting — the two mshkn calls this task makes (`create_computer` with a label, then `fork_label`) are the ones `invoke` already makes, and the shapes must match.

**Files:**
- Modify: `embryo/membrane/trials.py`
- Test: `tests/unit/test_embryo_trials.py`

**Interfaces:**
- Consumes: `parse_runs` (Task 2), the `Trial` fields (Task 3), `delete_checkpoint` (Task 1).
- Produces:
  - `TRIAL_CHAIN_PREFIX = "verb/trial/"` in `embryo/membrane/trials.py`.
  - `async def run_trial(api: MshknApi, trial: Trial, *, remaining: float, now: Callable[[], float] = time.monotonic) -> dict[str, Any]` — runs the whole sequence, sets `trial.results`, `trial.chain` and `trial.swept`, and returns `{"runs": [...], "status": ...}` plus `ran`/`of` when truncated.
  - `async def sweep_trial(api: MshknApi, trial: Trial) -> None` — deletes every checkpoint under `trial.chain` and sets `trial.swept`.

- [ ] **Step 1: Teach the fake to answer the same command differently twice**

A chain verb's whole point is that the same command answers differently on a second run. `FakeMshkn.outputs` is keyed by command and cannot express that. Add a sequence beside it, using the same idiom `recipe_statuses` already uses (pop while more than one remains, then repeat the last).

In `tests/support_embryo.py`, add the field to `FakeMshkn` beside `outputs`:

```python
    # A command that answers differently each time it is run, which is what a chain
    # verb does; `outputs` alone is keyed by command and cannot say that (#118).
    output_sequences: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)
```

and replace `_output`:

```python
    def _output(self, command: str) -> tuple[int, str, str]:
        seq = self.output_sequences.get(command)
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return self.outputs.get(command, (0, "", ""))
```

- [ ] **Step 2: Write the failing tests**

Replace the two existing chain/trial tests and add the new ones. In `tests/unit/test_embryo_trials.py`, delete `test_a_chain_declaration_is_trialled_without_a_chain` (its claim is what this change reverses) and add:

```python
async def test_a_chain_trial_runs_twice_on_one_scratch_chain_and_sweeps_it() -> None:
    api = FakeMshkn()
    state = State()
    cmd = render_command(parse_verb(CHAIN_VERB), {})
    api.output_sequences[cmd] = [(0, "1\n", ""), (0, "2\n", "")]
    result = await try_verb(
        api, state, CHAIN_VERB, None, runs=[{}, {}], until=200.0, now=lambda: 0.0, sleep=_no_sleep
    )
    assert result["status"] == "done"
    assert [r["stdout"] for r in result["runs"]] == ["1\n", "2\n"]
    assert all(r["chain_head"] is not None for r in result["runs"])
    kinds = [c[0] for c in api.calls]
    assert kinds.count("create_computer") == 1 and kinds.count("fork_label") == 1
    create = next(c for c in api.calls if c[0] == "create_computer")
    assert create[1]["label"] == "verb/trial/t-1"
    fork = next(c for c in api.calls if c[0] == "fork_label")
    assert fork[1]["label"] == "verb/trial/t-1"
    # the scratch chain is gone, and the verb's own chain was never touched
    assert api.chains == {}
    assert state.trials["t-1"].swept is True


async def test_an_ephemeral_trial_runs_each_invocation_on_a_fresh_computer() -> None:
    api = FakeMshkn()
    state = State()
    api.outputs[render_command(parse_verb(VERB), {"url": "a"})] = (0, "A", "")
    api.outputs[render_command(parse_verb(VERB), {"url": "b"})] = (0, "B", "")
    result = await try_verb(
        api,
        state,
        VERB,
        None,
        runs=[{"url": "a"}, {"url": "b"}],
        until=200.0,
        now=lambda: 0.0,
        sleep=_no_sleep,
    )
    assert [r["stdout"] for r in result["runs"]] == ["A", "B"]
    assert all("chain_head" not in r for r in result["runs"])
    creates = [c for c in api.calls if c[0] == "create_computer"]
    assert len(creates) == 2 and all(c[1]["label"] is None for c in creates)
    assert api.chains == {} and state.trials["t-1"].chain is None


async def test_a_non_zero_exit_does_not_stop_the_sequence() -> None:
    """The replay-protected hook of #118: the same signature twice must be accepted
    once and refused once, so run 2's non-zero exit is the reading, not a failure."""
    api = FakeMshkn()
    cmd = render_command(parse_verb(CHAIN_VERB), {})
    api.output_sequences[cmd] = [(0, "ok\n", ""), (1, "", "replay\n")]
    result = await try_verb(
        api, State(), CHAIN_VERB, None, runs=[{}, {}], until=200.0, now=lambda: 0.0, sleep=_no_sleep
    )
    assert [r["exit_code"] for r in result["runs"]] == [0, 1]
    assert result["status"] == "done"


async def test_an_error_stops_the_sequence_and_keeps_what_it_read() -> None:
    api = FakeMshkn()
    state = State()
    trial = Trial(
        id="t-x",
        verb=parse_verb(CHAIN_VERB),
        runs=[{}, {}],
        recipe_id="rcp-missing",
        status="building",
        results=[],
    )
    state.trials["t-x"] = trial
    result = await run_trial(api, trial, remaining=200.0)
    assert len(result["runs"]) == 1 and result["runs"][0]["status"] == "error"
    assert "not ready" in result["runs"][0]["error"]
    # the trial itself completed; the error is one run's reading, per spec §3
    assert result["status"] == "done" and trial.swept is True


async def test_an_out_of_time_sequence_returns_what_it_got_and_leaves_the_sweep() -> None:
    api = FakeMshkn()
    info = await api.create_recipe(CHAIN_VERB["dockerfile"])
    await api.get_recipe(info.id)
    cmd = render_command(parse_verb(CHAIN_VERB), {})
    api.output_sequences[cmd] = [(0, "1\n", ""), (0, "2\n", "")]
    trial = Trial(
        id="t-1",
        verb=parse_verb(CHAIN_VERB),
        runs=[{}, {}, {}],
        recipe_id=info.id,
        status="building",
        results=[],
    )
    # run_trial reads the clock once to fix its deadline and once per loop turn:
    # 200 s, then 200 s left, then 110 s left, then 15 s left — too little for a third
    ticks = iter([0.0, 0.0, 90.0, 185.0])
    result = await run_trial(api, trial, remaining=200.0, now=lambda: next(ticks))
    assert result["status"] == "out of time" and result["ran"] == 2 and result["of"] == 3
    assert [r["stdout"] for r in result["runs"]] == ["1\n", "2\n"]
    # the sweep costs time the turn does not have; the next poll does it
    assert trial.swept is False
    assert "delete_checkpoint" not in [c[0] for c in api.calls]


async def test_a_deferred_fork_stops_the_sequence() -> None:
    """A scratch label only this trial knows should never be busy, but mshkn may
    answer 202 anyway; that is data, not a crashed turn."""
    api = FakeMshkn()
    api.busy_labels.add("verb/trial/t-1")
    result = await try_verb(
        api,
        State(),
        CHAIN_VERB,
        None,
        runs=[{}, {}],
        until=200.0,
        now=lambda: 0.0,
        sleep=_no_sleep,
    )
    assert len(result["runs"]) == 2
    assert result["runs"][0]["status"] == "ok"
    assert result["runs"][1]["status"] == "error" and "deferred" in result["runs"][1]["error"]


async def test_the_next_turn_sweeps_a_trial_that_died_before_its_sweep() -> None:
    api = FakeMshkn()
    state = State()
    api.chains["verb/trial/t-7"] = ["ckpt-a", "ckpt-b"]
    state.trials["t-7"] = Trial(
        id="t-7",
        verb=parse_verb(CHAIN_VERB),
        runs=[{}, {}],
        recipe_id="rcp-1",
        status="done",
        results=[{"status": "ok"}],
        chain="verb/trial/t-7",
        swept=False,
    )
    assert await poll_trials(api, state, remaining=50.0) == []
    assert api.chains == {} and state.trials["t-7"].swept is True


async def test_a_trial_refuses_params_and_runs_together() -> None:
    api = FakeMshkn()
    result = await try_verb(api, State(), VERB, {"url": "u"}, runs=[{"url": "u"}], until=100.0)
    assert result["status"] == "invalid" and "not both" in result["error"]
    assert api.calls == []
```

Then repair the tests Task 3 broke. Three files construct the old shape — find every one with
`grep -rn "params=" tests/unit/test_embryo_trials.py tests/unit/test_embryo_turn.py tests/unit/test_embryo_commands.py`
and check nothing else does (`uv run mypy` names them all):

- `tests/unit/test_embryo_trials.py`
- `tests/unit/test_embryo_turn.py`
- `tests/unit/test_embryo_commands.py` — one call at about line 346, inside the test that pins
  `list`'s trial projection for the measure's "no undeclared capability" check. It becomes
  `runs=[{}]` and `results=[]`; its assertion on the listing is unchanged, since `list_state`
  projects only `id`, `verb`, `status` and `recipe_id`.

In each: `Trial(...)` constructor calls take `runs=[{...}]` and `results=[]` instead of `params=` and `result=None`; assertions on `result["stdout"]` become `result["runs"][0]["stdout"]`; `trial.result is not None` becomes `trial.results`. The out-of-time assertion in `test_a_trial_with_no_time_left_makes_no_request` becomes:

```python
    assert (await run_trial(api, state.trials["t-1"], remaining=5.0)) == {
        "runs": [],
        "status": "out of time",
        "ran": 0,
        "of": 1,
    }
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/test_embryo_trials.py -q
```

Expected: FAIL — `try_verb()` takes no `runs` keyword, and `run_trial` returns one run's document rather than `{"runs": [...]}`.

- [ ] **Step 4: Write the implementation**

Rewrite `embryo/membrane/trials.py`'s module docstring, `run_trial`, `_describe` and `try_verb`, and extend `poll_trials`. Keep `TRIAL_MARGIN` and `RUN_MARGIN` as they are.

```python
"""try (spec §5): build a declaration and run its invocations in order on computers
with no secrets and no policy check; a `chain` verb's invocations share a scratch
chain that is discarded with the trial (#118). Returns the build log and every
invocation's output as data."""
```

Add the prefix constant beside the margins:

```python
TRIAL_CHAIN_PREFIX = "verb/trial/"
# A chain trial's invocations share this label, never the verb's own `verb/<name>`:
# a verb name holds no slash, so the two can never collide, and a supersede of a
# catalogued verb cannot touch the live chain (#118).
```

Replace `run_trial` with:

```python
async def _one_run(
    api: MshknApi, trial: Trial, params: dict[str, Any], *, remaining: float
) -> dict[str, Any]:
    """One invocation. A `chain` verb's first run creates the scratch chain; later
    runs fork it, exactly as `verbs.invoke` advances a catalogued verb's chain."""
    assert trial.recipe_id is not None
    verb = trial.verb
    try:
        command = render_command(verb, params)
        if verb.state == "chain" and trial.chain is not None:
            forked = await api.fork_label(
                label=trial.chain, command=command, timeout=remaining
            )
            if isinstance(forked, Deferred):
                return {"status": "error", "error": f"deferred {forked.deferred_id}"}
            run = forked
        else:
            label = TRIAL_CHAIN_PREFIX + trial.id if verb.state == "chain" else None
            run = await api.create_computer(
                recipe_id=trial.recipe_id,
                command=command,
                needs=verb.needs,
                label=label,
                timeout=remaining,
            )
            trial.chain = label
    except (DeclarationError, MshknError) as exc:
        return {"status": "error", "error": str(exc)}
    doc = run_result_doc(run)
    if verb.state == "chain":
        doc["chain_head"] = run.created_checkpoint_id
    return doc


async def sweep_trial(api: MshknApi, trial: Trial) -> None:
    """Discard the scratch chain. #93 retention keeps the newest checkpoint of every
    label forever, so an unswept trial leaks one. Idempotent: a label with no
    checkpoints is swept by doing nothing."""
    if trial.chain is None or trial.swept:
        trial.swept = True
        return
    try:
        for ckpt in await api.list_checkpoints(trial.chain):
            await api.delete_checkpoint(ckpt.id)
    except MshknError:
        # The next turn's poll tries again; a leaked scratch chain is not worth a
        # crashed turn.
        return
    trial.swept = True


async def run_trial(
    api: MshknApi,
    trial: Trial,
    *,
    remaining: float,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Every invocation in order, while the turn has room for one. A non-zero exit is
    a reading and does not stop the sequence; an `error` does, since the next
    invocation would measure nothing."""
    until = now() + remaining
    results: list[dict[str, Any]] = []
    stopped = False
    for params in trial.runs:
        left = until - now()
        if left < RUN_MARGIN:
            break
        doc = await _one_run(api, trial, params, remaining=left)
        results.append(doc)
        if doc["status"] == "error":
            stopped = True
            break
    trial.results = results
    if len(results) < len(trial.runs) and not stopped:
        # Out of time: the sweep would spend time the turn does not have, so the
        # next turn's poll does it.
        return {"runs": results, "status": "out of time", "ran": len(results), "of": len(trial.runs)}
    await sweep_trial(api, trial)
    return {"runs": results, "status": "done"}
```

Import `Deferred` from `membrane.mshkn` at the top of the file (it already imports `MshknError` from there).

Replace `_describe` with:

```python
def _describe(trial: Trial, result: dict[str, Any]) -> str:
    runs = result.get("runs") or []
    head = f"trial {trial.id} of {trial.verb.name}: {len(runs)} of {len(trial.runs)} ran"
    blocks = [
        f"run {i + 1}: exit {r.get('exit_code')}\n"
        f"stdout:\n{r.get('stdout', '')}\nstderr:\n{r.get('stderr', '')}"
        if r.get("status") != "error"
        else f"run {i + 1}: error {r.get('error')}"
        for i, r in enumerate(runs)
    ]
    return "\n".join([head, *blocks])
```

In `try_verb`, add the `runs` parameter and build the trial from it. The signature becomes:

```python
async def try_verb(
    api: MshknApi,
    state: State,
    doc: object,
    params: object = None,
    *,
    runs: object = None,
    until: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
```

and the body's first lines become:

```python
    try:
        verb = parse_verb(doc)
        invocations = parse_runs(params, runs)
    except DeclarationError as exc:
        return {"status": "invalid", "error": str(exc)}
    trial = Trial(
        id=state.new_trial_id(),
        verb=verb,
        runs=invocations,
        recipe_id=None,
        status="building",
        results=[],
    )
```

Import `parse_runs` from `membrane.declarations`. In the two build-failure branches set `trial.build_log = ...` instead of `trial.result = {"build_log": ...}`. The tail becomes:

```python
    result = await run_trial(api, trial, remaining=until - now(), now=now)
    trial.status = "done"
    trial.build_log = log_tail(info.build_log)
    return {"trial": trial.id, "build_log": trial.build_log, **result}
```

Finally, extend `poll_trials` so it sweeps unswept trials as well as running ready ones. Replace its loop body's guard and add a sweep pass:

```python
async def poll_trials(api: MshknApi, state: State, *, remaining: float) -> list[InboxItem]:
    items: list[InboxItem] = []
    for trial in state.trials.values():
        if trial.status != "building" or trial.recipe_id is None:
            # A trial whose turn died between its last run and its sweep, or one that
            # ran out of time, leaves a scratch chain for this turn to discard (#118).
            if trial.chain is not None and not trial.swept:
                await sweep_trial(api, trial)
            continue
        info = await api.get_recipe(trial.recipe_id)
        if info.status == "ready":
            result = await run_trial(api, trial, remaining=remaining)
            trial.status = "done"
            trial.build_log = log_tail(info.build_log)
            items.append(InboxItem(kind="trial", text=_describe(trial, result)))
        elif info.status == "failed":
            trial.status = "failed"
            tail = log_tail(info.build_log)
            trial.build_log = tail
            items.append(
                InboxItem(
                    kind="trial",
                    text=f"trial {trial.id} of {trial.verb.name} failed to build:\n{tail}",
                )
            )
    return items
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/unit -q && uv run mypy
```

Expected: PASS, and mypy clean. The whole unit tier must be green at the end of this task — it is the task that closes the red window Task 3 opened.

- [ ] **Step 6: Commit**

```bash
git add embryo/membrane/trials.py tests/support_embryo.py tests/unit/test_embryo_trials.py tests/unit/test_embryo_turn.py tests/unit/test_embryo_commands.py
git commit -m "feat(embryo): a trial runs its invocations in order on a scratch chain (#118)"
```

---

### Task 5: the `try` tool offers `runs`

The model sees `try` through `TRY_TOOL` in `embryo/membrane/turn.py`. The tool description is where `runs` is taught — deliberately, not in the seed: the schema names the parameter and the result shows what it did, so no seed line is needed (Task 6).

**Files:**
- Modify: `embryo/membrane/turn.py` (`TRY_TOOL`, `do_try`, `_tool_summary`)
- Test: `tests/unit/test_embryo_turn.py`

**Interfaces:**
- Consumes: `try_verb(..., params, runs=..., until=...)` (Task 4).
- Produces: `TRY_TOOL`'s `input_schema` gains `runs`, an array of objects. `_tool_summary` gains a compact `runs` projection that Tasks 7 and 8 read out of the audit line.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_embryo_turn.py`:

```python
def test_the_try_tool_offers_a_list_of_invocations() -> None:
    props = TRY_TOOL["input_schema"]["properties"]
    assert props["runs"]["type"] == "array" and props["runs"]["items"]["type"] == "object"
    assert TRY_TOOL["input_schema"]["required"] == ["verb"]
    # the seed says nothing about runs; this description is where the model learns it
    assert "runs" in TRY_TOOL["description"] and "scratch chain" in TRY_TOOL["description"]


def test_the_audit_summarises_every_run_without_its_output() -> None:
    """A chain trial's exit_code and chain_head moved from the top level into runs,
    so without this an audited trial reads as {name, status, trial} and loses every
    reading. stdout stays out of the audit line, as it always has been."""
    summary = _tool_summary(
        {
            "name": "try",
            "result": {
                "trial": "t-1",
                "status": "done",
                "build_log": "ok",
                "runs": [
                    {
                        "status": "ok",
                        "exit_code": 0,
                        "computer_id": "comp-1",
                        "stdout": "1\n",
                        "stderr": "",
                        "chain_head": "ckpt-a",
                    },
                    {
                        "status": "ok",
                        "exit_code": 0,
                        "computer_id": "comp-2",
                        "stdout": "2\n",
                        "stderr": "",
                        "chain_head": "ckpt-b",
                    },
                ],
            },
        }
    )
    assert summary["name"] == "try" and summary["status"] == "done"
    assert summary["trial"] == "t-1"
    assert summary["runs"] == [
        {"status": "ok", "exit_code": 0, "chain_head": "ckpt-a"},
        {"status": "ok", "exit_code": 0, "chain_head": "ckpt-b"},
    ]


def test_the_audit_keeps_a_runs_error_and_omits_absent_keys() -> None:
    summary = _tool_summary(
        {
            "name": "try",
            "result": {
                "trial": "t-2",
                "status": "out of time",
                "runs": [{"status": "error", "error": "deferred def-1"}],
            },
        }
    )
    assert summary["runs"] == [{"status": "error", "error": "deferred def-1"}]
```

Add `TRY_TOOL` and `_tool_summary` to the existing `from membrane.turn import (...)` block at line 23 of that file.

`do_try`'s pass-through of `runs` is exercised end to end by Task 7's flow test, which is where a `try` call actually reaches `try_verb` through a turn.

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/unit/test_embryo_turn.py -q -k try_tool
```

Expected: FAIL — `KeyError: 'runs'`.

- [ ] **Step 3: Write the implementation**

In `embryo/membrane/turn.py`, replace `TRY_TOOL` with:

```python
TRY_TOOL = {
    "name": "try",
    "description": "Build a verb declaration and run its entrypoint on a computer with no "
    "secrets and no policy. Give `params` for one invocation, or `runs` — a list of "
    "parameter objects — for several, which run in order; a chain verb's invocations "
    "share one scratch chain, so a second run reads what the first left. Returns the "
    "build log and every invocation's stdout, stderr and exit code as data. Installs "
    "nothing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verb": {"type": "object"},
            "params": {"type": "object"},
            "runs": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["verb"],
    },
}
```

replace `do_try` with:

```python
    async def do_try(inp: dict[str, Any]) -> dict[str, Any]:
        return await try_verb(
            ctx.api,
            state,
            inp.get("verb"),
            inp.get("params"),
            runs=inp.get("runs"),
            until=ctx.deadline,
            now=ctx.now,
            sleep=ctx.sleep,
        )
```

and extend `_tool_summary` so a trial's sequence survives into the audit line. A chain
trial's `exit_code` and `chain_head` now live inside `runs`, so without this an audited
trial reads as `{name, status, trial}` and carries no reading at all. `stdout` and
`stderr` stay out, as they always have been — T14.5 proves `page_title`'s output through
the reply, never the audit.

```python
RUN_AUDIT_KEYS = ("exit_code", "chain_head", "error")


def _tool_summary(call: dict[str, Any]) -> dict[str, Any]:
    result = call["result"]
    summary: dict[str, Any] = {"name": call["name"], "status": result.get("status")}
    for key in ("exit_code", "computer_id", "chain_head", "id", "trial", "error"):
        if key in result:
            summary[key] = result[key]
    if isinstance(result.get("runs"), list):
        # A trial's invocations, each as small as the top level used to be: what it
        # did and where it left the chain, never what it printed (#118).
        summary["runs"] = [
            {"status": run.get("status"), **{k: run[k] for k in RUN_AUDIT_KEYS if k in run}}
            for run in result["runs"]
        ]
    return summary
```

- [ ] **Step 4: Run the whole unit tier to verify it passes**

```bash
uv run pytest tests/unit -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add embryo/membrane/turn.py tests/unit/test_embryo_turn.py
git commit -m "feat(embryo): the try tool offers a list of invocations (#118)"
```

---

### Task 6: correct the seed, and the spec it answers to

The seed says `try` "runs it once on a computer with no secrets, no chain and no policy". That clause becomes false when Task 4 lands, and a false line in the genome is worse than a missing one. Under the #123 contract (`CLAUDE.md`, "Keep the seed a seed") the correction is **invisible mechanism**: no experiment reveals that the trial's chain is scratch and thrown away, and without the sentence a model must assume a trial leaves state it is answerable for.

**Nothing about `runs` goes in the seed.** The tool schema names it and the result shows what it did.

**Files:**
- Modify: `embryo/seed.md`
- Modify: `docs/superpowers/specs/2026-09-08-embryo-design.md`
- Modify: `docs/embryo/README.md`
- Test: `tests/unit/test_embryo_priors.py`, `tests/unit/test_docs.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing other tasks import.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_embryo_priors.py`, inside `test_the_seed_is_bootstrap_and_invisible_mechanism_and_nothing_else`, add to the "present" tuple, in the invisible-mechanism group:

```python
        # #118: no experiment reveals that a trial's chain is scratch and discarded,
        # and without this a model must assume a trial leaves state it answers for
        "scratch chain",
```

and add to the "absent" tuple:

```python
        # TRY_TOOL's description carries this; the result shows what it did
        "`runs`",
```

Add a new test to the same file:

```python
def test_the_seed_does_not_say_a_trial_has_no_chain() -> None:
    """#118: `try` runs a chain verb's invocations on a scratch chain, so the old
    clause is false. A false line in the genome is worse than a missing one."""
    seed = (EMBRYO / "seed.md").read_text()
    assert "no chain" not in seed
    assert "runs it once" not in seed
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/unit/test_embryo_priors.py -q
```

Expected: FAIL — `assert "no chain" not in seed`.

- [ ] **Step 3: Correct the seed**

In `embryo/seed.md`, replace the `try` paragraph:

```markdown
A fourth tool, `try`, builds a verb declaration and runs it on computers with no secrets and no policy. A `chain` verb's trial runs on a scratch chain that is discarded with the trial. Use it to test a declaration before you propose it. It installs nothing.
```

- [ ] **Step 4: Correct the spec**

In `docs/superpowers/specs/2026-09-08-embryo-design.md`, make five edits:

1. §2, decision 4: change "on a computer with no authority" to "on computers with no authority".
2. §3, last line: change "a trial runs on a computer with no secrets, no chain and no policy, and installs nothing" to "a trial runs on computers with no secrets and no policy, on a scratch chain discarded with the trial, and installs nothing".
3. §5, `try`: change "creates a computer from it with no secrets, no chain label and no policy check, runs the rendered entrypoint, self-destructs it, and returns build log, stdout and exit code as data" to "runs one invocation per entry of the trial's `runs` list, in order, each self-destructing: an `ephemeral` verb's on a fresh computer with no label, a `chain` verb's on a scratch chain labelled `verb/trial/<trial id>` and discarded when the trial ends. It returns the build log and every invocation's stdout, stderr and exit code as data (#118)." Change the sentence "What a trial cannot do is exactly what a verb without approval cannot do: hold a secret, touch a chain, or be invoked by anyone" to "…: hold a secret, touch the verb's own chain, or be invoked by anyone".
4. §12, the `/brain/seed.md` row: change "`try` runs with no secrets, no chain and no policy" to "a trial's chain is scratch and discarded with the trial".
5. §12, the flow-tier sentence: change "a trial, proposals, approval, builds, a chain verb with two checkpoints" to "a trial, a chain trial swept to nothing, proposals, approval, builds, a chain verb with two checkpoints".

- [ ] **Step 5: Record the amendment**

`docs/embryo/README.md` records seed amendments as bullets under `### What this round does not establish`, beside the two that already begin "The two rounds are not fully comparable, and the seed is why" and "A third boundary, and the seed is why again". Add a third bullet there, in the same voice:

```markdown
- **2026-09-11 (#118).** `try` gained a list of invocations, and a `chain` verb's trial now runs them on a scratch chain discarded with the trial. The seed's clause "runs it once on a computer with no secrets, no chain and no policy" was false under this and was corrected, not extended; nothing about `runs` entered the seed, since the tool schema names it and the result shows what it did. Rounds before this date are not comparable on any postcondition involving a chain verb: `2026-09-10-run-6` spent a turn's reasoning and three blind invocations on a property a single second run now shows directly.
```

- [ ] **Step 6: Run the doc and prior tests to verify they pass**

```bash
uv run pytest tests/unit/test_embryo_priors.py tests/unit/test_docs.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add embryo/seed.md docs/superpowers/specs/2026-09-08-embryo-design.md docs/embryo/README.md tests/unit/test_embryo_priors.py
git commit -m "docs(embryo): the seed's 'no chain' clause is false under chain trials (#118)"
```

---

### Task 7: the scripted model trials the counter, and the flow tier proves it

`tests/flow/test_embryo_liturgy.py` is the deterministic proof that the DNA executes end to end, running the membrane in-process against the real app over the fake host. Turn 9 currently proposes the counter untrialled. It should trial it first — which is exactly what #118 says a model cannot do today.

The fake host's guest maps a command to one fixed result (`src/mshkn/host/fake.py`), so it cannot yet say that the same command answers differently on a second run. That is a gap in the fake, not a hack around it: answering differently is what a chain verb *is*.

**Files:**
- Modify: `src/mshkn/host/fake.py` (`FakeGuest`)
- Modify: `embryo/membrane/scripted.py` (turn 9's script)
- Test: `tests/flow/test_embryo_liturgy.py`

**Interfaces:**
- Consumes: `TRY_TOOL`'s `runs` (Task 5), the scratch label format.
- Produces: `FakeGuest.script_sequence: dict[str, list[ExecResult]]`.

- [ ] **Step 1: Write the failing flow assertions**

In `tests/flow/test_embryo_liturgy.py`, replace the turn-9 block's opening with:

```python
    # turn 9: a chain verb, trialled on a scratch chain first (#118), then proposed
    signed9 = {"msg": LITURGY[9], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed9)}, "mike\n")
    counter_cmd = embryo.script_output(COUNTER, {}, "1\n")
    flow.host.guest.script_sequence[counter_cmd] = [
        ExecResult(0, "1\n", ""),
        ExecResult(0, "2\n", ""),
    ]
    audit, reply = await embryo.public_say(signed9)
    trial = audit["tools"][0]
    assert trial["name"] == "try" and trial["status"] == "done", audit
    # the audit carries what each invocation did and where it left the chain, never
    # what it printed; two distinct heads are the disk surviving the first invocation
    assert [r["exit_code"] for r in trial["runs"]] == [0, 0], trial
    heads = [r["chain_head"] for r in trial["runs"]]
    assert all(heads) and heads[0] != heads[1], trial
    # the same command really ran twice, which is what a chain verb is for
    assert [c for _, c in flow.host.guest.commands].count(counter_cmd) == 2
    # the scratch chain is discarded with the trial; the verb's own chain is untouched
    scratch = (
        await flow.client.get("/checkpoints", params={"label": f"verb/trial/{trial['trial']}"})
    ).json()
    assert scratch == [], scratch
    assert (await flow.client.get("/checkpoints", params={"label": "verb/counter"})).json() == []
    assert (await embryo.root("approve", "p-6")).startswith("p-6 building")
```

Also update the docstring at the top of the file (line 4) to name the chain trial, and reset the guest's sequence before the two post-approval invocations so they still answer `1` then `2`:

```python
    flow.host.guest.script_sequence[counter_cmd] = [
        ExecResult(0, "1\n", ""),
        ExecResult(0, "2\n", ""),
    ]
    count = {"msg": "count", "sig": "c2ln"}
```

Import `ExecResult` at the top of the file if it is not already imported.

- [ ] **Step 2: Run the flow test to verify it fails**

```bash
uv run pytest tests/flow/test_embryo_liturgy.py -q
```

Expected: FAIL — `AttributeError: 'FakeGuest' object has no attribute 'script_sequence'`.

- [ ] **Step 3: Teach the fake guest a sequence**

In `src/mshkn/host/fake.py`, add to `FakeGuest.__init__`, beside `self.script`:

```python
        # A command that answers differently each time, which is what a chain verb
        # does; `script` alone is keyed by command and cannot say that.
        self.script_sequence: dict[str, list[ExecResult]] = {}
```

and replace the return of `exec`:

```python
        seq = self.script_sequence.get(command)
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return self.script.get(command, self.default)
```

Update the class docstring above `__init__` (which documents `script` and `stream_script`) to name `script_sequence` in the same style.

- [ ] **Step 4: Make the scripted model trial before it proposes**

In `embryo/membrane/scripted.py`, replace turn 9's branch:

```python
        if "counts how many times" in message and can_propose:
            return self._calls(
                [
                    # #118: a chain verb's persistence is the one thing a single-run
                    # trial cannot show, so the trial runs twice on a scratch chain.
                    self._call("try", verb=COUNTER, runs=[{}, {}]),
                    self._call("propose", **_proposal("verb", "counter", COUNTER)),
                ]
            )
```

- [ ] **Step 5: Run the flow and unit tiers to verify they pass**

```bash
uv run pytest tests/flow tests/unit -q
```

Expected: PASS. If `tests/unit/test_embryo_scripted.py` asserts turn 9 emits one call, update that assertion to the two calls — the script genuinely changed.

- [ ] **Step 6: Commit**

```bash
git add src/mshkn/host/fake.py embryo/membrane/scripted.py tests/flow/test_embryo_liturgy.py tests/unit/test_embryo_scripted.py
git commit -m "test(embryo): the liturgy trials the counter on a scratch chain before proposing it (#118)"
```

---

### Task 8: the live host proves it, and the gate stays green

T14.6 already reads turn 9's audit line, so the E2E tier gains assertions rather than a test. **Do not add an E2E test**: the gate line stays 170 passed, 6 skipped, 4 failed over 180, and the four failures must remain exactly the `Not implemented` tests of #65.

One consequence to get right: because the trial now builds the counter's Dockerfile, approval reuses that recipe and the verb is born `ready`, not `building` (ruling P3, which T14.5 already relies on for `page_title`).

**Files:**
- Modify: `tests/e2e/test_phase14_embryo.py` (`test_t14_6_counter_chain_has_two_checkpoints`)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Rewrite T14.6**

Replace the body of `test_t14_6_counter_chain_has_two_checkpoints`:

```python
    async def test_t14_6_counter_chain_has_two_checkpoints(self, doors: Doors) -> None:
        await doors.touch()
        audit, _ = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[9]))
        # #118: the chain verb is trialled twice on a scratch chain first, which is the
        # only way a trial can show that its disk survived an invocation.
        assert [t["name"] for t in audit["tools"]] == ["try", "propose"], audit
        trial = audit["tools"][0]
        assert trial["status"] == "done", trial
        # the audit summarises each invocation without its output (#118): two clean
        # exits and two distinct chain heads are the trial's disk surviving run 1.
        assert [r["exit_code"] for r in trial["runs"]] == [0, 0], trial
        heads = [r["chain_head"] for r in trial["runs"]]
        assert all(heads) and heads[0] != heads[1], trial
        scratch = await doors.client.get(
            "/checkpoints", params={"label": f"verb/trial/{trial['trial']}"}
        )
        assert scratch.json() == [], scratch.text
        pid = audit["proposals"][0]["id"]
        # The trial built this very Dockerfile, so approval reuses that recipe (ruling P3).
        assert await doors.approve_verb(pid, "counter") == "ready"
        listing = await doors.wait_ready("counter")
        assert listing["catalog"]["counter"]["status"] == "ready", listing["proposals"]
        _, one = await doors.public_say(_sign(doors.hatched.key_dir, "count"))
        _, two = await doors.public_say(_sign(doors.hatched.key_dir, "count"))
        assert one.startswith("1") and two.startswith("2"), (one, two)
        chain = (await doors.client.get("/checkpoints", params={"label": "verb/counter"})).json()
        assert len(chain) == 2
```

- [ ] **Step 2: Run the full gate**

```bash
uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov
```

Expected: PASS, zero warnings, coverage at or above 98 %. Fix anything red before continuing — do not weaken a test or add an xfail.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_phase14_embryo.py
git commit -m "test(e2e): T14.6 proves the counter's persistence in its trial (#118)"
```

- [ ] **Step 4: Run the live E2E gate**

```bash
export MSHKN_SERVER=mshkn
setsid nohup scripts/e2e.sh > /tmp/e2e-118.log 2>&1 < /dev/null &
```

Read the log; a full run takes about 23 minutes. Check the "running E2E against" line first — it must name the host's API URL, not `http://mshkn:8000`. Expected: **170 passed, 6 skipped, 4 failed**, the four being exactly the `Not implemented` tests of #65. Any other failure is a regression: fix it or stop and discuss.

Then check the service journal for tracebacks:

```bash
ssh $MSHKN_SERVER journalctl -u mshkn --since '30 min ago' --no-pager | grep -ci traceback
```

- [ ] **Step 5: Index the plan**

Add an entry to `docs/plans/README.md` in the format its neighbours use: the spec path, this plan path, what it delivers, status **implemented**, and the evidence files. Commit it.

```bash
git add docs/plans/README.md
git commit -m "docs: index the chain-trials plan (#118)"
```

---

## Done when

- The gate is green: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov`.
- The live E2E line is 170 passed, 6 skipped, 4 failed, the four being #65's `Not implemented` tests, with zero tracebacks in the journal.
- `embryo/seed.md` contains no false clause about `try`, and `tests/unit/test_embryo_priors.py` pins that.
- A PR against `main` carries `Closes #118`, the "What this does", "Design alignment" and "Validation performed" sections `CLAUDE.md` requires, with the gate output, the CI link and the live E2E summary line naming the failing set.
