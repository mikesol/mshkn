"""Pre-turn hooks (spec §6 step 1): verbs the membrane runs before the model
with the decoded payload as their single parameter. They fail closed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from membrane.principals import ANONYMOUS, principal_from_hook
from membrane.verbs import invoke

if TYPE_CHECKING:
    from membrane.declarations import Policy
    from membrane.mshkn import MshknApi
    from membrane.state import State


async def principal_for(
    api: MshknApi, state: State, policy: Policy, payload_text: str, *, remaining: float
) -> str:
    for name in policy.hooks:
        entry = state.catalog.get(name)
        if (
            entry is None
            or entry.status != "ready"
            or entry.recipe_id is None
            or entry.verb.asserts is None
        ):
            continue
        properties = entry.verb.params.get("properties", {})
        if len(properties) != 1:
            continue
        (param,) = properties
        result = await invoke(
            api, entry.verb, {param: payload_text}, recipe_id=entry.recipe_id, remaining=remaining
        )
        if result.get("status") != "ok":
            continue
        exit_code = result.get("exit_code")
        principal = principal_from_hook(
            entry.verb.asserts,
            result.get("stdout", ""),
            exit_code if isinstance(exit_code, int) else 1,
        )
        if principal != ANONYMOUS:
            return principal
    return ANONYMOUS
