"""Phase 1: Latency benchmarks — "Show Me the Stopwatch"

These tests run against a LIVE server with real Firecracker VMs.
Every latency claim in the design doc, measured properly.
"""

from __future__ import annotations

import time

from .conftest import (
    LatencyStats,
    checkpoint_computer,
    create_computer,
    create_recipe,
    destroy_computer,
    exec_command,
    fork_checkpoint,
    managed_computer,
)

BARE_CREATE_SAMPLES = 20
BARE_CREATE_P95_MS = 1600  # L3 miss = two-phase boot (~1500ms)
RECIPE_CREATE_SAMPLES = 10
RECIPE_CREATE_P95_MS = 1600  # L3 miss with recipe base volume
EMPTY_CHECKPOINT_SAMPLES = 10
EMPTY_CHECKPOINT_P95_MS = 1150
SMALL_STATE_CHECKPOINT_SAMPLES = 10
SMALL_STATE_CHECKPOINT_P95_MS = 1000
MANY_SMALL_FILES_CHECKPOINT_SAMPLES = 10
MANY_SMALL_FILES_CHECKPOINT_P95_MS = 1550
RESUME_SAMPLES = 10
RESUME_P95_MS = 650  # LOAD_SNAPSHOT path: tap+FC+SSH+reconfig
FORK_MINIMAL_SAMPLES = 10
FORK_MINIMAL_P95_MS = 650  # LOAD_SNAPSHOT path: tap+FC+SSH+reconfig

# ---------------------------------------------------------------------------
# T1.1 — Create Latency (Target: <= 2s)
# ---------------------------------------------------------------------------


class TestT11CreateLatency:
    """computer_create(uses: []) latency — target p95 <= 2000ms."""

    async def test_bare_create_latency(self, client):
        """Create bare computers repeatedly, assert a tight p95 latency target."""
        timings: list[float] = []

        for i in range(BARE_CREATE_SAMPLES):
            computer_id: str | None = None
            try:
                start = time.perf_counter()
                computer_id = await create_computer(client, uses=[])
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                print(f"  create #{i+1}: {elapsed_ms:.0f}ms")
            finally:
                if computer_id is not None:
                    await destroy_computer(client, computer_id)

        stats = LatencyStats(values_ms=timings)
        print(stats.report("T1.1 Bare Create", target_ms=BARE_CREATE_P95_MS))
        assert stats.p95 <= BARE_CREATE_P95_MS, (
            f"p95 create latency {stats.p95:.0f}ms exceeds {BARE_CREATE_P95_MS}ms target"
        )

    async def test_recipe_create_latency(self, long_client):
        """Create computers from a pre-built recipe, assert a tight p95 target."""
        # Build recipe first (not timed)
        recipe_id = await create_recipe(
            long_client,
            "FROM mshkn-base\nRUN apt-get update && apt-get install -y python3",
        )

        timings: list[float] = []
        for _i in range(RECIPE_CREATE_SAMPLES):
            computer_id: str | None = None
            try:
                start = time.perf_counter()
                computer_id = await create_computer(
                    long_client, recipe_id=recipe_id
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
            finally:
                if computer_id is not None:
                    await destroy_computer(long_client, computer_id)

        stats = LatencyStats(values_ms=timings)
        print(
            stats.report(
                "T1.1 Recipe Create",
                target_ms=RECIPE_CREATE_P95_MS,
            )
        )
        assert stats.p95 <= RECIPE_CREATE_P95_MS, (
            f"p95 recipe create latency {stats.p95:.0f}ms exceeds "
            f"{RECIPE_CREATE_P95_MS}ms target"
        )


# ---------------------------------------------------------------------------
# T1.6 — Warm L3 Cache Create Latency
# ---------------------------------------------------------------------------

WARM_L3_CREATE_SAMPLES = 10
WARM_L3_CREATE_P95_MS = 650  # LOAD_SNAPSHOT path: tap+FC+SSH+reconfig+caddy


class TestT16WarmL3CreateLatency:
    """Create with warm L3 cache — should be LOAD_SNAPSHOT fast."""

    async def test_warm_l3_cache_create_latency(self, client):
        """First create warms L3 cache, subsequent creates should be fast."""
        # Warm the L3 cache with a throwaway create
        warmup_id = await create_computer(client, uses=[])
        await destroy_computer(client, warmup_id)

        # Now measure with warm L3 cache
        timings: list[float] = []
        for i in range(WARM_L3_CREATE_SAMPLES):
            computer_id: str | None = None
            try:
                start = time.perf_counter()
                computer_id = await create_computer(client, uses=[])
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                print(f"  warm L3 create #{i+1}: {elapsed_ms:.0f}ms")
            finally:
                if computer_id is not None:
                    await destroy_computer(client, computer_id)

        stats = LatencyStats(values_ms=timings)
        print(stats.report("T1.6 Warm L3 Cache Create", target_ms=WARM_L3_CREATE_P95_MS))
        assert stats.p95 <= WARM_L3_CREATE_P95_MS, (
            f"p95 warm L3 create latency {stats.p95:.0f}ms exceeds {WARM_L3_CREATE_P95_MS}ms"
        )


# ---------------------------------------------------------------------------
# T1.2 — Checkpoint Latency (Target: <= 1s)
# ---------------------------------------------------------------------------


class TestT12CheckpointLatency:
    """Checkpoint latency under various state sizes — target p95 <= 1000ms."""

    async def test_empty_state_checkpoint(self, long_client):
        """Checkpoint immediately after create, assert a tight p95 target."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            timings: list[float] = []

            for i in range(EMPTY_CHECKPOINT_SAMPLES):
                start = time.perf_counter()
                await checkpoint_computer(
                    long_client, computer_id, label=f"empty-{i}"
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                print(f"  empty checkpoint #{i+1}: {elapsed_ms:.0f}ms")

            stats = LatencyStats(values_ms=timings)
            print(
                stats.report(
                    "T1.2 Empty State Checkpoint",
                    target_ms=EMPTY_CHECKPOINT_P95_MS,
                )
            )
            assert stats.p95 <= EMPTY_CHECKPOINT_P95_MS, (
                f"p95 empty checkpoint latency {stats.p95:.0f}ms exceeds "
                f"{EMPTY_CHECKPOINT_P95_MS}ms"
            )

    async def test_small_state_checkpoint(self, long_client):
        """Write 1MB file, then checkpoint repeatedly with a p95 target."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            timings: list[float] = []
            for i in range(SMALL_STATE_CHECKPOINT_SAMPLES):
                await exec_command(
                    long_client,
                    computer_id,
                    f"dd if=/dev/urandom of=/tmp/data_1mb_{i} bs=1M count=1 2>/dev/null",
                )

                start = time.perf_counter()
                await checkpoint_computer(
                    long_client, computer_id, label=f"small-1mb-{i}"
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                print(f"  small checkpoint #{i+1}: {elapsed_ms:.0f}ms")

            stats = LatencyStats(values_ms=timings)
            print(
                stats.report(
                    "T1.2 Small State Checkpoint",
                    target_ms=SMALL_STATE_CHECKPOINT_P95_MS,
                )
            )
            assert stats.p95 <= SMALL_STATE_CHECKPOINT_P95_MS, (
                f"p95 small-state checkpoint latency {stats.p95:.0f}ms exceeds "
                f"{SMALL_STATE_CHECKPOINT_P95_MS}ms"
            )

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
        """Write many small files, then checkpoint repeatedly with a p95 target."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            timings: list[float] = []
            for i in range(MANY_SMALL_FILES_CHECKPOINT_SAMPLES):
                await exec_command(
                    long_client,
                    computer_id,
                    (
                        f"mkdir -p /tmp/small_files_{i} && "
                        "for j in $(seq 1 1000); do "
                        f"  dd if=/dev/urandom of=/tmp/small_files_{i}/file_$j "
                        "bs=1K count=1 2>/dev/null; "
                        "done"
                    ),
                    timeout=60.0,
                )

                start = time.perf_counter()
                await checkpoint_computer(
                    long_client, computer_id, label=f"many-small-{i}"
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)
                print(f"  many-small checkpoint #{i+1}: {elapsed_ms:.0f}ms")

            stats = LatencyStats(values_ms=timings)
            print(
                stats.report(
                    "T1.2 Many Small Files Checkpoint",
                    target_ms=MANY_SMALL_FILES_CHECKPOINT_P95_MS,
                )
            )
            assert stats.p95 <= MANY_SMALL_FILES_CHECKPOINT_P95_MS, (
                f"p95 many-small-files checkpoint latency {stats.p95:.0f}ms exceeds "
                f"{MANY_SMALL_FILES_CHECKPOINT_P95_MS}ms"
            )


# ---------------------------------------------------------------------------
# T1.3 — Resume/Restore Latency (Target: <= 2s)
# ---------------------------------------------------------------------------


class TestT13ResumeLatency:
    """Resume/restore latency — currently uses cold boot, not snapshot restore."""

    async def test_resume_latency(self, long_client):
        """Resume via fork repeatedly and assert a tight p95 target."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="resume-test"
            )

            timings: list[float] = []
            forked_ids: list[str] = []
            try:
                for i in range(RESUME_SAMPLES):
                    start = time.perf_counter()
                    forked_id = await fork_checkpoint(long_client, checkpoint_id)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    timings.append(elapsed_ms)
                    forked_ids.append(forked_id)
                    print(f"  resume #{i+1}: {elapsed_ms:.0f}ms")
            finally:
                for fid in forked_ids:
                    await destroy_computer(long_client, fid)

            stats = LatencyStats(values_ms=timings)
            print(stats.report("T1.3 Resume Latency", target_ms=RESUME_P95_MS))
            assert stats.p95 <= RESUME_P95_MS, (
                f"p95 resume latency {stats.p95:.0f}ms exceeds {RESUME_P95_MS}ms"
            )


# ---------------------------------------------------------------------------
# T1.4 — Fork Latency (Target: <= 2s, "O(1)")
# ---------------------------------------------------------------------------


class TestT14ForkLatency:
    """Fork latency — target p95 <= 2000ms, should be O(1) w.r.t. state size."""

    async def test_fork_minimal_state(self, long_client):
        """Fork from checkpoint repeatedly and assert a tight p95 target."""
        forked_ids: list[str] = []

        async with managed_computer(long_client, uses=[]) as computer_id:
            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="fork-minimal"
            )

            timings: list[float] = []
            try:
                for i in range(FORK_MINIMAL_SAMPLES):
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
            print(stats.report("T1.4 Fork Minimal State", target_ms=FORK_MINIMAL_P95_MS))
            assert stats.p95 <= FORK_MINIMAL_P95_MS, (
                f"p95 fork latency {stats.p95:.0f}ms exceeds {FORK_MINIMAL_P95_MS}ms target"
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
# T1.7 — Fork Snapshot Restore Latency
# ---------------------------------------------------------------------------

FORK_RESTORE_SAMPLES = 10
FORK_RESTORE_P95_MS = 650  # LOAD_SNAPSHOT fork: tap+FC+SSH+reconfig+caddy


class TestT17ForkRestoreLatency:
    """Fork via LOAD_SNAPSHOT — verify state preservation + latency."""

    async def test_fork_snapshot_restore_latency(self, long_client):
        """Fork from checkpoint, verify state is preserved, assert latency."""
        async with managed_computer(long_client, uses=[]) as computer_id:
            # Write a marker file
            await exec_command(
                long_client, computer_id,
                "echo 'snapshot-restore-test' > /tmp/marker.txt"
            )

            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="restore-test"
            )

            timings: list[float] = []
            forked_ids: list[str] = []
            try:
                for i in range(FORK_RESTORE_SAMPLES):
                    start = time.perf_counter()
                    forked_id = await fork_checkpoint(long_client, checkpoint_id)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    timings.append(elapsed_ms)
                    forked_ids.append(forked_id)
                    print(f"  fork restore #{i+1}: {elapsed_ms:.0f}ms")

                # Verify state on last forked VM
                result = await exec_command(
                    long_client, forked_ids[-1], "cat /tmp/marker.txt"
                )
                assert result.stdout.strip() == "snapshot-restore-test", (
                    f"State not preserved: got {result.stdout.strip()!r}"
                )
            finally:
                for fid in forked_ids:
                    await destroy_computer(long_client, fid)

            stats = LatencyStats(values_ms=timings)
            print(stats.report("T1.7 Fork Restore Latency", target_ms=FORK_RESTORE_P95_MS))
            assert stats.p95 <= FORK_RESTORE_P95_MS, (
                f"p95 fork restore latency {stats.p95:.0f}ms exceeds {FORK_RESTORE_P95_MS}ms"
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
                resp = await long_client.post(
                    f"/checkpoints/{ckpt}/merge",
                    json={"checkpoint_a": ckpt_a, "checkpoint_b": ckpt_b},
                )
                resp.raise_for_status()
                elapsed_ms = (time.perf_counter() - start) * 1000

                print(f"T1.5 Merge: {elapsed_ms:.0f}ms")
            finally:
                await destroy_computer(long_client, fork_a)
                await destroy_computer(long_client, fork_b)
