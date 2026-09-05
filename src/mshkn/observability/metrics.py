"""Prometheus metrics and the timed() helper that feeds them."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram

from mshkn.errors import HostError, MshknError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

computers_active = Gauge("mshkn_computers_active", "Number of currently running VMs")
computers_created_total = Counter(
    "mshkn_computers_created_total", "Total number of computers created"
)
checkpoints_total = Counter("mshkn_checkpoints_total", "Total number of checkpoints created")
exec_duration_seconds = Histogram(
    "mshkn_exec_duration_seconds", "Duration of exec commands in seconds"
)
operation_duration_seconds = Histogram(
    "mshkn_operation_duration_seconds",
    "Duration of orchestrator operations in seconds",
    ["op"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
operation_errors_total = Counter(
    "mshkn_operation_errors_total",
    "Operations that raised, by kind (domain, host, unexpected)",
    ["op", "kind"],
)
thin_pool_used_ratio = Gauge(
    "mshkn_thin_pool_used_ratio",
    "dm-thin pool usage as a ratio, by kind (data, metadata)",
    ["kind"],
)
host_ram_used_ratio = Gauge("mshkn_host_ram_used_ratio", "Host RAM in use as a ratio")


@asynccontextmanager
async def timed(op: str) -> AsyncIterator[None]:
    """Observe the duration of an operation and count failures by kind."""
    start = time.monotonic()
    try:
        yield
    except HostError:
        operation_errors_total.labels(op=op, kind="host").inc()
        raise
    except MshknError:
        operation_errors_total.labels(op=op, kind="domain").inc()
        raise
    except Exception:
        operation_errors_total.labels(op=op, kind="unexpected").inc()
        raise
    finally:
        operation_duration_seconds.labels(op=op).observe(time.monotonic() - start)
