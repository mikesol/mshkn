"""Which effort a model call is made at (#122): the run's default, raised by the
reversibility of what the turn may do and by what the model asked for."""

from __future__ import annotations

from membrane.effort import API_DEFAULT, EFFORTS, highest, prior_for, resolve


def test_the_ladder_runs_from_least_to_most_deliberation() -> None:
    assert EFFORTS == ("low", "medium", "high", "xhigh", "max")
    assert API_DEFAULT in EFFORTS


def test_a_retryable_tool_list_asks_for_nothing() -> None:
    """local and read are discarded before they change anything outside the fork."""
    assert prior_for([]) is None
    assert prior_for(["local", "read", "local"]) is None


def test_an_irreversible_effect_raises_the_prior() -> None:
    for effect in ("communicate", "transact", "administer"):
        assert prior_for(["local", "read", effect]) == "high"


def test_an_unset_default_stays_off_the_wire() -> None:
    assert resolve(default=None, prior=None, requested=None) is None


def test_the_run_default_is_the_floor_when_nothing_outranks_it() -> None:
    assert resolve(default="medium", prior=None, requested=None) == "medium"


def test_the_prior_raises_the_run_default() -> None:
    assert resolve(default="medium", prior="high", requested=None) == "high"


def test_the_model_request_raises_the_run_default() -> None:
    assert resolve(default="medium", prior=None, requested="max") == "max"


def test_neither_the_prior_nor_the_request_can_lower_the_run_default() -> None:
    assert resolve(default="high", prior=None, requested="low") == "high"
    assert resolve(default="max", prior="high", requested="medium") == "max"


def test_a_request_below_the_api_default_leaves_an_unset_default_unset() -> None:
    """`None` is the API's default, not the bottom of the ladder: a model that asks
    for less than it must not win by the run having named no default."""
    assert resolve(default=None, prior=None, requested="low") is None
    assert resolve(default=None, prior=None, requested=API_DEFAULT) is None
    assert resolve(default=None, prior=None, requested="max") == "max"


def test_highest_names_the_most_deliberation_asked_for() -> None:
    """The model's standing request accumulates here rather than through `resolve`:
    these are requests against each other, and none of them is the API's default."""
    assert highest() is None
    assert highest(None, None) is None
    assert highest(None, "medium") == "medium"
    assert highest("xhigh", "medium") == "xhigh"
    assert highest("low", None) == "low"
