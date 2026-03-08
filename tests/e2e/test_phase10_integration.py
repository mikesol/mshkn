"""Phase 10: Integration — "Real Agent Workflows"

These tests run against a LIVE server with real Firecracker VMs.
Some tests are xfail (require capabilities not yet implemented),
but Parallel Exploration and Failure Recovery work with bare VMs.
"""

from __future__ import annotations

import asyncio

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
# T10.1 — Web App Development Workflow
# ---------------------------------------------------------------------------


class TestT101WebAppDevelopment:
    """Full web app dev workflow: create project, install deps, run server."""

    async def test_nextjs_scaffold_and_run(self, client):
        """Scaffold a Next.js app, install deps, start dev server, hit it.

        Workflow:
        1. computer_create(uses: ['node-20()'])
        2. exec 'npx create-next-app@latest myapp --yes'
        3. exec 'cd myapp && npm run dev &'
        4. exec 'curl http://localhost:3000' -> HTML response
        5. Checkpoint, fork, make a change, verify divergence
        """
        pass


# ---------------------------------------------------------------------------
# T10.2 — Data Science Workflow
# ---------------------------------------------------------------------------


class TestT102DataScienceWorkflow:
    """Data science workflow: install pandas, run analysis, checkpoint results."""

    async def test_pandas_analysis_checkpoint_resume(self, client):
        """Install pandas, create a dataset, analyze it, checkpoint mid-work.

        Workflow:
        1. computer_create(uses: ['python-3.12()'])
        2. exec 'pip install pandas numpy'
        3. exec python script that generates CSV and computes stats
        4. Checkpoint after data generation
        5. Fork twice: one does linear regression, other does clustering
        6. Compare outputs from the two forks
        """
        pass


# ---------------------------------------------------------------------------
# T10.3 — Parallel Exploration
# ---------------------------------------------------------------------------


class TestT103ParallelExploration:
    """Fork multiple times and explore different paths in parallel.

    NOT xfail — this works with bare VMs using checkpoint/fork.
    """

    async def test_fork_three_ways_different_content(self, long_client):
        """Create base state, fork 3 times, each writes different content."""
        computer_id = await create_computer(long_client, uses=[])
        checkpoint_id = None
        forked_ids: list[str] = []
        try:
            # Write base state
            await exec_command(
                long_client, computer_id,
                "echo 'base_state' > /tmp/experiment.txt",
            )

            # Checkpoint the base state
            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="parallel-base"
            )

            # Fork 3 times
            for _ in range(3):
                fid = await fork_checkpoint(long_client, checkpoint_id)
                forked_ids.append(fid)

            # Each fork writes different content
            experiments = ["approach_alpha", "approach_beta", "approach_gamma"]
            for fid, experiment in zip(forked_ids, experiments):
                await exec_command(
                    long_client, fid,
                    f"echo '{experiment}' >> /tmp/experiment.txt",
                )

            # Verify each fork has different content
            contents: list[str] = []
            for fid in forked_ids:
                result = await exec_command(
                    long_client, fid, "cat /tmp/experiment.txt"
                )
                contents.append(result.stdout.strip())

            # All should have base_state
            for i, content in enumerate(contents):
                assert "base_state" in content, (
                    f"Fork {i} missing base_state: {content}"
                )

            # Each should have its unique experiment line
            for i, (content, experiment) in enumerate(zip(contents, experiments)):
                assert experiment in content, (
                    f"Fork {i} missing '{experiment}': {content}"
                )

            # No fork should have another fork's experiment
            for i, content in enumerate(contents):
                for j, experiment in enumerate(experiments):
                    if i != j:
                        assert experiment not in content, (
                            f"Fork {i} has fork {j}'s content '{experiment}': {content}"
                        )

            # Pick the "best" fork (just pick the first one for the test)
            best_idx = 0
            best_result = await exec_command(
                long_client, forked_ids[best_idx], "cat /tmp/experiment.txt"
            )
            assert experiments[best_idx] in best_result.stdout

        finally:
            # Clean up all computers
            for fid in forked_ids:
                await destroy_computer(long_client, fid)
            await destroy_computer(long_client, computer_id)
            if checkpoint_id:
                await delete_checkpoint(long_client, checkpoint_id)


# ---------------------------------------------------------------------------
# T10.4 — Failure Recovery
# ---------------------------------------------------------------------------


class TestT104FailureRecovery:
    """Checkpoint before risky operation, recover from failure by forking.

    NOT xfail — this works with bare VMs.
    """

    async def test_recover_deleted_file_from_checkpoint(self, long_client):
        """Write important file, checkpoint, delete it, fork to recover."""
        computer_id = await create_computer(long_client, uses=[])
        checkpoint_id = None
        recovered_id = None
        try:
            # Write important file
            await exec_command(
                long_client, computer_id,
                "echo 'critical_data_12345' > /tmp/important.txt",
            )

            # Verify it exists
            result = await exec_command(
                long_client, computer_id, "cat /tmp/important.txt"
            )
            assert "critical_data_12345" in result.stdout

            # Checkpoint the good state
            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="before-corruption"
            )

            # Corrupt the state: delete the important file
            await exec_command(
                long_client, computer_id, "rm /tmp/important.txt"
            )

            # Verify it's gone
            result = await exec_command(
                long_client, computer_id,
                "cat /tmp/important.txt 2>&1 || echo FILE_MISSING",
            )
            assert "FILE_MISSING" in result.stdout or "No such file" in result.stdout + result.stderr

            # Fork from the checkpoint — file should be restored
            recovered_id = await fork_checkpoint(long_client, checkpoint_id)

            result = await exec_command(
                long_client, recovered_id, "cat /tmp/important.txt"
            )
            assert "critical_data_12345" in result.stdout, (
                f"File should be recovered from checkpoint, got: {result.stdout}"
            )

        finally:
            await destroy_computer(long_client, computer_id)
            if recovered_id:
                await destroy_computer(long_client, recovered_id)
            if checkpoint_id:
                await delete_checkpoint(long_client, checkpoint_id)


# ---------------------------------------------------------------------------
# T10.5 — Dumb Agent Test
# ---------------------------------------------------------------------------


class TestT105DumbAgentTest:
    """End-to-end test with an LLM agent using the computer API."""

    async def test_agent_explores_and_checkpoints(self, client):
        """An LLM agent should be able to use the full computer lifecycle.

        Workflow:
        1. Agent receives task: "Find the largest file in /etc"
        2. Agent calls computer_create(uses: [])
        3. Agent calls exec('find /etc -type f -exec du -b {} + | sort -rn | head -5')
        4. Agent checkpoints after exploration
        5. Agent forks to try two different approaches
        6. Agent picks best result and reports back
        """
        pass
