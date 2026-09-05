"""Process-wide state: the Runtime object and the BackgroundTasks registry.

There are no module-level mutable globals in mshkn; everything that used to
be one lives here, is built once in the app lifespan (or by a test), and is
reached through api.deps.get_runtime().
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.proxy.caddy import CaddyClient
from mshkn.ratelimit import RateLimiter
from mshkn.vm.manager import VMManager
from mshkn.vm.ssh import SSHPool

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import aiosqlite

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_SECONDS = 30.0


class BackgroundTasks:
    """Owns background asyncio tasks: keeps strong references, logs failures,
    lets callers cancel or await a task by key, and drains on shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._keyed: dict[str, asyncio.Task[Any]] = {}

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
        key: str | None = None,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        if key is not None:
            self._keyed[key] = task
        task.add_done_callback(partial(self._on_done, key))
        return task

    def _on_done(self, key: str | None, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if key is not None and self._keyed.get(key) is task:
            del self._keyed[key]
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("background task %s failed: %s", task.get_name(), exc, exc_info=exc)

    async def cancel(self, key: str) -> None:
        """Cancel the task registered under key (if any) and wait for it to finish."""
        task = self._keyed.pop(key, None)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def wait(self, key: str) -> None:
        """Wait for the task registered under key (if any) to finish."""
        task = self._keyed.get(key)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def drain(self, timeout: float) -> None:
        """Wait up to timeout for outstanding tasks, then cancel whatever is left."""
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return
        _done, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    def __len__(self) -> int:
        return len(self._tasks)


@dataclass
class Runtime:
    config: Config
    db: aiosqlite.Connection
    vm_manager: VMManager
    caddy: CaddyClient | None
    ssh_pool: SSHPool | None
    tasks: BackgroundTasks
    rate_limiter: RateLimiter
    rule_limiters: dict[str, RateLimiter] = field(default_factory=dict)
    build_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    @classmethod
    async def from_env(cls) -> Runtime:
        config = Config.from_env()
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await connect(config.db_path)
        await run_migrations(db, config.migrations_dir)
        caddy = CaddyClient(admin_url=config.caddy_admin_url, domain=config.domain)
        ssh_pool = SSHPool(config.ssh_key_path)
        tasks = BackgroundTasks()
        vm_manager = VMManager(config, db, caddy=caddy, ssh_pool=ssh_pool, tasks=tasks)
        return cls(
            config=config,
            db=db,
            vm_manager=vm_manager,
            caddy=caddy,
            ssh_pool=ssh_pool,
            tasks=tasks,
            rate_limiter=RateLimiter(max_requests=80, window_seconds=10.0),
        )

    async def start(self) -> None:
        """Recover host state and start the reaper. Called from the app lifespan."""
        await self.vm_manager.initialize()
        reaped = await self.vm_manager.reap_dead_vms()
        if reaped:
            logger.info("Startup: reaped %d dead VM(s)", reaped)
        self.tasks.spawn(self.vm_manager.run_reaper_loop(), name="reaper", key="reaper")

    async def close(self) -> None:
        await self.tasks.cancel("reaper")
        await self.tasks.drain(_DRAIN_TIMEOUT_SECONDS)
        if self.ssh_pool is not None:
            await self.ssh_pool.close_all()
        if self.caddy is not None:
            await self.caddy.close()
        await self.db.close()

    def build_lock(self, account_id: str) -> asyncio.Lock:
        """Per-account lock serializing recipe builds."""
        lock = self.build_locks.get(account_id)
        if lock is None:
            lock = self.build_locks[account_id] = asyncio.Lock()
        return lock

    def rule_limiter(self, rule_id: str, rate_limit_rpm: int) -> RateLimiter:
        """Per-ingress-rule limiter, rebuilt when the rule's rpm changes."""
        limiter = self.rule_limiters.get(rule_id)
        if limiter is None or limiter.max_requests != rate_limit_rpm:
            limiter = RateLimiter(max_requests=rate_limit_rpm, window_seconds=60.0)
            self.rule_limiters[rule_id] = limiter
        return limiter
