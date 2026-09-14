"""Capability files (capabilities design §4): a growth script the driver speaks.

A capability is `embryo/capabilities/<name>.md`: a frontmatter block (`name`,
`depends`, `postconditions`), prose, one `### <label> · <door>` section per row
whose fenced block holds the words root speaks and whose prose is the outcome,
and a `## Repair` section with the phrases root says when a build fails or an
approval is refused. The words and outcomes live only here; the checks the
frontmatter names live in `membrane.postconditions`.

A capability may pair with `embryo/capabilities/<name>.py` beside its markdown:
its own apparatus, not the driver's. The module may define `prepare(doors, log)`,
an async context manager over the extra context its rows template, and may
register the checks only it needs by assigning into `postconditions.CHECKS` at
import.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractAsyncContextManager
    from types import ModuleType
    from typing import TextIO

CAPABILITIES = Path(__file__).resolve().parents[1] / "capabilities"
DOORS: frozenset[str] = frozenset({"root say", "root list", "signed", "unsigned"})
TEMPLATES: frozenset[str] = frozenset({"key", "url", "token"})
FRONTMATTER_KEYS = ("name", "depends", "postconditions")
SEPARATOR = " · "  # U+00B7, between a row's label and its door in the heading
FENCE = "```"
TEMPLATE_RE = re.compile(r"\{([a-z_]+)\}")
PHRASE_RE = re.compile(r"^- (build|refused|provide): `([^`]+)`$")


class CapabilityError(ValueError):
    """A capability file that cannot be loaded, with the file and the row named."""


class Prepare(Protocol):
    """A capability module's `prepare`: an async context manager (write it with
    `@contextlib.asynccontextmanager`) that starts what the run needs, yields the
    context its rows template, and takes it away on exit. `doors` is the driver's
    `Doors`, which the module reaches the account through."""

    def __call__(
        self, doors: Any, log: TextIO
    ) -> AbstractAsyncContextManager[Mapping[str, str]]: ...


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
    # What root says when a verb still needs something and the reply named no
    # path for it (spec §7.2); None for a capability that provides nothing.
    provide: str | None = None


@dataclass(frozen=True)
class Capability:
    name: str
    depends: tuple[str, ...]
    postconditions: tuple[str, ...]
    rows: tuple[Row, ...]
    repair: Repair
    path: Path
    module: Path | None  # `<name>.py` beside the markdown, when the capability has one

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


def _sections(lines: list[str], path: Path) -> list[tuple[str, str, list[str]]]:
    """Every `##` or `### ` heading and the lines under it, in document order, as
    (marker, heading, body): the rows are the `###` ones and Repair is a `##` one,
    both found in this one pass. Anything before the first heading is prose, and a
    heading inside a fenced block is a line root speaks, not a heading."""
    sections: list[tuple[str, str, list[str]]] = []
    body: list[str] | None = None
    in_fence = False
    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
        elif not in_fence and (line.startswith("### ") or line.startswith("## ")):
            marker, _, heading = line.partition(" ")
            body = []
            sections.append((marker, heading.strip(), body))
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


def _rows(sections: list[tuple[str, str, list[str]]], path: Path) -> tuple[Row, ...]:
    rows: list[Row] = []
    seen: set[str] = set()
    for marker, heading, body in sections:
        if marker != "###":
            continue  # `## Repair`, or any other section of prose
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


def _repair(sections: list[tuple[str, str, list[str]]], path: Path) -> Repair:
    body = next(
        (b for marker, heading, b in sections if marker == "##" and heading == "Repair"), None
    )
    if body is None:
        raise CapabilityError(f"{path.name}: no '## Repair' section")
    phrases: dict[str, str] = {}
    for line in body:
        match = PHRASE_RE.match(line.strip())
        if match:
            phrases[match.group(1)] = match.group(2)
    for key in ("build", "refused"):
        if key not in phrases:
            raise CapabilityError(f"{path.name}: Repair: missing '{key}'")
    return Repair(
        build=phrases["build"], refused=phrases["refused"], provide=phrases.get("provide")
    )


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
    module = path.with_suffix(".py")
    sections = _sections(lines[body:], path)
    return Capability(
        name=name,
        depends=tuple(depends),
        postconditions=tuple(postconditions),
        rows=_rows(sections, path),
        repair=_repair(sections, path),
        path=path,
        module=module if module.exists() else None,
    )


def load_module(capability: Capability) -> ModuleType | None:
    """The capability's Python module, imported from the path beside its markdown,
    or None when it has none. It is not put in `sys.modules`: it is one
    capability's apparatus, named after a file, and nothing imports it by name."""
    if capability.module is None:
        return None
    spec = importlib.util.spec_from_file_location(
        f"capabilities.{capability.name}", capability.module
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
