from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mshkn.db import (
    get_computer,
    insert_computer,
    list_computers_by_account,
    update_computer_status,
)
from mshkn.models import Checkpoint, Computer, Manifest
from mshkn.vm.firecracker import (
    FirecrackerClient,
    FirecrackerConfig,
    kill_firecracker_process,
    start_firecracker_process,
)
from mshkn.vm.network import create_tap, destroy_tap, slot_to_ip, slot_to_mac, slot_to_tap
from mshkn.vm.storage import create_snapshot, remove_volume

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config

logger = logging.getLogger(__name__)


class VMManager:
    def __init__(self, config: Config, db: aiosqlite.Connection) -> None:
        self.config = config
        self.db = db
        self._next_slot = 1  # slot 0 reserved; will be loaded from DB on startup
        self._next_volume_id = 100  # volume 0 is base; start high to avoid conflicts

    async def initialize(self) -> None:
        """Load state from DB to set counters correctly."""
        computers = await list_computers_by_account(self.db, "%")  # TODO: list all
        if computers:
            max_vol = max(c.thin_volume_id for c in computers)
            self._next_volume_id = max_vol + 1
            slots = [int(c.tap_device.replace("tap", "")) for c in computers]
            self._next_slot = max(slots) + 1

    def _allocate_slot(self) -> int:
        slot = self._next_slot
        self._next_slot += 1
        return slot

    def _allocate_volume_id(self) -> int:
        vol_id = self._next_volume_id
        self._next_volume_id += 1
        return vol_id

    async def create(self, account_id: str, manifest: Manifest) -> Computer:
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
        slot = self._allocate_slot()
        volume_id = self._allocate_volume_id()
        _host_ip, vm_ip = slot_to_ip(slot)
        mac = slot_to_mac(slot)
        tap = slot_to_tap(slot)
        socket_path = f"/tmp/fc-{computer_id}.socket"
        volume_name = f"mshkn-{computer_id}"

        # 1. Create tap device
        await create_tap(slot)

        # 2. Create dm-thin snapshot from base
        await create_snapshot(
            pool_name=self.config.thin_pool_name,
            source_volume_id=0,  # base volume
            new_volume_id=volume_id,
            new_volume_name=volume_name,
            sectors=self.config.thin_volume_sectors,
        )

        # 3. Start Firecracker
        pid = await start_firecracker_process(socket_path)

        # 4. Configure and boot
        fc_client = FirecrackerClient(socket_path)
        try:
            await fc_client.configure_and_boot(
                FirecrackerConfig(
                    socket_path=socket_path,
                    kernel_path=str(self.config.kernel_path),
                    rootfs_path=f"/dev/mapper/{volume_name}",
                    tap_device=tap,
                    guest_mac=mac,
                    vcpu_count=2,
                    mem_size_mib=512,
                )
            )
        finally:
            await fc_client.close()

        # 5. Wait for SSH readiness
        await self._wait_for_ssh(vm_ip)

        # 6. Record in DB
        now = datetime.now(UTC).isoformat()
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=tap,
            vm_ip=vm_ip,
            socket_path=socket_path,
            firecracker_pid=pid,
            manifest_hash=manifest.content_hash(),
            status="running",
            created_at=now,
            last_exec_at=None,
        )
        await insert_computer(self.db, computer)
        logger.info("Created computer %s (slot=%d, ip=%s)", computer_id, slot, vm_ip)
        return computer

    async def fork_from_checkpoint(self, account_id: str, checkpoint: Checkpoint) -> Computer:
        """Fork a new computer from a checkpoint.

        Creates a dm-thin CoW snapshot of the checkpoint's disk (O(1)) and cold-boots
        a new VM from it. Cold boot is used because snapshot restore requires the same
        network config (IP) as the original — which conflicts with running the original
        VM concurrently. The disk snapshot preserves all filesystem state from the
        checkpoint.

        TODO: Implement snapshot restore for cases where the original VM is destroyed,
        or solve the networking reconfiguration problem for true instant resume.
        """
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
        slot = self._allocate_slot()
        volume_id = self._allocate_volume_id()
        _host_ip, vm_ip = slot_to_ip(slot)
        mac = slot_to_mac(slot)
        tap = slot_to_tap(slot)
        socket_path = f"/tmp/fc-{computer_id}.socket"
        volume_name = f"mshkn-{computer_id}"

        # Find the source computer's volume to snapshot from
        source_computer = await get_computer(self.db, checkpoint.computer_id or "")
        if source_computer is None:
            msg = f"Source computer {checkpoint.computer_id} not found for checkpoint"
            raise ValueError(msg)

        # 1. Create tap device
        await create_tap(slot)

        # 2. Create dm-thin snapshot from the checkpoint's source volume (O(1) CoW)
        await create_snapshot(
            pool_name=self.config.thin_pool_name,
            source_volume_id=source_computer.thin_volume_id,
            new_volume_id=volume_id,
            new_volume_name=volume_name,
            sectors=self.config.thin_volume_sectors,
        )

        # 3. Start Firecracker and cold-boot from the snapshot disk
        pid = await start_firecracker_process(socket_path)
        fc_client = FirecrackerClient(socket_path)
        try:
            await fc_client.configure_and_boot(
                FirecrackerConfig(
                    socket_path=socket_path,
                    kernel_path=str(self.config.kernel_path),
                    rootfs_path=f"/dev/mapper/{volume_name}",
                    tap_device=tap,
                    guest_mac=mac,
                    vcpu_count=2,
                    mem_size_mib=512,
                )
            )
        finally:
            await fc_client.close()

        # 4. Wait for SSH readiness
        await self._wait_for_ssh(vm_ip)

        # 5. Record in DB
        now = datetime.now(UTC).isoformat()
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=tap,
            vm_ip=vm_ip,
            socket_path=socket_path,
            firecracker_pid=pid,
            manifest_hash=checkpoint.manifest_hash,
            status="running",
            created_at=now,
            last_exec_at=None,
        )
        await insert_computer(self.db, computer)
        logger.info(
            "Forked computer %s from checkpoint %s (slot=%d, ip=%s)",
            computer_id, checkpoint.id, slot, vm_ip,
        )
        return computer

    async def destroy(self, computer_id: str) -> None:
        computer = await get_computer(self.db, computer_id)
        if computer is None:
            raise ValueError(f"Computer {computer_id} not found")

        # Kill Firecracker
        if computer.firecracker_pid is not None:
            await kill_firecracker_process(computer.firecracker_pid)

        # Remove dm-thin volume
        volume_name = f"mshkn-{computer_id}"
        await remove_volume(
            self.config.thin_pool_name, volume_name, computer.thin_volume_id,
        )

        # Remove tap device
        slot = int(computer.tap_device.replace("tap", ""))
        await destroy_tap(slot)

        # Update DB
        await update_computer_status(self.db, computer_id, "destroyed")
        logger.info("Destroyed computer %s", computer_id)

    async def _wait_for_ssh(self, vm_ip: str, timeout: float = 30.0) -> None:
        """Poll SSH until the VM is reachable."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=2",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-i",
                    str(self.config.ssh_key_path),
                    f"root@{vm_ip}",
                    "true",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await proc.wait()
                if rc == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        raise TimeoutError(f"VM at {vm_ip} did not become reachable via SSH")
