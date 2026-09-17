"""Reverse proxy routing via the Caddy admin API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import httpx

from mshkn.errors import HostError

logger = logging.getLogger(__name__)

ROUTES = "/config/apps/http/servers/main/routes"
TERMINAL_ROUTE_ID = "route-terminal"


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

    async def ensure_terminal_route(self) -> None:
        """Install the catch-all 404 at the end of the route list.

        Every computer route is a matcher plus a reverse_proxy, so a request
        whose Host matches none of them falls off the end and Caddy answers 200
        with an empty body. A tenant's `curl --fail` then exits 0 against a
        computer that was destroyed, while a live computer with a dead port
        returns 502 — the absent case reads as success and the reachable
        failure reads as an error, which is backwards. 404 rather than 502
        because the name resolves to no computer at all; 502 stays the dial
        failure, and the two must remain distinguishable.

        Deleting before appending keeps this idempotent and keeps the route
        last: `add_route` inserts at the head, so nothing lands behind it.
        """
        with contextlib.suppress(httpx.HTTPError):
            await self._client.delete(f"/id/{TERMINAL_ROUTE_ID}")
        route = {
            "@id": TERMINAL_ROUTE_ID,
            "handle": [
                {
                    "handler": "static_response",
                    "status_code": 404,
                    "body": "no such computer route\n",
                },
            ],
        }
        try:
            resp = await self._client.post(ROUTES, json=route)
        except httpx.HTTPError as exc:
            raise HostError(f"Caddy ensure_terminal_route failed: {exc!r}") from exc
        if resp.status_code >= 400:
            raise HostError(f"Caddy ensure_terminal_route failed: {resp.status_code} {resp.text}")
        logger.info("Installed Caddy terminal route: unrouted names answer 404")

    async def add_route(self, computer_id: str, vm_ip: str) -> None:
        """Add a reverse proxy route for a computer.

        Creates a Caddy route that matches {port}-{computer_id}.{domain}
        and proxies to {vm_ip}:{port}. It is inserted at the head of the list,
        so it precedes the matcher-less terminal route that must stay last.
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
                resp = await self._client.put(f"{ROUTES}/0", json=route)
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
        if self._client.is_closed:
            return False
        try:
            resp = await self._client.get("/config/")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def close(self) -> None:
        await self._client.aclose()
