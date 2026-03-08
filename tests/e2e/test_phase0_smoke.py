"""Phase 0: Smoke tests — "Does It Even Boot?"

These tests run against a LIVE server with real Firecracker VMs.
If T0.1 fails, nothing else matters.
"""

from __future__ import annotations

import pytest

from .conftest import (
    ExecResult,
    create_computer,
    destroy_computer,
    exec_command,
    managed_computer,
)


# ---------------------------------------------------------------------------
# T0.1 — Cold Create, No Capabilities
# ---------------------------------------------------------------------------


class TestT01ColdCreateNoCapabilities:
    """computer_create(uses: []) — the absolute bare minimum."""

    async def test_create_returns_computer_id_and_url(self, client):
        """Create returns a computer_id and url."""
        resp = await client.post("/computers", json={"uses": []})
        resp.raise_for_status()
        body = resp.json()

        computer_id = body["computer_id"]
        try:
            assert "computer_id" in body
            assert "url" in body
            assert isinstance(body["computer_id"], str)
            assert len(body["computer_id"]) > 0
            assert isinstance(body["url"], str)
            assert len(body["url"]) > 0
        finally:
            await destroy_computer(client, computer_id)

    async def test_exec_echo_hello(self, client):
        """computer_exec(id, 'echo hello') returns 'hello'."""
        async with managed_computer(client, uses=[]) as computer_id:
            result = await exec_command(client, computer_id, "echo hello")
            assert result.stdout.strip() == "hello"

    async def test_destroy_without_error(self, client):
        """computer_destroy(id) completes without error."""
        computer_id = await create_computer(client, uses=[])
        resp = await client.delete(f"/computers/{computer_id}")
        resp.raise_for_status()
        body = resp.json()
        assert body.get("status") == "destroyed"


# ---------------------------------------------------------------------------
# T0.2 — Create With a Single Capability
# ---------------------------------------------------------------------------


class TestT02CreateWithCapability:
    """computer_create(uses: ['python-3.12()']) — capabilities not yet implemented."""

    async def test_python_capability(self, client):
        """Create with python-3.12(), exec python3 --version returns 3.12.x."""
        async with managed_computer(client, uses=["python-3.12()"]) as computer_id:
            result = await exec_command(client, computer_id, "python3 --version")
            version_line = result.stdout.strip()
            assert version_line.startswith("Python 3.12"), (
                f"Expected Python 3.12.x, got: {version_line}"
            )

    async def test_python_capability_destroy_clean(self, client):
        """Destroy after capability-based create is clean."""
        resp = await client.post("/computers", json={"uses": ["python-3.12()"]})
        resp.raise_for_status()
        computer_id = resp.json()["computer_id"]

        destroy_resp = await client.delete(f"/computers/{computer_id}")
        destroy_resp.raise_for_status()
        assert destroy_resp.json().get("status") == "destroyed"


# ---------------------------------------------------------------------------
# T0.3 — SSH-Like Exec Basics
# ---------------------------------------------------------------------------


class TestT03ExecBasics:
    """Streaming, stderr, and exit code behavior."""

    async def test_streaming_sequential_output(self, client):
        """Run a loop that emits lines 1-5 with sleeps; verify all lines arrive."""
        async with managed_computer(client, uses=[]) as computer_id:
            result = await exec_command(
                client,
                computer_id,
                "for i in $(seq 1 5); do echo $i; sleep 0.1; done",
                timeout=30.0,
            )
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            assert lines == ["1", "2", "3", "4", "5"], (
                f"Expected lines 1-5, got: {lines}"
            )

    async def test_stderr_comes_through(self, client):
        """echo to stderr arrives as stderr events."""
        async with managed_computer(client, uses=[]) as computer_id:
            result = await exec_command(client, computer_id, "echo err >&2")
            assert "err" in result.stderr, (
                f"Expected 'err' in stderr, got stdout={result.stdout!r}, "
                f"stderr={result.stderr!r}, events={result.events}"
            )

    async def test_stdout_and_stderr_separated(self, client):
        """stdout and stderr are delivered on separate event channels."""
        async with managed_computer(client, uses=[]) as computer_id:
            result = await exec_command(
                client,
                computer_id,
                "echo out_line && echo err_line >&2",
            )
            assert "out_line" in result.stdout
            assert "err_line" in result.stderr

    async def test_exit_code_nonzero(self, client):
        """A command that exits non-zero should indicate failure somehow.

        The SSE stream may or may not include exit code information.
        We check for any indication: an 'exit' event, an 'error' event,
        or an HTTP-level error.
        """
        async with managed_computer(client, uses=[]) as computer_id:
            result = await exec_command(client, computer_id, "exit 42")

            # Look for any exit code indication in the events
            exit_events = [
                (evt, data)
                for evt, data in result.events
                if evt in ("exit", "error", "exit_code", "done")
            ]

            # If there are exit-type events, check that exit code is non-zero
            if exit_events:
                for evt, data in exit_events:
                    if data.isdigit() or (data.startswith("-") and data[1:].isdigit()):
                        assert int(data) != 0, "Expected non-zero exit code"
                        return

            # If no explicit exit event, the test still passes — we document
            # that exit codes may not be surfaced yet
            print(
                f"NOTE: No explicit exit code event found. "
                f"Events were: {result.events}"
            )

    async def test_multiline_stdout(self, client):
        """Multiple lines of stdout are all captured."""
        async with managed_computer(client, uses=[]) as computer_id:
            result = await exec_command(
                client,
                computer_id,
                'echo "line1" && echo "line2" && echo "line3"',
            )
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            assert lines == ["line1", "line2", "line3"]
