"""Unauthenticated system endpoints: health, metrics, alerts."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from mshkn.api.deps import get_runtime
from mshkn.api.schemas import AlertResponse

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/alerts", response_model=list[AlertResponse])
async def alerts(request: Request) -> list[AlertResponse]:
    """Return recent resource alerts."""
    return [AlertResponse(**asdict(a)) for a in get_runtime(request).alerts]
