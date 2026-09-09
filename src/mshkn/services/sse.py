"""Reassemble a Messages API event stream into the final message (#110). Every
block is kept: text, tool_use (partial JSON accumulated and parsed at the
block's stop), thinking with its signature; message_delta's usage is
cumulative and merged over message_start's. Unknown events and delta types are
ignored, as the API's versioning policy asks."""

from __future__ import annotations

import json
from typing import Any

RETRYABLE_STREAM_ERRORS = frozenset({"overloaded_error", "api_error"})


class StreamError(Exception):
    """The API sent an `error` event; `overloaded_error` is the streamed 529."""

    def __init__(self, type: str, message: str) -> None:  # noqa: A002
        super().__init__(f"{type}: {message}")
        self.type = type
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.type in RETRYABLE_STREAM_ERRORS


class IncompleteStream(Exception):  # noqa: N818
    """The stream ended before message_stop, or never began: a transport failure."""


def parse_events(text: str) -> list[dict[str, Any]]:
    """The JSON of every `data:` payload, in order; multi-line data joined by newlines."""
    events: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")]
        if not data:
            continue
        try:
            payload = json.loads("\n".join(data))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def reassemble(text: str) -> dict[str, Any]:
    message: dict[str, Any] | None = None
    partial: dict[int, list[str]] = {}
    stopped = False
    for event in parse_events(text):
        kind = event.get("type")
        if kind == "error":
            error = event.get("error") or {}
            raise StreamError(str(error.get("type", "error")), str(error.get("message", "")))
        if kind == "message_start":
            message = dict(event["message"])
            message["content"] = list(message.get("content") or [])
            continue
        if message is None:
            raise IncompleteStream("events before message_start")
        content: list[Any] = message["content"]
        if kind == "content_block_start":
            index = int(event["index"])
            while len(content) <= index:
                content.append(None)
            content[index] = dict(event["content_block"])
            if content[index].get("type") == "tool_use":
                partial[index] = []
        elif kind == "content_block_delta":
            index = int(event["index"])
            block = content[index]
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
            elif delta_type == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
            elif delta_type == "signature_delta":
                block["signature"] = delta["signature"]
            elif delta_type == "input_json_delta":
                partial.setdefault(index, []).append(delta["partial_json"])
        elif kind == "content_block_stop":
            index = int(event["index"])
            if index in partial:
                raw = "".join(partial.pop(index))
                content[index]["input"] = json.loads(raw) if raw.strip() else {}
        elif kind == "message_delta":
            message.update(event.get("delta") or {})
            usage = event.get("usage")
            if usage:
                message["usage"] = {**(message.get("usage") or {}), **usage}
        elif kind == "message_stop":
            stopped = True
            break
    if message is None or not stopped:
        raise IncompleteStream("stream ended before message_stop")
    return message
