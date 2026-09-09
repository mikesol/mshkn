"""`membrane serve` answers the Messages API wire format from the scripted model
(relay design §7), so the flow and live tiers drive the real relay."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

import pytest
from membrane.liturgy import LITURGY
from membrane.model import zero_usage
from membrane.scripted import ScriptedModel
from membrane.serve import answer, build_server, main

if TYPE_CHECKING:
    from pathlib import Path


def _request(
    system: str, message: str, tools: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "model": "scripted",
        "max_tokens": 64000,
        "stream": True,
        "system": system,
        "messages": [
            {
                "role": "user",
                "content": (
                    "[turn 1 | principal root | door api]\ninbox:\n\nrecall:\n\nmessage:\n"
                    f"{message}"
                ),
            }
        ],
        "tools": tools or [],
    }


def test_answer_is_a_message_with_end_turn_for_text_and_tool_use_for_calls() -> None:
    model = ScriptedModel()
    text = answer(model, _request("seed", LITURGY[1]))
    assert text["type"] == "message" and text["role"] == "assistant" and text["model"] == "scripted"
    assert text["content"][0]["type"] == "text" and "embryo" in text["content"][0]["text"]
    assert text["stop_reason"] == "end_turn" and text["usage"] == zero_usage()
    tools = [
        {"name": n, "description": "d", "input_schema": {"type": "object"}}
        for n in ("remember", "try", "propose")
    ]
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIServeTestKeyServeTestKeyServeTestKeyServeT"
    calls = answer(model, _request("seed", LITURGY[2].format(key=key), tools))
    assert calls["stop_reason"] == "tool_use"
    assert [b["type"] for b in calls["content"]] == ["tool_use", "tool_use", "tool_use"]
    assert [b["name"] for b in calls["content"]] == ["try", "propose", "propose"]


def test_the_server_answers_post_v1_messages_and_nothing_else() -> None:
    server = build_server(ScriptedModel(), 0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/messages",
            data=json.dumps(_request("seed", LITURGY[1])).encode(),
            headers={"content-type": "application/json", "x-api-key": "ignored"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200 and resp.headers["content-type"] == "application/json"
            body = json.loads(resp.read())
        assert body["stop_reason"] == "end_turn" and "embryo" in body["content"][0]["text"]
        bad = urllib.request.Request(f"http://127.0.0.1:{port}/other", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad)
        assert exc.value.code == 404
        junk = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/messages", data=b"{not json", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(junk)
        assert exc.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_main_refuses_to_serve_a_real_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=anthropic\nANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n"
    )
    monkeypatch.setenv("MEMBRANE_BRAIN", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["--port", "0"])
    assert exc.value.code == 2


def test_main_serves_the_scripted_model_until_shut_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import membrane.serve as serve_module

    (tmp_path / ".env").write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n")
    monkeypatch.setenv("MEMBRANE_BRAIN", str(tmp_path))
    served: list[tuple[Any, int]] = []

    def fake_serve(model: Any, port: int) -> None:
        served.append((model, port))

    monkeypatch.setattr(serve_module, "serve", fake_serve)
    main(["--port", "8123"])
    assert len(served) == 1 and isinstance(served[0][0], ScriptedModel) and served[0][1] == 8123
