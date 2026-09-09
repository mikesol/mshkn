"""The model behind the loop (spec §2 decision 3, §7): the Anthropic SDK over
plain JSON tools, or the scripted model that plays the liturgy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from membrane.config import Settings

# The output budget of one completion. Thinking counts against it (the model thinks by
# default), and 16000 is the largest a non-streaming request may ask for.
MAX_TOKENS = 16000
# The token counts of one completion, as the Messages API reports them
# (`response.usage`); summed per turn and printed in the audit line so the cost
# of a run is read from mshkn's exec_log (#101).
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def zero_usage() -> dict[str, int]:
    return dict.fromkeys(USAGE_KEYS, 0)


def add_usage(a: Mapping[str, int], b: Mapping[str, int]) -> dict[str, int]:
    return {key: a.get(key, 0) + b.get(key, 0) for key in USAGE_KEYS}


def usage_of(response: Any) -> dict[str, int]:
    """`response.usage` as a plain dict; a missing object or field counts as 0."""
    usage = getattr(response, "usage", None)
    return {key: int(getattr(usage, key, None) or 0) for key in USAGE_KEYS}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Completion:
    text: str
    calls: tuple[ToolCall, ...]
    content: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=zero_usage)
    stop_reason: str | None = None


class Model(Protocol):
    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Completion: ...


class AnthropicModel:
    def __init__(self, client: Any, model_id: str) -> None:
        self.client = client
        self.model_id = model_id

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        response = await self.client.messages.create(**kwargs)
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        content: list[dict[str, Any]] = response.model_dump()["content"]
        return Completion(
            text="\n".join(texts),
            calls=tuple(calls),
            content=content,
            usage=usage_of(response),
            stop_reason=getattr(response, "stop_reason", None),
        )


def build_model(settings: Settings) -> Model:
    if settings.model == "scripted":
        from membrane.scripted import ScriptedModel

        return ScriptedModel()
    import anthropic

    return AnthropicModel(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key), settings.model_id
    )
