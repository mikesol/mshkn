"""Phase 4: Networking — "Public URLs and Isolation"

These tests run against a LIVE server with real Firecracker VMs and Caddy reverse proxy.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx
import pytest

from .conftest import (
    checkpoint_computer,
    create_computer,
    destroy_computer,
    exec_command,
    fork_checkpoint,
    managed_computer,
)


def port_url(base_url: str, port: int) -> str:
    """Construct a port-specific URL from a base computer URL.

    base_url: https://comp-abc123.mshkn.dev
    returns:  https://{port}-comp-abc123.mshkn.dev
    """
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{port}-{parsed.hostname}"


# ---------------------------------------------------------------------------
# T4.1 — Auto-HTTPS: Start HTTP server in VM, hit public URL
# ---------------------------------------------------------------------------


class TestT41AutoHttps:
    """A simple HTTP server in the VM should be reachable at the computer's public URL."""

    async def test_http_server_reachable_via_public_url(self, client):
        """Start an HTTP server inside the VM and verify the public URL serves it."""
        async with managed_computer(client, uses=["python"]) as computer_id:
            # Start a simple HTTP server on port 8080 in the background
            await exec_command(
                client,
                computer_id,
                "nohup python3 -m http.server 8080 --directory /tmp &>/dev/null &",
                timeout=10.0,
            )
            # Give the server a moment to bind
            await asyncio.sleep(1)

            # Get the computer's public URL
            status_resp = await client.get(f"/computers/{computer_id}/status")
            status_resp.raise_for_status()
            base_url = status_resp.json()["url"]

            # Construct port-specific URL: https://8080-comp-xxx.mshkn.dev
            url = port_url(base_url, 8080)

            # Hit the public URL (follow redirects, verify TLS)
            async with httpx.AsyncClient(timeout=10.0) as external:
                resp = await external.get(url)
                assert resp.status_code == 200
                assert "Directory listing" in resp.text or "<html" in resp.text.lower()


# ---------------------------------------------------------------------------
# T4.2 — Multiple Ports: servers on 3000, 5000, 8080
# ---------------------------------------------------------------------------


class TestT42MultiplePorts:
    """Multiple servers on different ports should all be reachable."""

    async def test_three_ports_reachable(self, client):
        """Start servers on ports 3000, 5000, and 8080; all should respond."""
        async with managed_computer(client, uses=["python"]) as computer_id:
            ports = [3000, 5000, 8080]
            for port in ports:
                # Write server script to file then execute
                script = (
                    "import http.server\n"
                    "class H(http.server.BaseHTTPRequestHandler):\n"
                    "    def do_GET(self):\n"
                    "        self.send_response(200)\n"
                    "        self.end_headers()\n"
                    f"        self.wfile.write(b'port-{port}')\n"
                    "    def log_message(self, *a): pass\n"
                    f"http.server.HTTPServer(('', {port}), H).serve_forever()\n"
                )
                await exec_command(
                    client, computer_id,
                    f"cat > /tmp/srv{port}.py << 'PYEOF'\n{script}PYEOF",
                    timeout=5.0,
                )
                await exec_command(
                    client, computer_id,
                    f"nohup python3 /tmp/srv{port}.py &>/dev/null &",
                    timeout=5.0,
                )

            await asyncio.sleep(2)

            status_resp = await client.get(f"/computers/{computer_id}/status")
            status_resp.raise_for_status()
            base_url = status_resp.json()["url"]

            async with httpx.AsyncClient(timeout=10.0) as external:
                for port in ports:
                    url = port_url(base_url, port)
                    resp = await external.get(url)
                    assert resp.status_code == 200
                    assert f"port-{port}" in resp.text


# ---------------------------------------------------------------------------
# T4.3 — WebSocket Support
# ---------------------------------------------------------------------------


class TestT43WebSocket:
    """WebSocket connections through the public URL should work."""

    async def test_websocket_echo(self, client, long_client):
        """Start a WS echo server in the VM and verify a round trip."""
        async with managed_computer(
            long_client, uses=["python-3.12(websockets)"],
        ) as computer_id:
            # Write websocket echo server script
            ws_script = (
                "import asyncio, websockets\n"
                "async def echo(ws):\n"
                "    async for msg in ws:\n"
                "        await ws.send(msg)\n"
                "async def main():\n"
                "    async with websockets.serve(echo, '0.0.0.0', 9000):\n"
                "        await asyncio.Future()\n"
                "asyncio.run(main())\n"
            )
            await exec_command(
                client, computer_id,
                f"cat > /tmp/ws_echo.py << 'PYEOF'\n{ws_script}PYEOF",
                timeout=5.0,
            )
            await exec_command(
                client, computer_id,
                "nohup python3 /tmp/ws_echo.py &>/dev/null &",
                timeout=5.0,
            )
            await asyncio.sleep(2)

            status_resp = await client.get(f"/computers/{computer_id}/status")
            status_resp.raise_for_status()
            base_url = status_resp.json()["url"]

            # Construct wss://9000-comp-xxx.mshkn.dev
            ws_url = port_url(base_url, 9000).replace("https://", "wss://")

            try:
                import websockets

                async with websockets.connect(ws_url) as ws:
                    await ws.send("ping-from-test")
                    reply = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    assert reply == "ping-from-test"
            except ImportError:
                pytest.skip("websockets library not installed on test runner")


# ---------------------------------------------------------------------------
# T4.4 — URL Changes on Checkpoint/Resume
# ---------------------------------------------------------------------------


class TestT44UrlChangesOnCheckpointResume:
    """When a computer is checkpointed and forked, the new computer gets a new URL."""

    async def test_fork_gets_different_url(self, client, long_client):
        """Fork from a checkpoint produces a computer with a different URL."""
        computer_id = await create_computer(client, uses=[])
        forked_id = None
        try:
            # Write a marker so we can verify state was preserved
            await exec_command(client, computer_id, "echo marker42 > /root/marker.txt")

            checkpoint_id = await checkpoint_computer(client, computer_id, label="url-test")
            forked_id = await fork_checkpoint(long_client, checkpoint_id)

            # Get URLs for both computers
            orig_resp = await client.get(f"/computers/{computer_id}/status")
            orig_resp.raise_for_status()
            orig_url = orig_resp.json().get("url", "")

            fork_resp = await client.get(f"/computers/{forked_id}/status")
            fork_resp.raise_for_status()
            fork_url = fork_resp.json().get("url", "")

            assert orig_url != fork_url, (
                f"Original and forked computers should have different URLs, "
                f"both got: {orig_url}"
            )

            # Verify state was preserved in the fork
            result = await exec_command(client, forked_id, "cat /root/marker.txt")
            assert "marker42" in result.stdout
        finally:
            await destroy_computer(client, computer_id)
            if forked_id:
                await destroy_computer(client, forked_id)


# ---------------------------------------------------------------------------
# T4.5 — Network Isolation Between VMs
# ---------------------------------------------------------------------------


class TestT45NetworkIsolation:
    """VMs should not be able to reach each other's private networks."""

    async def test_vms_cannot_ping_each_other(self, client):
        """Two VMs should not be able to ping each other's private IPs."""
        comp_a = await create_computer(client, uses=[])
        comp_b = await create_computer(client, uses=[])
        try:
            # Get VM IPs from status
            status_a = await client.get(f"/computers/{comp_a}/status")
            status_a.raise_for_status()
            ip_a = status_a.json().get("vm_ip", "")

            status_b = await client.get(f"/computers/{comp_b}/status")
            status_b.raise_for_status()
            ip_b = status_b.json().get("vm_ip", "")

            assert ip_a, "VM A has no IP"
            assert ip_b, "VM B has no IP"

            # VM A tries to ping VM B — should fail
            result = await exec_command(
                client,
                comp_a,
                f"ping -c 1 -W 2 {ip_b} 2>&1 || echo PING_FAILED",
                timeout=10.0,
            )
            assert "PING_FAILED" in result.stdout or "100% packet loss" in result.stdout, (
                f"VM A should NOT be able to reach VM B at {ip_b}. "
                f"Output: {result.stdout}"
            )

            # VM B tries to ping VM A — should also fail
            result = await exec_command(
                client,
                comp_b,
                f"ping -c 1 -W 2 {ip_a} 2>&1 || echo PING_FAILED",
                timeout=10.0,
            )
            assert "PING_FAILED" in result.stdout or "100% packet loss" in result.stdout, (
                f"VM B should NOT be able to reach VM A at {ip_a}. "
                f"Output: {result.stdout}"
            )
        finally:
            await destroy_computer(client, comp_a)
            await destroy_computer(client, comp_b)
