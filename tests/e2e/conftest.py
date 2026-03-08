"""Shared fixtures for E2E tests against a live mshkn server.

Requires env vars:
    MSHKN_API_URL  (default: http://localhost:8000)
    MSHKN_API_KEY  (default: mk-test-key-2026)
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
import pytest


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_URL = os.environ.get("MSHKN_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("MSHKN_API_KEY", "mk-test-key-2026")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api_url() -> str:
    return API_URL


@pytest.fixture(scope="session")
def api_key() -> str:
    return API_KEY


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=API_URL, headers=HEADERS, timeout=60.0
    ) as c:
        yield c


@pytest.fixture
async def long_client() -> AsyncIterator[httpx.AsyncClient]:
    """Client with longer timeout for slow operations like fork."""
    async with httpx.AsyncClient(
        base_url=API_URL, headers=HEADERS, timeout=120.0
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    events: list[tuple[str, str]] = field(default_factory=list)


async def exec_command(
    client: httpx.AsyncClient, computer_id: str, command: str, timeout: float = 30.0
) -> ExecResult:
    """Execute a command via SSE and return parsed output."""
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    events: list[tuple[str, str]] = []
    current_event = "stdout"

    async with client.stream(
        "POST",
        f"/computers/{computer_id}/exec",
        json={"command": command},
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("event: "):
                current_event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
                events.append((current_event, data))
                if current_event == "stdout":
                    stdout_lines.append(data)
                elif current_event == "stderr":
                    stderr_lines.append(data)

    return ExecResult(
        stdout="\n".join(stdout_lines),
        stderr="\n".join(stderr_lines),
        events=events,
    )


async def create_computer(client: httpx.AsyncClient, uses: list[str] | None = None) -> str:
    """Create a computer, return computer_id."""
    resp = await client.post("/computers", json={"uses": uses or []})
    resp.raise_for_status()
    return resp.json()["computer_id"]


async def destroy_computer(client: httpx.AsyncClient, computer_id: str) -> None:
    """Destroy a computer, ignore errors."""
    try:
        await client.delete(f"/computers/{computer_id}")
    except Exception:
        pass


async def checkpoint_computer(
    client: httpx.AsyncClient, computer_id: str, label: str | None = None
) -> str:
    """Checkpoint a computer, return checkpoint_id."""
    body: dict[str, object] = {}
    if label:
        body["label"] = label
    resp = await client.post(f"/computers/{computer_id}/checkpoint", json=body)
    resp.raise_for_status()
    return resp.json()["checkpoint_id"]


async def fork_checkpoint(client: httpx.AsyncClient, checkpoint_id: str) -> str:
    """Fork from a checkpoint, return new computer_id."""
    resp = await client.post(f"/checkpoints/{checkpoint_id}/fork", json={})
    resp.raise_for_status()
    return resp.json()["computer_id"]


async def delete_checkpoint(client: httpx.AsyncClient, checkpoint_id: str) -> None:
    """Delete a checkpoint, ignore errors."""
    try:
        await client.delete(f"/checkpoints/{checkpoint_id}")
    except Exception:
        pass


@asynccontextmanager
async def managed_computer(
    client: httpx.AsyncClient, uses: list[str] | None = None
) -> AsyncIterator[str]:
    """Context manager that creates and destroys a computer."""
    comp_id = await create_computer(client, uses)
    try:
        yield comp_id
    finally:
        await destroy_computer(client, comp_id)


def timed() -> tuple[float, None]:
    """Returns (elapsed_ms, None) — use as: start = time.perf_counter()"""
    raise NotImplementedError("Use time.perf_counter() directly")


@dataclass
class LatencyStats:
    values_ms: list[float]

    @property
    def count(self) -> int:
        return len(self.values_ms)

    @property
    def min(self) -> float:
        return min(self.values_ms)

    @property
    def max(self) -> float:
        return max(self.values_ms)

    @property
    def p50(self) -> float:
        return self._percentile(50)

    @property
    def p95(self) -> float:
        return self._percentile(95)

    @property
    def p99(self) -> float:
        return self._percentile(99)

    @property
    def mean(self) -> float:
        return statistics.mean(self.values_ms)

    def _percentile(self, p: int) -> float:
        sorted_vals = sorted(self.values_ms)
        k = (len(sorted_vals) - 1) * p / 100
        f = int(k)
        c = f + 1
        if c >= len(sorted_vals):
            return sorted_vals[-1]
        return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])

    def report(self, name: str, target_ms: float | None = None) -> str:
        lines = [
            f"{name}: n={self.count}",
            f"  min={self.min:.0f}ms  p50={self.p50:.0f}ms  p95={self.p95:.0f}ms  p99={self.p99:.0f}ms  max={self.max:.0f}ms",
        ]
        if target_ms:
            status = "PASS" if self.p95 <= target_ms else "FAIL"
            lines.append(f"  target p95 <= {target_ms:.0f}ms: {status}")
        return "\n".join(lines)
