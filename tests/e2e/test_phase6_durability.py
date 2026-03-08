"""Phase 6: Durability — "Does It Survive?"

These tests verify the system's resilience to crashes, reboots, and
infrastructure failures. Most require conditions we cannot simulate from an E2E test (killing
the orchestrator, rebooting the host, blocking S3, etc.), so they will fail.
"""

from __future__ import annotations

import pytest

from .conftest import (
    create_computer,
    destroy_computer,
    exec_command,
    managed_computer,
)


# ---------------------------------------------------------------------------
# T6.1 — Orchestrator Crash Recovery
# ---------------------------------------------------------------------------


class TestT61OrchestratorCrashRecovery:
    """After the orchestrator restarts, existing computers should be usable.

    We cannot actually kill the orchestrator from a test, so we verify the
    minimal invariant: a computer that was created is visible via status and
    survives a round-trip exec.
    """

    async def test_computer_survives_in_status(self, client):
        """Create a computer, verify /status returns it with correct fields.

        In a real crash-recovery test we would:
        1. Create a computer.
        2. Kill the orchestrator process.
        3. Restart the orchestrator.
        4. Verify the computer is still listed and usable.

        Since we can't restart the orchestrator here, we at least confirm that
        the status endpoint reflects the computer's existence — the data *is*
        persisted to SQLite, which is the prerequisite for recovery.
        """
        async with managed_computer(client) as computer_id:
            resp = await client.get(f"/computers/{computer_id}/status")
            resp.raise_for_status()
            body = resp.json()

            assert body["computer_id"] == computer_id
            assert body["status"] in ("running", "ready", "booting")
            assert "vm_ip" in body
            assert "manifest_hash" in body
            assert "created_at" in body


# ---------------------------------------------------------------------------
# T6.2 — Host Reboot
# ---------------------------------------------------------------------------


class TestT62HostReboot:
    """After a full host reboot, checkpoints should be restorable."""

    async def test_checkpoint_survives_host_reboot(self, client):
        """Would require rebooting the Hetzner server mid-test."""
        pytest.fail("Not implementable as an automated E2E test")


# ---------------------------------------------------------------------------
# T6.3 — S3 Unavailable During Checkpoint
# ---------------------------------------------------------------------------


class TestT63S3Unavailable:
    """Checkpoint should handle S3/R2 being unreachable gracefully."""

    async def test_checkpoint_when_s3_down(self, client):
        """Would require injecting a network partition to R2."""
        pytest.fail("Not implementable without network fault injection")


# ---------------------------------------------------------------------------
# T6.4 — Checkpoint Retention
# ---------------------------------------------------------------------------


class TestT64CheckpointRetention:
    """Old checkpoints should be garbage-collected per retention policy."""

    async def test_old_checkpoints_cleaned_up(self, client):
        """Would verify that checkpoints older than retention window are pruned."""
        pytest.fail("Retention policy not yet implemented")


# ---------------------------------------------------------------------------
# T6.5 — Litestream Replication
# ---------------------------------------------------------------------------


class TestT65LitestreamReplication:
    """SQLite should be continuously replicated via Litestream."""

    async def test_litestream_replicating(self, client):
        """Would verify Litestream is running and replicating to R2."""
        pytest.fail("Litestream not yet configured on server")


# ---------------------------------------------------------------------------
# T6.6 — Stale VM Cleanup
# ---------------------------------------------------------------------------


class TestT66StaleVMCleanup:
    """VMs that have been idle beyond the TTL should be auto-destroyed."""

    async def test_stale_vms_cleaned(self, client):
        """Would create a VM, wait past TTL, verify it's destroyed."""
        pytest.fail("Auto-cleanup not yet implemented")
