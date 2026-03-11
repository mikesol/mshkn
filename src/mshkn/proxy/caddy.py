from __future__ import annotations

import asyncio
import logging
import re

import httpx

logger = logging.getLogger(__name__)


class CaddyClient:
    def __init__(self, admin_url: str = "http://localhost:2019", domain: str = "mshkn.dev") -> None:
        self.admin_url = admin_url
        self.domain = domain
        self._client = httpx.AsyncClient(base_url=admin_url, timeout=10.0)

    async def add_route(self, computer_id: str, vm_ip: str) -> None:
        """Add a reverse proxy route for a computer.

        Creates a Caddy route that matches {port}-{computer_id}.{domain}
        and proxies to {vm_ip}:{port}.
        """
        route_id = f"route-{computer_id}"
        # Escape dots in domain for regex
        domain_re = re.escape(self.domain)
        route = {
            "@id": route_id,
            "match": [
                {
                    "header_regexp": {
                        "Host": {
                            "name": "port_match",
                            "pattern": f"^(\\d+)-{re.escape(computer_id)}\\.{domain_re}$",
                        },
                    },
                },
            ],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": f"{vm_ip}:{{http.regexp.port_match.1}}"}],
                },
            ],
        }
        for attempt in range(3):
            try:
                resp = await self._client.post(
                    "/config/apps/http/servers/main/routes",
                    json=route,
                )
                if resp.status_code >= 400:
                    logger.error(
                        "Failed to add Caddy route for %s: %s %s",
                        computer_id, resp.status_code, resp.text,
                    )
                    raise RuntimeError(f"Caddy add_route failed: {resp.status_code} {resp.text}")
                break
            except (httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                if attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))
                    continue
                raise RuntimeError(f"Caddy add_route failed after retries: {exc}") from exc
        logger.info("Added Caddy route: *-%s.%s -> %s", computer_id, self.domain, vm_ip)

    async def remove_route(self, computer_id: str) -> None:
        """Remove a computer's reverse proxy route."""
        route_id = f"route-{computer_id}"
        resp = await self._client.delete(f"/id/{route_id}")
        if resp.status_code >= 400:
            # Route may not exist (e.g. Caddy restarted) — log but don't fail
            logger.warning(
                "Failed to remove Caddy route for %s: %s %s",
                computer_id, resp.status_code, resp.text,
            )
            return
        logger.info("Removed Caddy route for %s", computer_id)

    async def close(self) -> None:
        await self._client.aclose()
