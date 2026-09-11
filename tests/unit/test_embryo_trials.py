"""A trial (spec §5) runs a declaration with no authority and installs nothing."""

from __future__ import annotations

from membrane.declarations import parse_verb, render_command
from membrane.mshkn import MshknError
from membrane.state import State, Trial
from membrane.trials import RUN_MARGIN, poll_trials, run_trial, sweep_trial, try_verb

from tests.support_embryo import FakeMshkn
from tests.unit.test_embryo_declarations import VERB
from tests.unit.test_embryo_verbs import CHAIN_VERB


async def _no_sleep(seconds: float) -> None:
    return None


async def test_a_trial_builds_runs_and_returns_the_output_as_data() -> None:
    api = FakeMshkn()
    state = State()
    verb_cmd = render_command(parse_verb(VERB), {"url": "u"})
    api.outputs[verb_cmd] = (0, "Example Domain", "")
    result = await try_verb(
        api, state, VERB, {"url": "u"}, until=100.0, now=lambda: 0.0, sleep=_no_sleep
    )
    assert result["status"] == "done" and result["trial"] == "t-1"
    assert result["runs"][0]["stdout"] == "Example Domain" and result["runs"][0]["exit_code"] == 0
    assert state.trials["t-1"].status == "done"
    assert state.catalog == {} and state.proposals == {}
    create = next(c for c in api.calls if c[0] == "create_computer")
    assert create[1]["label"] is None


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
    assert result["status"] == "done"
    # the label is recorded before create_computer is awaited (#118 fix round 1), so
    # the failed run still leaves a real chain for a later sweep to look at, not None
    assert trial.chain == "verb/trial/t-x"
    # but this turn does NOT sweep it: the invocation's outcome is unknown, so the
    # checkpoint may still be committed after we gave up (PR #134 review)
    assert trial.swept is False
    assert ("list_checkpoints", {"label": "verb/trial/t-x"}) not in api.calls


async def test_a_checkpoint_that_lands_after_a_failed_run_is_swept_next_turn() -> None:
    """A client timeout does not cancel the server. If the sweep ran in the same turn
    it would list nothing, set `swept`, and leave the late checkpoint for #93
    retention to keep forever (PR #134 review)."""
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
    await run_trial(api, trial, remaining=200.0)
    assert trial.swept is False
    # the server commits the checkpoint after the turn gave up on it
    api.chains["verb/trial/t-x"] = ["ckpt-late"]
    trial.status = "done"
    assert await poll_trials(api, state, remaining=200.0) == []
    assert api.chains == {} and trial.swept is True


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


async def test_a_refused_delete_leaves_the_trial_unswept_for_the_next_poll() -> None:
    """sweep_trial's own contract (#118 fix round 1): a delete mshkn refuses never
    crashes the turn, and the trial stays unswept so the next poll tries again."""

    class RefusingMshkn(FakeMshkn):
        async def delete_checkpoint(self, checkpoint_id: str) -> None:
            self.calls.append(("delete_checkpoint", {"checkpoint_id": checkpoint_id}))
            raise MshknError(409, "conflict")

    api = RefusingMshkn()
    api.chains["verb/trial/t-1"] = ["ckpt-a"]
    trial = Trial(
        id="t-1",
        verb=parse_verb(CHAIN_VERB),
        runs=[],
        recipe_id="rcp-1",
        status="done",
        results=[],
        chain="verb/trial/t-1",
        swept=False,
    )
    await sweep_trial(api, trial)
    assert trial.swept is False
    assert api.chains["verb/trial/t-1"] == ["ckpt-a"]  # nothing was actually removed


async def test_a_trial_refuses_params_and_runs_together() -> None:
    api = FakeMshkn()
    result = await try_verb(api, State(), VERB, {"url": "u"}, runs=[{"url": "u"}], until=100.0)
    assert result["status"] == "invalid" and "not both" in result["error"]
    assert api.calls == []


async def test_a_wrong_base_fails_before_any_build() -> None:
    api = FakeMshkn()
    doc = {**VERB, "dockerfile": "FROM python:3.12"}
    api.reject_dockerfiles["FROM python:3.12"] = "recipes must be built FROM mshkn-base"
    result = await try_verb(
        api, State(), doc, {"url": "u"}, until=100.0, now=lambda: 0.0, sleep=_no_sleep
    )
    assert result["status"] == "failed" and "mshkn-base" in result["build_log"]


async def test_an_invalid_declaration_is_reported_not_built() -> None:
    api = FakeMshkn()
    result = await try_verb(api, State(), {**VERB, "effect": "harm"}, {}, until=100.0)
    assert result["status"] == "invalid" and "effect" in result["error"]
    assert api.calls == []


async def test_a_slow_build_outlives_the_turn_and_lands_in_the_inbox() -> None:
    api = FakeMshkn()
    state = State()
    info = await api.create_recipe(VERB["dockerfile"])
    api.recipe_statuses[info.id] = [
        "building",
        "building",
        "ready",
    ]  # two polls in the turn, one later
    clock = iter([0.0, 100.0, 100.0, 100.0])
    result = await try_verb(
        api, state, VERB, {"url": "u"}, until=100.0, now=lambda: next(clock), sleep=_no_sleep
    )
    assert result == {"status": "building", "trial": "t-1"}
    assert state.trials["t-1"].status == "building"
    api.outputs[render_command(parse_verb(VERB), {"url": "u"})] = (2, "", "curl: (6) no host")
    items = await poll_trials(api, state, remaining=50.0)
    assert len(items) == 1 and items[0].kind == "trial"
    assert "trial t-1 of page_title: 1 of 1 ran" in items[0].text
    assert "run 1: exit 2" in items[0].text and "no host" in items[0].text
    trial = state.trials["t-1"]
    assert trial.status == "done" and trial.results
    assert await poll_trials(api, state, remaining=50.0) == []


async def test_a_failed_build_lands_in_the_inbox_with_its_log() -> None:
    api = FakeMshkn()
    state = State()
    info = await api.create_recipe(VERB["dockerfile"])
    api.recipe_statuses[info.id] = ["building", "building", "failed"]
    clock = iter([0.0, 100.0])
    await try_verb(
        api, state, VERB, {"url": "u"}, until=100.0, now=lambda: next(clock), sleep=_no_sleep
    )
    items = await poll_trials(api, state, remaining=50.0)
    assert "trial t-1 of page_title failed to build" in items[0].text and "nope" in items[0].text
    assert state.trials["t-1"].status == "failed"


async def test_a_build_that_fails_within_the_turn_is_reported_at_once() -> None:
    """The recipe fails before the deadline, so try_verb sees "failed" directly
    from wait_for_recipe rather than through a later poll_trials call."""
    api = FakeMshkn()
    state = State()
    info = await api.create_recipe(VERB["dockerfile"])
    api.recipe_statuses[info.id] = ["building", "failed"]
    result = await try_verb(
        api, state, VERB, {"url": "u"}, until=100.0, now=lambda: 0.0, sleep=_no_sleep
    )
    assert (
        result["status"] == "failed" and result["trial"] == "t-1" and "nope" in result["build_log"]
    )
    assert state.trials["t-1"].status == "failed"


async def test_run_trial_reports_a_computer_creation_error_as_data() -> None:
    """A recipe that mshkn reports ready can still fail to become a computer
    (quota, a race); run_trial turns that into data, not an exception."""
    api = FakeMshkn()
    trial = Trial(
        id="t-x",
        verb=parse_verb(VERB),
        runs=[{"url": "u"}],
        recipe_id="rcp-missing",
        status="building",
        results=[],
    )
    result = await run_trial(api, trial, remaining=60.0)
    assert result["runs"][0]["status"] == "error" and "not ready" in result["runs"][0]["error"]


async def test_poll_trials_leaves_a_still_building_trial_untouched() -> None:
    api = FakeMshkn()
    state = State()
    info = await api.create_recipe(VERB["dockerfile"])
    api.recipe_statuses[info.id] = ["building"]
    state.trials["t-1"] = Trial(
        id="t-1",
        verb=parse_verb(VERB),
        runs=[{"url": "u"}],
        recipe_id=info.id,
        status="building",
        results=[],
    )
    items = await poll_trials(api, state, remaining=50.0)
    assert items == []
    assert state.trials["t-1"].status == "building"


async def test_a_trial_with_no_time_left_makes_no_request() -> None:
    """#109: near the deadline run_trial was given one second and timed out."""
    api, state = FakeMshkn(), State()
    clock = [0.0]
    out = await try_verb(
        api,
        state,
        VERB,
        {},
        until=RUN_MARGIN - 1.0,
        now=lambda: clock[0],
        sleep=_no_sleep,
    )
    # the recipe is submitted and ready, but the run waits for the next turn
    assert out["status"] == "building" and state.trials["t-1"].status == "building"
    assert [c[0] for c in api.calls] == ["create_recipe", "get_recipe"]
    assert (await run_trial(api, state.trials["t-1"], remaining=5.0)) == {
        "runs": [],
        "status": "out of time",
        "ran": 0,
        "of": 1,
    }


async def test_a_poll_with_no_time_left_leaves_the_trial_building_for_the_next_turn() -> None:
    """#135: a recipe that became ready between turns, polled in a turn with less
    than RUN_MARGIN left, must not be marked done having run nothing. The next
    poll with room runs it, exactly as try_verb defers a run it cannot fit."""
    api, state = FakeMshkn(), State()
    info = await api.create_recipe(VERB["dockerfile"])
    await api.get_recipe(info.id)  # ready
    state.trials["t-1"] = Trial(
        id="t-1",
        verb=parse_verb(VERB),
        runs=[{"url": "u"}],
        recipe_id=info.id,
        status="building",
        results=[],
    )
    api.calls.clear()
    assert await poll_trials(api, state, remaining=RUN_MARGIN - 1.0) == []
    assert state.trials["t-1"].status == "building"
    assert "create_computer" not in [c[0] for c in api.calls]
    api.outputs[render_command(parse_verb(VERB), {"url": "u"})] = (0, "ran", "")
    items = await poll_trials(api, state, remaining=50.0)
    assert len(items) == 1 and "trial t-1 of page_title: 1 of 1 ran" in items[0].text
    trial = state.trials["t-1"]
    assert trial.status == "done" and trial.results[0]["stdout"] == "ran"
