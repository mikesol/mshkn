"""Reverse proxy routing via the Caddy admin API."""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from mshkn.errors import HostError

logger = logging.getLogger(__name__)


class CaddyProxy:
    def __init__(
        self,
        admin_url: str,
        domain: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.admin_url = admin_url
        self.domain = domain
        self._client = httpx.AsyncClient(base_url=admin_url, timeout=10.0, transport=transport)

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
                        computer_id,
                        resp.status_code,
                        resp.text,
                    )
                    raise HostError(f"Caddy add_route failed: {resp.status_code} {resp.text}")
                break
            except (httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                if attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))
                    continue
                raise HostError(f"Caddy add_route failed after retries: {exc}") from exc
            except httpx.HTTPError as exc:
                # Not one of the two transient errors worth retrying (a read or
                # write timeout, say). Callers map HostError; a raw httpx
                # exception would reach them as an unhandled 500.
                raise HostError(f"Caddy add_route failed: {exc!r}") from exc
        logger.info("Added Caddy route: *-%s.%s -> %s", computer_id, self.domain, vm_ip)

    async def remove_route(self, computer_id: str) -> None:
        """Remove a computer's reverse proxy route. Never raises.

        A 404 means the route is already absent — the expected outcome when
        two deletes of the same computer race — so it is treated as success
        rather than logged as a failure.
        """
        route_id = f"route-{computer_id}"
        try:
            resp = await self._client.delete(f"/id/{route_id}")
        except httpx.HTTPError as exc:
            logger.warning("Failed to remove Caddy route for %s: %s", computer_id, exc)
            return
        if resp.status_code == 404:
            logger.info("Caddy route for %s already absent", computer_id)
            return
        if resp.status_code >= 400:
            logger.warning(
                "Failed to remove Caddy route for %s: %s %s",
                computer_id,
                resp.status_code,
                resp.text,
            )
            return
        logger.info("Removed Caddy route for %s", computer_id)

    async def healthy(self) -> bool:
        try:
            resp = await self._client.get("/config/")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def close(self) -> None:
        await self._client.aclose()
