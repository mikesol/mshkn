"""Background maintenance: dead VMs, idle VMs, checkpoint retention, host checks (spec §6.7)."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from mshkn.db import get_account_by_id, get_checkpoint, list_all_computers
from mshkn.models import Alert, CheckpointTrigger, ComputerStatus
from mshkn.observability.metrics import host_ram_used_ratio, thin_pool_used_ratio

if TYPE_CHECKING:
    from collections import deque
    from collections.abc import Callable

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import Host
    from mshkn.models import Computer
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.computers import ComputerService
    from mshkn.services.lifecycle import Lifecycle

logger = logging.getLogger(__name__)

IDLE_LABEL = "auto-idle-timeout"
_IDLE_CONCURRENCY = 5
_POOL_WARNING = 0.80
_POOL_CRITICAL = 0.95


class _DiskUsage(Protocol):
    @property
    def used(self) -> int: ...
    @property
    def total(self) -> int: ...


class Reaper:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        host: Host,
        computers: ComputerService,
        checkpoints: CheckpointService,
        lifecycle: Lifecycle,
        alerts: deque[Alert],
        *,
        disk_usage: Callable[[str], _DiskUsage] = shutil.disk_usage,
        meminfo_path: Path = Path("/proc/meminfo"),
    ) -> None:
        self.config = config
        self.db = db
        self.host = host
        self.computers = computers
        self.checkpoints = checkpoints
        self.lifecycle = lifecycle
        self.alerts = alerts
        self._disk_usage = disk_usage
        self._meminfo_path = meminfo_path

    async def run(self, interval: float = 60.0) -> None:
        logger.info(
            "Reaper started (interval=%.0fs, idle_timeout=%ds, retention=%d)",
            interval,
            self.config.idle_timeout_seconds,
            self.config.checkpoint_retention_count,
        )
        while True:
            await asyncio.sleep(interval)
            try:
                await self.cycle()
            except Exception:
                logger.exception("Reaper cycle failed")

    async def cycle(self) -> None:
        dead = await self.reap_dead()
        idle = await self.reap_idle()
        pruned = await self.checkpoints.prune()
        alerts = await self.check_host()
        await self.computers.refresh_active_gauge()
        if dead or idle or pruned or alerts:
            logger.info(
                "Reaper cycle: %d dead, %d idle VM(s), %d checkpoint(s) pruned, %d alert(s)",
                dead,
                idle,
                pruned,
                len(alerts),
            )

    async def reap_dead(self) -> int:
        reaped = 0
        for computer in await self._running():
            if computer.firecracker_pid is None or self.host.hypervisor.is_alive(
                computer.firecracker_pid
            ):
                continue
            logger.warning(
                "Reaping dead VM %s (PID %d gone)", computer.id, computer.firecracker_pid
            )
            try:
                await self.computers.cleanup_dead(computer)
                reaped += 1
            except Exception:
                logger.exception("Failed to reap VM %s", computer.id)
        return reaped

    async def reap_idle(self) -> int:
        timeout = self.config.idle_timeout_seconds
        if timeout <= 0:
            return 0
        now = datetime.now(UTC)
        idle: list[Computer] = []
        for computer in await self._running():
            ref = computer.last_exec_at or computer.created_at
            try:
                ref_time = datetime.fromisoformat(ref)
            except ValueError:
                continue
            if ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=UTC)
            if (now - ref_time).total_seconds() >= timeout:
                idle.append(computer)
        if not idle:
            return 0
        sem = asyncio.Semaphore(_IDLE_CONCURRENCY)

        async def one(computer: Computer) -> bool:
            async with sem:
                try:
                    await self._checkpoint_and_destroy(computer)
                    return True
                except Exception:
                    logger.exception("Failed to reap idle VM %s", computer.id)
                    return False

        results = await asyncio.gather(*(one(c) for c in idle))
        return sum(results)

    async def _checkpoint_and_destroy(self, computer: Computer) -> None:
        label: str | None = None
        if computer.source_checkpoint_id:
            source = await get_checkpoint(self.db, computer.source_checkpoint_id)
            if source is not None:
                label = source.label
        effective_label = label or IDLE_LABEL
        try:
            await self.checkpoints.create(
                computer, label=effective_label, trigger=CheckpointTrigger.IDLE
            )
        except Exception:
            logger.exception("Auto-checkpoint failed for VM %s, destroying anyway", computer.id)
        await self.computers.destroy(computer.id)
        logger.info("Destroyed idle VM %s", computer.id)
        account = await get_account_by_id(self.db, computer.account_id)
        if account is not None:
            self.lifecycle.spawn_drain(account, effective_label)

    async def check_host(self) -> list[Alert]:
        now = datetime.now(UTC).isoformat()
        found: list[Alert] = []
        try:
            disk = self._disk_usage("/")
            pct = disk.used / disk.total * 100
            if pct > 80:
                found.append(
                    Alert(
                        "critical" if pct > 95 else "warning",
                        "nvme",
                        f"NVMe usage at {pct:.1f}%",
                        round(pct, 1),
                        80.0,
                        now,
                    )
                )
        except Exception:
            logger.exception("Failed to check disk usage")
        try:
            meminfo: dict[str, int] = {}
            for line in self._meminfo_path.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
            total, available = meminfo.get("MemTotal", 0), meminfo.get("MemAvailable", 0)
            if total > 0:
                used_pct = (total - available) / total * 100
                host_ram_used_ratio.set(used_pct / 100.0)
                if used_pct > 90:
                    found.append(
                        Alert(
                            "critical",
                            "ram",
                            f"Host RAM usage at {used_pct:.1f}%",
                            round(used_pct, 1),
                            90.0,
                            now,
                        )
                    )
        except Exception:
            logger.exception("Failed to check RAM usage")
        try:
            usage = await self.host.blocks.usage()
            for kind, ratio in (
                ("data", usage.data_used_ratio),
                ("metadata", usage.metadata_used_ratio),
            ):
                thin_pool_used_ratio.labels(kind=kind).set(ratio)
                if ratio > _POOL_WARNING:
                    found.append(
                        Alert(
                            "critical" if ratio > _POOL_CRITICAL else "warning",
                            f"thin_pool_{kind}",
                            f"thin pool {kind} at {ratio * 100:.1f}%",
                            round(ratio, 3),
                            _POOL_WARNING,
                            now,
                        )
                    )
        except Exception:
            logger.exception("Failed to check thin pool usage")
        for alert in found:
            logger.warning("ALERT [%s]: %s", alert.level, alert.message)
            self.alerts.append(alert)
        return found

    async def _running(self) -> list[Computer]:
        return [c for c in await list_all_computers(self.db) if c.status == ComputerStatus.RUNNING]
