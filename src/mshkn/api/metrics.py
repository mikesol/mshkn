from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

router = APIRouter(tags=["metrics"])

computers_active = Gauge(
    "mshkn_computers_active",
    "Number of currently running VMs",
)
computers_created_total = Counter(
    "mshkn_computers_created_total",
    "Total number of computers created",
)
checkpoints_total = Counter(
    "mshkn_checkpoints_total",
    "Total number of checkpoints created",
)
exec_duration_seconds = Histogram(
    "mshkn_exec_duration_seconds",
    "Duration of exec commands in seconds",
)


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
