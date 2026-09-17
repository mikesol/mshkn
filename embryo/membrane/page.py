"""A page a capability serves itself, on a computer of its own.

Security needed one behind a bearer token; web-search needs one with nothing in
front of it, so that "read a page" is judged against a body this repository
controls rather than against the open web. Both want the same three things: a
computer from the brain's recipe, a stdlib HTTP server uploaded as a file, and a
keep-alive that stops the host's idle reaper from taking it mid-run.

Nothing here goes through a door and nothing is recorded as a command root sent:
a capability's page is scaffolding around its run, not part of it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from typing import TextIO

PORT = 8000
PAGE_PATH = "/page"
TOKEN_FILE = "/tmp/page-token"
SERVER_FILE = "/tmp/page.py"
# The page server is a background process, and the host's idle reaper does not
# count one as activity: `reap_idle` in `src/mshkn/services/reaper.py` reads
# `last_exec_at or created_at` against `idle_timeout_seconds`. A run that spans
# two builds and several turns outlasts that, so the page would be gone halfway
# through. One trivial command on an interval well under the reaper's window is
# what keeps it, and it belongs here rather than in a caller: every run that
# serves a page needs it.
#
# 30 and not the 300 this started at, which was read off `idle_timeout_seconds`'
# *default* of 1800. The host does not run the default. Probed on 2026-09-16: a
# computer was destroyed between 131 and 141 seconds after its last touch, and
# the reaper cycle before that -- 71 to 81 seconds after it -- left it alone,
# which puts the live timeout in (71, 141]. `security/2026-09-16-run-1` is what
# the old number cost: the page's computer was reaped at 19:17:57, before the
# first touch was even due, and the row that read it got a 200 with an empty
# body from the route's absence rather than an error.
KEEP_ALIVE_INTERVAL = 30.0
KEEP_ALIVE = "true"


def server_source(body: str, *, bearer: bool) -> str:
    """The server, to be uploaded as a file. When `bearer`, the token is read from
    `TOKEN_FILE` rather than taken as an argument, so no command line ever carries
    it; an unreadable token file is a 500 and never an open page. Standard library
    only: the brain recipe has python3 and nothing installed."""
    guard = (
        f"""
        with open({TOKEN_FILE!r}) as f:
            token = f.read().strip()
        if self.headers.get("Authorization") != "Bearer " + token:
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            return
"""
        if bearer
        else ""
    )
    return f"""import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

BODY = {body!r}
PAGE_PATH = {PAGE_PATH!r}


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != PAGE_PATH:
            self.send_error(404)
            return
{guard}
        data = BODY.encode()
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        sys.stderr.write("page: " + args[0] % args[1:] + "\\n")


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", {PORT}), Page).serve_forever()
"""


def page_url(created: Mapping[str, Any]) -> str:
    """The computer's route, port-prefixed, the way hatch.sh reaches the scripted
    model server: https://<port>-<id>.<domain>."""
    return str(created["url"]).replace("https://", f"https://{PORT}-", 1) + PAGE_PATH


async def keep_alive(doors: Any, computer_id: str, log: TextIO) -> None:
    """Touch the page server's computer every `KEEP_ALIVE_INTERVAL` seconds so the
    idle reaper never takes it. The command carries nothing: a failed touch is
    logged and the next one is tried, and the loop never raises into the run.

    The first touch comes before the first sleep. The reaper measures from
    `last_exec_at or created_at`, and until something touches it that is the
    moment the computer was made -- so a loop that slept first left the page's
    whole first interval uncovered, which is the interval `2026-09-16-run-1`
    died in."""
    while True:
        try:
            touched = await doors.api.post(
                f"/computers/{computer_id}/exec",
                json={"command": KEEP_ALIVE, "timeout_seconds": 30},
            )
            touched.raise_for_status()
        except Exception as exc:  # anything but cancellation must not end the loop
            log.write(f"could not keep {computer_id} alive: {type(exc).__name__}: {exc}\n")
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)


@contextlib.asynccontextmanager
async def serve(doors: Any, log: TextIO, body: str, *, token: str | None) -> AsyncIterator[str]:
    """A computer of its own, from the brain's recipe -- already declared, the way
    `hatch.sh` starts the scripted model server -- serving `body` at `PAGE_PATH`,
    kept alive for as long as the caller holds it. Yields the page's URL; the
    computer is destroyed on exit, whatever the run did."""
    head = await doors.head("brain")
    if head is None:
        raise RuntimeError("no brain to serve a page beside")
    request: dict[str, Any] = {}
    if head.get("recipe_id"):
        request["recipe_id"] = head["recipe_id"]
    created = await doors.api.post("/computers", json=request)
    created.raise_for_status()
    computer_id = str(created.json()["computer_id"])
    try:
        if token is not None:
            await doors.upload(computer_id, TOKEN_FILE, token.encode())
        await doors.upload(
            computer_id, SERVER_FILE, server_source(body, bearer=token is not None).encode()
        )
        started = await doors.api.post(
            f"/computers/{computer_id}/exec/bg", json={"command": f"python3 {SERVER_FILE}"}
        )
        started.raise_for_status()
        url = page_url(created.json())
        log.write(f"the page is at {url} (computer {computer_id})\n")
        keeper = asyncio.create_task(keep_alive(doors, computer_id, log))
        try:
            yield url
        finally:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
    finally:
        with contextlib.suppress(httpx.HTTPError):
            await doors.api.delete(f"/computers/{computer_id}")
