"""A trial (spec §5) runs a declaration with no authority and installs nothing."""

from __future__ import annotations

from membrane.declarations import parse_verb, render_command
from membrane.state import State, Trial
from membrane.trials import poll_trials, run_trial, try_verb

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
    assert result["stdout"] == "Example Domain" and result["exit_code"] == 0
    assert state.trials["t-1"].status == "done"
    assert state.catalog == {} and state.proposals == {}
    create = next(c for c in api.calls if c[0] == "create_computer")
    assert create[1]["label"] is None


async def test_a_chain_declaration_is_trialled_without_a_chain() -> None:
    api = FakeMshkn()
    state = State()
    result = await try_verb(
        api, state, CHAIN_VERB, {}, until=100.0, now=lambda: 0.0, sleep=_no_sleep
    )
    assert result["status"] == "done"
    assert api.chains == {}


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
    assert "trial t-1 of page_title: exit 2" in items[0].text and "no host" in items[0].text
    trial = state.trials["t-1"]
    assert trial.status == "done" and trial.result is not None
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
        params={"url": "u"},
        recipe_id="rcp-missing",
        status="building",
        result=None,
    )
    result = await run_trial(api, trial, remaining=10.0)
    assert result["status"] == "error" and "not ready" in result["error"]


async def test_poll_trials_leaves_a_still_building_trial_untouched() -> None:
    api = FakeMshkn()
    state = State()
    info = await api.create_recipe(VERB["dockerfile"])
    api.recipe_statuses[info.id] = ["building"]
    state.trials["t-1"] = Trial(
        id="t-1",
        verb=parse_verb(VERB),
        params={"url": "u"},
        recipe_id=info.id,
        status="building",
        result=None,
    )
    items = await poll_trials(api, state, remaining=50.0)
    assert items == []
    assert state.trials["t-1"].status == "building"
