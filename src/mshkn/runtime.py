"""Process-wide state: the Runtime object and the BackgroundTasks registry.

There are no module-level mutable globals in mshkn; everything that used to
be one lives here, is built once in the app lifespan (or by a test), and is
reached through api.deps.get_runtime().
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx

from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.host.firecracker_host import firecracker_host
from mshkn.ratelimit import RateLimiter
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.ingress import IngressService
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.reaper import Reaper
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import aiosqlite

    from mshkn.host import Host
    from mshkn.models import Alert

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_SECONDS = 30.0
_ALERT_HISTORY_SIZE = 100


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
        """Wait up to timeout for outstanding tasks, then cancel whatever is left.

        Tasks spawned while draining are awaited too: a deferred drain that
        self-destructs spawns a callback and a further drain as it runs, and a
        single snapshot of the set would tear those down mid-flight.
        """
        deadline = time.monotonic() + timeout
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.wait(pending, timeout=remaining)
        for task in [t for t in self._tasks if not t.done()]:
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def __len__(self) -> int:
        return len(self._tasks)


@dataclass
class Runtime:
    """Everything the API needs, wired once and reached through api.deps.get_runtime()."""

    config: Config
    db: aiosqlite.Connection
    host: Host
    tasks: BackgroundTasks
    allocator: SlotAllocator
    rate_limiter: RateLimiter
    recipes: RecipeService
    computers: ComputerService
    checkpoints: CheckpointService
    lifecycle: Lifecycle
    ingress: IngressService
    reaper: Reaper
    alerts: deque[Alert]
    http: httpx.AsyncClient

    @classmethod
    def build(
        cls,
        config: Config,
        db: aiosqlite.Connection,
        host: Host,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> Runtime:
        """Wire the services once. Tests call this with a FakeHost."""
        tasks = BackgroundTasks()
        allocator = SlotAllocator()
        client = http if http is not None else httpx.AsyncClient()
        alerts: deque[Alert] = deque(maxlen=_ALERT_HISTORY_SIZE)
        recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
        computers = ComputerService(config, db, host, allocator, recipes)
        checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
        lifecycle = Lifecycle(db, computers, checkpoints, tasks, client)
        ingress = IngressService(db, computers, checkpoints, lifecycle, tasks)
        reaper = Reaper(config, db, host, computers, checkpoints, lifecycle, alerts)
        return cls(
            config=config,
            db=db,
            host=host,
            tasks=tasks,
            allocator=allocator,
            rate_limiter=RateLimiter(max_requests=80, window_seconds=10.0),
            recipes=recipes,
            computers=computers,
            checkpoints=checkpoints,
            lifecycle=lifecycle,
            ingress=ingress,
            reaper=reaper,
            alerts=alerts,
            http=client,
        )

    @classmethod
    async def from_env(cls) -> Runtime:
        config = Config.from_env()
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await connect(config.db_path)
        await run_migrations(db, config.migrations_dir)
        return cls.build(config, db, firecracker_host(config))

    async def start(self) -> None:
        """Recover host state and start the reaper. Called from the app lifespan."""
        await self.allocator.initialize(self.db, self.host.blocks)
        reaped = await self.reaper.reap_dead()
        if reaped:
            logger.info("Startup: reaped %d dead VM(s)", reaped)
        await self.computers.refresh_active_gauge()
        self.tasks.spawn(self.reaper.run(), name="reaper", key="reaper")

    async def close(self) -> None:
        await self.tasks.cancel("reaper")
        await self.tasks.drain(_DRAIN_TIMEOUT_SECONDS)
        await self.http.aclose()
        await self.host.guest.close()
        await self.host.proxy.close()
        await self.db.close()
