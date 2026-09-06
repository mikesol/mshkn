"""Production Host wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import Host
from mshkn.host.caddy import CaddyProxy
from mshkn.host.dmthin import DmThinBlockStore
from mshkn.host.firecracker import FirecrackerHypervisor
from mshkn.host.r2 import RcloneObjectStore
from mshkn.host.ssh import SshGuest

if TYPE_CHECKING:
    from mshkn.config import Config


def firecracker_host(config: Config) -> Host:
    """Build the production Host. Constructs exactly one FirecrackerHypervisor:

    its staging-slot lock is an instance attribute, and the staging slot
    (254) is host-global, so a second instance would race it.
    """
    return Host(
        hypervisor=FirecrackerHypervisor(config),
        blocks=DmThinBlockStore(config.thin_pool_name, config.thin_volume_sectors),
        guest=SshGuest(config.ssh_key_path),
        objects=RcloneObjectStore(config.r2_bucket),
        proxy=CaddyProxy(config.caddy_admin_url, config.domain),
    )
