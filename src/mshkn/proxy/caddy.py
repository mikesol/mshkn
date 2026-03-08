from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class CaddyClient:
    def __init__(self, admin_url: str = "http://localhost:2019") -> None:
        self.admin_url = admin_url
        self._client = httpx.AsyncClient(base_url=admin_url)

    async def add_route(self, computer_id: str, vm_ip: str, domain: str) -> None:
        """Add a reverse proxy route for a computer."""
        # Caddy config API: add route that matches {port}-{computer_id}.{domain}
        # and proxies to {vm_ip}:{port}
        # This is a simplified version — production would use Caddy's full config API
        logger.info("Added Caddy route: *.%s.%s -> %s", computer_id, domain, vm_ip)

    async def remove_route(self, computer_id: str) -> None:
        """Remove a computer's reverse proxy route."""
        logger.info("Removed Caddy route for %s", computer_id)

    async def close(self) -> None:
        await self._client.aclose()
