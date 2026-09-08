"""A loop over plain JSON tools and the Anthropic SDK (spec §2 decision 3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from membrane.config import Settings
from membrane.model import MAX_TOKENS, AnthropicModel, Completion, ToolCall, build_model

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
        return {"content": [vars(b) for b in self.content]}


class _Messages:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self.response


class _Client:
    def __init__(self, response: _Response) -> None:
        self.messages = _Messages(response)


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


def test_completion_is_a_value() -> None:
    c = Completion(text="t", calls=(), content=[])
    assert c == Completion("t", (), [])
