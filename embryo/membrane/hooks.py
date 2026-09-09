"""Pre-turn hooks (spec §6 step 1): verbs the membrane runs before the model
with the decoded payload as their single parameter. They fail closed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from membrane.principals import ANONYMOUS, principal_from_hook
from membrane.verbs import invoke

if TYPE_CHECKING:
    from membrane.declarations import Policy
    from membrane.mshkn import MshknApi
    from membrane.state import State


async def principal_for(
    api: MshknApi,
    state: State,
    policy: Policy,
    payload_text: str,
    *,
    remaining: float,
    runs: list[dict[str, Any]] | None = None,
) -> str:
    """The principal the hooks name, anonymous if none does. Every hook actually
    invoked is appended to `runs` (name, computer, exit code, what it named), so
    the audit line shows why a knock was or was not recognised (#101)."""
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
        exit_code = result.get("exit_code")
        if result.get("status") != "ok":
            principal = ANONYMOUS
        else:
            principal = principal_from_hook(
                entry.verb.asserts,
                result.get("stdout", ""),
                exit_code if isinstance(exit_code, int) else 1,
            )
        if runs is not None:
            runs.append(
                {
                    "name": name,
                    "status": result.get("status"),
                    "computer_id": result.get("computer_id"),
                    "exit_code": exit_code,
                    "principal": principal,
                }
            )
        if principal != ANONYMOUS:
            return principal
    return ANONYMOUS
