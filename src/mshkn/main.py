from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import aiosqlite
from fastapi import FastAPI, Request

from mshkn.api.checkpoints import router as checkpoints_router
from mshkn.api.computers import router as computers_router
from mshkn.api.errors import install_error_handlers
from mshkn.api.ingress import router as ingress_router
from mshkn.api.recipes import router as recipes_router
from mshkn.api.system import router as system_router
from mshkn.config import Config
from mshkn.db import run_migrations
from mshkn.observability.logging import configure_logging, request_id_var
from mshkn.proxy.caddy import CaddyClient
from mshkn.vm.manager import VMManager
from mshkn.vm.ssh import SSHPool

configure_logging()
logger = logging.getLogger(__name__)


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
app.include_router(ingress_router)
app.include_router(system_router)
app.include_router(recipes_router)
install_error_handlers(app)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Attach a request id to the response and to every log line during the request."""
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-Id"] = request_id
    return response
