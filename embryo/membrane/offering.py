"""What a policy offers (spec §6 step 4): the names of the tools a turn from one
principal would be given, under one policy, against one catalog.

One function, because there are two callers and they must not drift: `build_tools`
builds the turn's real tools from it, and `try_policy` reports it for a document
that has not been proposed (#171). It is pure -- no state, no mshkn, no model --
so a dry run of a candidate policy costs nothing and installs nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from membrane.invariants import may_invoke, may_propose
from membrane.principals import is_authenticated

if TYPE_CHECKING:
    from collections.abc import Mapping

    from membrane.declarations import Policy
    from membrane.state import CatalogEntry


def offered_names(principal: str, policy: Policy, catalog: Mapping[str, CatalogEntry]) -> list[str]:
    """The sorted tool names, which is what the audit line's `offered` is.

    `remember` and `effort` go to any authenticated principal -- `effort` under
    §10.7's gate for a reason of its own, that an anonymous caller who could ask for
    `max` on every turn is a cost attack through the public door; `try` and `propose`
    go to a principal whose grant lets it propose (§10.7 refuses anonymous outright);
    a catalogued verb to one the policy or the verb's own `allow` lets invoke it, and
    only once its build is `ready` with a recipe to run.
    """
    names: list[str] = []
    if is_authenticated(principal):
        names += ["remember", "effort"]
        if may_propose(principal, policy):
            names += ["try", "propose"]
    names += [
        name
        for name, entry in catalog.items()
        if entry.status == "ready"
        and entry.recipe_id is not None
        and may_invoke(principal, entry.verb, policy)
    ]
    return sorted(names)
