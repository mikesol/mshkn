"""Unauthenticated system endpoints: health, metrics, alerts."""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from mshkn.api.deps import get_runtime
from mshkn.api.schemas import AlertResponse, HealthResponse

if TYPE_CHECKING:
    from mshkn.config import Config
    from mshkn.runtime import Runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


def _firecracker_present(config: Config) -> str:
    if shutil.which("firecracker") is None:
        return "firecracker binary not on PATH"
    if not config.kernel_path.exists():
        return f"kernel not found at {config.kernel_path}"
    return "ok"


async def _database(rt: Runtime) -> str:
    cursor = await rt.db.execute("SELECT 1")
    await cursor.fetchone()
    return "ok"


async def _storage(rt: Runtime) -> str:
    await rt.host.blocks.usage()
    return "ok"


async def _proxy(rt: Runtime) -> str:
    return "ok" if await rt.host.proxy.healthy() else "proxy admin API not reachable"


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    rt = get_runtime(request)
    subsystems: dict[str, str] = {}
    for name, check in (("database", _database), ("storage", _storage), ("proxy", _proxy)):
        try:
            subsystems[name] = await check(rt)
        except Exception as exc:
            subsystems[name] = f"{type(exc).__name__}: {exc}"
    subsystems["firecracker"] = _firecracker_present(rt.config)
    ordered = {k: subsystems[k] for k in ("database", "firecracker", "storage", "proxy")}
    status = "ok" if all(v == "ok" for v in ordered.values()) else "degraded"
    if status != "ok":
        logger.warning("health degraded: %s", {k: v for k, v in ordered.items() if v != "ok"})
    return HealthResponse(status=status, subsystems=ordered)


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/alerts", response_model=list[AlertResponse])
async def alerts(request: Request) -> list[AlertResponse]:
    return [AlertResponse(**asdict(a)) for a in get_runtime(request).alerts]
