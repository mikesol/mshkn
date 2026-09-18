"""The docs name real things.

Every backticked path, dotted module name, `METHOD /route`, `mshkn_*` metric and
`MSHKN_*`/`R2_*` variable in the documents listed in DOCS must exist in the
code, and the architecture doc must name every route and metric.
A doc that drifts from the code fails here instead of misleading a reader.

The capability index (`docs/embryo/README.md`) gets two more: its table must be
the one `capability index` derives from the promotions on disk, and its sentence
about per-module checks must name what those modules register. Both are claims a
reader acts on rather than decoration -- the Promoted column is how a fresh
session decides what can be built next, and it was wrong in two of three rows
(#204).
"""

from __future__ import annotations

import importlib
import re
from dataclasses import fields
from pathlib import Path

import pytest
from membrane.capabilities import catalog, load_module
from membrane.capability import DEFAULT_OUT, INDEX_HEADER, index_table
from membrane.postconditions import CHECKS
from prometheus_client import REGISTRY

from mshkn.app import create_app
from mshkn.config import _ALIASES, Config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# Extended by later tasks as each document lands.
DOCS: tuple[str, ...] = (
    "README.md",
    "CLAUDE.md",
    "DEPLOY.md",
    "docs/infrastructure.md",
    "docs/ARCHITECTURE.md",
    "docs/plans/README.md",
    "embryo/README.md",
    "docs/embryo/README.md",
    "docs/embryo/hatch/README.md",
    "docs/embryo/security/README.md",
    "docs/embryo/web-search/README.md",
)

# Words a document must not contain because the thing they name is gone.
BANNED: dict[str, tuple[str, ...]] = {
    "README.md": ("Nix", "VMManager", "poetry", "xfail", "Telegram"),
    "CLAUDE.md": ("Telegram", "capability_cache", "Nix", "Priority 1 (Bug Fixes)"),
    "DEPLOY.md": ("Nix", "poetry", "nix-env"),
}

# Variables that scripts read, not Config; they are allowed in the docs.
SCRIPT_VARS = frozenset({"MSHKN_SERVER", "MSHKN_API_URL", "MSHKN_API_KEY"})

# Routes FastAPI adds on its own; the architecture doc does not list them.
FRAMEWORK_ROUTES = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

PATH_RE = re.compile(r"`((?:src|tests|scripts|docs|migrations|systemd|embryo)/[A-Za-z0-9_./{}-]+)`")
MODULE_RE = re.compile(r"`(mshkn(?!\.dev`)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")  # not the domain
ROUTE_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/[A-Za-z0-9_{}/]*)`")
METRIC_RE = re.compile(r"`(mshkn_[a-z_]+)(?:\{[^}]*\})?`")
ENV_RE = re.compile(r"`((?:MSHKN|R2)_[A-Z0-9_]+)(?:=[^`]*)?`")

INDEX = "docs/embryo/README.md"
# "security's is `secret_page`, and web-search's are `searched` and `read_page`"
OWN_CHECKS_RE = re.compile(r"([a-z][a-z-]*)'s (?:is|are) ((?:`[a-z_]+`(?:,? and )?)+)")


def _text(doc: str) -> str:
    return (ROOT / doc).read_text()


def _app_routes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in create_app().routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if not methods or path in FRAMEWORK_ROUTES:
            continue
        found.update((m, path) for m in methods if m not in ("HEAD", "OPTIONS"))
    return found


def _registry_names() -> tuple[set[str], set[str]]:
    """(every name a doc may use, the family names a complete doc must mention)."""
    allowed: set[str] = set()
    families: set[str] = set()
    for family in REGISTRY.collect():
        if not family.name.startswith("mshkn_"):
            continue
        families.add(family.name)
        allowed.update(
            {family.name, f"{family.name}_total", f"{family.name}_count", f"{family.name}_sum"}
        )
    return allowed, families


def _resolves(name: str) -> bool:
    """`mshkn.runtime.Runtime.build` resolves as module `mshkn.runtime`, then attributes."""
    parts = name.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj: object = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for attr in parts[cut:]:
            if not hasattr(obj, attr):
                return False
            obj = getattr(obj, attr)
        return True
    return False


def _env_names() -> set[str]:
    return {f"MSHKN_{f.name.upper()}" for f in fields(Config)} | set(_ALIASES) | SCRIPT_VARS


@pytest.mark.parametrize("doc", DOCS)
def test_every_path_exists(doc: str) -> None:
    missing = sorted(
        {p for p in PATH_RE.findall(_text(doc)) if not (ROOT / p.rstrip("/")).exists()}
    )
    assert missing == [], f"{doc} names paths that do not exist: {missing}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_module_imports(doc: str) -> None:
    broken = sorted(n for n in set(MODULE_RE.findall(_text(doc))) if not _resolves(n))
    assert broken == [], f"{doc} names modules or attributes that do not exist: {broken}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_route_exists(doc: str) -> None:
    unknown = sorted(set(ROUTE_RE.findall(_text(doc))) - _app_routes())
    assert unknown == [], f"{doc} names routes the app does not serve: {unknown}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_metric_exists(doc: str) -> None:
    allowed, _ = _registry_names()
    unknown = sorted(set(METRIC_RE.findall(_text(doc))) - allowed)
    assert unknown == [], f"{doc} names metrics the registry does not have: {unknown}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_env_var_is_config(doc: str) -> None:
    unknown = sorted(set(ENV_RE.findall(_text(doc))) - _env_names())
    assert unknown == [], f"{doc} names variables Config does not read: {unknown}"


@pytest.mark.parametrize("doc", sorted(BANNED))
def test_retired_terms_are_absent(doc: str) -> None:
    present = [term for term in BANNED[doc] if term in _text(doc)]
    assert present == [], f"{doc} still mentions retired things: {present}"


def test_the_capability_index_table_is_the_derived_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """`capability index` is run from the repository root, so the test is too: the
    directory cells and the promotion lookups are both relative to `--out`."""
    monkeypatch.chdir(ROOT)
    lines = _text(INDEX).splitlines()
    start = lines.index(INDEX_HEADER)
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    assert lines[start:end] == index_table(DEFAULT_OUT), (
        f"{INDEX}'s table has drifted from the PROMOTED.md files on disk; "
        "run `uv run capability index`"
    )


def test_the_capability_index_names_each_module_own_checks() -> None:
    """A check is attributed to the module that defined it, not to import order:
    `CHECKS` is a module-level dict every capability mutates, so the registry
    carries another capability's entries by the time this reads it."""
    for capability in catalog().values():
        load_module(capability)
    claimed = {
        name: set(re.findall(r"`([a-z_]+)`", names))
        for name, names in OWN_CHECKS_RE.findall(_text(INDEX))
    }
    registered = {
        name: own
        for name in catalog()
        if (own := {c for c, fn in CHECKS.items() if fn.__module__ == f"capabilities.{name}"})
    }
    assert claimed == registered, f"{INDEX} misstates which checks a capability's module registers"


def test_architecture_lists_every_route_and_metric() -> None:
    text = _text("docs/ARCHITECTURE.md")
    documented = set(ROUTE_RE.findall(text))
    undocumented = sorted(_app_routes() - documented)
    assert undocumented == [], (
        f"the architecture doc must name every route and metric: missing routes {undocumented}"
    )
    _, families = _registry_names()
    named = set(METRIC_RE.findall(text))
    missing = sorted(f for f in families if f not in named and f"{f}_total" not in named)
    assert missing == [], (
        f"the architecture doc must name every route and metric: missing metrics {missing}"
    )
