"""A text/event-stream body becomes the final message with every block kept (#110):
text, tool_use with chunked partial JSON, thinking with its signature, cumulative usage."""

from __future__ import annotations

import json

import pytest

from mshkn.services.sse import IncompleteStream, StreamError, parse_events, reassemble


def _sse(*events: dict[str, object]) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


START: dict[str, object] = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 25, "output_tokens": 1},
    },
}


def test_text_tool_use_thinking_and_usage_are_reassembled() -> None:
    text = _sse(
        START,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Let me "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "see."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig=="},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "ping"},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "propose", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"kind": "ve'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": 'rb", "n": 1}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 42},
        },
        {"type": "message_stop"},
    )
    message = reassemble(text)
    assert message["content"] == [
        {"type": "thinking", "thinking": "Let me see.", "signature": "sig=="},
        {"type": "text", "text": "Hello"},
        {"type": "tool_use", "id": "toolu_1", "name": "propose", "input": {"kind": "verb", "n": 1}},
    ]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 25, "output_tokens": 42}
    assert message["id"] == "msg_1" and message["model"] == "claude-opus-5"


def test_an_empty_tool_input_is_an_empty_object_and_unknown_events_are_ignored() -> None:
    text = _sse(
        START,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t", "name": "list", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": ""},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "future_delta", "x": 1}},
        {"type": "content_block_stop", "index": 0},
        {"type": "future_event", "payload": {}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    )
    message = reassemble(text)
    assert message["content"][0]["input"] == {}
    assert message["stop_reason"] == "tool_use"


def test_max_tokens_stop_reason_and_omitted_thinking_survive() -> None:
    text = _sse(
        START,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "s"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "max_tokens"},
            "usage": {"output_tokens": 64000},
        },
        {"type": "message_stop"},
    )
    message = reassemble(text)
    assert message["content"] == [{"type": "thinking", "thinking": "", "signature": "s"}]
    assert message["stop_reason"] == "max_tokens" and message["usage"]["output_tokens"] == 64000


def test_an_error_event_raises_and_says_whether_it_is_retryable() -> None:
    overloaded = _sse(
        START, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    )
    with pytest.raises(StreamError) as exc:
        reassemble(overloaded)
    assert (
        exc.value.retryable
        and exc.value.type == "overloaded_error"
        and "Overloaded" in str(exc.value)
    )
    invalid = _sse(
        START, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    )
    with pytest.raises(StreamError) as exc:
        reassemble(invalid)
    assert not exc.value.retryable


def test_a_stream_cut_before_message_stop_is_incomplete() -> None:
    with pytest.raises(IncompleteStream):
        reassemble(
            _sse(
                START,
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
    with pytest.raises(IncompleteStream):
        reassemble("")
    with pytest.raises(IncompleteStream):
        reassemble(_sse({"type": "message_stop"}))  # no message_start


def test_parse_events_joins_data_lines_and_skips_junk() -> None:
    text = (
        'event: message_stop\ndata: {"type":\ndata: "message_stop"}\n\n'
        ": comment\n\ndata: not json\n\n"
    )
    assert parse_events(text) == [{"type": "message_stop"}]
