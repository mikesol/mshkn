"""A loop over plain JSON tools and the Anthropic SDK (spec §2 decision 3)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from membrane.config import Settings
from membrane.model import (
    DEADLINE,
    MAX_TOKENS,
    USAGE_KEYS,
    AnthropicModel,
    ToolCall,
    add_usage,
    build_model,
    zero_usage,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Response:
    content: list[_Block]

    def model_dump(self) -> dict[str, Any]:
        # a streamed ParsedMessage dumps its blocks with the SDK's parsed_output
        return {"content": [{**vars(b), "parsed_output": None} for b in self.content]}


class _Stream:
    """The SDK's AsyncMessageStream as the model uses it: get_final_message() and the
    snapshot of what has arrived so far. `hang` makes the final message never come."""

    def __init__(self, response: _Response, *, hang: bool = False) -> None:
        self.response, self.hang = response, hang

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> _Response:
        if self.hang:
            await asyncio.sleep(3600)
        return self.response

    @property
    def current_message_snapshot(self) -> _Response:
        return self.response


class _Messages:
    def __init__(self, response: _Response, *, hang: bool = False) -> None:
        self.response, self.hang = response, hang
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _Stream:
        self.calls.append(kwargs)
        return _Stream(self.response, hang=self.hang)


class _Client:
    def __init__(self, response: _Response, *, hang: bool = False) -> None:
        self.messages = _Messages(response, hang=hang)


async def test_complete_maps_blocks_to_text_and_calls() -> None:
    client = _Client(
        _Response(
            [
                _Block("text", text="Let me look."),
                _Block(
                    "tool_use", id="tu_1", name="page_title", input={"url": "https://example.com"}
                ),
            ]
        )
    )
    model = AnthropicModel(client, "claude-opus-5")
    tools = [{"name": "page_title", "description": "d", "input_schema": {"type": "object"}}]
    out = await model.complete(
        system="seed", messages=[{"role": "user", "content": "hi"}], tools=tools
    )
    assert out.text == "Let me look."
    assert out.calls == (ToolCall("tu_1", "page_title", {"url": "https://example.com"}),)
    assert out.content[1]["name"] == "page_title"
    # what goes back to the API as the assistant turn carries nothing of the SDK's own
    assert all("parsed_output" not in block for block in out.content)
    sent = client.messages.calls[0]
    assert sent["model"] == "claude-opus-5" and sent["max_tokens"] == MAX_TOKENS
    assert sent["system"] == "seed" and sent["tools"] == tools
    assert "thinking" not in sent


async def test_unrecognized_block_types_are_skipped_but_kept_in_content() -> None:
    client = _Client(
        _Response([_Block("text", text="a"), _Block("server_tool_use", id="x", name="y")])
    )
    out = await AnthropicModel(client, "m").complete(system="s", messages=[], tools=[])
    assert out.text == "a" and out.calls == ()
    assert [b["type"] for b in out.content] == ["text", "server_tool_use"]


async def test_no_tools_means_no_tools_parameter() -> None:
    client = _Client(_Response([_Block("text", text="a"), _Block("text", text="b")]))
    out = await AnthropicModel(client, "m").complete(system="s", messages=[], tools=[])
    assert out.text == "a\nb" and out.calls == ()
    assert "tools" not in client.messages.calls[0]


def test_build_model_picks_the_kind(tmp_path: Path) -> None:
    scripted = build_model(
        Settings(
            brain=tmp_path,
            api_url="u",
            api_key="k",
            model="scripted",
            model_id="m",
            anthropic_api_key=None,
            openai_api_key=None,
        )
    )
    assert type(scripted).__name__ == "ScriptedModel"
    real = build_model(
        Settings(
            brain=tmp_path,
            api_url="u",
            api_key="k",
            model="anthropic",
            model_id="claude-opus-5",
            anthropic_api_key="sk-test",
            openai_api_key="o",
        )
    )
    assert isinstance(real, AnthropicModel) and real.model_id == "claude-opus-5"


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None = None


async def test_usage_is_read_off_the_response_with_missing_fields_as_zero() -> None:
    response = _Response([_Block("text", text="a")])
    response.usage = _Usage(input_tokens=120, output_tokens=7, cache_read_input_tokens=None)  # type: ignore[attr-defined]
    out = await AnthropicModel(_Client(response), "m").complete(system="s", messages=[], tools=[])
    assert out.usage == {
        "input_tokens": 120,
        "output_tokens": 7,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


async def test_a_response_without_usage_counts_nothing() -> None:
    out = await AnthropicModel(_Client(_Response([_Block("text", text="a")])), "m").complete(
        system="s", messages=[], tools=[]
    )
    assert out.usage == zero_usage()


def test_add_usage_sums_every_key() -> None:
    a = dict(zip(USAGE_KEYS, (1, 2, 0, 4), strict=True))
    b = dict(zip(USAGE_KEYS, (10, 20, 30, 40), strict=True))
    assert add_usage(a, b) == {
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 44,
    }
    assert tuple(zero_usage()) == USAGE_KEYS


async def test_stop_reason_is_kept() -> None:
    response = _Response([_Block("text", text="")])
    response.stop_reason = "max_tokens"  # type: ignore[attr-defined]
    out = await AnthropicModel(_Client(response), "m").complete(system="s", messages=[], tools=[])
    assert out.stop_reason == "max_tokens"
    assert MAX_TOKENS == 64000  # streamed, so no HTTP timeout bounds it; the turn's clock does


async def test_the_turns_clock_ends_a_completion_and_keeps_its_words() -> None:
    """#106: a completion is streamed and bounded by the time left in the turn; at the
    deadline the text so far is the completion, a half-built call is not a call."""
    response = _Response(
        [_Block("text", text="So far"), _Block("tool_use", id="tu", name="try", input={})]
    )
    response.stop_reason = None  # type: ignore[attr-defined]
    client = _Client(response, hang=True)
    out = await AnthropicModel(client, "m").complete(
        system="s", messages=[], tools=[], timeout=0.01
    )
    assert out.stop_reason == DEADLINE and out.text == "So far" and out.calls == ()
    assert out.content[1]["name"] == "try"  # the snapshot is kept whole in content
