"""The loop of a say turn (spec §6 step 5): system = seed + self, messages =
the window + the input; each tool call runs through its handler; the loop
ends at final text, the cap, or the deadline."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from membrane.model import Model

type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

CAP = 20
OUT_OF_TIME = "I ran out of time before finishing this turn."
CAP_REACHED = "I reached the tool-call cap for this turn."


@dataclass(frozen=True)
class Tool:
    definition: dict[str, Any]
    handler: Handler


@dataclass(frozen=True)
class LoopResult:
    text: str
    calls: list[dict[str, Any]]
    stopped: Literal["done", "cap", "deadline"]


async def run_loop(
    model: Model,
    *,
    system: str,
    history: list[dict[str, Any]],
    user: str,
    tools: dict[str, Tool],
    deadline: float,
    now: Callable[[], float] = time.monotonic,
    cap: int = CAP,
) -> LoopResult:
    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": user}]
    definitions = [t.definition for t in tools.values()]
    calls: list[dict[str, Any]] = []
    last_text = ""
    while True:
        if now() >= deadline:
            return LoopResult(
                text=f"{OUT_OF_TIME} {last_text}".strip(), calls=calls, stopped="deadline"
            )
        completion = await model.complete(system=system, messages=messages, tools=definitions)
        last_text = completion.text or last_text
        if not completion.calls:
            return LoopResult(text=completion.text, calls=calls, stopped="done")
        messages.append({"role": "assistant", "content": completion.content})
        results: list[dict[str, Any]] = []
        for call in completion.calls:
            if len(calls) >= cap:
                return LoopResult(
                    text=f"{CAP_REACHED} {last_text}".strip(), calls=calls, stopped="cap"
                )
            if now() >= deadline:
                return LoopResult(
                    text=f"{OUT_OF_TIME} {last_text}".strip(), calls=calls, stopped="deadline"
                )
            tool = tools.get(call.name)
            if tool is None:
                result: dict[str, Any] = {"status": "error", "error": f"no such tool {call.name}"}
            else:
                result = await tool.handler(call.input)
            calls.append({"name": call.name, "input": call.input, "result": result})
            results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}
            )
        messages.append({"role": "user", "content": results})
