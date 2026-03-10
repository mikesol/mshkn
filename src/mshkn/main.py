from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import aiosqlite
from fastapi import FastAPI, Request

from mshkn.api.checkpoints import router as checkpoints_router
from mshkn.api.computers import router as computers_router
from mshkn.api.metrics import router as metrics_router
from mshkn.config import Config
from mshkn.db import run_migrations
from mshkn.logging import JSONFormatter
from mshkn.proxy.caddy import CaddyClient
from mshkn.vm.manager import VMManager
from mshkn.vm.ssh import SSHPool


def _configure_logging() -> None:
    """Set up structured JSON logging for the application."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.INFO)
    # Ensure uvicorn loggers also use our formatter
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False


_configure_logging()
logger = logging.getLogger(__name__)


async def get_db() -> aiosqlite.Connection:
    """Dependency placeholder -- overridden in tests, set in lifespan for prod."""
    raise RuntimeError("DB not initialized")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = Config.from_env()
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(config.db_path)
    await run_migrations(db, config.migrations_dir)
    app.state.db = db
    app.state.config = config
    caddy = CaddyClient(admin_url=config.caddy_admin_url, domain=config.domain)
    ssh_pool = SSHPool(config.ssh_key_path)
    app.state.ssh_pool = ssh_pool
    vm_manager = VMManager(config, db, caddy=caddy, ssh_pool=ssh_pool)
    await vm_manager.initialize()
    # Reap any VMs that died while orchestrator was down
    reaped = await vm_manager.reap_dead_vms()
    if reaped:
        logger.info("Startup: reaped %d dead VM(s)", reaped)
    app.state.vm_manager = vm_manager
    # Start background reaper
    reaper_task = asyncio.create_task(vm_manager.run_reaper_loop())
    yield
    reaper_task.cancel()
    await ssh_pool.close_all()
    await caddy.close()
    await db.close()


app = FastAPI(title="mshkn", version="0.1.0", lifespan=lifespan)
app.include_router(computers_router)
app.include_router(checkpoints_router)
app.include_router(metrics_router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Add X-Request-Id header to all responses."""
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/alerts")
async def get_alerts(request: Request) -> list[dict[str, object]]:
    """Return recent resource alerts."""
    vm_manager: VMManager = request.app.state.vm_manager
    return [asdict(a) for a in vm_manager.alerts]
