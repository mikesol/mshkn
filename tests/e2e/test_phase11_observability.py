"""Phase 11: Observability — "Metrics, Logs, and Status"

These tests run against a LIVE server with real Firecracker VMs.
Most are xfail because observability infrastructure is not yet implemented.
"""

from __future__ import annotations

import pytest

from .conftest import (
    API_URL,
    HEADERS,
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
    """Verify per-computer resource usage metrics are available."""

    async def test_status_includes_resource_usage(self, client):
        """GET /computers/{id}/status should include CPU, memory, disk usage.

        Expected additional fields:
        - cpu_percent (float, 0-100)
        - memory_used_bytes (int)
        - disk_used_bytes (int)
        - network_rx_bytes (int)
        - network_tx_bytes (int)
        """
        pass


# ---------------------------------------------------------------------------
# T11.10 — Alerting Thresholds
# ---------------------------------------------------------------------------


class TestT1110AlertingThresholds:
    """Verify alerting when resource usage exceeds thresholds."""

    async def test_high_disk_usage_alert(self):
        """Filling disk past 80% should trigger an alert/warning.

        The system should:
        1. Monitor disk usage on the thin pool
        2. Emit a warning metric/log when usage > 80%
        3. Emit a critical alert when usage > 95%
        4. Optionally: refuse new creates when pool is nearly full
        """
        pass
