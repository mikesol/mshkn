"""Root commands (spec §6): list, approve, reject, disable, revert, and root's
own say. Each begins by polling builds and trials so list shows transitions."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from membrane.invariants import door_is_open
from membrane.proposals import approve, disable, reject, revert
from membrane.trials import poll_trials
from membrane.turn import say
from membrane.verbs import chain_head, poll_builds

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from membrane.memory import MemoryStore
    from membrane.model import Model
    from membrane.mshkn import MshknApi
    from membrane.state import Brain, State

USAGE = (
    "usage: membrane say <b64>\n"
    "       membrane root say <b64> | list | approve <id> | reject <id> <b64 reason> | "
    "disable <verb> | revert <id>\n"
)


async def list_state(api: MshknApi, state: State) -> str:
    policy = state.policy
    catalog: dict[str, Any] = {}
    for name, entry in state.catalog.items():
        head, length = (None, 0)
        if entry.verb.state == "chain":
            head, length = await chain_head(api, entry.verb)
        catalog[name] = {
            "status": entry.status,
            "effect": entry.verb.effect,
            "state": entry.verb.state,
            "recipe_id": entry.recipe_id,
            "chain_head": head,
            "chain_length": length,
        }
    # A declared hook only names a caller once its recipe is ready: principal_for
    # skips every other entry and the turn falls back to anonymous. `status` is
    # what policy says, `hooks_ready` is what the door can actually do (§10.6).
    hooks_ready = [
        hook
        for hook in policy.hooks
        if (hook_entry := state.catalog.get(hook)) is not None and hook_entry.status == "ready"
    ]
    listing = {
        "turn": state.turn,
        "door": {
            "status": "open" if door_is_open(policy) else "closed",
            "hooks": list(policy.hooks),
            "hooks_ready": hooks_ready,
        },
        "policy": policy.to_doc(),
        "principals": sorted(state.principals),
        "catalog": catalog,
        "proposals": [p.to_doc() for p in state.proposals.values()],
        # A trial's recipe is on the account too (§5); naming it here is what lets
        # the measure tell a trial's recipe from an undeclared one (#101).
        "trials": [
            {"id": t.id, "verb": t.verb.name, "status": t.status, "recipe_id": t.recipe_id}
            for t in state.trials.values()
        ],
        "inbox": len(state.inbox),
    }
    return json.dumps(listing, indent=1, sort_keys=True) + "\n"


def _decode(b64: str) -> str:
    return base64.b64decode(b64).decode(errors="replace")


async def root(
    argv: list[str],
    *,
    brain: Brain,
    state: State,
    api: MshknApi,
    model: Model | None,
    memory: MemoryStore | None,
    deadline: float,
    now: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[str, int]:
    command, args = argv[0], argv[1:]
    if command == "say":
        assert model is not None and memory is not None  # run() opens both for a say
        return (
            await say(
                brain=brain,
                state=state,
                api=api,
                model=model,
                memory=memory,
                payload_b64=args[0],
                door="api",
                deadline=deadline,
                now=now,
                sleep=sleep,
            ),
            0,
        )
    state.inbox.extend(await poll_builds(api, state))
    state.inbox.extend(await poll_trials(api, state, remaining=deadline - now()))
    try:
        if command == "list":
            return await list_state(api, state), 0
        if command == "approve":
            return (await approve(api, state, args[0])) + "\n", 0
        if command == "reject":
            return reject(state, args[0], _decode(args[1])) + "\n", 0
        if command == "disable":
            return disable(state, args[0]) + "\n", 0
        if command == "revert":
            return revert(state, args[0]) + "\n", 0
        # cli.run's _valid() never lets an unknown command reach here, but
        # root() must be safe when called directly too (P13): no
        # fall-through to revert for a command it does not recognize.
        return USAGE, 2
    except KeyError as exc:
        return f"no proposal {exc.args[0]}\n", 1
