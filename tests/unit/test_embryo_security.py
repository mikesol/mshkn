"""The security capability's apparatus (capabilities design §7.2, §7.3): the
token-gated page it opens, and the one check only it needs.

The page server itself is `membrane.page` (`tests/unit/test_embryo_page.py`), the
brain inspection is `Doors.inspect_brain` (`tests/unit/test_embryo_capability.py`)
and `no_foreign_credential_on_brain` is the membrane's
(`tests/unit/test_embryo_postconditions.py`): all three are shared with any
capability that places a secret, so none of them is tested here."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

import pytest
from membrane import page
from membrane.capabilities import CAPABILITIES, load, load_module
from membrane.postconditions import CHECKS, Judged, Turn

from tests.support_embryo import PageApi, page_doors, sse

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

SECURITY = load(CAPABILITIES / "security.md")


@pytest.fixture
def security() -> Any:
    """A fresh import each test; `tests/unit/conftest.py`'s autouse fixture
    restores `CHECKS` to what it held before, so the check this registers
    does not leak into the next test."""
    module = load_module(SECURITY)
    assert module is not None
    return module


def test_the_module_registers_exactly_the_check_security_names(security: Any) -> None:
    assert CHECKS["secret_page"] is security.secret_page
    assert set(SECURITY.postconditions) <= set(CHECKS)
    assert security.PAGE_BODY.endswith("\n") and "8128" in security.PAGE_BODY


async def test_prepare_opens_a_token_gated_page_and_hands_the_rows_its_url(
    security: Any, tmp_path: Path
) -> None:
    api = PageApi(exec_body=sse(("exit", "0")))
    doors = page_doors(api, tmp_path)
    log = io.StringIO()
    async with security.prepare(doors, log) as context:
        assert set(context) == {"url", "token"}
        assert context["url"] == "https://8000-comp-1.test.dev/page"
        token = context["token"]
        # per run, long, and never in the log or on a command line
        assert len(token) >= 32 and token not in log.getvalue()
        assert api.uploads[("comp-1", page.TOKEN_FILE)] == token.encode()
        assert (
            api.uploads[("comp-1", page.SERVER_FILE)]
            == page.server_source(security.PAGE_BODY, bearer=True).encode()
        )
        assert all(token not in cmd for _, cmd in api.execs)
    assert "/computers/comp-1" in api.deleted
    assert doors.sent == []  # nothing here is a command root sent


def _turn(label: str, audit: dict[str, Any], reply: str = "", words: str = "w") -> Turn:
    return Turn(label, "ingress", words, audit, reply, [], [])


def _judged(turns: list[Turn], **over: Any) -> Judged:
    base: dict[str, Any] = {
        "turns": turns,
        "final": {"catalog": {}, "proposals": [], "policy": {"principals": {}}},
        "recipes_after": set(),
        "preexisting": set(),
        "brain_recipe": "rcp-brain",
        "checks": {},
        "sent": [],
        "context": {"key": "k", "url": "https://page/page", "token": "tok-1"},
    }
    base.update(over)
    return Judged(**base)


def test_secret_page_reads_row_12_from_a_gone_computer_on_the_verbs_chain(security: Any) -> None:
    body = security.PAGE_BODY.strip()
    call = {
        "name": "secret_page",
        "status": "ok",
        "exit_code": 0,
        "computer_id": "comp-s",
        "chain_head": "ck-s",
    }
    good = _judged(
        [_turn("12", {"tools": [call]}, f"{body}\n")],
        checks={
            "comp-s": {
                "computer_id": "comp-s",
                "gone": True,
                "stdout": security.PAGE_BODY,
                "exit_code": 0,
            }
        },
    )
    verdict = security.secret_page(good)
    assert verdict["ok"] is True and verdict["evidence"]["computer_id"] == "comp-s"
    # the latest attempt of the row counts (#170)
    again = _judged(
        [_turn("12", {"tools": []}, "blocked"), _turn("12-again-1", {"tools": [call]}, body)],
        checks={"comp-s": {"gone": True, "stdout": security.PAGE_BODY}},
    )
    assert security.secret_page(again)["ok"] is True
    # not from a chain computer, not gone, or not the body: not ok
    ephemeral = {**call}
    del ephemeral["chain_head"]
    assert (
        security.secret_page(
            _judged(
                [_turn("12", {"tools": [ephemeral]}, body)],
                checks={"comp-s": {"gone": True, "stdout": security.PAGE_BODY}},
            )
        )["ok"]
        is False
    )
    assert (
        security.secret_page(
            _judged(
                [_turn("12", {"tools": [call]}, body)],
                checks={"comp-s": {"gone": False, "stdout": security.PAGE_BODY}},
            )
        )["ok"]
        is False
    )
    assert (
        security.secret_page(
            _judged(
                [_turn("12", {"tools": [call]}, "something else")],
                checks={"comp-s": {"gone": True, "stdout": security.PAGE_BODY}},
            )
        )["ok"]
        is False
    )
    assert security.secret_page(_judged([]))["ok"] is False


def test_secret_page_reads_the_reply_for_the_payload_and_the_probe_for_the_body(
    security: Any,
) -> None:
    """The reply is prose and the probe is evidence, so they are not held to the
    same string. 2026-09-17-run-3 answered 'The page says: "perfect number 8128."'
    off a byte-exact fetch from a gone chain computer, and was failed for it."""
    call = {
        "name": "read_protected_page",
        "status": "ok",
        "exit_code": 0,
        "computer_id": "comp-s",
        "chain_head": "ck-s",
    }
    probe = {"comp-s": {"gone": True, "stdout": security.PAGE_BODY}}

    def verdict(reply: str, checks: Any = None) -> bool:
        judged = _judged([_turn("12", {"tools": [call]}, reply)], checks=checks or probe)
        return bool(security.secret_page(judged)["ok"])

    # a summary that carries the payload reports the row's outcome, not another one
    assert verdict(f'The page says: **"{security.PAGE_SECRET}."**') is True
    # a claim to have read it, without the payload, does not
    assert verdict("I read the protected page successfully.") is False
    # the probe is still held to the whole body: the payload alone will not do
    assert (
        verdict(
            security.PAGE_SECRET,
            {"comp-s": {"gone": True, "stdout": f"{security.PAGE_SECRET}\n"}},
        )
        is False
    )
