"""Capability files (capabilities design §4): frontmatter, a turn table and a
Repair section, loaded into a frozen document the driver speaks from."""

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

| Label | Door | Words | Outcome |
|---|---|---|---|
| 1 | root say | Hello {{key}} | A reply. |
| 2 | signed | Who am I? | Named. |
| 3 | unsigned | Who am I? | Anonymous. |
| 4 | root list | | The end. |

## Repair

- build: `check your build`
- refused: `check your inbox`
"""


def _write(tmp_path: Path, name: str, depends: str = "[]", text: str | None = None) -> Path:
    path = tmp_path / f"{name}.md"
    path.write_text(text or MINIMAL.format(name=name, depends=depends))
    return path


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


def test_depends_accepts_a_bullet_list(tmp_path: Path) -> None:
    cap = load(_write(tmp_path, "two", depends="\n  - one\n  - zero"))
    assert cap.depends == ("one", "zero")


def test_the_name_must_match_the_file(tmp_path: Path) -> None:
    path = tmp_path / "other.md"
    path.write_text(MINIMAL.format(name="one", depends="[]"))
    with pytest.raises(CapabilityError, match=r"name 'one' does not match other\.md"):
        load(path)


def test_an_unknown_frontmatter_key_is_an_error(tmp_path: Path) -> None:
    text = MINIMAL.format(name="one", depends="[]").replace("name: one", "name: one\nowner: me")
    with pytest.raises(CapabilityError, match="unknown frontmatter key 'owner'"):
        load(_write(tmp_path, "one", text=text))


def test_a_missing_frontmatter_key_is_an_error(tmp_path: Path) -> None:
    text = MINIMAL.format(name="one", depends="[]").replace("depends: []\n", "")
    with pytest.raises(CapabilityError, match="missing frontmatter key 'depends'"):
        load(_write(tmp_path, "one", text=text))


def test_an_unknown_door_is_an_error(tmp_path: Path) -> None:
    text = MINIMAL.format(name="one", depends="[]").replace("| 2 | signed |", "| 2 | shouted |")
    with pytest.raises(CapabilityError, match="row 2: door 'shouted' is not one of"):
        load(_write(tmp_path, "one", text=text))
    assert {"root say", "root list", "signed", "unsigned"} == DOORS


def test_a_duplicate_label_is_an_error(tmp_path: Path) -> None:
    text = MINIMAL.format(name="one", depends="[]").replace("| 3 | unsigned |", "| 2 | unsigned |")
    with pytest.raises(CapabilityError, match="row 2 appears twice"):
        load(_write(tmp_path, "one", text=text))


def test_a_root_list_row_carries_no_words_and_a_speaking_row_must(tmp_path: Path) -> None:
    text = MINIMAL.format(name="one", depends="[]").replace(
        "| 4 | root list | |", "| 4 | root list | bye |"
    )
    with pytest.raises(CapabilityError, match="row 4: a root list row carries no words"):
        load(_write(tmp_path, "one", text=text))
    text = MINIMAL.format(name="one", depends="[]").replace(
        "| 2 | signed | Who am I? |", "| 2 | signed | |"
    )
    with pytest.raises(CapabilityError, match="row 2: no words"):
        load(_write(tmp_path, "one", text=text))


def test_an_unknown_template_is_an_error_at_load(tmp_path: Path) -> None:
    text = MINIMAL.format(name="one", depends="[]").replace("Hello {key}", "Hello {token}")
    with pytest.raises(CapabilityError, match="row 1: unknown template 'token'"):
        load(_write(tmp_path, "one", text=text))
    assert {"key", "url"} == TEMPLATES


def test_a_row_with_the_wrong_number_of_cells_is_an_error(tmp_path: Path) -> None:
    text = MINIMAL.format(name="one", depends="[]").replace(
        "| 2 | signed | Who am I? | Named. |", "| 2 | signed | Who | am I? | Named. |"
    )
    with pytest.raises(CapabilityError, match="row 2: expected 4 cells, found 5"):
        load(_write(tmp_path, "one", text=text))


def test_the_repair_section_is_required_and_has_two_phrases(tmp_path: Path) -> None:
    text = MINIMAL.format(name="one", depends="[]").split("## Repair")[0]
    with pytest.raises(CapabilityError, match="no '## Repair' section"):
        load(_write(tmp_path, "one", text=text))
    text = MINIMAL.format(name="one", depends="[]").replace("- refused: `check your inbox`\n", "")
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
    assert hatch.row("10").door == "root list"
    assert hatch.repair == Repair(build="check your build", refused="check your inbox")
    assert catalog()["hatch"] == hatch
