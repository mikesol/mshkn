from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    capability_cache_dir: Path = field(default_factory=lambda: Path("/opt/mshkn/capability-cache"))
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

    # Networking
    domain: str = "mshkn.dev"
    caddy_admin_url: str = "http://localhost:2019"

    @classmethod
    def from_env(cls) -> Config:
        kwargs: dict[str, Any] = {}
        env_map: dict[str, str] = {
            "MSHKN_HOST": "host",
            "MSHKN_PORT": "port",
            "MSHKN_DB_PATH": "db_path",
            "R2_ENDPOINT": "r2_endpoint",
            "R2_ACCESS_KEY_ID": "r2_access_key_id",
            "R2_SECRET_ACCESS_KEY": "r2_secret_access_key",
            "R2_BUCKET": "r2_bucket",
            "MSHKN_DOMAIN": "domain",
        }
        for env_var, attr in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                if attr == "port":
                    kwargs[attr] = int(val)
                elif attr in ("db_path", "migrations_dir", "base_rootfs_path", "kernel_path"):
                    kwargs[attr] = Path(val)
                else:
                    kwargs[attr] = val
        return cls(**kwargs)
