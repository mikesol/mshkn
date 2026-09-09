"""A turn (relay design §6). `say` runs the principal, the builds, the input and
the tools as before, then posts the model request to the relay and acknowledges.
`resume <job_id>` is the wake-up: it appends the answer, runs its calls, and
posts the next request or closes the turn. Every command settles a pending turn
first. A `say` while one is pending is queued and runs next."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from membrane.declarations import DeclarationError
from membrane.hooks import principal_for
from membrane.invariants import door_is_open, may_invoke, may_propose
from membrane.loop import CAP_REACHED, OUT_OF_TOKENS, Tool, run_calls
from membrane.memory import Provenance
from membrane.model import add_usage, compose_request, parse_message, request_headers
from membrane.mshkn import MshknError
from membrane.principals import ROOT, is_authenticated, namespace_of
from membrane.proposals import propose
from membrane.state import WINDOW, Exchange, Pending, Queued
from membrane.trials import poll_trials, try_verb
from membrane.verbs import invoke, poll_builds

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from membrane.config import Settings
    from membrane.memory import MemoryStore
    from membrane.mshkn import MshknApi, RelayJob
    from membrane.state import Brain, CatalogEntry, InboxItem, State

Door = Literal["api", "ingress"]
# One fork's clock: bounds the tool runs of that fork, not the model (#110).
TURN_DEADLINE = 240.0
DOOR_CLOSED = "The public door is closed."
BAD_PAYLOAD = "The payload is not base64."
MODEL_FAILED = "The model service failed this turn."

REMEMBER_TOOL = {
    "name": "remember",
    "description": "Store a fact in memory with your provenance. Available to authenticated "
    "principals.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}
TRY_TOOL = {
    "name": "try",
    "description": "Build a verb declaration and run its entrypoint once on a computer with no "
    "secrets, no chain and no policy. Returns the build log, stdout, stderr and exit code as "
    "data. Installs nothing.",
    "input_schema": {
        "type": "object",
        "properties": {"verb": {"type": "object"}, "params": {"type": "object"}},
        "required": ["verb"],
    },
}
PROPOSE_TOOL = {
    "name": "propose",
    "description": "Propose a change to yourself for root to approve: a verb, a full replacement "
    "policy, or a full replacement of your self-description. A whole document, not a diff.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["verb", "policy", "prompt"]},
            "title": {"type": "string"},
            "rationale": {"type": "string"},
            "supersedes": {"type": ["string", "null"]},
            "verb": {"type": "object"},
            "policy": {"type": "object"},
            "prompt": {"type": "string"},
        },
        "required": ["kind", "title", "rationale"],
    },
}


@dataclass
class Context:
    """What every command runs with. Memory opens on first use: a `list` that
    settles nothing never pays for mem0."""

    brain: Brain
    state: State
    api: MshknApi
    settings: Settings
    deadline: float
    memory: MemoryStore | None = None
    open_memory: Callable[[], MemoryStore] | None = None
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def mem(self) -> MemoryStore:
        if self.memory is None:
            assert self.open_memory is not None, "no memory store to open"
            self.memory = self.open_memory()
        return self.memory


def decode_payload(b64: str) -> tuple[str, str] | None:
    try:
        text = base64.b64decode(b64, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return text, text
    if isinstance(body, dict) and isinstance(body.get("msg"), str):
        return body["msg"], text
    return text, text


def compose_input(
    *,
    turn: int,
    principal: str,
    door: str,
    inbox: list[InboxItem],
    recalled: list[str],
    message: str,
) -> str:
    inbox_text = "".join(f"- {item.text}\n" for item in inbox)
    recall_text = "".join(f"- {text}\n" for text in recalled)
    return (
        f"[turn {turn} | principal {principal} | door {door}]\n"
        f"inbox:\n{inbox_text}\nrecall:\n{recall_text}\nmessage:\n{message}"
    )


def history_from(window: list[Exchange]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for exchange in window[-WINDOW:]:
        history.append(
            {
                "role": "user",
                "content": f"[{exchange.principal} via {exchange.door}] {exchange.input}",
            }
        )
        history.append({"role": "assistant", "content": exchange.reply})
    return history


def audit_line(**fields: Any) -> str:
    return "audit " + json.dumps(fields, sort_keys=True)


def _tool_summary(call: dict[str, Any]) -> dict[str, Any]:
    result = call["result"]
    summary: dict[str, Any] = {"name": call["name"], "status": result.get("status")}
    for key in ("exit_code", "computer_id", "chain_head", "id", "trial", "error"):
        if key in result:
            summary[key] = result[key]
    return summary


def _excerpt(body: object, limit: int = 300) -> str:
    text = body if isinstance(body, str) else json.dumps(body)
    return text[:limit]


def build_tools(ctx: Context, pending: Pending) -> dict[str, Tool]:
    """The tools of this fork, from the current policy and catalog (§6 step 4):
    a verb approved while the model thought is usable on the next request."""
    state, policy, principal = ctx.state, ctx.state.policy, pending.principal
    provenance = Provenance(principal=principal, door=pending.door, turn=pending.turn)
    tools: dict[str, Tool] = {}

    async def remember(inp: dict[str, Any]) -> dict[str, Any]:
        ctx.mem().add(str(inp.get("text", "")), provenance)
        return {"status": "remembered"}

    async def do_try(inp: dict[str, Any]) -> dict[str, Any]:
        params = inp.get("params") or {}
        return await try_verb(
            ctx.api,
            state,
            inp.get("verb"),
            dict(params),
            until=ctx.deadline,
            now=ctx.now,
            sleep=ctx.sleep,
        )

    async def do_propose(inp: dict[str, Any]) -> dict[str, Any]:
        try:
            proposal = propose(state, inp)
        except DeclarationError as exc:
            return {"status": "invalid", "error": str(exc)}
        pending.made.append(proposal.id)
        return proposal.to_doc()

    if is_authenticated(principal):
        tools["remember"] = Tool(REMEMBER_TOOL, remember)
        if may_propose(principal, policy):
            tools["try"] = Tool(TRY_TOOL, do_try)
            tools["propose"] = Tool(PROPOSE_TOOL, do_propose)
    for name, entry in state.catalog.items():
        if (
            entry.status != "ready"
            or entry.recipe_id is None
            or not may_invoke(principal, entry.verb, policy)
        ):
            continue

        def make(
            verb_entry: CatalogEntry,
        ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
            recipe_id = verb_entry.recipe_id
            assert recipe_id is not None  # the loop above filtered on it

            async def handler(inp: dict[str, Any]) -> dict[str, Any]:
                return await invoke(
                    ctx.api,
                    verb_entry.verb,
                    inp,
                    recipe_id=recipe_id,
                    remaining=ctx.deadline - ctx.now(),
                )

            return handler

        tools[name] = Tool(entry.verb.tool(), make(entry))
    return tools


async def post_request(ctx: Context, pending: Pending, tools: dict[str, Tool]) -> None:
    """Compose the request and hand it to the relay; the job id is the turn's pointer."""
    settings = ctx.settings
    system = ctx.brain.seed()
    if ctx.state.self_description:
        system = f"{system}\n\n{ctx.state.self_description}"
    body = compose_request(
        model_id=settings.model_id,
        system=system,
        # A snapshot: the next fork appends to `pending.messages`, and the request
        # already handed to the relay must not change under it.
        messages=list(pending.messages),
        tools=[t.definition for t in tools.values()],
        effort=settings.effort,
    )
    pending.job = await ctx.api.create_relay_job(
        target=f"{settings.anthropic_base_url}/v1/messages",
        headers=request_headers(settings.anthropic_api_key),
        body=body,
    )
    pending.model_calls += 1


async def start_turn(
    ctx: Context,
    *,
    principal: str,
    door: str,
    message: str,
    payload_text: str,
    hook_runs: list[dict[str, Any]],
) -> str:
    """Steps 2 to 4 of §6, then the first request. Returns the start audit line
    and the acknowledgement."""
    state = ctx.state
    if namespace_of(principal) is not None:
        state.principals.add(principal)
    state.turn += 1
    turn = state.turn
    state.inbox.extend(await poll_builds(ctx.api, state))
    state.inbox.extend(await poll_trials(ctx.api, state, remaining=ctx.deadline - ctx.now()))
    # Ruling P2 stands: only an authenticated principal drains the inbox. An
    # anonymous turn still polls above, so a build or trial transition lands in
    # state.inbox for the next authenticated turn to see.
    if is_authenticated(principal):
        inbox, state.inbox = state.inbox, []
    else:
        inbox = []
    recalled = ctx.mem().recall(message, principal=principal) if is_authenticated(principal) else []
    user = compose_input(
        turn=turn, principal=principal, door=door, inbox=inbox, recalled=recalled, message=message
    )
    pending = Pending(
        turn=turn,
        principal=principal,
        door=door,
        message=message,
        payload=payload_text,
        messages=[*history_from(state.window), {"role": "user", "content": user}],
        offered=[],
        job="",
        hooks=hook_runs,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        memory_written=is_authenticated(principal),
    )
    tools = build_tools(ctx, pending)
    # What the principal was offered, not only what the model called: §10.7 is an
    # authorization claim about the tool list, and this is what makes it readable
    # from the exec_log alone (§10.5).
    pending.offered = sorted(tools)
    state.pending = pending
    await post_request(ctx, pending, tools)
    ack = json.dumps({"turn": turn, "job": pending.job})
    started = audit_line(
        turn=turn,
        principal=principal,
        door=door,
        hooks=hook_runs,
        offered=pending.offered,
        job=pending.job,
        started=True,
    )
    return started + "\n" + ack + "\n"


async def say(ctx: Context, *, payload_b64: str, door: Door) -> str:
    decoded = decode_payload(payload_b64)
    if decoded is None:
        return (
            audit_line(door=door, principal=None, error="bad payload") + "\n" + BAD_PAYLOAD + "\n"
        )
    message, payload_text = decoded
    hook_runs: list[dict[str, Any]] = []
    if door == "api":
        principal = ROOT
    else:
        if not door_is_open(ctx.state.policy):
            return audit_line(door=door, principal=None, closed=True) + "\n" + DOOR_CLOSED + "\n"
        principal = await principal_for(
            ctx.api,
            ctx.state,
            ctx.state.policy,
            payload_text,
            remaining=ctx.deadline - ctx.now(),
            runs=hook_runs,
        )
    if ctx.state.pending is not None:
        ctx.state.queue.append(
            Queued(
                principal=principal,
                door=door,
                message=message,
                payload=payload_text,
                hooks=hook_runs,
            )
        )
        position = len(ctx.state.queue)
        queued = audit_line(door=door, principal=principal, hooks=hook_runs, queued=position)
        return queued + "\n" + json.dumps({"queued": position}) + "\n"
    return await start_turn(
        ctx,
        principal=principal,
        door=door,
        message=message,
        payload_text=payload_text,
        hook_runs=hook_runs,
    )


async def resume(ctx: Context, job_id: str) -> str:
    """The wake-up. A forged or stale id does nothing (§10.2)."""
    pending = ctx.state.pending
    if pending is None or pending.job != job_id:
        return f"no pending turn for job {job_id}\n"
    try:
        job = await ctx.api.get_relay_job(job_id)
    except MshknError as exc:
        if exc.status == 404:
            return await close_turn(
                ctx, text=f"{MODEL_FAILED} job {job_id} is gone", stopped="error"
            )
        raise
    return await finish(ctx, job) or f"job {job_id} is {job.status}\n"


async def settle(ctx: Context) -> str:
    """What every command does first: a pending job that has settled is finished
    now, whether or not its wake-up arrived. Nothing to do is an empty string."""
    pending = ctx.state.pending
    if pending is None:
        return ""
    try:
        job = await ctx.api.get_relay_job(pending.job)
    except MshknError as exc:
        if exc.status == 404:
            return await close_turn(
                ctx, text=f"{MODEL_FAILED} job {pending.job} is gone", stopped="error"
            )
        # Any other failure is the relay's, not this command's: leave the turn
        # pending and let the next command settle it.
        return ""
    return await finish(ctx, job)


async def finish(ctx: Context, job: RelayJob) -> str:
    if job.status in ("queued", "in_progress"):
        return ""
    if job.status != "completed":
        return await close_turn(ctx, text=f"{MODEL_FAILED} {job.error}", stopped="error")
    if job.response_status is None or not 200 <= job.response_status < 300:
        return await close_turn(
            ctx,
            text=f"{MODEL_FAILED} HTTP {job.response_status}: {_excerpt(job.response_body)}",
            stopped="error",
        )
    if not isinstance(job.response_body, dict):
        return await close_turn(
            ctx, text=f"{MODEL_FAILED} the response is not a message", stopped="error"
        )
    return await continue_turn(ctx, job.response_body)


async def continue_turn(ctx: Context, message: dict[str, Any]) -> str:
    pending = ctx.state.pending
    assert pending is not None
    completion = parse_message(message)
    pending.usage = add_usage(pending.usage, completion.usage)
    if completion.stop_reason == "max_tokens":
        # The budget ran out mid-response: whatever calls arrived are not run.
        return await close_turn(
            ctx, text=f"{OUT_OF_TOKENS} {completion.text}".strip(), stopped="max_tokens"
        )
    if not completion.calls:
        return await close_turn(ctx, text=completion.text, stopped="done")
    pending.messages.append({"role": "assistant", "content": completion.content})
    tools = build_tools(ctx, pending)
    before = len(pending.calls)
    results, outcome = await run_calls(
        completion, tools, pending, deadline=ctx.deadline, now=ctx.now
    )
    if outcome == "cap":
        return await close_turn(ctx, text=f"{CAP_REACHED} {completion.text}".strip(), stopped="cap")
    pending.messages.append({"role": "user", "content": results})
    previous = pending.job
    pending.forks += 1
    # §10.7 is an authorization claim about the tool list, and the turn's list is
    # every request's: a verb approved while the model thought is offered by the
    # request below, and a fork that ends on the cap posts none, so offers none.
    pending.offered = sorted(set(pending.offered) | set(tools))
    await post_request(ctx, pending, tools)
    summaries = [_tool_summary(c) for c in pending.calls[before:]]
    continued = audit_line(
        turn=pending.turn,
        job=previous,
        calls=summaries,
        next_job=pending.job,
        continued=True,
    )
    return continued + "\n"


async def close_turn(ctx: Context, *, text: str, stopped: str) -> str:
    """Step 6 of §6: memory, the window, the audit line, the reply and the
    proposals. Then the queue's head starts the next turn in this same fork."""
    state = ctx.state
    pending = state.pending
    assert pending is not None
    proposals_made = [
        {
            "id": pid,
            "sha256": hashlib.sha256(
                json.dumps(state.proposals[pid].to_doc(), sort_keys=True).encode()
            ).hexdigest(),
        }
        for pid in pending.made
    ]
    if pending.memory_written:
        ctx.mem().add(
            f"{pending.principal}: {pending.message}\nembryo: {text}",
            Provenance(principal=pending.principal, door=pending.door, turn=pending.turn),
        )
    audit: dict[str, Any] = {
        "turn": pending.turn,
        "principal": pending.principal,
        "door": pending.door,
        "hooks": pending.hooks,
        "offered": pending.offered,
        "tools": [_tool_summary(c) for c in pending.calls],
        "proposals": proposals_made,
        "memory_written": pending.memory_written,
        "stopped": stopped,
        # The cost of the turn, from the model's own usage reports (#101). mem0's
        # extraction and embedding calls are not counted here.
        "model_calls": pending.model_calls,
        "usage": pending.usage,
        "forks": pending.forks,
        "started_at": pending.started_at,
        "job": pending.job,
    }
    lines = [text]
    for pid in pending.made:
        lines.append(f"proposal {pid}")
        lines.append(json.dumps(state.proposals[pid].to_doc(), indent=1))
    output = "\n".join(lines) + "\n"
    state.window.append(
        Exchange(
            turn=pending.turn,
            principal=pending.principal,
            door=pending.door,
            input=pending.message,
            reply=text,
            output=output,
            audit=audit,
        )
    )
    del state.window[:-WINDOW]
    state.pending = None
    result = audit_line(**audit) + "\n" + output
    if state.queue:
        head = state.queue.pop(0)
        result += await start_turn(
            ctx,
            principal=head.principal,
            door=head.door,
            message=head.message,
            payload_text=head.payload,
            hook_runs=head.hooks,
        )
    return result
