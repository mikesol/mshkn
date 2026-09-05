from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, get_type_hints

from mshkn.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

# Variables that predate the generic MSHKN_<FIELD> rule. They win over the generic name.
_ALIASES: dict[str, str] = {
    "R2_ENDPOINT": "r2_endpoint",
    "R2_ACCESS_KEY_ID": "r2_access_key_id",
    "R2_SECRET_ACCESS_KEY": "r2_secret_access_key",
    "R2_BUCKET": "r2_bucket",
    "MSHKN_IDLE_TIMEOUT": "idle_timeout_seconds",
    "MSHKN_CHECKPOINT_RETENTION": "checkpoint_retention_count",
}
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class Config:
    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Paths
    db_path: Path = field(default_factory=lambda: Path("/opt/mshkn/mshkn.db"))
    migrations_dir: Path = field(default_factory=lambda: Path("migrations"))
    base_rootfs_path: Path = field(
        default_factory=lambda: Path("/opt/firecracker/rootfs.ext4"),
    )
    kernel_path: Path = field(default_factory=lambda: Path("/opt/firecracker/vmlinux.bin"))
    checkpoint_local_dir: Path = field(default_factory=lambda: Path("/opt/mshkn/checkpoints"))
    ssh_key_path: Path = field(default_factory=lambda: Path("/root/.ssh/id_ed25519"))

    # dm-thin
    thin_pool_data_path: Path = field(default_factory=lambda: Path("/opt/mshkn/thin-pool-data"))
    thin_pool_meta_path: Path = field(default_factory=lambda: Path("/opt/mshkn/thin-pool-meta"))
    thin_pool_data_size_gb: int = 100
    thin_pool_name: str = "mshkn-pool"
    thin_volume_sectors: int = 16777216  # 8GB

    # R2
    r2_bucket: str = "mshkn-checkpoints"
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""

    # Idle timeout and retention
    idle_timeout_seconds: int = 1800  # 30 minutes
    checkpoint_retention_count: int = 20  # per account, keep last N

    # Networking
    domain: str = "mshkn.dev"
    caddy_admin_url: str = "http://localhost:2019"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        """Build a Config from environment variables.

        Every field reads MSHKN_<FIELD_UPPER>; the aliases in _ALIASES are
        honored too and take precedence. Values are parsed by the field's
        annotated type; failures raise ConfigError naming the variable.
        """
        env = os.environ if environ is None else environ
        hints = get_type_hints(cls)
        kwargs: dict[str, object] = {}
        for f in fields(cls):
            var = f"MSHKN_{f.name.upper()}"
            raw = env.get(var)
            if raw is not None:
                kwargs[f.name] = _parse(var, raw, hints[f.name])
        for var, name in _ALIASES.items():
            raw = env.get(var)
            if raw is not None:
                kwargs[name] = _parse(var, raw, hints[name])
        return cls(**kwargs)  # type: ignore[arg-type]


def _parse(var: str, raw: str, kind: object) -> object:
    try:
        if kind is int:
            return int(raw)
        if kind is float:
            return float(raw)
        if kind is bool:
            lowered = raw.strip().lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError(f"expected a boolean, got {raw!r}")
        if kind is Path:
            if not raw:
                raise ValueError("empty path")
            return Path(raw)
        if kind is str:
            return raw
    except ValueError as exc:
        raise ConfigError(f"{var}: {exc}") from None
    raise ConfigError(f"{var}: unsupported field type {kind!r}")
