"""Invocations (spec §4): ephemeral verbs run on a fresh computer from their
recipe; chain verbs advance a labelled checkpoint chain by fork-by-label (#89).
Builds are polled once per turn (§5)."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from membrane.declarations import DeclarationError, render_command
from membrane.mshkn import Deferred, MshknError
from membrane.state import InboxItem

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from membrane.declarations import Verb
    from membrane.mshkn import MshknApi, RecipeInfo, RunResult
    from membrane.state import State

POLL_INTERVAL = 3.0
LOG_TAIL = 2000


def log_tail(text: str | None, limit: int = LOG_TAIL) -> str:
    return (text or "")[-limit:]


def run_result_doc(run: RunResult) -> dict[str, Any]:
    return {
        "status": "ok",
        "computer_id": run.computer_id,
        "exit_code": run.exit_code,
        "stdout": run.stdout,
        "stderr": run.stderr,
    }


async def submit_recipe(api: MshknApi, dockerfile: str) -> RecipeInfo:
    return await api.create_recipe(dockerfile)


async def wait_for_recipe(
    api: MshknApi,
    recipe_id: str,
    *,
    until: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RecipeInfo:
    info = await api.get_recipe(recipe_id)
    while info.status not in ("ready", "failed") and now() < until:
        await sleep(POLL_INTERVAL)
        info = await api.get_recipe(recipe_id)
    return info


async def chain_head(api: MshknApi, verb: Verb) -> tuple[str | None, int]:
    checkpoints = await api.list_checkpoints(verb.chain)
    if not checkpoints:
        return None, 0
    return checkpoints[0].id, len(checkpoints)


async def invoke(
    api: MshknApi, verb: Verb, params: dict[str, Any], *, recipe_id: str, remaining: float
) -> dict[str, Any]:
    if remaining <= 0:
        return {"verb": verb.name, "status": "error", "error": "out of time"}
    try:
        command = render_command(verb, params)
    except DeclarationError as exc:
        return {"verb": verb.name, "status": "error", "error": str(exc)}
    try:
        if verb.state == "ephemeral":
            run = await api.create_computer(
                recipe_id=recipe_id,
                command=command,
                needs=verb.needs,
                label=None,
                timeout=remaining,
            )
        else:
            head, _ = await chain_head(api, verb)
            if head is None:
                run = await api.create_computer(
                    recipe_id=recipe_id,
                    command=command,
                    needs=verb.needs,
                    label=verb.chain,
                    timeout=remaining,
                )
            else:
                forked = await api.fork_label(label=verb.chain, command=command, timeout=remaining)
                if isinstance(forked, Deferred):
                    return {
                        "verb": verb.name,
                        "status": "deferred",
                        "deferred_id": forked.deferred_id,
                    }
                run = forked
    except MshknError as exc:
        return {"verb": verb.name, "status": "error", "error": exc.detail}
    result: dict[str, Any] = {"verb": verb.name, **run_result_doc(run)}
    if verb.state == "chain":
        result["chain_head"] = run.created_checkpoint_id
    return result


async def poll_builds(api: MshknApi, state: State) -> list[InboxItem]:
    items: list[InboxItem] = []
    for name, entry in state.catalog.items():
        if entry.status != "building" or entry.recipe_id is None:
            continue
        info = await api.get_recipe(entry.recipe_id)
        proposal = state.proposals.get(entry.proposal_id)
        if info.status == "ready":
            entry.status = "ready"
            if proposal is not None:
                proposal.status = "ready"
            items.append(InboxItem(kind="build", text=f"verb {name} is ready"))
        elif info.status == "failed":
            entry.status = "failed"
            tail = log_tail(info.build_log)
            if proposal is not None:
                proposal.status = "failed"
                proposal.log = tail
            items.append(
                InboxItem(
                    kind="build",
                    text=f"verb {name} failed to build (proposal {entry.proposal_id}):\n{tail}",
                )
            )
    return items
