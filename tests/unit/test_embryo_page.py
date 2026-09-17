"""`membrane.page`: the page a capability serves itself, behind a bearer token or
not, and the keep-alive that stops the host's idle reaper taking it mid-run."""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Any

import pytest
from membrane import page

from tests.support_embryo import PageApi, page_doors, sse

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

BODY = "the body says: perfect number 8128.\n"


def _handler(source: str, tmp_path: Path) -> Any:
    """The uploaded script is real Python: compile it and hand back its handler
    class, so the 200/401/404 split is pinned without a socket."""
    namespace: dict[str, Any] = {"__name__": "not_main"}
    exec(compile(source, "page.py", "exec"), namespace)
    handler = namespace["Page"]

    class Fake(handler):  # type: ignore[misc, valid-type]
        def __init__(self, path: str, auth: str | None = None) -> None:
            self.path, self.headers = path, {"Authorization": auth} if auth else {}
            self.status: int | None = None
            self.wfile = io.BytesIO()

        def send_response(self, code: int, *_: Any) -> None:
            self.status = code

        def send_header(self, *_: Any) -> None: ...
        def end_headers(self) -> None: ...
        def send_error(self, code: int, *_: Any) -> None:
            self.status = code

    return Fake


def test_a_bearer_page_serves_the_body_only_to_the_token(tmp_path: Path) -> None:
    source = page.server_source(BODY, bearer=True)
    assert page.TOKEN_FILE in source  # the token is read from a file, never an argument
    fake = _handler(source.replace(repr(page.TOKEN_FILE), repr(str(tmp_path / "tok"))), tmp_path)
    (tmp_path / "tok").write_text("tok")
    ok = fake("/page", "Bearer tok")
    ok.do_GET()
    assert ok.status == 200 and ok.wfile.getvalue() == BODY.encode()
    for auth in ("Bearer nope", None):
        bad = fake("/page", auth)
        bad.do_GET()
        assert bad.status == 401 and bad.wfile.getvalue() == b""
    missing = fake("/other", "Bearer tok")
    missing.do_GET()
    assert missing.status == 404


def test_an_open_page_serves_the_body_to_anyone_and_reads_no_token_file(tmp_path: Path) -> None:
    """web-search's page has nothing in front of it, and must not fall back to a
    token file it was never given: an absent file is how a bearer page breaks
    *closed*, so an open page must not consult one at all."""
    source = page.server_source(BODY, bearer=False)
    assert page.TOKEN_FILE not in source and "Authorization" not in source
    fake = _handler(source, tmp_path)
    ok = fake("/page")
    ok.do_GET()
    assert ok.status == 200 and ok.wfile.getvalue() == BODY.encode()
    missing = fake("/elsewhere")
    missing.do_GET()
    assert missing.status == 404


def test_the_page_url_is_the_computers_route_port_prefixed() -> None:
    assert page.page_url({"url": "https://comp-9.mshkn.dev"}) == (
        f"https://{page.PORT}-comp-9.mshkn.dev{page.PAGE_PATH}"
    )


async def test_serve_starts_the_page_on_the_brains_recipe_and_takes_it_away(
    tmp_path: Path,
) -> None:
    api = PageApi(exec_body=sse(("exit", "0")))
    doors = page_doors(api, tmp_path)
    log = io.StringIO()
    async with page.serve(doors, log, BODY, token="tok-1") as url:
        assert url == "https://8000-comp-1.test.dev/page"
        # the token and the server reached the computer as files; no command names either
        assert api.uploads[("comp-1", page.TOKEN_FILE)] == b"tok-1"
        assert (
            api.uploads[("comp-1", page.SERVER_FILE)]
            == page.server_source(BODY, bearer=True).encode()
        )
        assert api.bg == [("comp-1", f"python3 {page.SERVER_FILE}")]
        assert ("POST", "/computers") in api.requests  # the brain's recipe, no new recipe
    assert "/computers/comp-1" in api.deleted
    # the driver never sees these as by-hand commands: nothing went through a door
    assert doors.sent == []
    assert "the page is at https://8000-comp-1.test.dev/page" in log.getvalue()
    assert "tok-1" not in log.getvalue()


async def test_serve_without_a_token_uploads_no_token_file(tmp_path: Path) -> None:
    api = PageApi(exec_body=sse(("exit", "0")))
    async with page.serve(page_doors(api, tmp_path), io.StringIO(), BODY, token=None):
        assert [path for (_, path) in api.uploads] == [page.SERVER_FILE]


async def test_serve_needs_a_brain_to_serve_a_page_beside(tmp_path: Path) -> None:
    api = PageApi(checkpoints=[])
    with pytest.raises(RuntimeError, match="no brain to serve"):
        async with page.serve(page_doors(api, tmp_path), io.StringIO(), BODY, token=None):
            pass
    assert api.n == 0  # the failure is before the first computer


async def test_the_page_is_touched_while_the_context_is_open_and_never_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server is a background process, which the host's idle reaper does not
    count as activity (`reap_idle` reads `last_exec_at or created_at`), so the
    page would die mid-run without this. The touch carries nothing but `true`."""
    monkeypatch.setattr(page, "KEEP_ALIVE_INTERVAL", 0.01)
    api = PageApi(exec_body=sse(("exit", "0")))
    async with page.serve(page_doors(api, tmp_path), io.StringIO(), BODY, token="tok-1"):
        await asyncio.sleep(0.1)
        touches = [r for r in api.requests if r == ("POST", "/computers/comp-1/exec")]
        assert len(touches) >= 2, api.requests
        assert {cmd for who, cmd in api.execs if who == "comp-1"} == {page.KEEP_ALIVE}
        assert all("tok-1" not in cmd for _, cmd in api.execs)
    # cancelled with the context: no touch lands after it
    assert [r for r in api.requests if r == ("POST", "/computers/comp-1/exec")] == touches


async def test_the_first_touch_lands_before_the_first_interval_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`keep_alive` slept before its first touch, so the page's computer went
    `KEEP_ALIVE_INTERVAL` seconds untouched from the moment it was created --
    and `created_at` is what `reap_idle` measures against until something
    touches it. `security/2026-09-16-run-1` lost the page to that gap: the
    reaper destroyed `comp-3ff2ec6b4917` at 19:17:57 with an
    `auto-idle-timeout` checkpoint, 45 seconds before row 12 read it and long
    before the first touch was due. With the route gone the URL answered 200
    with an empty body rather than 502, so `curl --fail` exited 0 with nothing
    on stdout and `secret_page` failed on a page that had been fine when the
    row was written.

    The interval is asserted too, because the comment that chose 300 read the
    host's *default* 1800 and the host does not run the default. Probing it on
    2026-09-16: a computer was destroyed between 131 and 141 seconds after its
    last touch, and the reaper cycle before that -- 71 to 81 seconds after it --
    left it alone, which puts the live timeout in (71, 141]. 300 is longer than
    any of that; the bound here is what says so.
    """
    api = PageApi(exec_body=sse(("exit", "0")))
    async with page.serve(page_doors(api, tmp_path), io.StringIO(), BODY, token=None):
        # far less than the interval: only a touch that precedes the sleep lands
        await asyncio.sleep(0.05)
        assert ("POST", "/computers/comp-1/exec") in api.requests, api.requests
    assert page.KEEP_ALIVE_INTERVAL <= 60.0


async def test_a_failed_touch_is_logged_and_the_keep_alive_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A touch the host refuses must not raise into the run or stop the loop."""
    monkeypatch.setattr(page, "KEEP_ALIVE_INTERVAL", 0.01)
    api = PageApi(exec_body=sse(("exit", "0")), fail="/computers/comp-1/exec")
    log = io.StringIO()
    async with page.serve(page_doors(api, tmp_path), log, BODY, token="tok-1"):
        await asyncio.sleep(0.1)
    assert log.getvalue().count("could not keep comp-1 alive: HTTPStatusError") >= 2
    assert "tok-1" not in log.getvalue()
    assert "/computers/comp-1" in api.deleted  # the run itself was untouched


async def test_a_touch_that_raises_something_other_than_an_http_error_is_also_survived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minor 4: `keep_alive` caught only `httpx.HTTPError`, so anything else ended
    the task and `await keeper` on the way out re-raised it, failing an otherwise
    complete run. It must catch every exception but cancellation."""
    monkeypatch.setattr(page, "KEEP_ALIVE_INTERVAL", 0.01)
    api = PageApi(exec_body=sse(("exit", "0")), exec_raises_for=page.KEEP_ALIVE)
    log = io.StringIO()
    async with page.serve(page_doors(api, tmp_path), log, BODY, token=None):
        await asyncio.sleep(0.1)
    assert log.getvalue().count("could not keep comp-1 alive: RuntimeError: boom") >= 2
    assert "/computers/comp-1" in api.deleted


async def test_the_page_computer_goes_even_when_the_run_falls_over(tmp_path: Path) -> None:
    api = PageApi(exec_body=sse(("exit", "0")))
    with pytest.raises(RuntimeError, match="boom"):
        async with page.serve(page_doors(api, tmp_path), io.StringIO(), BODY, token=None):
            raise RuntimeError("boom")
    assert "/computers/comp-1" in api.deleted
