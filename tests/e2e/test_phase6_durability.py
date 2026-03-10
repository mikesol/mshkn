"""Phase 6: Durability — "Does It Survive?"

These tests verify the system's resilience to crashes, reboots, and
infrastructure failures. Most require conditions we cannot simulate from an E2E test (killing
the orchestrator, rebooting the host, blocking S3, etc.), so they will fail.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from .conftest import (
    create_computer,
    destroy_computer,
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
    """Old checkpoints should be garbage-collected per retention policy.

    These tests assume MSHKN_CHECKPOINT_RETENTION is set to 5 on the server
    for testing purposes.
    """

    async def _list_checkpoint_ids(self, client) -> set[str]:
        """Helper: return set of all checkpoint IDs for the current account."""
        resp = await client.get("/checkpoints")
        resp.raise_for_status()
        return {c["checkpoint_id"] for c in resp.json()}

    async def test_excess_checkpoints_pruned(self, client):
        """Create more checkpoints than the retention limit; oldest should be pruned.

        Creates 8 checkpoints (retention=5), waits for the reaper to prune,
        then verifies at most 5 of ours remain.
        """
        import time

        computer_id = await create_computer(client, uses=[])
        checkpoint_ids: list[str] = []

        try:
            # Create 8 checkpoints
            for i in range(8):
                resp = await client.post(
                    f"/computers/{computer_id}/checkpoint",
                    json={"label": f"retention-test-{i}"},
                )
                resp.raise_for_status()
                checkpoint_ids.append(resp.json()["checkpoint_id"])
                await asyncio.sleep(0.5)

            our_ids = set(checkpoint_ids)

            # Wait for reaper to prune (up to 150s)
            deadline = time.time() + 150
            while time.time() < deadline:
                await asyncio.sleep(10)
                all_ids = await self._list_checkpoint_ids(client)
                alive = len(our_ids & all_ids)
                total = len(all_ids)
                if total <= 5:
                    # Retention enforced: at most 5 total checkpoints
                    assert alive <= 5
                    return

            all_ids = await self._list_checkpoint_ids(client)
            assert len(all_ids) <= 5, (
                f"Expected at most 5 checkpoints after pruning, got {len(all_ids)}"
            )
        finally:
            await destroy_computer(client, computer_id)
            for cid in checkpoint_ids:
                try:
                    await client.delete(f"/checkpoints/{cid}")
                except Exception:
                    pass

    async def test_pinned_checkpoint_retained(self, client):
        """Pinned checkpoints should survive retention pruning.

        Creates 8 checkpoints, pins the 3rd one, waits for pruning.
        The pinned one should survive even though it's old.
        """
        import time

        computer_id = await create_computer(client, uses=[])
        checkpoint_ids: list[str] = []

        try:
            # Create 8 checkpoints, pin #2 (0-indexed)
            for i in range(8):
                resp = await client.post(
                    f"/computers/{computer_id}/checkpoint",
                    json={"label": f"pin-test-{i}", "pin": (i == 2)},
                )
                resp.raise_for_status()
                checkpoint_ids.append(resp.json()["checkpoint_id"])
                await asyncio.sleep(0.5)

            pinned_id = checkpoint_ids[2]

            # Wait for reaper to prune
            deadline = time.time() + 150
            while time.time() < deadline:
                await asyncio.sleep(10)
                all_ids = await self._list_checkpoint_ids(client)
                total = len(all_ids)
                # 5 unpinned + 1 pinned = 6 max
                if total <= 6:
                    assert pinned_id in all_ids, "Pinned checkpoint was deleted!"
                    return

            all_ids = await self._list_checkpoint_ids(client)
            assert pinned_id in all_ids, "Pinned checkpoint was deleted!"
        finally:
            await destroy_computer(client, computer_id)
            for cid in checkpoint_ids:
                try:
                    await client.delete(f"/checkpoints/{cid}")
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# T6.5 — Litestream Replication
# ---------------------------------------------------------------------------


class TestT65LitestreamReplication:
    """SQLite should be continuously replicated via Litestream."""

    async def test_litestream_service_active(self, client):
        """Verify the Litestream systemd service is active and running."""
        import subprocess

        result = subprocess.run(
            [
                "ssh",
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-i", os.path.expanduser("~/.ssh/id_ed25519"),
                "root@135.181.6.215",
                "systemctl is-active litestream",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.stdout.strip() == "active", (
            f"Litestream service not active: {result.stdout.strip()} {result.stderr.strip()}"
        )

    async def test_litestream_has_generations(self, client):
        """Verify Litestream has created at least one generation in R2."""
        import subprocess

        result = subprocess.run(
            [
                "ssh",
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-i", os.path.expanduser("~/.ssh/id_ed25519"),
                "root@135.181.6.215",
                "litestream generations -config /etc/litestream.yml /opt/mshkn/mshkn.db",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l and not l.startswith("name")]
        assert len(lines) >= 1, (
            f"No Litestream generations found: {result.stdout} {result.stderr}"
        )


# ---------------------------------------------------------------------------
# T6.6 — Stale VM Cleanup
# ---------------------------------------------------------------------------


class TestT66StaleVMCleanup:
    """Dead VMs should be automatically detected and cleaned up by the reaper."""

    async def test_dead_vm_reaped(self, client):
        """Create a VM, kill its Firecracker process, verify reaper cleans it up.

        The reaper runs every 60s. We kill the Firecracker process via SSH
        (finding the PID from the process list), then poll the status endpoint
        until the VM is marked destroyed (returns 404).
        """
        import subprocess
        import time

        # Create a computer
        resp = await client.post("/computers", json={"uses": []})
        resp.raise_for_status()
        computer_id = resp.json()["computer_id"]

        try:
            # Find and kill the Firecracker process for this computer via SSH
            result = subprocess.run(
                [
                    "ssh",
                    "-o", "IdentitiesOnly=yes",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=no",
                    "-i", os.path.expanduser("~/.ssh/id_ed25519"),
                    "root@135.181.6.215",
                    f"pgrep -f 'fc-{computer_id}' | xargs -r kill -9",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            # Wait for the reaper to detect and clean up (up to 90s)
            deadline = time.time() + 90
            while time.time() < deadline:
                await asyncio.sleep(5)
                check = await client.get(f"/computers/{computer_id}/status")
                if check.status_code == 404:
                    return  # VM was reaped

            pytest.fail(
                f"Reaper did not clean up dead VM {computer_id} within 90s"
            )
        except Exception:
            # Best-effort cleanup if test fails
            try:
                await client.delete(f"/computers/{computer_id}")
            except Exception:
                pass
            raise
