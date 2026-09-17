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
# Every computer route is `route-` + a computer id, and every computer id is
# `comp-` + hex (`ComputerService._bring_up`). The sweep matches on this rather
# than on "not a route I recognise" so that an unfamiliar route — route-api,
# anything a human installed by hand — is never a deletion candidate.
COMPUTER_ROUTE_PREFIX = "route-comp-"
_INDEX_RACE = "array index out of bounds"
_REMOVE_ATTEMPTS = 3


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
        # Serialises every config mutation. Caddy's admin API resolves an `@id`
        # to an array index and then applies the change by index, and the two
        # steps are not atomic: concurrent writes shift the indexes under each
        # other. Measured against Caddy 2.11.4 (#154), 60 concurrent deletes
        # leave 40-47 routes behind and destroy 2-5 routes nobody targeted; the
        # same 60 deletes serialised leave none behind and destroy none.
        # Reads are not held, since they cannot corrupt the array.
        self._mutate = asyncio.Lock()

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
        async with self._mutate:
            with contextlib.suppress(httpx.HTTPError):
                await self._client.delete(f"/id/{TERMINAL_ROUTE_ID}")
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
        async with self._mutate:
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
                    # Not one of the two transient errors worth retrying (a read
                    # or write timeout, say). Callers map HostError; a raw httpx
                    # exception would reach them as an unhandled 500.
                    raise HostError(f"Caddy add_route failed: {exc!r}") from exc
        logger.info("Added Caddy route: *-%s.%s -> %s", computer_id, self.domain, vm_ip)

    async def remove_route(self, computer_id: str) -> None:
        """Remove a computer's reverse proxy route. Never raises.

        A 404 means the route is already absent — the expected outcome when
        two deletes of the same computer race — so it is treated as success
        rather than logged as a failure.

        `self._mutate` is what stops the route leaking (#154). Two failures
        remain possible once the deletes are serialised, and both are retried:
        a connection Caddy closed while it sat idle in the pool, and the 500
        `array index out of bounds` that another writer can still provoke.
        Re-issuing the DELETE resolves the id afresh, which is why the second
        is a retry rather than a different verb — PATCH by `@id` resolves the
        index the same way. The reaper's sweep covers whatever is still lost.
        """
        route_id = f"route-{computer_id}"
        failure = ""
        for attempt in range(_REMOVE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(0.05 * attempt)
            try:
                async with self._mutate:
                    resp = await self._client.delete(f"/id/{route_id}")
            except (httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                # Caddy closes idle keep-alive connections, so a delete can pick
                # up a pooled connection the server has already dropped and fail
                # with "Server disconnected without sending a response". With the
                # deletes serialised this is the only failure left in practice.
                # `add_route` already retried these two; `remove_route` did not.
                failure = repr(exc)
                continue
            except httpx.HTTPError as exc:
                # A read or write timeout: the request may already have been
                # applied, so re-sending it is not obviously safe. The reaper's
                # sweep covers this case instead.
                logger.warning("Failed to remove Caddy route for %s: %s", computer_id, exc)
                return
            if resp.status_code == 404:
                logger.info("Caddy route for %s already absent", computer_id)
                return
            if resp.status_code < 400:
                logger.info("Removed Caddy route for %s", computer_id)
                return
            failure = f"{resp.status_code} {resp.text}"
            if _INDEX_RACE not in resp.text:
                break
        logger.warning("Failed to remove Caddy route for %s: %s", computer_id, failure)

    async def list_route_ids(self) -> list[str]:
        """Every `@id` currently installed, in list order. Never raises.

        Feeds the reaper's sweep, which is maintenance: an unreachable or
        unreadable admin API yields an empty list, making the sweep a no-op,
        rather than failing the whole cycle.
        """
        try:
            resp = await self._client.get(ROUTES)
        except httpx.HTTPError as exc:
            logger.warning("Failed to list Caddy routes: %s", exc)
            return []
        if resp.status_code >= 400:
            logger.warning("Failed to list Caddy routes: %s %s", resp.status_code, resp.text)
            return []
        try:
            routes = resp.json()
        except ValueError:
            logger.warning("Caddy route listing was not JSON")
            return []
        # Caddy answers `null`, not `[]`, for a server with no routes.
        if not isinstance(routes, list):
            return []
        return [
            route["@id"]
            for route in routes
            if isinstance(route, dict) and isinstance(route.get("@id"), str)
        ]

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
