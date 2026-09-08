"""try (spec §5): build a declaration and run it on a computer with no secrets,
no chain and no policy check; return the build log and the output as data."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from membrane.declarations import DeclarationError, parse_verb, render_command
from membrane.mshkn import MshknError
from membrane.state import InboxItem, Trial
from membrane.verbs import log_tail, run_result_doc, submit_recipe, wait_for_recipe

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from membrane.mshkn import MshknApi
    from membrane.state import State

TRIAL_MARGIN = 30.0


async def run_trial(api: MshknApi, trial: Trial, *, remaining: float) -> dict[str, Any]:
    assert trial.recipe_id is not None
    try:
        command = render_command(trial.verb, trial.params)
        run = await api.create_computer(
            recipe_id=trial.recipe_id,
            command=command,
            needs=trial.verb.needs,
            label=None,
            timeout=remaining,
        )
    except (DeclarationError, MshknError) as exc:
        return {"status": "error", "error": str(exc)}
    return {**run_result_doc(run), "status": "done"}


def _describe(trial: Trial, result: dict[str, Any]) -> str:
    return (
        f"trial {trial.id} of {trial.verb.name}: exit {result.get('exit_code')}\n"
        f"stdout:\n{result.get('stdout', '')}\nstderr:\n{result.get('stderr', '')}"
    )


async def try_verb(
    api: MshknApi,
    state: State,
    doc: object,
    params: dict[str, Any],
    *,
    until: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    try:
        verb = parse_verb(doc)
    except DeclarationError as exc:
        return {"status": "invalid", "error": str(exc)}
    trial = Trial(
        id=state.new_trial_id(),
        verb=verb,
        params=params,
        recipe_id=None,
        status="building",
        result=None,
    )
    state.trials[trial.id] = trial
    try:
        info = await submit_recipe(api, verb.dockerfile)
    except MshknError as exc:
        trial.status = "failed"
        trial.result = {"build_log": exc.detail}
        return {"status": "failed", "trial": trial.id, "build_log": exc.detail}
    trial.recipe_id = info.id
    info = await wait_for_recipe(api, info.id, until=until - TRIAL_MARGIN, now=now, sleep=sleep)
    if info.status == "failed":
        trial.status = "failed"
        trial.result = {"build_log": log_tail(info.build_log)}
        return {"status": "failed", "trial": trial.id, "build_log": log_tail(info.build_log)}
    if info.status != "ready":
        return {"status": "building", "trial": trial.id}
    result = await run_trial(api, trial, remaining=max(until - now(), 1.0))
    trial.status = "done"
    trial.result = result
    return {"trial": trial.id, "build_log": log_tail(info.build_log), **result}


async def poll_trials(api: MshknApi, state: State, *, remaining: float) -> list[InboxItem]:
    items: list[InboxItem] = []
    for trial in state.trials.values():
        if trial.status != "building" or trial.recipe_id is None:
            continue
        info = await api.get_recipe(trial.recipe_id)
        if info.status == "ready":
            result = await run_trial(api, trial, remaining=remaining)
            trial.status = "done"
            trial.result = result
            items.append(InboxItem(kind="trial", text=_describe(trial, result)))
        elif info.status == "failed":
            trial.status = "failed"
            tail = log_tail(info.build_log)
            trial.result = {"build_log": tail}
            items.append(
                InboxItem(
                    kind="trial",
                    text=f"trial {trial.id} of {trial.verb.name} failed to build:\n{tail}",
                )
            )
    return items
