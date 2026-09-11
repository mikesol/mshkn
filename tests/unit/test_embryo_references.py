"""A reply's claims about ids the membrane issued are lookups, not judgements
(#121): `p-N` and `t-N` exist or they do not."""

from __future__ import annotations

from membrane.declarations import parse_verb
from membrane.proposals import propose
from membrane.references import describe, unknown_references
from membrane.state import State, Trial

from tests.unit.test_embryo_declarations import VERB


def _state(proposals: int = 0, trials: int = 0) -> State:
    state = State()
    for _ in range(proposals):
        propose(state, {"kind": "verb", "title": "t", "rationale": "r", "verb": VERB})
    for _ in range(trials):
        tid = state.new_trial_id()
        state.trials[tid] = Trial(
            id=tid, verb=parse_verb(VERB), runs=[{}], recipe_id=None, status="done", results=[]
        )
    return state


def test_an_id_that_exists_is_not_reported() -> None:
    state = _state(proposals=2, trials=1)
    assert unknown_references("p-1 supersedes p-2; t-1 showed it", state) == []


def test_an_id_the_membrane_never_issued_is_reported() -> None:
    """2026-09-10-run-6, turn 9-count-1: the reply named `p-9` as proposed; the
    turn's record was `proposals: []` and the list ended at p-8."""
    state = _state(proposals=8)
    assert unknown_references("**`p-9` — `call_count` v2.** p-9 supersedes p-7.", state) == ["p-9"]


def test_both_kinds_are_reported_once_each_in_order() -> None:
    state = _state(proposals=1, trials=1)
    text = "t-3 then P-12, then p-2 again and p-12 again, and t-3"
    assert unknown_references(text, state) == ["p-2", "p-12", "t-3"]


def test_a_hyphenated_word_or_a_bare_number_is_not_an_id() -> None:
    state = _state()
    assert unknown_references("step-9, up-2, http-2, p9, p-, 12, top-1", state) == []


def test_a_past_or_hypothetical_mention_is_still_a_fact() -> None:
    """The check is a lookup, and what it appends is a fact, not a correction:
    a reply that says "if I had proposed p-3" gets told p-3 does not exist,
    which is true and harmless. The issue's argument for the inbox shape."""
    state = _state(proposals=2)
    assert unknown_references("if I had proposed p-3 last turn", state) == ["p-3"]


def test_describe_names_what_exists_in_the_voice_of_a_build_result() -> None:
    state = _state(proposals=8)
    assert describe(turn=9, unknown=["p-9"], state=state) == (
        "your reply on turn 9 named p-9; no such proposal exists; the proposals are p-1 to p-8"
    )


def test_describe_covers_trials_and_more_than_one_id() -> None:
    state = _state(proposals=1, trials=2)
    assert describe(turn=4, unknown=["p-2", "p-5", "t-3"], state=state) == (
        "your reply on turn 4 named p-2, p-5, t-3; no such proposal or trial exists; "
        "the proposals are p-1; the trials are t-1 to t-2"
    )


def test_describe_when_nothing_of_that_kind_exists() -> None:
    state = _state()
    assert describe(turn=1, unknown=["t-1"], state=state) == (
        "your reply on turn 1 named t-1; no such trial exists; there are no trials"
    )
