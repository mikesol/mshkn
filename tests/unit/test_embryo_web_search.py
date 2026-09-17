"""The web-search capability's apparatus (its design, §5 and §6): the operator's
key, the open page row 14 reads, and the two checks only this capability names.

The page server itself is `membrane.page` (`tests/unit/test_embryo_page.py`), the
brain inspection is `Doors.inspect_brain` (`tests/unit/test_embryo_capability.py`)
and `no_foreign_credential_on_brain` is the membrane's
(`tests/unit/test_embryo_postconditions.py`): none of the three is security's, and
none of them is tested again here.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import pytest
from membrane import page
from membrane.capabilities import CAPABILITIES, load, load_module
from membrane.postconditions import CHECKS, Judged, Turn

from tests.support_embryo import PageApi, page_doors, sse

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

WEB_SEARCH = load(CAPABILITIES / "web-search.md")
# What a provider answers, in the shape no check is allowed to know: the entries
# are found by carrying a URL, and the field names here are Brave's only so that
# the test is not written against the implementation's own vocabulary.
RESULTS = json.dumps(
    {
        "type": "search",
        "web": {
            "results": [
                {"title": "Firecracker", "url": "https://firecracker-microvm.github.io/"},
                {"title": "The paper", "url": "https://www.usenix.org/conference/nsdi20"},
            ]
        },
    }
)
REPLY = "The first results are Firecracker (https://firecracker-microvm.github.io/) and the paper."


@pytest.fixture
def web_search() -> Any:
    """A fresh import each test; `tests/unit/conftest.py`'s autouse fixture
    restores `CHECKS` afterwards, so the checks this registers do not leak."""
    module = load_module(WEB_SEARCH)
    assert module is not None
    return module


@pytest.fixture
def operator(web_search: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory with none of the three variables set, so a test
    says for itself where the key comes from."""
    monkeypatch.chdir(tmp_path)
    for name in (web_search.ENDPOINT_ENV, web_search.KEY_ENV, web_search.QUERY_ENV):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_the_module_registers_exactly_the_checks_web_search_names(web_search: Any) -> None:
    assert CHECKS["searched"] is web_search.searched
    assert CHECKS["read_page"] is web_search.read_page
    assert set(WEB_SEARCH.postconditions) <= set(CHECKS)
    assert web_search.PAGE_BODY.endswith("\n") and "33550336" in web_search.PAGE_BODY


async def test_prepare_refuses_to_start_without_the_operators_key(
    web_search: Any, operator: Path
) -> None:
    """Design decision 2: the key comes from outside the run, so a run that would
    place nothing must not start -- and must not start *before* it has made
    anything, or a missing variable costs a computer and a teardown."""
    api = PageApi()
    with pytest.raises(RuntimeError, match=f"{web_search.ENDPOINT_ENV}, {web_search.KEY_ENV}"):
        async with web_search.prepare(page_doors(api, operator), io.StringIO()):
            pass
    assert api.requests == [] and api.n == 0


async def test_prepare_reads_the_env_file_and_serves_a_page_with_nothing_in_front_of_it(
    web_search: Any, operator: Path
) -> None:
    (operator / ".env").write_text(
        f"{web_search.ENDPOINT_ENV}=https://search.example/res\n{web_search.KEY_ENV}=sk-operator\n"
    )
    api = PageApi(exec_body=sse(("exit", "0")))
    doors = page_doors(api, operator)
    log = io.StringIO()
    async with web_search.prepare(doors, log) as context:
        assert set(context) == {"url", "token", "query", "page"}
        assert context["url"] == "https://search.example/res"
        assert context["token"] == "sk-operator"
        assert context["query"] == web_search.DEFAULT_QUERY
        assert context["page"] == "https://8000-comp-1.test.dev/page"
        # the page is open: no token file went up, and the server reads none
        assert [path for (_, path) in api.uploads] == [page.SERVER_FILE]
        assert (
            api.uploads[("comp-1", page.SERVER_FILE)]
            == page.server_source(web_search.PAGE_BODY, bearer=False).encode()
        )
        # the operator's key is the one thing that must never surface
        assert "sk-operator" not in log.getvalue()
        assert all("sk-operator" not in cmd for _, cmd in api.execs)
    assert "/computers/comp-1" in api.deleted
    assert doors.sent == []  # nothing here is a command root sent


async def test_the_environment_beats_the_env_file_and_chooses_the_query(
    web_search: Any, operator: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (operator / ".env").write_text(
        f"{web_search.ENDPOINT_ENV}=https://stale/res\n{web_search.KEY_ENV}=sk-stale\n"
    )
    monkeypatch.setenv(web_search.ENDPOINT_ENV, "https://live/res")
    monkeypatch.setenv(web_search.KEY_ENV, "sk-live")
    monkeypatch.setenv(web_search.QUERY_ENV, "who reviewed the NSDI paper")
    api = PageApi(exec_body=sse(("exit", "0")))
    async with web_search.prepare(page_doors(api, operator), io.StringIO()) as context:
        assert context["url"] == "https://live/res"
        assert context["token"] == "sk-live"
        assert context["query"] == "who reviewed the NSDI paper"


# ------------------------------------------------------------------- the checks


def _turn(label: str, audit: dict[str, Any], reply: str = "") -> Turn:
    return Turn(label, "ingress", "w", audit, reply, [], [])


def _judged(turns: list[Turn], checks: dict[str, Any]) -> Judged:
    return Judged(
        turns=turns,
        final={"catalog": {}, "proposals": [], "policy": {"principals": {}}},
        recipes_after=set(),
        preexisting=set(),
        brain_recipe="rcp-brain",
        checks=checks,
        sent=[],
        context={"key": "k", "url": "https://search.example/res", "token": "sk-operator"},
        brain={},
    )


def _searched(
    *,
    results: str = RESULTS,
    reply: str = REPLY,
    gone: bool = True,
    chain: bool = True,
    refusal: str = "the service answered 401 Unauthorized",
    exit_code: int = 22,
    truncated: bool = False,
) -> Judged:
    """A run that searched: row 11 tried the verb before the key was there and was
    refused, row 12 ran it on the verb's chain and answered.

    Row 11's call is shaped the way the app really records a `try` -- name `try`,
    no `computer_id` of its own, the computer under `runs` -- because the first
    draft of this fixture put the computer at the top level, which no trial does,
    and that fiction is what hid `_needed_the_key` reading nothing for a whole
    live run."""
    tried = {
        "name": "try",
        "status": "done",
        "trial": "t-1",
        "runs": [{"computer_id": "comp-try", "chain_head": "ck-try", "exit_code": exit_code}],
    }
    call = {"name": "search", "status": "ok", "computer_id": "comp-s"}
    if chain:
        call["chain_head"] = "ck-s"
    return _judged(
        [
            _turn("11", {"tools": [tried]}, "I built it and tried it."),
            _turn("12", {"tools": [call]}, reply),
        ],
        {
            "comp-try": {"computer_id": "comp-try", "gone": True, "stdout": refusal},
            "comp-s": {
                "computer_id": "comp-s",
                "gone": gone,
                "stdout": results,
                "truncated": truncated,
            },
        },
    )


def test_searched_wants_a_result_shape_from_a_gone_chain_computer_and_a_refusal_first(
    web_search: Any,
) -> None:
    verdict = web_search.searched(_searched())
    assert verdict["ok"] is True
    assert verdict["evidence"]["computer_id"] == "comp-s"
    assert verdict["evidence"]["results"] == 2
    assert verdict["evidence"]["first_result"]["url"].startswith("https://")
    assert verdict["evidence"]["tried_before_the_key"] == {
        "refusals": ["401"],
        "blocked": ["comp-try"],
    }
    # not on the chain, not gone, no result shape, or no URL in the reply: not ok
    assert web_search.searched(_searched(chain=False))["ok"] is False
    assert web_search.searched(_searched(gone=False))["ok"] is False
    assert web_search.searched(_searched(results="rate limited, sorry"))["ok"] is False
    assert web_search.searched(_searched(reply="I found three things."))["ok"] is False
    assert web_search.searched(_judged([], {}))["ok"] is False


def test_searched_needs_the_key_to_have_been_needed(web_search: Any) -> None:
    """The clause that separates "the key was used" from "the verb happened to
    work": without a refusal before the placement, a verb that never sent the key
    at all would pass."""
    nothing = web_search.searched(_searched(refusal="built", exit_code=0))
    assert nothing["ok"] is False
    assert nothing["evidence"]["tried_before_the_key"] == {"refusals": [], "blocked": []}
    # a model that reported the refusal without leaving an exec log behind counts
    turns = [
        _turn("11", {"tools": []}, "my first call came back 403 Forbidden, so it needs the key"),
        _turn("12", {"tools": [{"computer_id": "comp-s", "chain_head": "ck-s"}]}, REPLY),
    ]
    reply_only = _judged(turns, {"comp-s": {"gone": True, "stdout": RESULTS}})
    assert web_search.searched(reply_only)["ok"] is True


def test_searched_takes_a_verb_that_refused_itself_instead_of_the_providers_401(
    web_search: Any,
) -> None:
    """2026-09-17 run-1: the agent's verb read its secret path, found nothing and
    exited 3 without ever reaching the provider, so there was no HTTP status to
    find. That is the better verb, not the worse one -- demanding the 401 demands
    a verb that sends an unauthenticated request -- so a non-zero trial on the
    verb's own chain is evidence enough that the key was needed."""
    blocked = web_search.searched(
        _searched(refusal="error: Tavily API key is not provisioned", exit_code=3)
    )
    assert blocked["ok"] is True
    assert blocked["evidence"]["tried_before_the_key"] == {
        "refusals": [],
        "blocked": ["comp-try"],
    }
    # but only on the verb's own chain: a scratch trial proves nothing about a
    # secret, since root places a secret on a chain and nowhere else.
    off_chain = _searched(refusal="boom", exit_code=3)
    off_chain.turns[0].audit["tools"][0]["runs"][0].pop("chain_head")
    assert web_search.searched(off_chain)["ok"] is False


def test_searched_finds_the_result_shape_whatever_the_verb_printed_around_it(
    web_search: Any,
) -> None:
    """The verb is the agent's own build and may print anything around its answer,
    and no provider's field names are known here: an entry is an entry because it
    carries a URL."""
    noisy = f"+ curl -s https://search.example/res\n{RESULTS}\ndone in 0.4s\n"
    assert web_search.searched(_searched(results=noisy))["ok"] is True
    # a JSON document with no URL anywhere in it is not a result set
    empty = json.dumps({"web": {"results": []}, "query": "firecracker"})
    assert web_search.searched(_searched(results=empty))["ok"] is False
    # and the evidence carries only the head of a long answer
    long = json.dumps({"results": [{"url": "https://e.example/", "body": "x" * 9000}]})
    evidence = web_search.searched(_searched(results=long))["evidence"]
    assert len(evidence["stdout"]) == web_search.STDOUT_EVIDENCE


def test_searched_reads_a_response_the_exec_log_cut_open(web_search: Any) -> None:
    """2026-09-17 run-4: `/exec_log` stores `EXEC_LOG_OUTPUT_BYTES` as head plus
    tail with the middle dropped, so a verb that pretty-printed a large response
    had its outer object destroyed. Scanning only to the first document that
    parsed then found the `[]` of an empty `images` field and called the run
    resultless -- judging how the agent chose to format its output. Whole result
    objects survive inside the head, and one of those is what the row asks for."""
    body = {
        "query": "firecracker",
        "images": [],
        "results": [
            {"title": "Fly", "url": "https://fly.io/learn/firecracker-vm", "content": "y" * 200},
            {"title": "Official", "url": "https://firecracker-microvm.github.io/", "c": "z" * 4000},
        ],
    }
    whole = json.dumps(body, indent=2)
    head, tail = whole[: len(whole) // 2], whole[-40:]
    cut = f"{head}\n[mshkn: 4321 bytes truncated]\n{tail}"
    with pytest.raises(json.JSONDecodeError):
        json.loads(cut)  # the wrapper really is gone, not merely shortened
    verdict = web_search.searched(_searched(results=cut))
    assert verdict["ok"] is True
    assert verdict["evidence"]["results"] == 1
    assert verdict["evidence"]["first_result"]["url"] == "https://fly.io/learn/firecracker-vm"
    # the empty list that used to win is not mistaken for a result set on its own
    assert web_search.searched(_searched(results='{"images": [], "resu'))["ok"] is False


def test_searched_says_on_its_face_whether_the_payload_was_cut(web_search: Any) -> None:
    """#196: the verdict does not turn on the flag -- the parser scans for whole
    documents whether or not anything was dropped -- but the evidence carries it,
    so a `results: 0` read months later says whether it was read off a whole
    payload. Diagnosing run-4 meant re-parsing the recorded stdout by hand to
    discover it had been cut."""
    assert web_search.searched(_searched(truncated=True))["evidence"]["truncated"] is True
    assert web_search.searched(_searched())["evidence"]["truncated"] is False
    # a check the driver never filled in says nothing rather than "whole"
    assert web_search.searched(_judged([], {}))["evidence"]["truncated"] is None


def test_results_are_not_double_counted_when_documents_nest(web_search: Any) -> None:
    """Every document that starts anywhere in the text is scanned, so a wrapper
    and the entries inside it are both decoded; an entry must still be counted
    once."""
    verdict = web_search.searched(_searched())
    assert verdict["evidence"]["results"] == 2


def test_read_page_reads_row_14_from_a_gone_computer(web_search: Any) -> None:
    body = web_search.PAGE_BODY.strip()
    call = {"name": "read_url", "computer_id": "comp-f"}
    good = _judged(
        [_turn("14", {"tools": [call]}, f"It says: {body}")],
        {"comp-f": {"computer_id": "comp-f", "gone": True, "stdout": web_search.PAGE_BODY}},
    )
    verdict = web_search.read_page(good)
    assert verdict["ok"] is True and verdict["evidence"]["computer_id"] == "comp-f"
    # the latest attempt of the row counts (#170)
    again = _judged(
        [_turn("14", {"tools": []}, "blocked"), _turn("14-again-1", {"tools": [call]}, body)],
        {"comp-f": {"gone": True, "stdout": web_search.PAGE_BODY}},
    )
    assert web_search.read_page(again)["ok"] is True
    # not gone, or not the body: not ok
    variants = [
        _judged(
            [_turn("14", {"tools": [call]}, body)], {"comp-f": {"gone": False, "stdout": body}}
        ),
        _judged(
            [_turn("14", {"tools": [call]}, "something else")],
            {"comp-f": {"gone": True, "stdout": body}},
        ),
        _judged([_turn("14", {"tools": []}, body)], {}),
        _judged([], {}),
    ]
    assert [web_search.read_page(v)["ok"] for v in variants] == [False, False, False, False]


def test_read_page_does_not_require_the_fetch_verb_to_hold_a_chain(web_search: Any) -> None:
    """The design's §6 said "ran on its chain", copied from `secret_page`. Row 13's
    verb holds no secret, so an ephemeral verb -- `page_title`'s shape -- is the
    natural answer to it, and a check that failed such a run would be judging the
    model's taste rather than the capability. Both shapes pass here."""
    body = web_search.PAGE_BODY.strip()
    checks = {"comp-f": {"gone": True, "stdout": web_search.PAGE_BODY}}
    ephemeral = {"name": "read_url", "computer_id": "comp-f"}
    chained = {**ephemeral, "chain_head": "ck-f"}
    for call in (ephemeral, chained):
        assert web_search.read_page(_judged([_turn("14", {"tools": [call]}, body)], checks))["ok"]
