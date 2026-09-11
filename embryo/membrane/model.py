"""The model as the membrane reaches it (spec §7, relay design §6): a request
body composed for the Messages API and handed to the relay, and the final
message parsed back. No SDK: the membrane never calls the model itself."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

# The output budget of one completion. Thinking counts against it (the model
# thinks by default). The relay reads the stream to its end, so nothing but the
# relay's patience bounds a completion (#110).
MAX_TOKENS = 64000
ANTHROPIC_VERSION = "2023-06-01"
# The token counts of one completion, as the Messages API reports them
# (`usage`); summed per turn and printed in the audit line so the cost of a
# run is read from mshkn's exec_log (#101).
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
# One cache breakpoint (#126). The default five-minute entry is the right one
# here: the calls of a turn are seconds apart, every read refreshes the entry's
# timer for free, and the one-hour TTL only doubles the write price.
CACHE_CONTROL = {"type": "ephemeral"}


def zero_usage() -> dict[str, int]:
    return dict.fromkeys(USAGE_KEYS, 0)


def add_usage(a: Mapping[str, int], b: Mapping[str, int]) -> dict[str, int]:
    return {key: a.get(key, 0) + b.get(key, 0) for key in USAGE_KEYS}


def usage_from(doc: Mapping[str, Any] | None) -> dict[str, int]:
    """A message's `usage` as a plain dict; a missing object or field counts as 0."""
    usage = doc or {}
    return {key: int(usage.get(key) or 0) for key in USAGE_KEYS}


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
    """What `membrane serve` and the unit tier answer with: the scripted model."""

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> Completion: ...


def compose_request(
    *,
    model_id: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    effort: str | None,
) -> dict[str, Any]:
    """The `POST /v1/messages` body: streamed, so a long answer produces bytes
    throughout and the relay reassembles it (relay design §12)."""
    body: dict[str, Any] = {
        "model": model_id,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        # The stable prefix of every call of a turn, marked cacheable (#126).
        # `tools` render before `system`, so this one breakpoint covers the tool
        # list as well as the seed and the self-description.
        "system": [{"type": "text", "text": system, "cache_control": CACHE_CONTROL}],
        "messages": messages,
        # Everything in `messages` is settled by the time the request is composed,
        # so the automatic breakpoint lands at the end of it: the next call of the
        # turn reads its whole history back instead of re-encoding it.
        "cache_control": CACHE_CONTROL,
    }
    if tools:
        body["tools"] = tools
    if effort is not None:
        body["output_config"] = {"effort": effort}
    return body


def system_text(blocks: object) -> str:
    """The system prompt read back out of a composed body: the scripted model is
    handed the words, not the cacheable blocks `compose_request` wraps them in."""
    if not isinstance(blocks, list):
        return ""
    return "\n\n".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict))


def request_headers(api_key: str | None) -> dict[str, str]:
    """The headers the relay forwards; the key rides in them until #92."""
    headers = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def parse_message(doc: Mapping[str, Any]) -> Completion:
    """A stored message as the loop reads it: text, calls, the content echoed back
    without the SDK's `parsed_output` (live run 2026-09-09-run-7), usage, stop reason."""
    texts: list[str] = []
    calls: list[ToolCall] = []
    content: list[dict[str, Any]] = []
    for block in doc.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            calls.append(
                ToolCall(
                    id=str(block["id"]),
                    name=str(block["name"]),
                    input=dict(block.get("input") or {}),
                )
            )
        content.append({k: v for k, v in block.items() if k != "parsed_output"})
    return Completion(
        text="\n".join(texts),
        calls=tuple(calls),
        content=content,
        usage=usage_from(doc.get("usage")),
        stop_reason=doc.get("stop_reason"),
    )
