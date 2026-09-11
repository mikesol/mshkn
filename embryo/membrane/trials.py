"""try (spec §5): build a declaration and run its invocations in order on computers
with no secrets and no policy check; a `chain` verb's invocations share a scratch
chain that is discarded with the trial (#118). Returns the build log and every
invocation's output as data."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from membrane.declarations import DeclarationError, parse_runs, parse_verb, render_command
from membrane.mshkn import Deferred, MshknError
from membrane.state import InboxItem, Trial
from membrane.verbs import log_tail, run_result_doc, submit_recipe, wait_for_recipe

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from membrane.mshkn import MshknApi
    from membrane.state import State

TRIAL_MARGIN = 30.0
# A trial's computer needs at least this long to boot and run; with less left in
# the turn the trial stays `building` for the next turn to run (#109).
RUN_MARGIN = 20.0
TRIAL_CHAIN_PREFIX = "verb/trial/"
# A chain trial's invocations share this label, never the verb's own `verb/<name>`:
# a verb name holds no slash, so the two can never collide, and a supersede of a
# catalogued verb cannot touch the live chain (#118).


async def _one_run(
    api: MshknApi, trial: Trial, params: dict[str, Any], *, remaining: float
) -> dict[str, Any]:
    """One invocation. A `chain` verb's first run creates the scratch chain; later
    runs fork it, exactly as `verbs.invoke` advances a catalogued verb's chain."""
    assert trial.recipe_id is not None
    verb = trial.verb
    try:
        command = render_command(verb, params)
        if verb.state == "chain" and trial.chain is not None:
            forked = await api.fork_label(label=trial.chain, command=command, timeout=remaining)
            if isinstance(forked, Deferred):
                return {"status": "error", "error": f"deferred {forked.deferred_id}"}
            run = forked
        else:
            label = TRIAL_CHAIN_PREFIX + trial.id if verb.state == "chain" else None
            run = await api.create_computer(
                recipe_id=trial.recipe_id,
                command=command,
                needs=verb.needs,
                label=label,
                timeout=remaining,
            )
            trial.chain = label
    except (DeclarationError, MshknError) as exc:
        return {"status": "error", "error": str(exc)}
    doc = run_result_doc(run)
    if verb.state == "chain":
        doc["chain_head"] = run.created_checkpoint_id
    return doc


async def sweep_trial(api: MshknApi, trial: Trial) -> None:
    """Discard the scratch chain. #93 retention keeps the newest checkpoint of every
    label forever, so an unswept trial leaks one. Idempotent: a label with no
    checkpoints is swept by doing nothing."""
    if trial.chain is None or trial.swept:
        trial.swept = True
        return
    try:
        for ckpt in await api.list_checkpoints(trial.chain):
            await api.delete_checkpoint(ckpt.id)
    except MshknError:
        # The next turn's poll tries again; a leaked scratch chain is not worth a
        # crashed turn.
        return
    trial.swept = True


async def run_trial(
    api: MshknApi,
    trial: Trial,
    *,
    remaining: float,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Every invocation in order, while the turn has room for one. A non-zero exit is
    a reading and does not stop the sequence; an `error` does, since the next
    invocation would measure nothing."""
    until = now() + remaining
    results: list[dict[str, Any]] = []
    stopped = False
    for params in trial.runs:
        left = until - now()
        if left < RUN_MARGIN:
            break
        doc = await _one_run(api, trial, params, remaining=left)
        results.append(doc)
        if doc["status"] == "error":
            stopped = True
            break
    trial.results = results
    if len(results) < len(trial.runs) and not stopped:
        # Out of time: the sweep would spend time the turn does not have, so the
        # next turn's poll does it.
        return {
            "runs": results,
            "status": "out of time",
            "ran": len(results),
            "of": len(trial.runs),
        }
    await sweep_trial(api, trial)
    return {"runs": results, "status": "done"}


def _describe(trial: Trial, result: dict[str, Any]) -> str:
    runs = result.get("runs") or []
    head = f"trial {trial.id} of {trial.verb.name}: {len(runs)} of {len(trial.runs)} ran"
    blocks = [
        f"run {i + 1}: exit {r.get('exit_code')}\n"
        f"stdout:\n{r.get('stdout', '')}\nstderr:\n{r.get('stderr', '')}"
        if r.get("status") != "error"
        else f"run {i + 1}: error {r.get('error')}"
        for i, r in enumerate(runs)
    ]
    return "\n".join([head, *blocks])


async def try_verb(
    api: MshknApi,
    state: State,
    doc: object,
    params: object = None,
    *,
    runs: object = None,
    until: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    try:
        verb = parse_verb(doc)
        invocations = parse_runs(params, runs)
    except DeclarationError as exc:
        return {"status": "invalid", "error": str(exc)}
    trial = Trial(
        id=state.new_trial_id(),
        verb=verb,
        runs=invocations,
        recipe_id=None,
        status="building",
        results=[],
    )
    state.trials[trial.id] = trial
    try:
        info = await submit_recipe(api, verb.dockerfile)
    except MshknError as exc:
        trial.status = "failed"
        trial.build_log = exc.detail
        return {"status": "failed", "trial": trial.id, "build_log": exc.detail}
    trial.recipe_id = info.id
    info = await wait_for_recipe(api, info.id, until=until - TRIAL_MARGIN, now=now, sleep=sleep)
    if info.status == "failed":
        trial.status = "failed"
        trial.build_log = log_tail(info.build_log)
        return {"status": "failed", "trial": trial.id, "build_log": log_tail(info.build_log)}
    if info.status != "ready" or until - now() < RUN_MARGIN:
        # Built but no time left to run it: the next turn's poll runs it (§5).
        return {"status": "building", "trial": trial.id}
    result = await run_trial(api, trial, remaining=until - now(), now=now)
    trial.status = "done"
    trial.build_log = log_tail(info.build_log)
    return {"trial": trial.id, "build_log": trial.build_log, **result}


async def poll_trials(api: MshknApi, state: State, *, remaining: float) -> list[InboxItem]:
    items: list[InboxItem] = []
    for trial in state.trials.values():
        if trial.status != "building" or trial.recipe_id is None:
            # A trial whose turn died between its last run and its sweep, or one that
            # ran out of time, leaves a scratch chain for this turn to discard (#118).
            if trial.chain is not None and not trial.swept:
                await sweep_trial(api, trial)
            continue
        info = await api.get_recipe(trial.recipe_id)
        if info.status == "ready":
            result = await run_trial(api, trial, remaining=remaining)
            trial.status = "done"
            trial.build_log = log_tail(info.build_log)
            items.append(InboxItem(kind="trial", text=_describe(trial, result)))
        elif info.status == "failed":
            trial.status = "failed"
            tail = log_tail(info.build_log)
            trial.build_log = tail
            items.append(
                InboxItem(
                    kind="trial",
                    text=f"trial {trial.id} of {trial.verb.name} failed to build:\n{tail}",
                )
            )
    return items
