"""The loop (spec §6 step 5) ends at the model's final text, the tool-call cap, or the deadline."""

from __future__ import annotations

from typing import Any

from membrane.loop import CAP, CAP_REACHED, OUT_OF_TIME, OUT_OF_TOKENS, Tool, run_loop
from membrane.model import Completion, ToolCall, zero_usage

from tests.support_embryo import StubModel, text_completion, tool_call_completion


async def _echo(inp: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "echo": inp}


ECHO = Tool(
    definition={"name": "echo", "description": "d", "input_schema": {"type": "object"}},
    handler=_echo,
)


async def test_a_turn_with_no_calls_is_one_completion() -> None:
    model = StubModel([text_completion("hello")])
    out = await run_loop(
        model, system="s", history=[], user="hi", tools={"echo": ECHO}, deadline=1e9
    )
    assert out.text == "hello" and out.calls == [] and out.stopped == "done"
    system, messages, tools = model.calls[0]
    assert system == "s" and messages == [{"role": "user", "content": "hi"}]
    assert tools == [ECHO.definition]


async def test_tool_calls_are_executed_and_their_results_returned_as_tool_result_blocks() -> None:
    model = StubModel([tool_call_completion("echo", x=1), text_completion("done: 1")])
    out = await run_loop(
        model,
        system="s",
        history=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "before"},
        ],
        user="go",
        tools={"echo": ECHO},
        deadline=1e9,
    )
    assert out.text == "done: 1" and out.stopped == "done"
    assert out.calls == [
        {"name": "echo", "input": {"x": 1}, "result": {"status": "ok", "echo": {"x": 1}}}
    ]
    _, messages, _ = model.calls[1]
    assert messages[0] == {"role": "user", "content": "earlier"}
    assert messages[2] == {"role": "user", "content": "go"}
    assert messages[3]["role"] == "assistant" and messages[3]["content"][0]["type"] == "tool_use"
    block = messages[4]["content"][0]
    assert (
        messages[4]["role"] == "user"
        and block["type"] == "tool_result"
        and block["tool_use_id"] == "tu_echo"
    )
    assert '"echo": {"x": 1}' in block["content"]


async def test_unknown_tools_are_errors_not_exceptions() -> None:
    model = StubModel([tool_call_completion("nope"), text_completion("ok")])
    out = await run_loop(model, system="s", history=[], user="u", tools={}, deadline=1e9)
    assert out.calls[0]["result"] == {"status": "error", "error": "no such tool nope"}


async def test_the_cap_ends_the_turn() -> None:
    model = StubModel([tool_call_completion("echo") for _ in range(CAP + 5)])
    out = await run_loop(
        model, system="s", history=[], user="u", tools={"echo": ECHO}, deadline=1e9
    )
    assert out.stopped == "cap" and len(out.calls) == CAP and CAP_REACHED in out.text


async def test_the_deadline_ends_the_turn() -> None:
    clock = iter([0.0, 0.0, 300.0, 300.0, 300.0])
    model = StubModel(
        [tool_call_completion("echo"), tool_call_completion("echo"), text_completion("late")]
    )
    out = await run_loop(
        model,
        system="s",
        history=[],
        user="u",
        tools={"echo": ECHO},
        deadline=240.0,
        now=lambda: next(clock),
    )
    assert out.stopped == "deadline" and OUT_OF_TIME in out.text and len(out.calls) == 1


async def test_the_deadline_can_be_crossed_between_calls_in_one_completion() -> None:
    # A single completion carrying two tool calls: the deadline check inside
    # the per-call loop (not just the top-of-turn check) must be reachable.
    two_calls = Completion(
        text="",
        calls=(
            ToolCall(id="tu_a", name="echo", input={}),
            ToolCall(id="tu_b", name="echo", input={}),
        ),
        content=[
            {"type": "tool_use", "id": "tu_a", "name": "echo", "input": {}},
            {"type": "tool_use", "id": "tu_b", "name": "echo", "input": {}},
        ],
    )
    clock = iter([0.0, 0.0, 300.0])
    model = StubModel([two_calls])
    out = await run_loop(
        model,
        system="s",
        history=[],
        user="u",
        tools={"echo": ECHO},
        deadline=240.0,
        now=lambda: next(clock),
    )
    assert out.stopped == "deadline" and OUT_OF_TIME in out.text and len(out.calls) == 1


async def test_usage_is_summed_over_the_completions_of_a_turn() -> None:
    first = Completion(
        text="",
        calls=(ToolCall("tu_1", "echo", {"x": 1}),),
        content=[{"type": "tool_use", "id": "tu_1", "name": "echo", "input": {"x": 1}}],
        usage={
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    )
    second = Completion(
        text="done",
        calls=(),
        content=[{"type": "text", "text": "done"}],
        usage={
            "input_tokens": 150,
            "output_tokens": 5,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 80,
        },
    )
    model = StubModel([first, second])
    out = await run_loop(
        model, system="s", history=[], user="go", tools={"echo": ECHO}, deadline=1e9
    )
    assert out.model_calls == 2
    assert out.usage == {
        "input_tokens": 250,
        "output_tokens": 15,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 80,
    }


async def test_a_scripted_completion_costs_nothing() -> None:
    model = StubModel([text_completion("hi")])
    out = await run_loop(model, system="s", history=[], user="go", tools={}, deadline=1e9)
    assert out.model_calls == 1 and out.usage == zero_usage()


async def test_an_exhausted_output_budget_ends_the_turn_honestly() -> None:
    # Live run 2026-09-09-run-1, turn 2: 8192 output tokens of thinking, no text, no
    # call, and the turn was reported as done with an empty reply.
    spent = Completion(text="", calls=(), content=[], stop_reason="max_tokens")
    out = await run_loop(
        StubModel([spent]), system="s", history=[], user="go", tools={"echo": ECHO}, deadline=1e9
    )
    assert out.stopped == "max_tokens" and out.text == OUT_OF_TOKENS and out.model_calls == 1
    partial = Completion(
        text="I will",
        calls=(ToolCall("tu_1", "echo", {"x": 1}),),
        content=[{"type": "text", "text": "I will"}],
        stop_reason="max_tokens",
    )
    out = await run_loop(
        StubModel([partial]), system="s", history=[], user="go", tools={"echo": ECHO}, deadline=1e9
    )
    # a call that arrived with a truncated response is not run
    assert out.stopped == "max_tokens" and out.calls == [] and out.text == f"{OUT_OF_TOKENS} I will"
