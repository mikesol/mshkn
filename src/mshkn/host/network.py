from __future__ import annotations

import asyncio
import logging

from mshkn.host.shell import RunFn, ShellError
from mshkn.host.shell import run as shell_run

logger = logging.getLogger(__name__)


def slot_to_ip(slot: int) -> tuple[str, str]:
    """Return (host_ip, vm_ip) for a given slot number."""
    return f"172.16.{slot}.1", f"172.16.{slot}.2"


def slot_to_mac(slot: int) -> str:
    """Return guest MAC address for a given slot. Encodes IP for fcnet-setup.sh."""
    return f"06:00:AC:10:{slot:02X}:02"


def slot_to_tap(slot: int) -> str:
    return f"tap{slot}"


async def tap_exists(tap: str, *, run: RunFn = shell_run) -> bool:
    return (await run(f"ip link show {tap} 2>/dev/null", check=False)).strip() != ""


async def create_tap(slot: int, *, run: RunFn = shell_run) -> None:
    tap = slot_to_tap(slot)
    host_ip, vm_ip = slot_to_ip(slot)
    # Remove stale tap if it exists from a previous run.
    # Retry the add in case a dying process still holds the device fd.
    await run(f"ip link del {tap}", check=False)
    for attempt in range(3):
        try:
            await run(f"ip tuntap add dev {tap} mode tap")
            break
        except ShellError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.5)
            await run(f"ip link del {tap}", check=False)
    # Configure address, bring up, pre-populate ARP, and add iptables rules.
    # Combined into one shell call to reduce subprocess overhead (~5ms each).
    vm_mac = slot_to_mac(slot)
    await run(
        f"ip addr add {host_ip}/30 dev {tap} && "
        f"ip link set {tap} up && "
        f"ip neigh replace {vm_ip} lladdr {vm_mac} dev {tap} nud permanent && "
        f"iptables -I FORWARD -i {tap} -s {vm_ip} "
        f"! -d 172.16.0.0/12 -j ACCEPT && "
        f"iptables -I FORWARD -i {tap} -s {vm_ip} -d 172.16.0.0/12 -j DROP"
    )
    logger.info("Created tap device %s at %s/30", tap, host_ip)


async def destroy_tap(slot: int, *, run: RunFn = shell_run) -> None:
    tap = slot_to_tap(slot)
    _, vm_ip = slot_to_ip(slot)
    # Remove iptables rules (best-effort)
    await run(
        f"iptables -D FORWARD -i {tap} -s {vm_ip} ! -d 172.16.0.0/12 -j ACCEPT",
        check=False,
    )
    await run(
        f"iptables -D FORWARD -i {tap} -s {vm_ip} -d 172.16.0.0/12 -j DROP",
        check=False,
    )
    if not await tap_exists(tap, run=run):
        logger.debug("Tap %s already gone", tap)
        return
    try:
        await run(f"ip link del {tap}")
    except ShellError as e:
        logger.warning("Failed to delete tap %s: %s", tap, e.stderr.strip())
    else:
        logger.info("Destroyed tap device %s", tap)
