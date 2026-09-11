"""Which effort a model call is made at (#122).

Effort used to be chosen once, at hatch, and applied to every call of a life.
It is chosen here instead, per request, from three things that can only raise
each other: the run's default (`MEMBRANE_EFFORT`), a prior taken from the
reversibility of what the turn may do, and what the model itself asked for with
the `effort` tool. Nothing lowers the default -- the operator sets the floor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
# What the Messages API applies when `output_config` is absent. `None` therefore
# means "the API's default", not "the bottom of the ladder", and the two must not
# be confused: a model asking for `low` against an unset default must not get it.
API_DEFAULT = "high"

# The reversibility line of the declaration schema (spec §"Verbs"). A `local` or
# `read` verb is retryable -- a bad verb is discarded before approval, a bad
# computer is destroyed -- so deliberation has little to buy. The other three are
# not: a sent message, a payment, a changed account.
RETRYABLE: frozenset[str] = frozenset({"local", "read"})
IRREVERSIBLE_EFFORT = "high"


def prior_for(effects: Iterable[str]) -> str | None:
    """The effort the turn's tool list argues for, or None where it has no opinion.

    The membrane must choose before the call, so what the turn *may* do is the only
    proxy it has for what the turn is *about to* do. Today this is inert: §10.8
    (`membrane.invariants.refuse_approval`) lets the embryo approve only `local`
    and `read` verbs, so every tool it can ever hold is on the retryable side.
    """
    return IRREVERSIBLE_EFFORT if any(e not in RETRYABLE for e in effects) else None


def highest(*candidates: str | None) -> str | None:
    """The most deliberation any of them names, or None where none of them names any.

    The model's standing request accumulates through this and not through `resolve`:
    two requests are weighed against each other, and neither of them is the API's
    default, so neither may be floored at it.
    """
    named = [e for e in candidates if e is not None]
    return max(named, key=EFFORTS.index) if named else None


def resolve(*, default: str | None, prior: str | None, requested: str | None) -> str | None:
    """This call's effort: the run's default, raised by the prior and by the model's
    request, never lowered. None is the API's default and stays off the wire unless
    something outranks it."""
    floor = API_DEFAULT if default is None else default
    best = highest(floor, prior, requested)
    return default if best == floor else best
