"""Capability files (capabilities design §4): a growth script the driver speaks.

A capability is `embryo/capabilities/<name>.md`: a frontmatter block (`name`,
`depends`, `postconditions`), prose, one `### <label> · <door>` section per row
whose fenced block holds the words root speaks and whose prose is the outcome,
and a `## Repair` section with the two phrases root says when a build fails or an
approval is refused. The words and outcomes live only here; the checks the
frontmatter names live in `membrane.postconditions`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

CAPABILITIES = Path(__file__).resolve().parents[1] / "capabilities"
DOORS: frozenset[str] = frozenset({"root say", "root list", "signed", "unsigned"})
TEMPLATES: frozenset[str] = frozenset({"key", "url"})
FRONTMATTER_KEYS = ("name", "depends", "postconditions")
SEPARATOR = " · "  # U+00B7, between a row's label and its door in the heading
FENCE = "```"
TEMPLATE_RE = re.compile(r"\{([a-z_]+)\}")
PHRASE_RE = re.compile(r"^- (build|refused): `([^`]+)`$")


class CapabilityError(ValueError):
    """A capability file that cannot be loaded, with the file and the row named."""


@dataclass(frozen=True)
class Row:
    label: str
    door: str
    words: str
    outcome: str


@dataclass(frozen=True)
class Repair:
    build: str
    refused: str


@dataclass(frozen=True)
class Capability:
    name: str
    depends: tuple[str, ...]
    postconditions: tuple[str, ...]
    rows: tuple[Row, ...]
    repair: Repair
    path: Path

    @property
    def words(self) -> dict[str, str]:
        return {row.label: row.words for row in self.rows}

    def row(self, label: str) -> Row:
        for row in self.rows:
            if row.label == label:
                return row
        raise KeyError(label)


def _frontmatter(lines: list[str], path: Path) -> tuple[dict[str, object], int]:
    """The block between the first two `---` lines: `key: value`, `key: [a, b]`,
    or `key:` followed by `  - item` lines. Returns the values and the index after."""
    if not lines or lines[0].strip() != "---":
        raise CapabilityError(f"{path.name}: no frontmatter")
    values: dict[str, object] = {}
    current: str | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            for key in FRONTMATTER_KEYS:
                if key not in values:
                    raise CapabilityError(f"{path.name}: missing frontmatter key '{key}'")
            return values, i + 1
        if line.startswith("  - ") and current is not None:
            items = values[current]
            assert isinstance(items, list)
            items.append(line[4:].strip())
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not key:
            raise CapabilityError(f"{path.name}: frontmatter line {i} is not `key: value`")
        if key not in FRONTMATTER_KEYS:
            raise CapabilityError(f"{path.name}: unknown frontmatter key '{key}'")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            values[key] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
            current = key
        elif value == "":
            values[key] = []
            current = key
        else:
            values[key] = value
            current = None
    raise CapabilityError(f"{path.name}: frontmatter never closed")


def _is_fence(line: str) -> bool:
    """A fenced block is delimited by a line that is exactly ``` (trailing
    whitespace aside); no info string, no `~~~`."""
    return line.rstrip() == FENCE


def _sections(lines: list[str], path: Path) -> list[tuple[str, list[str]]]:
    """The `### ` headings and the lines under each, in document order. Anything
    before the first heading is prose, and a row's section ends at the next `###`
    heading or at any `##` heading (`## Repair` is where they all end). Headings
    inside a fenced block are words, not headings."""
    sections: list[tuple[str, list[str]]] = []
    body: list[str] | None = None
    in_fence = False
    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith("### "):
            body = []
            sections.append((line[4:].strip(), body))
            continue
        elif not in_fence and line.startswith("## "):
            body = None
            continue
        if body is not None:
            body.append(line)
    if in_fence:
        raise CapabilityError(f"{path.name}: a words block is never closed")
    return sections


def _words(label: str, body: list[str], path: Path) -> tuple[str | None, str]:
    """A row's one fenced block, verbatim and without its trailing newline (None
    when the row has none), and its outcome: every other line, stripped."""
    blocks: list[list[str]] = []
    block: list[str] | None = None
    outcome: list[str] = []
    for line in body:
        if _is_fence(line):
            if block is None:
                block = []
                blocks.append(block)
            else:
                block = None
        elif block is None:
            outcome.append(line)
        else:
            block.append(line)
    if len(blocks) > 1:
        raise CapabilityError(f"{path.name}: row {label}: more than one words block")
    return ("\n".join(blocks[0]) if blocks else None), "\n".join(outcome).strip()


def _rows(lines: list[str], path: Path) -> tuple[Row, ...]:
    rows: list[Row] = []
    seen: set[str] = set()
    for heading, body in _sections(lines, path):
        label, sep, door = heading.partition(SEPARATOR)
        label, door = label.strip(), door.strip()
        if not sep or not label or not door:
            raise CapabilityError(
                f"{path.name}: row heading '### {heading}' is not '### <label> · <door>'"
            )
        if label in seen:
            raise CapabilityError(f"{path.name}: row {label} appears twice")
        seen.add(label)
        if door not in DOORS:
            raise CapabilityError(
                f"{path.name}: row {label}: door '{door}' is not one of {sorted(DOORS)}"
            )
        block, outcome = _words(label, body, path)
        if door == "root list":
            if block is not None:
                raise CapabilityError(f"{path.name}: row {label}: a root list row carries no words")
            words = ""
        elif not block:
            raise CapabilityError(f"{path.name}: row {label}: no words")
        else:
            words = block
        for name in TEMPLATE_RE.findall(words):
            if name not in TEMPLATES:
                raise CapabilityError(f"{path.name}: row {label}: unknown template '{name}'")
        rows.append(Row(label, door, words, outcome))
    if not rows:
        raise CapabilityError(f"{path.name}: no rows")
    return tuple(rows)


def _repair(lines: list[str], path: Path) -> Repair:
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "## Repair")
    except StopIteration:
        raise CapabilityError(f"{path.name}: no '## Repair' section") from None
    phrases: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        match = PHRASE_RE.match(line.strip())
        if match:
            phrases[match.group(1)] = match.group(2)
    for key in ("build", "refused"):
        if key not in phrases:
            raise CapabilityError(f"{path.name}: Repair: missing '{key}'")
    return Repair(build=phrases["build"], refused=phrases["refused"])


def load(path: Path) -> Capability:
    lines = path.read_text().splitlines()
    values, body = _frontmatter(lines, path)
    name = values["name"]
    if not isinstance(name, str) or not name:
        raise CapabilityError(f"{path.name}: name must be a string")
    if name != path.stem:
        raise CapabilityError(f"{path.name}: name '{name}' does not match {path.name}")
    depends, postconditions = values["depends"], values["postconditions"]
    if not isinstance(depends, list) or not isinstance(postconditions, list):
        raise CapabilityError(f"{path.name}: depends and postconditions must be lists")
    return Capability(
        name=name,
        depends=tuple(depends),
        postconditions=tuple(postconditions),
        rows=_rows(lines[body:], path),
        repair=_repair(lines[body:], path),
        path=path,
    )


def catalog(directory: Path = CAPABILITIES) -> dict[str, Capability]:
    return {cap.name: cap for cap in (load(p) for p in sorted(directory.glob("*.md")))}


def order(catalog: Mapping[str, Capability], name: str) -> list[Capability]:
    """Dependencies first, `name` last; every capability once."""
    if name not in catalog:
        raise CapabilityError(f"no capability named '{name}'")
    ordered: list[Capability] = []
    visiting: list[str] = []

    def visit(current: str) -> None:
        if current in visiting:
            cycle = " -> ".join([*visiting[visiting.index(current) :], current])
            raise CapabilityError(f"cycle: {cycle}")
        if any(c.name == current for c in ordered):
            return
        visiting.append(current)
        for dep in catalog[current].depends:
            if dep not in catalog:
                raise CapabilityError(f"{current} depends on '{dep}', which does not exist")
            visit(dep)
        visiting.pop()
        ordered.append(catalog[current])

    visit(name)
    return ordered
