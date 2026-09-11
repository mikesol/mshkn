"""One completion's tool calls, run against the handlers (relay design §6). The
loop of a turn is a chain of forks: the relay posts the answer, `resume` runs
its calls here, posts the next request and exits. Every call gets a result (a
call past the fork's clock gets "out of time", so the model can retry); the cap
counts every call of the turn, across forks, and ends it."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from membrane.model import Completion
    from membrane.state import Pending

type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

CAP = 20
CAP_REACHED = "I reached the tool-call cap for this turn."
OUT_OF_TOKENS = "I ran out of output tokens before finishing this turn."
OUT_OF_TIME = "out of time"


@dataclass(frozen=True)
class Tool:
    definition: dict[str, Any]
    handler: Handler
    # What invoking it does outside the fork, on the declaration schema's scale
    # (spec §"Verbs"). The effort prior reads it (#122). The built-ins default to
    # `local`: none of them changes anything outside the membrane without an
    # approval, and `effort` does not change anything outside the membrane at all.
    effect: str = "local"


async def run_calls(
    completion: Completion,
    tools: dict[str, Tool],
    pending: Pending,
    *,
    deadline: float,
    now: Callable[[], float],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], Literal["ok", "cap"]]:
    """The tool_result blocks for the completion's calls, appended to
    `pending.calls` as they run. "cap" when a call would exceed the turn's cap:
    the caller ends the turn, so the results so far are not sent."""
    results: list[dict[str, Any]] = []
    for call in completion.calls:
        if len(pending.calls) >= cap:
            return results, "cap"
        tool = tools.get(call.name)
        if tool is None:
            result: dict[str, Any] = {"status": "error", "error": f"no such tool {call.name}"}
        elif now() >= deadline:
            result = {"status": "error", "error": OUT_OF_TIME}
        else:
            result = await tool.handler(call.input)
        pending.calls.append({"name": call.name, "input": call.input, "result": result})
        results.append(
            {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}
        )
    return results, "ok"
