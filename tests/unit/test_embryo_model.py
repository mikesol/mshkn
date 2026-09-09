"""The model as the membrane reaches it (relay design §6): a request body for the
Messages API, posted to the relay, and the final message parsed back."""

from __future__ import annotations

from membrane.model import (
    ANTHROPIC_VERSION,
    MAX_TOKENS,
    USAGE_KEYS,
    ToolCall,
    add_usage,
    compose_request,
    parse_message,
    request_headers,
    usage_from,
    zero_usage,
)

TOOLS = [{"name": "page_title", "description": "d", "input_schema": {"type": "object"}}]


def test_compose_request_streams_with_the_full_budget_and_only_what_is_set() -> None:
    body = compose_request(
        model_id="claude-opus-5",
        system="seed",
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS,
        effort=None,
    )
    assert body == {
        "model": "claude-opus-5",
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "system": "seed",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
    }
    assert MAX_TOKENS == 64000
    bare = compose_request(model_id="m", system="s", messages=[], tools=[], effort="medium")
    assert "tools" not in bare and bare["output_config"] == {"effort": "medium"}
    assert "thinking" not in bare


def test_request_headers_carry_the_version_and_the_key_when_there_is_one() -> None:
    assert request_headers("sk-test") == {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "x-api-key": "sk-test",
    }
    assert "x-api-key" not in request_headers(None)


def test_parse_message_maps_blocks_to_text_and_calls_and_keeps_content_whole() -> None:
    out = parse_message(
        {
            "content": [
                {"type": "thinking", "thinking": "", "signature": "s"},
                {"type": "text", "text": "Let me look."},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "page_title",
                    "input": {"url": "https://example.com"},
                    "parsed_output": None,
                },
                {"type": "server_tool_use", "id": "x", "name": "y"},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 120, "output_tokens": 7, "cache_read_input_tokens": None},
        }
    )
    assert out.text == "Let me look."
    assert out.calls == (ToolCall("tu_1", "page_title", {"url": "https://example.com"}),)
    assert [b["type"] for b in out.content] == ["thinking", "text", "tool_use", "server_tool_use"]
    assert all("parsed_output" not in block for block in out.content)
    assert out.stop_reason == "tool_use"
    assert out.usage == {
        "input_tokens": 120,
        "output_tokens": 7,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def test_parse_message_joins_texts_and_counts_nothing_without_usage() -> None:
    out = parse_message({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]})
    assert out.text == "a\nb" and out.calls == () and out.usage == zero_usage()
    assert out.stop_reason is None
    assert parse_message({}).text == "" and parse_message({}).content == []


def test_usage_helpers() -> None:
    a = dict(zip(USAGE_KEYS, (1, 2, 0, 4), strict=True))
    b = dict(zip(USAGE_KEYS, (10, 20, 30, 40), strict=True))
    assert add_usage(a, b) == {
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 44,
    }
    assert tuple(zero_usage()) == USAGE_KEYS
    assert (
        usage_from(None) == zero_usage() and usage_from({"output_tokens": 3})["output_tokens"] == 3
    )
