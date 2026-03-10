from __future__ import annotations

import logging

from mshkn.shell import ShellError, run

logger = logging.getLogger(__name__)


def slot_to_ip(slot: int) -> tuple[str, str]:
    """Return (host_ip, vm_ip) for a given slot number."""
    return f"172.16.{slot}.1", f"172.16.{slot}.2"


def slot_to_mac(slot: int) -> str:
    """Return guest MAC address for a given slot. Encodes IP for fcnet-setup.sh."""
    return f"06:00:AC:10:{slot:02X}:02"


def slot_to_tap(slot: int) -> str:
    return f"tap{slot}"


async def create_tap(slot: int) -> None:
    tap = slot_to_tap(slot)
    host_ip, vm_ip = slot_to_ip(slot)
    await run(f"ip tuntap add dev {tap} mode tap")
    await run(f"ip addr add {host_ip}/30 dev {tap}")
    await run(f"ip link set {tap} up")
    # Allow VM → internet (non-172.16.0.0/12 destinations) but block VM → VM
    await run(
        f"iptables -I FORWARD -i {tap} -s {vm_ip} "
        f"! -d 172.16.0.0/12 -j ACCEPT"
    )
    await run(f"iptables -I FORWARD -i {tap} -s {vm_ip} -d 172.16.0.0/12 -j DROP")
    logger.info("Created tap device %s at %s/30", tap, host_ip)


async def destroy_tap(slot: int) -> None:
    tap = slot_to_tap(slot)
    _, vm_ip = slot_to_ip(slot)
    # Remove iptables rules (best-effort)
    await run(
        f"iptables -D FORWARD -i {tap} -s {vm_ip} "
        f"! -d 172.16.0.0/12 -j ACCEPT",
        check=False,
    )
    await run(
        f"iptables -D FORWARD -i {tap} -s {vm_ip} -d 172.16.0.0/12 -j DROP",
        check=False,
    )
    try:
        await run(f"ip link del {tap}")
    except ShellError as e:
        logger.warning("Failed to delete tap %s: %s", tap, e.stderr.strip())
    else:
        logger.info("Destroyed tap device %s", tap)


async def ensure_nat(interface: str = "enp35s0") -> None:
    result = await run(
        f"iptables -t nat -C POSTROUTING -o {interface} -j MASQUERADE",
        check=False,
    )
    if "No chain" in result or result == "":
        pass
    await run(
        f"iptables -t nat -A POSTROUTING -o {interface} -j MASQUERADE",
        check=False,
    )
