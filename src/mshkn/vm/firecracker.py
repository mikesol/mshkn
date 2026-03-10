from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from mshkn.shell import run

logger = logging.getLogger(__name__)

BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/init root=/dev/vda rw"


@dataclass(frozen=True)
class FirecrackerConfig:
    socket_path: str
    kernel_path: str
    rootfs_path: str
    tap_device: str
    guest_mac: str
    vcpu_count: int = 2
    mem_size_mib: int = 512
    boot_args: str = field(default=BOOT_ARGS)


class FirecrackerClient:
    """Async client for a single Firecracker instance via Unix socket API."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        self._client = httpx.AsyncClient(transport=transport, base_url="http://localhost")

    async def configure_and_boot(self, config: FirecrackerConfig) -> None:
        await self._put("/machine-config", {
            "vcpu_count": config.vcpu_count,
            "mem_size_mib": config.mem_size_mib,
        })
        await self._put("/boot-source", {
            "kernel_image_path": config.kernel_path,
            "boot_args": config.boot_args,
        })
        await self._put("/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": config.rootfs_path,
            "is_root_device": True,
            "is_read_only": False,
        })
        await self._put("/network-interfaces/eth0", {
            "iface_id": "eth0",
            "guest_mac": config.guest_mac,
            "host_dev_name": config.tap_device,
        })
        await self._put("/actions", {"action_type": "InstanceStart"})
        logger.info("Firecracker VM configured and started via %s", self.socket_path)

    async def pause(self) -> None:
        await self._patch("/vm", {"state": "Paused"})

    async def resume(self) -> None:
        await self._patch("/vm", {"state": "Resumed"})

    async def create_snapshot(self, snapshot_path: str, memory_path: str) -> None:
        await self._put("/snapshot/create", {
            "snapshot_type": "Full",
            "snapshot_path": snapshot_path,
            "mem_file_path": memory_path,
        })

    async def load_snapshot(
        self, snapshot_path: str, memory_path: str, resume_vm: bool = True
    ) -> None:
        await self._put("/snapshot/load", {
            "snapshot_path": snapshot_path,
            "mem_backend": {
                "backend_type": "File",
                "backend_path": memory_path,
            },
            "resume_vm": resume_vm,
        })

    async def close(self) -> None:
        await self._client.aclose()

    async def _put(self, path: str, body: dict[str, object]) -> None:
        resp = await self._client.put(path, json=body)
        if resp.status_code not in (200, 204):
            logger.error("Firecracker PUT %s failed: %s %s", path, resp.status_code, resp.text)
            resp.raise_for_status()

    async def _patch(self, path: str, body: dict[str, object]) -> None:
        resp = await self._client.patch(path, json=body)
        if resp.status_code not in (200, 204):
            logger.error("Firecracker PATCH %s failed: %s %s", path, resp.status_code, resp.text)
            resp.raise_for_status()


async def start_firecracker_process(socket_path: str) -> int:
    """Start a Firecracker process and return its PID."""
    # Remove stale socket
    await run(f"rm -f {socket_path}", check=False)

    proc = await asyncio.create_subprocess_exec(
        "firecracker", "--api-sock", socket_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.pid is not None
    # Poll for socket creation instead of fixed 500ms sleep
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        if Path(socket_path).exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise TimeoutError(f"Firecracker socket {socket_path} not created within 2s")
    logger.info("Started Firecracker process PID=%d socket=%s", proc.pid, socket_path)
    return proc.pid


async def kill_firecracker_process(pid: int) -> None:
    """Kill a Firecracker process by PID."""
    try:
        os.kill(pid, signal.SIGKILL)
        logger.info("Killed Firecracker PID=%d", pid)
    except ProcessLookupError:
        logger.warning("Firecracker PID=%d already dead", pid)
