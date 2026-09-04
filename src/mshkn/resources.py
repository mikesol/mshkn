"""VM resource requests: parsing and bounds for the API's `needs` field."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mshkn.errors import InvalidInput

if TYPE_CHECKING:
    from collections.abc import Mapping

MIN_MEM_MIB = 128
MAX_MEM_MIB = 32 * 1024
MIN_VCPUS = 1
MAX_VCPUS = 16
_KNOWN_KEYS = frozenset({"ram", "cores"})


@dataclass(frozen=True)
class Resources:
    mem_mib: int = 256
    vcpus: int = 2

    @property
    def is_default(self) -> bool:
        return self == DEFAULT_RESOURCES

    @classmethod
    def from_needs(cls, needs: Mapping[str, object] | None) -> Resources:
        """Parse the API's `needs` dict. Missing or empty means the defaults."""
        if not needs:
            return DEFAULT_RESOURCES
        unknown = sorted(set(needs) - _KNOWN_KEYS)
        if unknown:
            raise InvalidInput(f"unknown needs field(s): {', '.join(unknown)}")
        mem_mib = _parse_ram(needs["ram"]) if "ram" in needs else DEFAULT_RESOURCES.mem_mib
        vcpus = _parse_cores(needs["cores"]) if "cores" in needs else DEFAULT_RESOURCES.vcpus
        if not MIN_MEM_MIB <= mem_mib <= MAX_MEM_MIB:
            raise InvalidInput(f"ram must be between {MIN_MEM_MIB}MB and {MAX_MEM_MIB // 1024}GB")
        if not MIN_VCPUS <= vcpus <= MAX_VCPUS:
            raise InvalidInput(f"cores must be between {MIN_VCPUS} and {MAX_VCPUS}")
        return cls(mem_mib=mem_mib, vcpus=vcpus)


DEFAULT_RESOURCES = Resources()


def _parse_ram(value: object) -> int:
    if not isinstance(value, str):
        raise InvalidInput("ram must be a string like '512MB' or '8GB'")
    raw = value.strip().upper()
    if raw.endswith("GB"):
        number, scale = raw[:-2], 1024
    elif raw.endswith("MB"):
        number, scale = raw[:-2], 1
    else:
        raise InvalidInput("ram must end with MB or GB")
    try:
        amount = float(number)
    except ValueError:
        raise InvalidInput(f"ram value {value!r} is not a number") from None
    if amount <= 0:
        raise InvalidInput("ram must be positive")
    return int(amount * scale)


def _parse_cores(value: object) -> int:
    if isinstance(value, bool):
        raise InvalidInput("cores must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise InvalidInput("cores must be an integer")
