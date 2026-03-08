"""Phase 1: Latency benchmarks — "Show Me the Stopwatch"

These tests run against a LIVE server with real Firecracker VMs.
Every latency claim in the design doc, measured properly.
"""

from __future__ import annotations

import time

import pytest

from .conftest import (
    LatencyStats,
    checkpoint_computer,
    create_computer,
    destroy_computer,
    exec_command,
    fork_checkpoint,
    managed_computer,
)


# ---------------------------------------------------------------------------
# T1.1 — Create Latency (Target: <= 2s)
# ---------------------------------------------------------------------------


class TestT11CreateLatency:
    """computer_create(uses: []) latency — target p95 <= 2000ms."""

    async def test_bare_create_latency(self, client):
        """Create 10 bare computers, measure each, assert p95 <= 2000ms."""
        timings: list[float] = []
        created_ids: list[str] = []

        try:
            for i in range(10):
                start = time.perf_counter()
                computer_id = await create_computer(client, uses=[])
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                created_ids.append(computer_id)
                print(f"  create #{i+1}: {elapsed_ms:.0f}ms")
        finally:
            for cid in created_ids:
                await destroy_computer(client, cid)

        stats = LatencyStats(values_ms=timings)
        print(stats.report("T1.1 Bare Create", target_ms=2000))
        assert stats.p95 <= 2000, (
            f"p95 create latency {stats.p95:.0f}ms exceeds 2000ms target"
        )

    async def test_warm_cache_capability_create_latency(self, client):
        """Create with capabilities (warm cache) — not yet implemented."""
        timings: list[float] = []
        created_ids: list[str] = []

        try:
            for i in range(5):
                start = time.perf_counter()
                computer_id = await create_computer(
                    client, uses=["python-3.12(numpy)"]
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                created_ids.append(computer_id)
        finally:
            for cid in created_ids:
                await destroy_computer(client, cid)

        stats = LatencyStats(values_ms=timings)
        print(stats.report("T1.1 Warm Cache Capability Create", target_ms=2000))
        assert stats.p95 <= 2000

    async def test_cold_cache_capability_create_latency(self, client):
        """Create with capabilities (cold cache) — not yet implemented."""
        timings: list[float] = []
        created_ids: list[str] = []

        try:
            for i in range(5):
                start = time.perf_counter()
                computer_id = await create_computer(
                    client, uses=["python-3.12(numpy)"]
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                created_ids.append(computer_id)
        finally:
            for cid in created_ids:
                await destroy_computer(client, cid)

        stats = LatencyStats(values_ms=timings)
        print(stats.report("T1.1 Cold Cache Capability Create"))


# ---------------------------------------------------------------------------
# T1.2 — Checkpoint Latency (Target: <= 1s)
# ---------------------------------------------------------------------------


class TestT12CheckpointLatency:
    """Checkpoint latency under various state sizes — target p95 <= 1000ms."""

    async def test_empty_state_checkpoint(self, long_client):
        """Checkpoint immediately after create (empty state) x5."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            timings: list[float] = []

            for i in range(5):
                start = time.perf_counter()
                await checkpoint_computer(
                    long_client, computer_id, label=f"empty-{i}"
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                print(f"  empty checkpoint #{i+1}: {elapsed_ms:.0f}ms")

            stats = LatencyStats(values_ms=timings)
            print(stats.report("T1.2 Empty State Checkpoint", target_ms=1000))
            assert stats.p95 <= 1000, (
                f"p95 empty checkpoint latency {stats.p95:.0f}ms exceeds 1000ms"
            )

    async def test_small_state_checkpoint(self, long_client):
        """Write 1MB file, then checkpoint. Measure latency."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            # Write 1MB of data
            await exec_command(
                long_client,
                computer_id,
                "dd if=/dev/urandom of=/tmp/data_1mb bs=1M count=1 2>/dev/null",
            )

            start = time.perf_counter()
            await checkpoint_computer(
                long_client, computer_id, label="small-1mb"
            )
            elapsed_ms = (time.perf_counter() - start) * 1000

            print(f"T1.2 Small State (1MB) Checkpoint: {elapsed_ms:.0f}ms")
            stats = LatencyStats(values_ms=[elapsed_ms])
            print(stats.report("T1.2 Small State Checkpoint", target_ms=1000))

    async def test_large_state_checkpoint(self, long_client):
        """Write 100MB file, then checkpoint. Measure honestly (may exceed 1s)."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            # Write 100MB of data
            await exec_command(
                long_client,
                computer_id,
                "dd if=/dev/urandom of=/tmp/data_100mb bs=1M count=100 2>/dev/null",
                timeout=60.0,
            )

            start = time.perf_counter()
            await checkpoint_computer(
                long_client, computer_id, label="large-100mb"
            )
            elapsed_ms = (time.perf_counter() - start) * 1000

            print(f"T1.2 Large State (100MB) Checkpoint: {elapsed_ms:.0f}ms")
            stats = LatencyStats(values_ms=[elapsed_ms])
            print(stats.report("T1.2 Large State Checkpoint", target_ms=1000))

    async def test_many_small_files_checkpoint(self, long_client):
        """Write 1000 x 1KB files, then checkpoint. Measure."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            # Create 1000 small files
            await exec_command(
                long_client,
                computer_id,
                (
                    "mkdir -p /tmp/small_files && "
                    "for i in $(seq 1 1000); do "
                    "  dd if=/dev/urandom of=/tmp/small_files/file_$i bs=1K count=1 2>/dev/null; "
                    "done"
                ),
                timeout=60.0,
            )

            start = time.perf_counter()
            await checkpoint_computer(
                long_client, computer_id, label="many-small"
            )
            elapsed_ms = (time.perf_counter() - start) * 1000

            print(f"T1.2 Many Small Files (1000x1KB) Checkpoint: {elapsed_ms:.0f}ms")
            stats = LatencyStats(values_ms=[elapsed_ms])
            print(stats.report("T1.2 Many Small Files Checkpoint", target_ms=1000))


# ---------------------------------------------------------------------------
# T1.3 — Resume/Restore Latency (Target: <= 2s)
# ---------------------------------------------------------------------------


class TestT13ResumeLatency:
    """Resume/restore latency — currently uses cold boot, not snapshot restore."""

    async def test_resume_latency(self, long_client):
        """Resume from a checkpoint — not yet a distinct operation."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="resume-test"
            )

            start = time.perf_counter()
            # If a resume endpoint existed, we'd call it here.
            # For now, fork is the closest thing.
            forked_id = await fork_checkpoint(long_client, checkpoint_id)
            elapsed_ms = (time.perf_counter() - start) * 1000

            await destroy_computer(long_client, forked_id)

            print(f"T1.3 Resume (via fork): {elapsed_ms:.0f}ms")
            stats = LatencyStats(values_ms=[elapsed_ms])
            print(stats.report("T1.3 Resume Latency", target_ms=2000))
            assert stats.p95 <= 2000


# ---------------------------------------------------------------------------
# T1.4 — Fork Latency (Target: <= 2s, "O(1)")
# ---------------------------------------------------------------------------


class TestT14ForkLatency:
    """Fork latency — target p95 <= 2000ms, should be O(1) w.r.t. state size."""

    async def test_fork_minimal_state(self, long_client):
        """Fork from checkpoint with minimal state x5, assert p95 <= 2000ms."""
        forked_ids: list[str] = []

        async with managed_computer(long_client, uses=[]) as computer_id:
            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="fork-minimal"
            )

            timings: list[float] = []
            try:
                for i in range(5):
                    start = time.perf_counter()
                    forked_id = await fork_checkpoint(long_client, checkpoint_id)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    timings.append(elapsed_ms)
                    forked_ids.append(forked_id)
                    print(f"  fork #{i+1}: {elapsed_ms:.0f}ms")
            finally:
                for fid in forked_ids:
                    await destroy_computer(long_client, fid)

            stats = LatencyStats(values_ms=timings)
            print(stats.report("T1.4 Fork Minimal State", target_ms=2000))
            assert stats.p95 <= 2000, (
                f"p95 fork latency {stats.p95:.0f}ms exceeds 2000ms target"
            )

    async def test_fork_o1_comparison(self, long_client):
        """Fork from 1MB state vs 50MB state — compare times to check O(1) claim.

        We don't assert statistical indistinguishability (too few samples),
        but we report both so the human can eyeball it.
        """
        small_timings: list[float] = []
        large_timings: list[float] = []
        cleanup_ids: list[str] = []

        try:
            # --- Small state (1MB) ---
            small_comp = await create_computer(long_client, uses=[])
            cleanup_ids.append(small_comp)

            await exec_command(
                long_client,
                small_comp,
                "dd if=/dev/urandom of=/tmp/data_1mb bs=1M count=1 2>/dev/null",
            )
            small_ckpt = await checkpoint_computer(
                long_client, small_comp, label="fork-small"
            )

            for i in range(3):
                start = time.perf_counter()
                fid = await fork_checkpoint(long_client, small_ckpt)
                elapsed_ms = (time.perf_counter() - start) * 1000
                small_timings.append(elapsed_ms)
                cleanup_ids.append(fid)
                print(f"  fork small #{i+1}: {elapsed_ms:.0f}ms")

            # --- Large state (50MB) ---
            large_comp = await create_computer(long_client, uses=[])
            cleanup_ids.append(large_comp)

            await exec_command(
                long_client,
                large_comp,
                "dd if=/dev/urandom of=/tmp/data_50mb bs=1M count=50 2>/dev/null",
                timeout=60.0,
            )
            large_ckpt = await checkpoint_computer(
                long_client, large_comp, label="fork-large"
            )

            for i in range(3):
                start = time.perf_counter()
                fid = await fork_checkpoint(long_client, large_ckpt)
                elapsed_ms = (time.perf_counter() - start) * 1000
                large_timings.append(elapsed_ms)
                cleanup_ids.append(fid)
                print(f"  fork large #{i+1}: {elapsed_ms:.0f}ms")

        finally:
            for cid in cleanup_ids:
                await destroy_computer(long_client, cid)

        small_stats = LatencyStats(values_ms=small_timings)
        large_stats = LatencyStats(values_ms=large_timings)

        print(small_stats.report("T1.4 Fork 1MB State"))
        print(large_stats.report("T1.4 Fork 50MB State"))
        print(
            f"  O(1) check: small_mean={small_stats.mean:.0f}ms, "
            f"large_mean={large_stats.mean:.0f}ms, "
            f"ratio={large_stats.mean / small_stats.mean:.2f}x"
            if small_stats.mean > 0
            else "  O(1) check: small_mean=0ms (too fast to measure)"
        )


# ---------------------------------------------------------------------------
# T1.5 — Merge Latency
# ---------------------------------------------------------------------------


class TestT15MergeLatency:
    """Merge latency — not yet implemented."""

    async def test_merge_latency(self, long_client):
        """Merge two forks — not yet implemented."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            ckpt = await checkpoint_computer(
                long_client, computer_id, label="merge-base"
            )
            fork_a = await fork_checkpoint(long_client, ckpt)
            fork_b = await fork_checkpoint(long_client, ckpt)

            try:
                ckpt_a = await checkpoint_computer(
                    long_client, fork_a, label="merge-a"
                )
                ckpt_b = await checkpoint_computer(
                    long_client, fork_b, label="merge-b"
                )

                start = time.perf_counter()
                # Merge endpoint doesn't exist yet
                resp = await long_client.post(
                    f"/checkpoints/{ckpt_a}/merge",
                    json={"other_checkpoint_id": ckpt_b},
                )
                resp.raise_for_status()
                elapsed_ms = (time.perf_counter() - start) * 1000

                print(f"T1.5 Merge: {elapsed_ms:.0f}ms")
            finally:
                await destroy_computer(long_client, fork_a)
                await destroy_computer(long_client, fork_b)
