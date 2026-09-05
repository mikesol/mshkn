"""Unauthenticated system endpoints: health, metrics, alerts."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

if TYPE_CHECKING:
    from mshkn.vm.manager import VMManager

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/alerts")
async def alerts(request: Request) -> list[dict[str, object]]:
    """Return recent resource alerts."""
    vm_manager: VMManager = request.app.state.vm_manager
    return [asdict(a) for a in vm_manager.alerts]
