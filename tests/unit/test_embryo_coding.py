"""The coding capability (2026-09-17-coding-design.md): the script's shape, the
command it reads out of row 20's reply, the probes it runs on a fork of the
verb's chain, and the two checks only it needs."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from membrane.capabilities import CAPABILITIES, catalog, load, load_module, order
from membrane.capability import Doors, Record
from membrane.postconditions import Turn

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

CODING = load(CAPABILITIES / "coding.md")


def test_the_script_is_seven_rows_on_security_and_names_no_mechanism() -> None:
    assert CODING.name == "coding"
    assert CODING.depends == ("security",)  # `load` makes both frontmatter lists tuples
    assert CODING.postconditions == (
        "root_unforgeable",
        "no_undeclared_capability",
        "nothing_by_hand",
        "runs_again",
        "fixed",
    )
    assert [r.label for r in CODING.rows] == ["15", "16", "17", "18", "19", "20", "21"]
    assert [r.door for r in CODING.rows] == [
        "signed",
        "signed",
        "signed",
        "signed",
        "signed",
        "signed",
        "root list",
    ]
    assert [r.label for r in CODING.rows if r.proposes] == ["15", "18"]
    assert CODING.repair.provide is None
    assert CODING.repair.build and CODING.repair.refused
    assert CODING.repair.stalled and CODING.repair.silent
    words = " ".join(r.words for r in CODING.rows)
    for mechanism in ("verb", "Dockerfile", "chain", "python", "test"):
        assert mechanism.lower() not in words.lower(), mechanism
    # the list root feeds it, empty line and all, is in the words and not paraphrased
    assert "4\n\nN/A\n6" in CODING.row("17").words
    assert "/tmp/amounts" in CODING.row("20").words  # root's own path, the only one
    assert order(catalog(), "coding") == [catalog()["hatch"], catalog()["security"], CODING]


@pytest.fixture
def coding() -> Any:
    """A fresh import each test; conftest's autouse fixture restores CHECKS."""
    module = load_module(CODING)
    assert module is not None
    return module


def test_the_command_is_read_from_a_fenced_block_or_inline_code(coding: Any) -> None:
    fenced = "Run this:\n\n```\ntotal /tmp/amounts\n```\n"
    assert coding.command_in(fenced) == "total /tmp/amounts"
    tagged = "```sh\n/verb/total.sh /tmp/amounts\n```"
    assert coding.command_in(tagged) == "/verb/total.sh /tmp/amounts"
    inline = "You would run `python3 /work/total.py /tmp/amounts` yourself."
    assert coding.command_in(inline) == "python3 /work/total.py /tmp/amounts"
    assert coding.command_in("I would run the program on the file.") is None
    assert coding.command_in("") is None


def test_a_multi_line_block_is_not_a_command(coding: Any) -> None:
    assert coding.command_in("```\ncd /work\n./total.sh /tmp/amounts\n```") is None


def test_a_shell_prompt_is_not_part_of_the_command(coding: Any) -> None:
    # pasted whole, the `$` is the command and the shell exits 127
    assert coding.command_in("```bash\n$ /verb/total.sh /tmp/amounts\n```") == (
        "/verb/total.sh /tmp/amounts"
    )
    assert coding.command_in("Run `$ total /tmp/amounts`.") == "total /tmp/amounts"


def test_the_path_root_named_is_not_read_as_the_command(coding: Any) -> None:
    # row 20's own words carry the path, so a reply quoting it back offers it first
    reply = "Leave the file at `/tmp/amounts`, then run `total /tmp/amounts`."
    assert coding.command_in(reply) == "total /tmp/amounts"
    assert coding.command_in("Leave it at `/tmp/amounts`.") is None


def test_the_proposal_documents_are_not_read_for_a_command(coding: Any) -> None:
    reply = (
        "I would run the program on the file.\n"
        "proposal p-1\nentrypoint: sh -c 'echo `cat /tmp/amounts`'\n"
    )
    assert coding.command_in(reply) is None


def test_every_number_in_the_output_is_offered_as_the_total(coding: Any) -> None:
    assert coding.totals_in("105.00") == [105.0]
    assert coding.totals_in("3 amounts\ntotal: 105") == [3.0, 105.0]
    # each of these a correct program plausibly prints; taking the last loses them all
    assert coding.totals_in("Total: 105.00 (3 amounts)") == [105.0, 3.0]
    assert coding.totals_in("total=105.00, count=3") == [105.0, 3.0]
    assert coding.totals_in("Total: 105.00 across 3 lines") == [105.0, 3.0]
    assert coding.totals_in("105,00") == [105.0, 0.0]  # a comma splits, so the total is there
    assert coding.totals_in("no numbers here") == []


class ProbeApi:
    """The routes `verify` uses: checkpoints, fork, upload, exec, destroy.

    The probe names and the total come from the module, so a fixture and the code
    cannot drift apart on either the order the three files are written in or what
    a correct program prints for them."""

    def __init__(self, coding: Any, *, exits: dict[str, int] | None = None) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.execs: list[str] = []
        self.deleted: list[str] = []
        self.checkpoints = [{"id": "ck-1", "label": "verb/total", "created_at": "t"}]
        self.names = [name for name, _ in coding.AMOUNTS]
        self.total = coding.PROBE_TOTAL
        self.exits = exits or {}
        self.seen = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/checkpoints":
            label = request.url.params.get("label")
            return httpx.Response(
                200, json=[c for c in self.checkpoints if not label or c["label"] == label]
            )
        if request.method == "POST" and path.endswith("/fork"):
            return httpx.Response(200, json={"computer_id": "comp-1"})
        if request.method == "POST" and path.endswith("/upload"):
            self.uploads.append((request.url.params["path"], request.content))
            return httpx.Response(200, json={"status": "uploaded"})
        if request.method == "POST" and path.endswith("/exec"):
            self.execs.append(json.loads(request.content)["command"])
            which = self.names[self.seen]
            self.seen += 1
            code = self.exits.get(which, 0)
            body = "" if code else f"{self.total:.2f}"
            stream = f"event: stdout\r\ndata: {body}\r\n\r\nevent: exit\r\ndata: {code}\r\n\r\n"
            return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
        if request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(200, json={"status": "destroyed"})
        return httpx.Response(404, json={"detail": path})


def _doors(api: ProbeApi, tmp_path: Path) -> Doors:
    client = httpx.AsyncClient(
        base_url="http://api",
        headers={"Authorization": "Bearer k"},
        transport=httpx.MockTransport(api.handler),
    )
    return Doors(client, client, "rule", Record(tmp_path / "run"))


FINAL = {"catalog": {"total": {"chain": "verb/total", "state": "chain", "status": "ready"}}}
REPLY20 = "Run:\n\n```\n/verb/total.sh /tmp/amounts\n```\n"


def _turns() -> list[Turn]:
    return [Turn("20", "ingress", "what command", {"tools": []}, REPLY20, [], [])]


async def test_verify_runs_the_command_over_three_files_on_a_fork(
    coding: Any, tmp_path: Path
) -> None:
    api = ProbeApi(coding, exits={"word": 2})
    doors = _doors(api, tmp_path)
    await coding.verify(doors, _turns(), FINAL, io.StringIO())
    assert api.execs == ["/verb/total.sh /tmp/amounts"] * 3
    assert [p for p, _ in api.uploads] == [coding.PROBE_PATH] * 3
    assert [body for _, body in api.uploads] == [
        coding.CLEAN.encode(),
        coding.WITH_BLANKS.encode(),
        coding.WITH_WORD.encode(),
    ]
    assert api.deleted == ["/computers/comp-1"]  # the fork is gone, the chain untouched
    # `capabilities.py:72-75`: `sent` is snapshotted after `verify` returns, so a
    # probe that reached for `doors.root` or `doors.provision` would be charged to
    # the agent by `nothing_by_hand`. The probes go with the account key, no door.
    assert doors.sent == []
    assert coding.probes["chain"] == "verb/total"
    assert coding.probes["command"] == "/verb/total.sh /tmp/amounts"
    assert coding.probes["checkpoint"] == "ck-1"
    assert coding.probes["results"]["clean"] == {
        "exit_code": 0,
        "stdout": f"{coding.PROBE_TOTAL:.2f}",
        "totals": [coding.PROBE_TOTAL],
    }
    assert coding.probes["results"]["word"]["exit_code"] == 2


async def test_verify_records_a_reply_that_named_no_command(coding: Any, tmp_path: Path) -> None:
    api = ProbeApi(coding)
    turns = [Turn("20", "ingress", "what command", {"tools": []}, "I would run it.", [], [])]
    await coding.verify(_doors(api, tmp_path), turns, FINAL, io.StringIO())
    assert coding.probes == {"error": "row 20 named no command"}
    assert api.execs == []


async def test_verify_reads_an_empty_code_span_as_no_command(coding: Any, tmp_path: Path) -> None:
    """`INLINE_CODE_RE` matches a span of one space, which strips to `""` and not
    to `None`. An `is None` guard would let it through, and the module would
    upload three files and exec `""` over each, recording the exit codes of
    nothing as if the agent's program had run."""
    assert not coding.command_in("Run ` `.")
    api = ProbeApi(coding)
    turns = [Turn("20", "ingress", "what command", {"tools": []}, "Run ` `.", [], [])]
    await coding.verify(_doors(api, tmp_path), turns, FINAL, io.StringIO())
    assert coding.probes == {"error": "row 20 named no command"}
    assert api.execs == []


async def test_verify_refuses_to_guess_among_several_chains(coding: Any, tmp_path: Path) -> None:
    api = ProbeApi(coding)
    final = {
        "catalog": {
            "alpha": {"chain": "verb/alpha", "state": "chain"},
            "beta": {"chain": "verb/beta", "state": "chain"},
        }
    }
    turns = [Turn("20", "ingress", "w", {"tools": []}, "```\n./run /tmp/amounts\n```", [], [])]
    await coding.verify(_doors(api, tmp_path), turns, final, io.StringIO())
    assert coding.probes["error"] == (
        "several chain verbs, and './run /tmp/amounts' singles out none of them"
    )
    assert coding.probes["candidates"] == ["verb/alpha", "verb/beta"]
    assert "results" not in coding.probes  # nothing probed, so there are no results
    assert api.execs == []


async def test_verify_records_a_catalog_that_holds_no_chain_verb(
    coding: Any, tmp_path: Path
) -> None:
    """Design §7: the agent having shipped nothing that persists is a capability
    failure in its own right, and a reader of the record must be able to tell it
    from an apparatus that could not choose among several candidates."""
    api = ProbeApi(coding)
    final = {"catalog": {"greet": {"chain": "verb/greet", "state": "ephemeral"}}}
    await coding.verify(_doors(api, tmp_path), _turns(), final, io.StringIO())
    assert coding.probes["error"] == (
        "the catalog holds no chain verb to run '/verb/total.sh /tmp/amounts' on"
    )
    assert coding.probes["candidates"] == []
    assert api.execs == []


async def test_verify_picks_the_longest_verb_name_the_command_names(
    coding: Any, tmp_path: Path
) -> None:
    """`total` and `subtotal` both appear in `/verb/subtotal.sh /tmp/amounts`,
    because one name is a substring of the other. The command named its chain
    exactly, and reporting that as ambiguous would cost both exercises."""
    api = ProbeApi(coding)
    api.checkpoints = [{"id": "ck-2", "label": "verb/subtotal", "created_at": "t"}]
    final = {
        "catalog": {
            "total": {"chain": "verb/total", "state": "chain"},
            "subtotal": {"chain": "verb/subtotal", "state": "chain"},
        }
    }
    reply = "```\n/verb/subtotal.sh /tmp/amounts\n```"
    turns = [Turn("20", "ingress", "w", {"tools": []}, reply, [], [])]
    await coding.verify(_doors(api, tmp_path), turns, final, io.StringIO())
    assert coding.probes["chain"] == "verb/subtotal"
    assert len(api.execs) == 3


async def test_verify_does_not_let_the_path_root_named_decide_the_chain(
    coding: Any, tmp_path: Path
) -> None:
    """Row 20's words put `/tmp/amounts` into every command the agent can name, so
    a verb called `amounts` is named by all of them -- and being the longer name it
    beat the `total` the command actually invokes. `verify` forked a chain the
    program is not on and failed a correct agent, with a record that named the
    wrong chain and looked authoritative."""
    api = ProbeApi(coding)
    final = {
        "catalog": {
            "total": {"chain": "verb/total", "state": "chain"},
            "amounts": {"chain": "verb/amounts", "state": "chain"},
        }
    }
    await coding.verify(_doors(api, tmp_path), _turns(), final, io.StringIO())
    assert coding.probes["chain"] == "verb/total"
    assert len(api.execs) == 3


async def test_verify_reads_a_verb_named_only_inside_that_path_as_no_tie(
    coding: Any, tmp_path: Path
) -> None:
    """`mount` sits inside `/tmp/amounts` and is exactly as long as `total`, so
    matching against the whole command tied the two and reported an unambiguous
    run as ambiguous, costing both exercises."""
    api = ProbeApi(coding)
    final = {
        "catalog": {
            "total": {"chain": "verb/total", "state": "chain"},
            "mount": {"chain": "verb/mount", "state": "chain"},
        }
    }
    await coding.verify(_doors(api, tmp_path), _turns(), final, io.StringIO())
    assert coding.probes["chain"] == "verb/total"
    assert len(api.execs) == 3


async def test_verify_reads_two_verbs_declaring_one_chain_as_one_candidate(
    coding: Any, tmp_path: Path
) -> None:
    """`total` and `subtotal` may both checkpoint onto `verb/money`. Counting names
    rather than chains reported the one chain there is as impossible to choose, and
    the record offered it twice as if it were two."""
    api = ProbeApi(coding)
    api.checkpoints = [{"id": "ck-3", "label": "verb/money", "created_at": "t"}]
    final = {
        "catalog": {
            "total": {"chain": "verb/money", "state": "chain"},
            "subtotal": {"chain": "verb/money", "state": "chain"},
        }
    }
    turns = [Turn("20", "ingress", "w", {"tools": []}, "```\n./run /tmp/amounts\n```", [], [])]
    await coding.verify(_doors(api, tmp_path), turns, final, io.StringIO())
    assert coding.probes["chain"] == "verb/money"
    assert coding.probes["checkpoint"] == "ck-3"
    assert len(api.execs) == 3


async def test_verify_reads_past_an_ephemeral_verb_to_the_one_chain(
    coding: Any, tmp_path: Path
) -> None:
    """Every catalog entry carries a `chain` name; only a `state: chain` verb ever
    checkpoints onto it. One chain verb beside an ephemeral one is not ambiguous."""
    api = ProbeApi(coding)
    final = {
        "catalog": {
            "total": {"chain": "verb/total", "state": "chain"},
            "greet": {"chain": "verb/greet", "state": "ephemeral"},
        }
    }
    await coding.verify(_doors(api, tmp_path), _turns(), final, io.StringIO())
    assert coding.probes["chain"] == "verb/total"
    assert len(api.execs) == 3


async def test_verify_records_a_chain_that_has_no_head_to_fork(coding: Any, tmp_path: Path) -> None:
    """A verb declared `state: chain` that never checkpointed: the catalog names
    the chain, and the account has nothing under the label to fork."""
    api = ProbeApi(coding)
    api.checkpoints = []
    await coding.verify(_doors(api, tmp_path), _turns(), FINAL, io.StringIO())
    assert coding.probes["error"] == "verb/total has no head"
    assert coding.probes["chain"] == "verb/total"
    assert "results" not in coding.probes
    assert api.execs == []


class BreakingApi(ProbeApi):
    """A host that answers `breaks` for `after` calls and then returns 500."""

    def __init__(self, coding: Any, *, breaks: str, after: int = 0) -> None:
        super().__init__(coding)
        self.breaks, self.after = breaks, after
        self.hits = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith(self.breaks):
            self.hits += 1
            if self.hits > self.after:
                return httpx.Response(500, json={"detail": "the host is gone"})
        return super().handler(request)


async def test_verify_writes_a_probe_that_broke_into_the_findings(
    coding: Any, tmp_path: Path
) -> None:
    """`run_verify` swallows what escapes into the driver's log, which is not the
    committed record, so a probe that died that way would leave `probes` empty --
    and `runs_again` would read that as a program that does not run, which is not
    what happened. The failure, and whatever the probes got before it, are the
    finding."""
    broke = BreakingApi(coding, breaks="/exec", after=1)
    await coding.verify(_doors(broke, tmp_path), _turns(), FINAL, io.StringIO())
    assert "500" in coding.probes["error"]
    assert coding.probes["chain"] == "verb/total"
    assert coding.probes["command"] == "/verb/total.sh /tmp/amounts"
    assert list(coding.probes["results"]) == ["clean"]  # what ran before the host broke
    assert broke.deleted == ["/computers/comp-1"]  # the fork went even so


async def test_verify_destroys_nothing_when_the_fork_itself_failed(
    coding: Any, tmp_path: Path
) -> None:
    no_fork = BreakingApi(coding, breaks="/fork")
    await coding.verify(_doors(no_fork, tmp_path), _turns(), FINAL, io.StringIO())
    assert "500" in coding.probes["error"]
    assert coding.probes["results"] == {}
    assert no_fork.deleted == []  # nothing was made, so there is nothing to destroy


class NoCheckpointsApi(ProbeApi):
    """A host whose `GET /checkpoints` answers 500, which is what `Doors.head`
    raises through (`capability.py:737-748` calls `raise_for_status`)."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/checkpoints":
            return httpx.Response(500, json={"detail": "the host is gone"})
        return super().handler(request)


class FullDisk:
    """A log whose `write` raises, the way one on a disk with no space left does."""

    def write(self, _line: str) -> int:
        raise OSError(28, "No space left on device")


class ForkWithoutComputerApi(ProbeApi):
    """A fork that answers 200 with a body of another shape: reading
    `computer_id` off it raises `KeyError`."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/fork"):
            return httpx.Response(200, json={"id": "comp-1"})
        return super().handler(request)


async def test_verify_records_a_checkpoint_listing_that_failed(coding: Any, tmp_path: Path) -> None:
    """`doors.head` is a call to the host like any other, and it used to sit
    outside the net: a 500 on `GET /checkpoints` escaped into `run_verify`'s log
    and left `probes` empty for `runs_again` to read as a program that does not
    run. A host that hiccuped is a finding, not a verdict on the agent."""
    api = NoCheckpointsApi(coding)
    await coding.verify(_doors(api, tmp_path), _turns(), FINAL, io.StringIO())
    assert "500" in coding.probes["error"]
    assert coding.probes["chain"] == "verb/total"  # how far it got before the host broke
    assert coding.probes["command"] == "/verb/total.sh /tmp/amounts"
    assert api.execs == []


async def test_verify_leaves_a_finding_when_the_log_cannot_be_written(
    coding: Any, tmp_path: Path
) -> None:
    """The handler logged before it recorded, and the record was written after the
    net: a full disk on top of a host that answered 500 left `probes` empty and
    sent the `OSError` into `run_verify` -- which is exactly the state `runs_again`
    reads as a program that does not run. The finding lands first now, and the log
    failing after it costs the driver a line and not the record."""
    api = NoCheckpointsApi(coding)
    with pytest.raises(OSError, match="No space left"):
        await coding.verify(_doors(api, tmp_path), _turns(), FINAL, FullDisk())
    assert "500" in coding.probes["error"]
    assert coding.probes["chain"] == "verb/total"
    assert coding.probes["command"] == "/verb/total.sh /tmp/amounts"
    assert coding.probes["results"] == {}


async def test_verify_records_a_fork_that_answered_without_a_computer_id(
    coding: Any, tmp_path: Path
) -> None:
    """A 200 whose body has another shape raises `KeyError`, which is neither
    `httpx.HTTPError` nor `ValueError`; the narrower net let it escape and leave
    `probes` empty."""
    api = ForkWithoutComputerApi(coding)
    await coding.verify(_doors(api, tmp_path), _turns(), FINAL, io.StringIO())
    assert coding.probes["error"] == "KeyError: 'computer_id'"
    assert coding.probes["checkpoint"] == "ck-1"
    assert coding.probes["results"] == {}
    assert api.execs == []
    assert api.deleted == []  # no computer id, so nothing to destroy
