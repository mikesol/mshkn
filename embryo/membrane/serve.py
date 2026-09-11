"""`membrane serve`: the scripted model behind `POST /v1/messages` on the
Messages API wire format (relay design §7), so the deterministic tiers drive the
real relay. Standard library only; `stream` is ignored and a plain JSON message
is answered, which the relay stores verbatim. Refuses to serve a real model."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from membrane.config import load_settings
from membrane.model import Model, system_text, zero_usage
from membrane.scripted import ScriptedModel

DEFAULT_PORT = 8000
# The message id counter. Safe only because this server is single-threaded
# (`HTTPServer`, one request at a time): a threading server would need a lock.
_counter = 0


async def answer_async(model: Model, body: dict[str, Any]) -> dict[str, Any]:
    """The message the scripted model gives for a request body. The async half, so
    an ASGI caller inside a running loop can await it (the flow tier does)."""
    global _counter
    _counter += 1
    completion = await model.complete(
        system=system_text(body.get("system")),
        messages=list(body.get("messages") or []),
        tools=list(body.get("tools") or []),
    )
    return {
        "id": f"msg_scripted_{_counter}",
        "type": "message",
        "role": "assistant",
        "model": str(body.get("model", "scripted")),
        "content": completion.content,
        "stop_reason": "tool_use" if completion.calls else "end_turn",
        "stop_sequence": None,
        "usage": zero_usage(),
    }


def answer(model: Model, body: dict[str, Any]) -> dict[str, Any]:
    """`answer_async` for the blocking server, which has no loop of its own."""
    return asyncio.run(answer_async(model, body))


class Handler(BaseHTTPRequestHandler):
    """The server object carries the model as `server.model`."""

    def do_POST(self) -> None:  # the stdlib's name
        if self.path != "/v1/messages":
            self._json(
                404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}}
            )
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._json(
                400,
                {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}},
            )
            return
        model: Model = self.server.model  # type: ignore[attr-defined]
        self._json(200, answer(model, body))

    def _json(self, status: int, doc: dict[str, Any]) -> None:
        data = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's name
        sys.stderr.write("serve: " + format % args + "\n")


def build_server(
    model: Model, port: int, host: str = "0.0.0.0"
) -> HTTPServer:  # the VM's own network
    server = HTTPServer((host, port), Handler)
    server.model = model  # type: ignore[attr-defined]
    return server


def serve(model: Model, port: int) -> None:
    sys.stderr.write(f"membrane serve: scripted model on port {port}\n")
    build_server(model, port).serve_forever()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="membrane serve")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    settings = load_settings()
    if settings.model != "scripted":
        sys.stderr.write("membrane serve: only MEMBRANE_MODEL=scripted can be served\n")
        sys.exit(2)
    serve(ScriptedModel(), args.port)
