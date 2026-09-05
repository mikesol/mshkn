from __future__ import annotations

import pytest

from mshkn.errors import InvalidInput
from mshkn.resources import DEFAULT_RESOURCES, Resources


def test_defaults() -> None:
    assert Resources(mem_mib=256, vcpus=2) == DEFAULT_RESOURCES
    assert DEFAULT_RESOURCES.is_default
    assert Resources.from_needs(None) is DEFAULT_RESOURCES
    assert Resources.from_needs({}) is DEFAULT_RESOURCES


@pytest.mark.parametrize(
    ("needs", "expected"),
    [
        ({"ram": "8GB"}, Resources(mem_mib=8192, vcpus=2)),
        ({"ram": "512MB"}, Resources(mem_mib=512, vcpus=2)),
        ({"ram": " 1.5gb "}, Resources(mem_mib=1536, vcpus=2)),
        ({"cores": 4}, Resources(mem_mib=256, vcpus=4)),
        ({"cores": "3"}, Resources(mem_mib=256, vcpus=3)),
        ({"ram": "2GB", "cores": 8}, Resources(mem_mib=2048, vcpus=8)),
    ],
)
def test_from_needs_parses(needs: dict[str, object], expected: Resources) -> None:
    got = Resources.from_needs(needs)
    assert got == expected
    assert not got.is_default


@pytest.mark.parametrize(
    "needs",
    [
        {"ram": "8"},
        {"ram": "8TB"},
        {"ram": 8},
        {"ram": "lots"},
        {"ram": "64MB"},
        {"ram": "33GB"},
        {"cores": 0},
        {"cores": 17},
        {"cores": True},
        {"cores": "two"},
        {"cores": 2.5},
        {"gpu": 1},
    ],
)
def test_from_needs_rejects(needs: dict[str, object]) -> None:
    with pytest.raises(InvalidInput):
        Resources.from_needs(needs)


def test_error_names_the_field() -> None:
    with pytest.raises(InvalidInput, match="ram"):
        Resources.from_needs({"ram": "8TB"})
    with pytest.raises(InvalidInput, match="cores"):
        Resources.from_needs({"cores": 0})
    with pytest.raises(InvalidInput, match="gpu"):
        Resources.from_needs({"gpu": 1})
