from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import aiosqlite
from fastapi import FastAPI

from mshkn.api.checkpoints import router as checkpoints_router
from mshkn.api.computers import router as computers_router
from mshkn.config import Config
from mshkn.db import run_migrations
from mshkn.vm.manager import VMManager


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
    vm_manager = VMManager(config, db)
    await vm_manager.initialize()
    app.state.vm_manager = vm_manager
    yield
    await db.close()


app = FastAPI(title="mshkn", version="0.1.0", lifespan=lifespan)
app.include_router(computers_router)
app.include_router(checkpoints_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
