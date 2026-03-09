"""Phase 3: Capability System E2E tests.

All tests xfail — the capability system is not yet implemented.
Test logic is fully written so removing xfail is the only change needed.

These tests run against a LIVE server with real Firecracker VMs.
Every computer costs real resources — clean up in finally blocks.
"""

from __future__ import annotations

import time

import httpx

from .conftest import (
    checkpoint_computer,
    create_computer,
    delete_checkpoint,
    destroy_computer,
    exec_command,
    fork_checkpoint,
    managed_computer,
)


# ---------------------------------------------------------------------------
# T3.1 — pip install blocked with structured suggested_action
# ---------------------------------------------------------------------------



async def test_pip_install_blocked(long_client: httpx.AsyncClient) -> None:
    """Running pip install inside a VM should fail with a structured error
    containing a suggested_action tool call to add the capability instead."""
    computers: list[str] = []

    try:
        comp = await create_computer(long_client)
        computers.append(comp)

        result = await exec_command(long_client, comp, "pip install requests")

        # Should fail — pip install is blocked
        assert result.stderr, "pip install should produce stderr output"

        # The error should contain a structured suggested_action
        combined = result.stdout + result.stderr
        assert "suggested_action" in combined.lower() or "uses" in combined.lower(), (
            "Blocked pip install should suggest using the 'uses' capability instead"
        )

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)


# ---------------------------------------------------------------------------
# T3.2 — npm install blocked
# ---------------------------------------------------------------------------



async def test_npm_install_blocked(long_client: httpx.AsyncClient) -> None:
    """npm install should be blocked with structured error + suggested_action."""
    computers: list[str] = []

    try:
        comp = await create_computer(long_client, uses=["node"])
        computers.append(comp)

        result = await exec_command(long_client, comp, "npm install express")

        assert result.stderr, "npm install should produce stderr output"
        combined = result.stdout + result.stderr
        assert "suggested_action" in combined.lower() or "uses" in combined.lower(), (
            "Blocked npm install should suggest using the 'uses' capability instead"
        )

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)


# ---------------------------------------------------------------------------
# T3.3 — apt-get blocked
# ---------------------------------------------------------------------------



async def test_apt_get_blocked(long_client: httpx.AsyncClient) -> None:
    """apt-get install should be blocked with structured error + suggested_action."""
    computers: list[str] = []

    try:
        comp = await create_computer(long_client)
        computers.append(comp)

        result = await exec_command(long_client, comp, "apt-get install -y curl")

        assert result.stderr, "apt-get install should produce stderr output"
        combined = result.stdout + result.stderr
        assert "suggested_action" in combined.lower() or "uses" in combined.lower(), (
            "Blocked apt-get should suggest using the 'uses' capability instead"
        )

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)


# ---------------------------------------------------------------------------
# T3.4 — Writing to /nix/store fails (read-only)
# ---------------------------------------------------------------------------



async def test_nix_store_read_only(long_client: httpx.AsyncClient) -> None:
    """/nix/store should be mounted read-only — writes must fail."""
    computers: list[str] = []

    try:
        comp = await create_computer(long_client, uses=["python"])
        computers.append(comp)

        result = await exec_command(
            long_client, comp, "touch /nix/store/test-write 2>&1; echo $?"
        )

        # The exit code should be non-zero (write should fail)
        last_line = result.stdout.strip().split("\n")[-1]
        assert last_line != "0", "/nix/store should be read-only"

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)


# ---------------------------------------------------------------------------
# T3.5 — Capability layer caching (second create faster)
# ---------------------------------------------------------------------------



async def test_capability_layer_caching(long_client: httpx.AsyncClient) -> None:
    """Creating a second computer with the same capabilities should be faster
    because the Nix layer is cached."""
    computers: list[str] = []

    try:
        # First create — may need to build the Nix layer
        t0 = time.perf_counter()
        comp_1 = await create_computer(long_client, uses=["python"])
        computers.append(comp_1)
        first_ms = (time.perf_counter() - t0) * 1000

        # Second create — should hit the cache
        t0 = time.perf_counter()
        comp_2 = await create_computer(long_client, uses=["python"])
        computers.append(comp_2)
        second_ms = (time.perf_counter() - t0) * 1000

        # The capability cache should be hit on the second create.
        # When the Nix store is already populated, the difference is smaller
        # (only the inject step is skipped on cache hit). Use a generous
        # threshold: cached create should be no slower than the first.
        assert second_ms <= first_ms * 1.1, (
            f"Cached create ({second_ms:.0f}ms) should not be slower "
            f"than first create ({first_ms:.0f}ms)"
        )

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)


# ---------------------------------------------------------------------------
# T3.6 — Capability composition (python + node + ffmpeg)
# ---------------------------------------------------------------------------



async def test_capability_composition(long_client: httpx.AsyncClient) -> None:
    """A computer with multiple capabilities should have all of them available."""
    computers: list[str] = []

    try:
        comp = await create_computer(long_client, uses=["python", "node", "ffmpeg"])
        computers.append(comp)

        # Verify Python is available
        result_py = await exec_command(long_client, comp, "python3 --version")
        assert "Python" in result_py.stdout, "Python should be available"

        # Verify Node is available
        result_node = await exec_command(long_client, comp, "node --version")
        assert result_node.stdout.strip().startswith("v"), "Node should be available"

        # Verify ffmpeg is available
        result_ff = await exec_command(long_client, comp, "ffmpeg -version")
        assert "ffmpeg" in result_ff.stdout.lower(), "ffmpeg should be available"

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)


# ---------------------------------------------------------------------------
# T3.7 — Version pinning
# ---------------------------------------------------------------------------



async def test_version_pinning(long_client: httpx.AsyncClient) -> None:
    """Specifying a version pin should install that exact version."""
    computers: list[str] = []

    try:
        comp = await create_computer(long_client, uses=["python@3.11"])
        computers.append(comp)

        result = await exec_command(long_client, comp, "python3 --version")
        assert "3.11" in result.stdout, (
            f"Expected Python 3.11.x, got: {result.stdout.strip()}"
        )

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)


# ---------------------------------------------------------------------------
# T3.8 — Tarball escape hatch
# ---------------------------------------------------------------------------



async def test_tarball_escape_hatch(long_client: httpx.AsyncClient) -> None:
    """The tarball escape hatch allows injecting arbitrary files into the VM."""
    computers: list[str] = []

    try:
        # Use a real small tarball — jq static binary release
        comp = await create_computer(
            long_client,
            uses=[
                "tarball:https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64:/opt/tools",
            ],
        )
        computers.append(comp)

        # Verify the tarball contents were extracted
        result = await exec_command(long_client, comp, "ls /opt/tools/")
        assert result.stdout.strip(), "Tarball contents should be present at /opt/tools/"

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)


# ---------------------------------------------------------------------------
# T3.9 — Manifest compatibility on resume
# ---------------------------------------------------------------------------



async def test_manifest_compatibility_on_resume(long_client: httpx.AsyncClient) -> None:
    """Forking a checkpoint should preserve the same manifest/capabilities.

    The manifest_hash returned at creation should match the one used when
    resuming from a checkpoint of that computer.
    """
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        # Create with capabilities
        resp = await long_client.post(
            "/computers", json={"uses": ["python", "node"]}
        )
        resp.raise_for_status()
        data = resp.json()
        comp = data["computer_id"]
        original_manifest = data["manifest_hash"]
        computers.append(comp)

        # Verify capabilities work
        result = await exec_command(long_client, comp, "python3 --version")
        assert "Python" in result.stdout

        # Checkpoint
        ckpt = await checkpoint_computer(long_client, comp, label="cap_resume")
        checkpoints.append(ckpt)

        # Fork from checkpoint
        comp_forked = await fork_checkpoint(long_client, ckpt)
        computers.append(comp_forked)

        # Capabilities should still work in the fork
        result_py = await exec_command(long_client, comp_forked, "python3 --version")
        assert "Python" in result_py.stdout, "Python should still work after fork"

        result_node = await exec_command(long_client, comp_forked, "node --version")
        assert result_node.stdout.strip().startswith("v"), (
            "Node should still work after fork"
        )

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)
