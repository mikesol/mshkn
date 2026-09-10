"""The tool calls of one completion run against the handlers (relay design §6):
each gets a result, an unknown tool is an error, a call past the fork's clock
is an "out of time" result, and the cap ends the turn."""

from __future__ import annotations

from typing import Any

from membrane.loop import CAP, OUT_OF_TIME, Tool, run_calls
from membrane.state import Pending

from tests.support_embryo import text_completion, tool_call_completion


async def _echo(inp: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "echo": inp}


ECHO = Tool(
    definition={"name": "echo", "description": "d", "input_schema": {"type": "object"}},
    handler=_echo,
)


def _pending() -> Pending:
    return Pending(
        turn=1,
        principal="root",
        door="api",
        message="m",
        payload="m",
        messages=[],
        offered=["echo"],
        job="rj-1",
    )


async def test_each_call_gets_a_tool_result_block_and_is_recorded() -> None:
    pending = _pending()
    completion = tool_call_completion("echo", x=1)
    results, outcome = await run_calls(
        completion, {"echo": ECHO}, pending, deadline=1e9, now=lambda: 0.0
    )
    assert outcome == "ok"
    assert results == [
        {
            "type": "tool_result",
            "tool_use_id": "tu_echo",
            "content": '{"status": "ok", "echo": {"x": 1}}',
        }
    ]
    assert pending.calls == [
        {"name": "echo", "input": {"x": 1}, "result": {"status": "ok", "echo": {"x": 1}}}
    ]


async def test_no_calls_is_no_results() -> None:
    results, outcome = await run_calls(
        text_completion("hi"), {"echo": ECHO}, _pending(), deadline=1e9, now=lambda: 0.0
    )
    assert results == [] and outcome == "ok"


async def test_unknown_tools_are_errors_not_exceptions() -> None:
    pending = _pending()
    results, _ = await run_calls(
        tool_call_completion("nope"), {"echo": ECHO}, pending, deadline=1e9, now=lambda: 0.0
    )
    assert pending.calls[0]["result"] == {"status": "error", "error": "no such tool nope"}
    assert results[0]["tool_use_id"] == "tu_nope"


async def test_a_call_past_the_forks_clock_is_an_out_of_time_result_not_the_turns_end() -> None:
    pending = _pending()
    results, outcome = await run_calls(
        tool_call_completion("echo", x=1), {"echo": ECHO}, pending, deadline=10.0, now=lambda: 11.0
    )
    assert outcome == "ok"
    assert pending.calls[0]["result"] == {"status": "error", "error": OUT_OF_TIME}
    assert len(results) == 1


async def test_the_cap_counts_across_forks_and_ends_the_turn() -> None:
    pending = _pending()
    pending.calls = [{"name": "echo", "input": {}, "result": {}} for _ in range(CAP - 1)]
    results, outcome = await run_calls(
        tool_call_completion("echo", x=1), {"echo": ECHO}, pending, deadline=1e9, now=lambda: 0.0
    )
    assert outcome == "ok" and len(pending.calls) == CAP and len(results) == 1
    results, outcome = await run_calls(
        tool_call_completion("echo", x=2), {"echo": ECHO}, pending, deadline=1e9, now=lambda: 0.0
    )
    assert outcome == "cap" and results == [] and len(pending.calls) == CAP
