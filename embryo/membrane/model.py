"""The model behind the loop (spec §2 decision 3, §7): the Anthropic SDK over
plain JSON tools, or the scripted model that plays the liturgy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from membrane.config import Settings

MAX_TOKENS = 8192


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
        return Completion(text="\n".join(texts), calls=tuple(calls), content=content)


def build_model(settings: Settings) -> Model:
    if settings.model == "scripted":
        from membrane.scripted import ScriptedModel

        # ScriptedModel is a stub until Task 12; it will structurally satisfy Model then.
        return cast("Model", ScriptedModel())
    import anthropic

    return AnthropicModel(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key), settings.model_id
    )
