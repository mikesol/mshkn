"""The coding capability (2026-09-17-coding-design.md): the script's shape, the
command it reads out of row 20's reply, the probes it runs on a fork of the
verb's chain, and the two checks only it needs."""

from __future__ import annotations

import pytest
from membrane.capabilities import CAPABILITIES, catalog, load, order

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
