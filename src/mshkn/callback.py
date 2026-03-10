"""Best-effort callback delivery with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def deliver_callback(
    url: str, payload: dict[str, Any], max_retries: int = 3,
) -> None:
    """POST payload to url with bounded retry. Best-effort, never raises."""
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=10)
                if resp.status_code < 500:
                    logger.info("Callback delivered to %s (status %d)", url, resp.status_code)
                    return  # Success or client error (don't retry 4xx)
                logger.warning(
                    "Callback to %s returned %d, retrying (%d/%d)",
                    url, resp.status_code, attempt + 1, max_retries,
                )
        except Exception:
            logger.warning(
                "Callback to %s failed, retrying (%d/%d)",
                url, attempt + 1, max_retries,
            )
        if attempt < max_retries - 1:
            await asyncio.sleep(2**attempt)  # 1s, 2s backoff
    logger.warning("Callback delivery failed after %d attempts: %s", max_retries, url)
