"""Phase 11: Observability — "Metrics, Logs, and Status"

These tests run against a LIVE server with real Firecracker VMs.
Most are xfail because observability infrastructure is not yet implemented.
"""

from __future__ import annotations

import pytest

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
# T11.1 — Prometheus Endpoint
# ---------------------------------------------------------------------------


class TestT111PrometheusEndpoint:
    """Verify /metrics endpoint exposes Prometheus-format metrics."""

    async def test_metrics_endpoint_exists(self, client):
        """GET /metrics should return Prometheus text format.

        Expected metrics:
        - mshkn_computers_active (gauge)
        - mshkn_computers_created_total (counter)
        - mshkn_checkpoints_total (counter)
        - mshkn_exec_duration_seconds (histogram)
        """
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        text = resp.text
        assert "mshkn_computers_active" in text
        assert "# HELP" in text
        assert "# TYPE" in text


# ---------------------------------------------------------------------------
# T11.2 — Structured JSON Logs
# ---------------------------------------------------------------------------


class TestT112StructuredLogs:
    """Verify the server emits structured JSON logs."""

    async def test_logs_are_json(self):
        """Server logs should be structured JSON with standard fields.

        Expected fields per log line:
        - timestamp (ISO 8601)
        - level (info, warn, error)
        - msg (human-readable message)
        - computer_id (when applicable)
        - request_id (for HTTP requests)
        """
        pass


# ---------------------------------------------------------------------------
# T11.3 — Computer Status Endpoint
# ---------------------------------------------------------------------------


class TestT113ComputerStatus:
    """Verify /computers/{id}/status returns accurate data."""

    async def test_status_returns_expected_fields(self, client):
        """GET /computers/{id}/status should return machine state.

        Expected fields: computer_id, status, created_at, vm_ip, url
        """
        async with managed_computer(client, uses=[]) as computer_id:
            resp = await client.get(f"/computers/{computer_id}/status")
            resp.raise_for_status()
            body = resp.json()

            expected_fields = ["computer_id", "status", "created_at"]
            for f in expected_fields:
                assert f in body, f"Missing field '{f}' in status response: {body}"

            assert body["computer_id"] == computer_id
            assert body["status"] in ("running", "ready", "active"), (
                f"Unexpected status: {body['status']}"
            )


# ---------------------------------------------------------------------------
# T11.4 — Request Tracing
# ---------------------------------------------------------------------------


class TestT114RequestTracing:
    """Verify requests have trace IDs for debugging."""

    async def test_response_includes_request_id(self, client):
        """Responses should include an X-Request-ID header.

        This enables correlating client requests with server logs.
        """
        resp = await client.post("/computers", json={"uses": []})
        computer_id = resp.json().get("computer_id")
        try:
            assert "x-request-id" in resp.headers or "X-Request-Id" in resp.headers
        finally:
            if computer_id:
                await destroy_computer(client, computer_id)


# ---------------------------------------------------------------------------
# T11.5 — Error Response Format
# ---------------------------------------------------------------------------


class TestT115ErrorResponseFormat:
    """Verify error responses have consistent JSON structure."""

    async def test_404_returns_structured_error(self, client):
        """GET /computers/nonexistent should return structured JSON error.

        Expected format:
        {
            "error": "not_found",
            "message": "Computer not found",
            "computer_id": "nonexistent"
        }
        """
        resp = await client.get("/computers/nonexistent-computer-id/status")
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body or "detail" in body


# ---------------------------------------------------------------------------
# T11.6 — Health Check
# ---------------------------------------------------------------------------


class TestT116HealthCheck:
    """Verify the health check endpoint reports system status."""

    async def test_health_check_subsystems(self, client):
        """GET /health should report status of each subsystem.

        Expected subsystems: database, firecracker, storage, network
        """
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") in ("healthy", "ok")
        # Detailed subsystem checks:
        if "subsystems" in body:
            for sub in ["database", "firecracker", "storage"]:
                assert sub in body["subsystems"], (
                    f"Missing subsystem '{sub}' in health check"
                )


# ---------------------------------------------------------------------------
# T11.7 — Audit Log
# ---------------------------------------------------------------------------


class TestT117AuditLog:
    """Verify that security-relevant operations are audit-logged."""

    async def test_create_destroy_logged(self):
        """Create and destroy operations should appear in an audit log.

        The audit log should capture:
        - Who (API key / account)
        - What (operation: create, destroy, checkpoint, fork)
        - When (timestamp)
        - What resource (computer_id, checkpoint_id)
        """
        pass


# ---------------------------------------------------------------------------
# T11.8 — Checkpoint DAG Parent Pointers
# ---------------------------------------------------------------------------


class TestT118CheckpointDag:
    """Verify checkpoint list includes parent_id for lineage tracking."""

    async def test_fork_checkpoint_has_parent_id(self, long_client):
        """Create -> checkpoint A -> fork -> checkpoint B; B should have parent_id=A.

        The checkpoint DAG enables navigating the full history of forks
        and understanding which checkpoint a given state derived from.
        """
        computer_id = await create_computer(long_client, uses=[])
        checkpoint_a = None
        forked_id = None
        checkpoint_b = None
        try:
            # Write some state and checkpoint
            await exec_command(
                long_client, computer_id, "echo 'state_a' > /tmp/state.txt"
            )
            checkpoint_a = await checkpoint_computer(
                long_client, computer_id, label="checkpoint-A"
            )

            # Fork from checkpoint A
            forked_id = await fork_checkpoint(long_client, checkpoint_a)

            # Write new state in fork and checkpoint
            await exec_command(
                long_client, forked_id, "echo 'state_b' >> /tmp/state.txt"
            )
            checkpoint_b = await checkpoint_computer(
                long_client, forked_id, label="checkpoint-B"
            )

            # List checkpoints and verify parent lineage
            resp = await long_client.get("/checkpoints")
            resp.raise_for_status()
            checkpoints = resp.json()

            # Find checkpoint B
            cp_b = None
            for cp in checkpoints:
                if cp.get("checkpoint_id") == checkpoint_b:
                    cp_b = cp
                    break

            assert cp_b is not None, (
                f"Checkpoint B ({checkpoint_b}) not found in list"
            )
            assert cp_b.get("parent_id") is not None, (
                f"Checkpoint B should have a parent_id, got: {cp_b}"
            )

        finally:
            await destroy_computer(long_client, computer_id)
            if forked_id:
                await destroy_computer(long_client, forked_id)
            if checkpoint_a:
                await delete_checkpoint(long_client, checkpoint_a)
            if checkpoint_b:
                await delete_checkpoint(long_client, checkpoint_b)


# ---------------------------------------------------------------------------
# T11.9 — Resource Usage Per Computer
# ---------------------------------------------------------------------------


class TestT119ResourceUsage:
    """T11.5 — Verify computer_status returns accurate live VM metrics."""

    async def test_status_includes_resource_usage(self, client):
        """GET /computers/{id}/status should include CPU, memory, disk usage.

        Maps to spec T11.5: ram_usage_mb is sane, disk_usage_mb reflects reality,
        processes array has entries with PIDs and commands.
        """
        async with managed_computer(client, uses=[]) as computer_id:
            resp = await client.get(f"/computers/{computer_id}/status")
            resp.raise_for_status()
            body = resp.json()

            # Must have live metric fields
            assert "cpu_pct" in body, f"Missing cpu_pct: {body}"
            assert "ram_usage_mb" in body, f"Missing ram_usage_mb: {body}"
            assert "ram_total_mb" in body, f"Missing ram_total_mb: {body}"
            assert "disk_usage_mb" in body, f"Missing disk_usage_mb: {body}"
            assert "disk_total_mb" in body, f"Missing disk_total_mb: {body}"
            assert "processes" in body, f"Missing processes: {body}"

            # Sanity checks
            assert isinstance(body["cpu_pct"], (int, float))
            assert body["cpu_pct"] >= 0
            assert body["ram_usage_mb"] > 0, "RAM usage should not be zero"
            assert body["ram_total_mb"] > 0, "RAM total should not be zero"
            assert body["ram_usage_mb"] <= body["ram_total_mb"]
            assert body["disk_usage_mb"] >= 0
            assert body["disk_total_mb"] > 0

            # Processes should be a list with at least init
            assert isinstance(body["processes"], list)
            assert len(body["processes"]) >= 1, "Expected at least 1 process"
            proc = body["processes"][0]
            assert "pid" in proc
            assert "command" in proc

    async def test_disk_usage_reflects_writes(self, client):
        """Write data to disk, verify status reflects increased usage."""
        async with managed_computer(client, uses=[]) as computer_id:
            # Get baseline
            resp = await client.get(f"/computers/{computer_id}/status")
            resp.raise_for_status()
            baseline_disk = resp.json()["disk_usage_mb"]

            # Write ~50MB of data
            await exec_command(
                client, computer_id, "dd if=/dev/zero of=/tmp/bigfile bs=1M count=50"
            )

            # Check that disk usage increased
            resp = await client.get(f"/computers/{computer_id}/status")
            resp.raise_for_status()
            new_disk = resp.json()["disk_usage_mb"]
            assert new_disk > baseline_disk, (
                f"Disk usage didn't increase after write: {baseline_disk} -> {new_disk}"
            )


# ---------------------------------------------------------------------------
# T11.10 — computer_status vs exec consistency (T11.6)
# ---------------------------------------------------------------------------


class TestT1110StatusConsistency:
    """T11.6 — Verify computer_status metrics match exec output."""

    async def test_ram_matches_free(self, client):
        """computer_status ram_usage_mb should roughly match `free -m`."""
        async with managed_computer(client, uses=[]) as computer_id:
            # Get status
            resp = await client.get(f"/computers/{computer_id}/status")
            resp.raise_for_status()
            status_ram = resp.json()["ram_usage_mb"]

            # Get free -m output and parse used MB from "Mem: total used free ..."
            result = await exec_command(client, computer_id, "free -m")
            for line in result.stdout.splitlines():
                if line.startswith("Mem:"):
                    parts = line.split()
                    exec_ram = int(parts[2])
                    break
            else:
                pytest.fail(f"Could not parse free -m output: {result.stdout}")

            # Within 20% — both readings are point-in-time snapshots
            diff = abs(status_ram - exec_ram)
            tolerance = max(exec_ram * 0.2, 10)  # at least 10MB tolerance
            assert diff <= tolerance, (
                f"RAM mismatch: status={status_ram}MB, free={exec_ram}MB, diff={diff}MB"
            )

    async def test_disk_matches_df(self, client):
        """computer_status disk_usage_mb should roughly match `df`."""
        async with managed_computer(client, uses=[]) as computer_id:
            # Get status
            resp = await client.get(f"/computers/{computer_id}/status")
            resp.raise_for_status()
            status_disk = resp.json()["disk_usage_mb"]

            # Get df output
            result = await exec_command(client, computer_id, "df -BM /")
            for line in result.stdout.splitlines():
                if line.startswith("/"):
                    parts = line.split()
                    exec_disk = int(parts[2].rstrip("M"))
                    break
            else:
                pytest.fail(f"Could not parse df output: {result.stdout}")

            # Within 20%
            diff = abs(status_disk - exec_disk)
            tolerance = max(exec_disk * 0.2, 10)
            assert diff <= tolerance, (
                f"Disk mismatch: status={status_disk}MB, df={exec_disk}MB, diff={diff}MB"
            )


# ---------------------------------------------------------------------------
# T11.4 — Alerting (GET /alerts endpoint exists and returns structured data)
# ---------------------------------------------------------------------------


class TestT114Alerting:
    """T11.4 — Verify the alerting endpoint exists and returns structured data."""

    async def test_alerts_endpoint_exists(self, client):
        """GET /alerts should return a list (possibly empty on a healthy system)."""
        resp = await client.get("/alerts")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)

    async def test_alert_structure(self, client):
        """If any alerts exist, they should have the expected fields."""
        resp = await client.get("/alerts")
        resp.raise_for_status()
        alerts = resp.json()
        for alert in alerts:
            assert "level" in alert
            assert alert["level"] in ("warning", "critical")
            assert "source" in alert
            assert "message" in alert
            assert "value" in alert
            assert "threshold" in alert
            assert "timestamp" in alert
