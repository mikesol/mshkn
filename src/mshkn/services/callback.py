"""Best-effort webhook delivery with bounded exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def deliver_callback(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    max_retries: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """POST payload to url. Retries 5xx and transport errors; never raises."""
    for attempt in range(max_retries):
        try:
            resp = await client.post(url, json=payload, timeout=10)
            if resp.status_code < 500:
                logger.info("Callback delivered to %s (status %d)", url, resp.status_code)
                return
            logger.warning(
                "Callback to %s returned %d, retrying (%d/%d)",
                url,
                resp.status_code,
                attempt + 1,
                max_retries,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Callback to %s failed (%s), retrying (%d/%d)",
                url,
                type(exc).__name__,
                attempt + 1,
                max_retries,
            )
        if attempt < max_retries - 1:
            await sleep(float(2**attempt))
    logger.warning("Callback delivery failed after %d attempts: %s", max_retries, url)
