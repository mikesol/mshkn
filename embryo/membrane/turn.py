"""A say turn (spec §6): principal, builds, input, tools, loop, close, audit."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, Literal

from membrane.declarations import DeclarationError
from membrane.hooks import principal_for
from membrane.invariants import door_is_open, may_invoke, may_propose
from membrane.loop import Tool, run_loop
from membrane.memory import Provenance
from membrane.principals import ROOT, is_authenticated, namespace_of
from membrane.proposals import propose
from membrane.state import WINDOW, Exchange
from membrane.trials import poll_trials, try_verb
from membrane.verbs import invoke, poll_builds

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from membrane.memory import MemoryStore
    from membrane.model import Model
    from membrane.mshkn import MshknApi
    from membrane.state import Brain, CatalogEntry, InboxItem, State

Door = Literal["api", "ingress"]
TURN_DEADLINE = 240.0
DOOR_CLOSED = "The public door is closed."
BAD_PAYLOAD = "The payload is not base64."

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


async def say(
    *,
    brain: Brain,
    state: State,
    api: MshknApi,
    model: Model,
    memory: MemoryStore,
    payload_b64: str,
    door: Door,
    deadline: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    decoded = decode_payload(payload_b64)
    if decoded is None:
        return (
            audit_line(door=door, principal=None, error="bad payload") + "\n" + BAD_PAYLOAD + "\n"
        )
    message, payload_text = decoded
    policy = state.policy

    # 1. principal
    if door == "api":
        principal = ROOT
    else:
        if not door_is_open(policy):
            return audit_line(door=door, principal=None, closed=True) + "\n" + DOOR_CLOSED + "\n"
        principal = await principal_for(
            api, state, policy, payload_text, remaining=deadline - now()
        )
    if namespace_of(principal) is not None:
        state.principals.add(principal)

    # 2. builds
    state.turn += 1
    turn = state.turn
    state.inbox.extend(await poll_builds(api, state))
    state.inbox.extend(await poll_trials(api, state, remaining=deadline - now()))

    # 3. input
    # Ruling P2: the inbox is drained only for an authenticated principal
    # (§10.7 denies anonymous try/propose, so it holds nothing to act on the
    # inbox with). An anonymous turn still polls above, so a build or trial
    # transition lands in state.inbox for the next authenticated turn to see.
    if is_authenticated(principal):
        inbox, state.inbox = state.inbox, []
    else:
        inbox = []
    recalled = memory.recall(message, principal=principal) if is_authenticated(principal) else []
    user = compose_input(
        turn=turn, principal=principal, door=door, inbox=inbox, recalled=recalled, message=message
    )

    # 4. tools
    provenance = Provenance(principal=principal, door=door, turn=turn)
    made: list[str] = []
    tools: dict[str, Tool] = {}

    async def remember(inp: dict[str, Any]) -> dict[str, Any]:
        memory.add(str(inp.get("text", "")), provenance)
        return {"status": "remembered"}

    async def do_try(inp: dict[str, Any]) -> dict[str, Any]:
        params = inp.get("params") or {}
        return await try_verb(
            api, state, inp.get("verb"), dict(params), until=deadline, now=now, sleep=sleep
        )

    async def do_propose(inp: dict[str, Any]) -> dict[str, Any]:
        try:
            proposal = propose(state, inp)
        except DeclarationError as exc:
            return {"status": "invalid", "error": str(exc)}
        made.append(proposal.id)
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
                    api, verb_entry.verb, inp, recipe_id=recipe_id, remaining=deadline - now()
                )

            return handler

        tools[name] = Tool(entry.verb.tool(), make(entry))

    # 5. loop
    system = brain.seed()
    described = state.self_description
    if described:
        system = f"{system}\n\n{described}"
    result = await run_loop(
        model,
        system=system,
        history=history_from(state.window),
        user=user,
        tools=tools,
        deadline=deadline,
        now=now,
    )

    # 6. close
    proposals_made = [
        {
            "id": pid,
            "sha256": hashlib.sha256(
                json.dumps(state.proposals[pid].to_doc(), sort_keys=True).encode()
            ).hexdigest(),
        }
        for pid in made
    ]
    write_memory = is_authenticated(principal)
    if write_memory:
        memory.add(f"{principal}: {message}\nembryo: {result.text}", provenance)
    state.window.append(
        Exchange(turn=turn, principal=principal, door=door, input=message, reply=result.text)
    )
    del state.window[:-WINDOW]
    audit = audit_line(
        turn=turn,
        principal=principal,
        door=door,
        # What the principal was offered, not only what the model called: §10.7
        # is an authorization claim about the tool list, and this is what makes
        # it readable from the exec_log alone (§10.5).
        offered=sorted(tools),
        tools=[_tool_summary(c) for c in result.calls],
        proposals=proposals_made,
        memory_written=write_memory,
        stopped=result.stopped,
        # The cost of the turn, from the model's own usage reports (#101). mem0's
        # extraction and embedding calls are not counted here.
        model_calls=result.model_calls,
        usage=result.usage,
    )
    out = [audit, result.text]
    for pid in made:
        out.append(f"proposal {pid}")
        out.append(json.dumps(state.proposals[pid].to_doc(), indent=1))
    return "\n".join(out) + "\n"
