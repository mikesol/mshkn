"""The coding capability (2026-09-17-coding-design.md): the script's shape, the
command it reads out of row 20's reply, the probes it runs on a fork of the
verb's chain, and the two checks only it needs."""

from __future__ import annotations

from typing import Any

import pytest
from membrane.capabilities import CAPABILITIES, catalog, load, load_module, order

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
