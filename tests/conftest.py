"""Assign a tier marker to every test based on its directory.

tests/unit/  -> unit   (pure; no I/O beyond temp files)
tests/flow/  -> flow   (real ASGI app, fake host, temp SQLite)
tests/e2e/   -> e2e    (live server; deselected by default via pyproject addopts)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable

_TIERS = ("unit", "flow", "e2e")


def pytest_collection_modifyitems(config: pytest.Config, items: Iterable[pytest.Item]) -> None:
    tests_root = config.rootpath / "tests"
    for item in items:
        try:
            tier = item.path.relative_to(tests_root).parts[0]
        except ValueError:
            continue
        if tier in _TIERS:
            item.add_marker(getattr(pytest.mark, tier))
