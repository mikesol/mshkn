"""Capability files (capabilities design §4): frontmatter, a headed section per
row with its words in a fenced block, and a Repair section, loaded into a frozen
document the driver speaks from."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from membrane.capabilities import (
    CAPABILITIES,
    DOORS,
    TEMPLATES,
    Capability,
    CapabilityError,
    Repair,
    Row,
    catalog,
    load,
    order,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

MINIMAL = """---
name: {name}
depends: {depends}
postconditions:
  - authentication
---

# {name}

Prose for the human, and a block of it that is not a row's words:

```
not words
```

### 1 · root say

```
Hello {{key}}
```

A reply.

### 2 · signed

```
Who am I?
```

Named.

### 3 · unsigned

```
Who am I?
```

Anonymous.

### 4 · root list

The end.

## Repair

- build: `check your build`
- refused: `check your inbox`
"""


def _write(tmp_path: Path, name: str, depends: str = "[]", text: str | None = None) -> Path:
    path = tmp_path / f"{name}.md"
    path.write_text(text or MINIMAL.format(name=name, depends=depends))
    return path


def _text(depends: str = "[]") -> str:
    return MINIMAL.format(name="one", depends=depends)


def test_load_reads_frontmatter_rows_and_repair(tmp_path: Path) -> None:
    cap = load(_write(tmp_path, "one"))
    assert cap == Capability(
        name="one",
        depends=(),
        postconditions=("authentication",),
        rows=(
            Row("1", "root say", "Hello {key}", "A reply."),
            Row("2", "signed", "Who am I?", "Named."),
            Row("3", "unsigned", "Who am I?", "Anonymous."),
            Row("4", "root list", "", "The end."),
        ),
        repair=Repair(build="check your build", refused="check your inbox"),
        path=tmp_path / "one.md",
    )
    assert cap.words == {"1": "Hello {key}", "2": "Who am I?", "3": "Who am I?", "4": ""}
    assert cap.row("2").door == "signed"


def test_a_rows_words_keep_their_line_breaks_and_lose_the_trailing_newline(
    tmp_path: Path,
) -> None:
    """The block is verbatim: what the row speaks is what is between the fences,
    so a paragraph the writer broke over three lines arrives with its breaks."""
    text = _text().replace("Hello {key}", "Hello {key}\n\nand hello again")
    cap = load(_write(tmp_path, "one", text=text))
    assert cap.row("1").words == "Hello {key}\n\nand hello again"


def test_depends_accepts_a_bullet_list(tmp_path: Path) -> None:
    cap = load(_write(tmp_path, "two", depends="\n  - one\n  - zero"))
    assert cap.depends == ("one", "zero")


def test_depends_accepts_the_inline_list_form(tmp_path: Path) -> None:
    """`depends: [hatch]` is what a writer reaches for first; the loader reads it
    as well as the bullet form, and an empty pair of brackets is no dependency."""
    assert load(_write(tmp_path, "two", depends="[one]")).depends == ("one",)
    assert load(_write(tmp_path, "three", depends="[one, zero]")).depends == ("one", "zero")
    assert load(_write(tmp_path, "four", depends="[]")).depends == ()


def test_the_name_must_match_the_file(tmp_path: Path) -> None:
    path = tmp_path / "other.md"
    path.write_text(_text())
    with pytest.raises(CapabilityError, match=r"name 'one' does not match other\.md"):
        load(path)


def test_an_unknown_frontmatter_key_is_an_error(tmp_path: Path) -> None:
    text = _text().replace("name: one", "name: one\nowner: me")
    with pytest.raises(CapabilityError, match="unknown frontmatter key 'owner'"):
        load(_write(tmp_path, "one", text=text))


def test_a_missing_frontmatter_key_is_an_error(tmp_path: Path) -> None:
    text = _text().replace("depends: []\n", "")
    with pytest.raises(CapabilityError, match="missing frontmatter key 'depends'"):
        load(_write(tmp_path, "one", text=text))


def test_a_heading_that_is_not_a_label_and_a_door_is_an_error(tmp_path: Path) -> None:
    text = _text().replace("### 2 · signed", "### 2 signed")
    with pytest.raises(
        CapabilityError, match=r"row heading '### 2 signed' is not '### <label> · <door>'"
    ):
        load(_write(tmp_path, "one", text=text))


def test_an_unknown_door_is_an_error(tmp_path: Path) -> None:
    text = _text().replace("### 2 · signed", "### 2 · shouted")
    with pytest.raises(CapabilityError, match="row 2: door 'shouted' is not one of"):
        load(_write(tmp_path, "one", text=text))
    assert {"root say", "root list", "signed", "unsigned"} == DOORS


def test_a_duplicate_label_is_an_error(tmp_path: Path) -> None:
    text = _text().replace("### 3 · unsigned", "### 2 · unsigned")
    with pytest.raises(CapabilityError, match="row 2 appears twice"):
        load(_write(tmp_path, "one", text=text))


def test_a_root_list_row_carries_no_words_and_a_speaking_row_must(tmp_path: Path) -> None:
    text = _text().replace("### 4 · root list\n\nThe end.", "### 4 · root list\n\n```\nbye\n```")
    with pytest.raises(CapabilityError, match="row 4: a root list row carries no words"):
        load(_write(tmp_path, "one", text=text))
    text = _text().replace("### 2 · signed\n\n```\nWho am I?\n```", "### 2 · signed")
    with pytest.raises(CapabilityError, match="row 2: no words"):
        load(_write(tmp_path, "one", text=text))


def test_a_row_with_two_words_blocks_is_an_error(tmp_path: Path) -> None:
    """One block is the words; a second one leaves the driver to guess which."""
    text = _text().replace("Named.", "Named.\n\n```\nWho are you?\n```")
    with pytest.raises(CapabilityError, match="row 2: more than one words block"):
        load(_write(tmp_path, "one", text=text))


def test_a_words_block_that_is_never_closed_is_an_error(tmp_path: Path) -> None:
    text = _text().replace("Who am I?\n```\n\nNamed.", "Who am I?\n\nNamed.")
    with pytest.raises(CapabilityError, match="a words block is never closed"):
        load(_write(tmp_path, "one", text=text))


def test_a_file_with_no_rows_is_an_error(tmp_path: Path) -> None:
    text = _text().split("### 1 · root say")[0] + "## Repair\n\n- build: `b`\n- refused: `r`\n"
    with pytest.raises(CapabilityError, match="no rows"):
        load(_write(tmp_path, "one", text=text))


def test_an_unknown_template_is_an_error_at_load(tmp_path: Path) -> None:
    text = _text().replace("Hello {key}", "Hello {mood}")
    with pytest.raises(CapabilityError, match="row 1: unknown template 'mood'"):
        load(_write(tmp_path, "one", text=text))
    assert {"key", "url"} == TEMPLATES


def test_the_repair_section_is_required_and_has_two_phrases(tmp_path: Path) -> None:
    text = _text().split("## Repair")[0]
    with pytest.raises(CapabilityError, match="no '## Repair' section"):
        load(_write(tmp_path, "one", text=text))
    text = _text().replace("- refused: `check your inbox`\n", "")
    with pytest.raises(CapabilityError, match="Repair: missing 'refused'"):
        load(_write(tmp_path, "one", text=text))


def test_catalog_loads_every_file_and_order_puts_dependencies_first(tmp_path: Path) -> None:
    _write(tmp_path, "zero")
    _write(tmp_path, "one", depends="\n  - zero")
    _write(tmp_path, "two", depends="\n  - one")
    caps = catalog(tmp_path)
    assert sorted(caps) == ["one", "two", "zero"]
    assert [c.name for c in order(caps, "two")] == ["zero", "one", "two"]
    assert [c.name for c in order(caps, "zero")] == ["zero"]


def test_order_refuses_a_cycle_and_an_unknown_name(tmp_path: Path) -> None:
    _write(tmp_path, "a", depends="\n  - b")
    _write(tmp_path, "b", depends="\n  - a")
    caps = catalog(tmp_path)
    with pytest.raises(CapabilityError, match="cycle"):
        order(caps, "a")
    with pytest.raises(CapabilityError, match="no capability named 'c'"):
        order(caps, "c")
    _write(tmp_path, "d", depends="\n  - nope")
    with pytest.raises(CapabilityError, match="d depends on 'nope', which does not exist"):
        order(catalog(tmp_path), "d")


def test_hatch_is_the_first_capability() -> None:
    hatch = load(CAPABILITIES / "hatch.md")
    assert hatch.name == "hatch" and hatch.depends == ()
    assert hatch.postconditions == (
        "authentication",
        "root_unforgeable",
        "authorization",
        "page_title",
        "counter",
        "no_undeclared_capability",
        "nothing_by_hand",
    )
    assert [r.label for r in hatch.rows] == [
        "1",
        "2",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "9-count-1",
        "9-count-2",
        "10",
    ]
    assert hatch.row("2").door == "root say" and "{key}" in hatch.row("2").words
    assert hatch.row("5").door == "unsigned" and hatch.row("5").words == hatch.row("4").words
    assert hatch.row("9-count-1").words == "count" == hatch.row("9-count-2").words
    assert hatch.row("10").door == "root list" and hatch.row("10").words == ""
    assert hatch.repair == Repair(build="check your build", refused="check your inbox")
    assert catalog()["hatch"] == hatch


def test_the_words_the_tiers_speak_are_hatch_md() -> None:
    """One source (capabilities design §4): the tiers read `WORDS` off the loaded
    file, and the file is the only place the words are written."""
    from tests.support_embryo import HATCH, WORDS

    assert load(CAPABILITIES / "hatch.md") == HATCH
    assert WORDS["1"].startswith("Hello. I am the one who hatched you.")
    assert WORDS["4"] == "Who am I?" == WORDS["5"]
    # Row 2 is one paragraph the model reads in one go: the fenced block holds it
    # on one line, so converting the file did not wrap what root says.
    assert "\n" not in WORDS["2"] and WORDS["2"].endswith("My public key is {key}")
    # the pre-capabilities single script lived directly under embryo/; only the
    # priors do now, and every capability's words live under CAPABILITIES instead.
    assert {p.name for p in CAPABILITIES.parent.glob("*.md")} == {"README.md", "seed.md"}
