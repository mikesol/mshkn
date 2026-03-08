"""Phase 5: Concurrency and Limits — "How Many Can We Handle?"

These tests run against a LIVE server with real Firecracker VMs.
Concurrent tests need especially careful cleanup.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from .conftest import (
    LatencyStats,
    create_computer,
    checkpoint_computer,
    destroy_computer,
    exec_command,
    managed_computer,
    API_URL,
    HEADERS,
)


# ---------------------------------------------------------------------------
# T5.1 — 10 Concurrent Computers
# ---------------------------------------------------------------------------


class TestT51ConcurrentComputers:
    """Create, exec, and destroy 10 computers concurrently."""

    async def test_10_concurrent_create_exec_destroy(self, client):
        """Create 10 computers in parallel, exec on all, destroy all."""
        n = 10
        computer_ids: list[str] = []

        try:
            # Phase 1: Create all 10 concurrently
            t_create_start = time.perf_counter()
            create_tasks = [create_computer(client, uses=[]) for _ in range(n)]
            computer_ids = await asyncio.gather(*create_tasks)
            t_create_ms = (time.perf_counter() - t_create_start) * 1000
            assert len(computer_ids) == n, (
                f"Expected {n} computers, got {len(computer_ids)}"
            )

            # Phase 2: Exec "echo hello" on all 10 concurrently
            t_exec_start = time.perf_counter()
            exec_tasks = [
                exec_command(client, cid, "echo hello") for cid in computer_ids
            ]
            results = await asyncio.gather(*exec_tasks)
            t_exec_ms = (time.perf_counter() - t_exec_start) * 1000

            for i, result in enumerate(results):
                assert result.stdout.strip() == "hello", (
                    f"Computer {computer_ids[i]} returned {result.stdout!r} instead of 'hello'"
                )

            # Phase 3: Destroy all 10 concurrently
            t_destroy_start = time.perf_counter()
            destroy_tasks = [destroy_computer(client, cid) for cid in computer_ids]
            await asyncio.gather(*destroy_tasks)
            t_destroy_ms = (time.perf_counter() - t_destroy_start) * 1000

            # Mark as cleaned up so finally block doesn't double-destroy
            cleaned = computer_ids[:]
            computer_ids = []

            # Report timing
            print(
                f"\n--- T5.1 Timing Report ---\n"
                f"  Create {n} computers: {t_create_ms:.0f}ms\n"
                f"  Exec on {n} computers: {t_exec_ms:.0f}ms\n"
                f"  Destroy {n} computers: {t_destroy_ms:.0f}ms\n"
                f"  Total: {t_create_ms + t_exec_ms + t_destroy_ms:.0f}ms"
            )
        finally:
            # Cleanup any computers that weren't destroyed
            cleanup = [destroy_computer(client, cid) for cid in computer_ids]
            if cleanup:
                await asyncio.gather(*cleanup)


# ---------------------------------------------------------------------------
# T5.2 — Concurrent Checkpoints
# ---------------------------------------------------------------------------


class TestT52ConcurrentCheckpoints:
    """Checkpoint multiple computers concurrently."""

    async def test_3_concurrent_checkpoints(self, client):
        """Create 3 computers, checkpoint all 3 concurrently."""
        n = 3
        computer_ids: list[str] = []
        checkpoint_ids: list[str] = []

        try:
            # Create 3 computers sequentially (not the focus of this test)
            for i in range(n):
                cid = await create_computer(client, uses=[])
                computer_ids.append(cid)
                # Write some state so checkpoints are meaningful
                await exec_command(client, cid, f"echo 'state-{i}' > /tmp/state.txt")

            # Checkpoint all 3 concurrently
            t_start = time.perf_counter()
            checkpoint_tasks = [
                checkpoint_computer(client, cid, label=f"concurrent-{i}")
                for i, cid in enumerate(computer_ids)
            ]
            checkpoint_ids = list(await asyncio.gather(*checkpoint_tasks))
            t_ms = (time.perf_counter() - t_start) * 1000

            assert len(checkpoint_ids) == n
            for cpid in checkpoint_ids:
                assert cpid and isinstance(cpid, str)

            target_ms = 1000
            status = "PASS" if t_ms <= target_ms else "FAIL"
            print(
                f"\n--- T5.2 Timing Report ---\n"
                f"  {n} concurrent checkpoints: {t_ms:.0f}ms\n"
                f"  Target <= {target_ms}ms: {status}"
            )
        finally:
            cleanup = [destroy_computer(client, cid) for cid in computer_ids]
            if cleanup:
                await asyncio.gather(*cleanup)


# ---------------------------------------------------------------------------
# T5.3 — Concurrent Creates and Destroys
# ---------------------------------------------------------------------------


class TestT53ConcurrentCreatesAndDestroys:
    """Mix creates and destroys while other computers are running."""

    async def test_concurrent_create_and_destroy_with_running_vms(self, client):
        """While 3 computers are running, create 2 more and destroy 1 concurrently."""
        running_ids: list[str] = []
        new_ids: list[str] = []

        try:
            # Create 3 initial computers
            for _ in range(3):
                cid = await create_computer(client, uses=[])
                running_ids.append(cid)

            # Verify all 3 are alive
            for cid in running_ids:
                result = await exec_command(client, cid, "echo alive")
                assert result.stdout.strip() == "alive"

            # Concurrently: create 2 new + destroy 1 existing
            victim = running_ids[0]

            async def create_one() -> str:
                return await create_computer(client, uses=[])

            async def destroy_one(cid: str) -> None:
                resp = await client.delete(f"/computers/{cid}")
                resp.raise_for_status()
                assert resp.json().get("status") == "destroyed"

            results = await asyncio.gather(
                create_one(),
                create_one(),
                destroy_one(victim),
                return_exceptions=True,
            )

            # Process results
            running_ids.remove(victim)  # Victim was destroyed

            for r in results[:2]:
                if isinstance(r, str):
                    new_ids.append(r)
                elif isinstance(r, Exception):
                    raise r

            # Verify surviving original computers still work
            for cid in running_ids:
                result = await exec_command(client, cid, "echo still-alive")
                assert result.stdout.strip() == "still-alive"

            # Verify new computers work
            for cid in new_ids:
                result = await exec_command(client, cid, "echo new-and-alive")
                assert result.stdout.strip() == "new-and-alive"

            # Verify destroyed computer is really gone
            status_resp = await client.get(f"/computers/{victim}/status")
            assert status_resp.status_code in (404, 410, 400), (
                f"Destroyed computer {victim} should return 4xx, got {status_resp.status_code}"
            )
        finally:
            all_ids = running_ids + new_ids
            cleanup = [destroy_computer(client, cid) for cid in all_ids]
            if cleanup:
                await asyncio.gather(*cleanup)


# ---------------------------------------------------------------------------
# T5.4 — Memory Pressure
# ---------------------------------------------------------------------------


class TestT54MemoryPressure:
    """Create computers until memory limit is hit."""

    @pytest.mark.skip(reason="Dangerous: could OOM the host")
    async def test_memory_pressure(self, client):
        """Create computers until we run out of memory or hit a limit.

        This test is skipped to prevent accidental OOM.
        The approach: create computers in batches, checking host memory
        after each batch, and stop before hitting a dangerous threshold.
        """
        computer_ids: list[str] = []
        max_computers = 50  # Safety cap
        batch_size = 5

        try:
            for batch in range(0, max_computers, batch_size):
                tasks = [create_computer(client, uses=[]) for _ in range(batch_size)]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                new_ids = [r for r in results if isinstance(r, str)]
                computer_ids.extend(new_ids)

                errors = [r for r in results if isinstance(r, Exception)]
                if errors:
                    print(
                        f"Batch {batch // batch_size}: {len(new_ids)} created, "
                        f"{len(errors)} failed. Stopping."
                    )
                    break

                # Quick health check on the latest computer
                if new_ids:
                    result = await exec_command(client, new_ids[-1], "free -m | head -2")
                    print(f"Batch {batch // batch_size}: {len(computer_ids)} total. {result.stdout}")

            print(f"Created {len(computer_ids)} computers before stopping.")
            assert len(computer_ids) > 0
        finally:
            cleanup = [destroy_computer(client, cid) for cid in computer_ids]
            if cleanup:
                await asyncio.gather(*cleanup)


# ---------------------------------------------------------------------------
# T5.5 — NVMe Pressure
# ---------------------------------------------------------------------------


class TestT55NvmePressure:
    """Create computers and fill disk inside each."""

    @pytest.mark.skip(reason="Dangerous: could fill production disk")
    async def test_nvme_pressure(self, client):
        """Write large files inside VMs to stress the thin pool.

        Skipped to prevent accidental disk exhaustion. The test creates
        a few computers and writes data, checking thin pool usage.
        """
        computer_ids: list[str] = []
        n = 5

        try:
            for i in range(n):
                cid = await create_computer(client, uses=[])
                computer_ids.append(cid)

                # Write 100MB of data inside the VM
                await exec_command(
                    client,
                    cid,
                    "dd if=/dev/urandom of=/tmp/bigfile bs=1M count=100 2>&1",
                    timeout=60.0,
                )

                # Check disk usage
                result = await exec_command(client, cid, "df -h /")
                print(f"VM {i}: {result.stdout}")

            # All VMs should still be functional
            for cid in computer_ids:
                result = await exec_command(client, cid, "echo ok")
                assert result.stdout.strip() == "ok"
        finally:
            cleanup = [destroy_computer(client, cid) for cid in computer_ids]
            if cleanup:
                await asyncio.gather(*cleanup)


# ---------------------------------------------------------------------------
# T5.6 — Resource Allocation
# ---------------------------------------------------------------------------


class TestT56ResourceAllocation:
    """Request specific resource amounts for a computer."""

    async def test_custom_ram_allocation(self, client):
        """Create a computer with 8GB RAM, verify it has ~8GB."""
        resp = await client.post(
            "/computers",
            json={"uses": [], "needs": {"ram": "8GB"}},
        )
        resp.raise_for_status()
        computer_id = resp.json()["computer_id"]

        try:
            result = await exec_command(client, computer_id, "free -m")
            # Parse total memory from free output
            lines = result.stdout.strip().splitlines()
            mem_line = [l for l in lines if l.startswith("Mem:")]
            assert mem_line, f"Could not find Mem: line in free output: {result.stdout}"

            total_mb = int(mem_line[0].split()[1])
            # 8GB = 8192MB, allow some overhead margin (7500-8500)
            assert 7500 <= total_mb <= 8500, (
                f"Expected ~8192MB RAM, got {total_mb}MB"
            )
        finally:
            await destroy_computer(client, computer_id)


# ---------------------------------------------------------------------------
# T5.7 — Per-Account VM Limit
# ---------------------------------------------------------------------------


class TestT57PerAccountVmLimit:
    """Enforce a maximum number of VMs per account."""

    async def test_vm_limit_enforced(self, client):
        """Creating more VMs than the per-account limit should be rejected."""
        computer_ids: list[str] = []
        limit = 20  # Expected per-account limit

        try:
            for i in range(limit + 5):
                try:
                    cid = await create_computer(client, uses=[])
                    computer_ids.append(cid)
                except httpx.HTTPStatusError as e:
                    # Should get 429 or 403 when limit is exceeded
                    assert e.response.status_code in (429, 403, 400), (
                        f"Expected rate limit error, got {e.response.status_code}"
                    )
                    assert len(computer_ids) <= limit, (
                        f"Limit should have been hit at {limit}, "
                        f"but created {len(computer_ids)} before rejection"
                    )
                    print(f"Limit enforced after {len(computer_ids)} computers")
                    return

            pytest.fail(
                f"Created {len(computer_ids)} computers without hitting any limit"
            )
        finally:
            cleanup = [destroy_computer(client, cid) for cid in computer_ids]
            if cleanup:
                await asyncio.gather(*cleanup)


# ---------------------------------------------------------------------------
# T5.8 — Idle Timeout
# ---------------------------------------------------------------------------


class TestT58IdleTimeout:
    """Computers should be automatically destroyed after an idle period."""

    async def test_idle_computer_destroyed(self, client):
        """A computer left idle should be auto-destroyed after the timeout."""
        computer_id = await create_computer(client, uses=[])

        try:
            # Verify it's alive
            result = await exec_command(client, computer_id, "echo alive")
            assert result.stdout.strip() == "alive"

            # Wait for the idle timeout (assumed ~60s for testing)
            idle_timeout_s = 65
            print(f"Waiting {idle_timeout_s}s for idle timeout...")
            await asyncio.sleep(idle_timeout_s)

            # Check that the computer was auto-destroyed
            status_resp = await client.get(f"/computers/{computer_id}/status")
            assert status_resp.status_code in (404, 410), (
                f"Expected computer to be auto-destroyed after idle timeout, "
                f"but got status {status_resp.status_code}: {status_resp.text}"
            )
            # If it was destroyed, no cleanup needed
            computer_id = ""
        finally:
            if computer_id:
                await destroy_computer(client, computer_id)


# ---------------------------------------------------------------------------
# T5.9 — API Rate Limiting
# ---------------------------------------------------------------------------


class TestT59ApiRateLimiting:
    """The API should enforce rate limits on requests."""

    async def test_rapid_requests_throttled(self, client):
        """Sending many rapid requests should eventually get rate-limited."""
        computer_id = await create_computer(client, uses=[])

        try:
            throttled = False
            num_requests = 100

            # Fire rapid exec requests
            async def rapid_exec(i: int) -> int:
                try:
                    resp = await client.post(
                        f"/computers/{computer_id}/exec",
                        json={"command": f"echo {i}"},
                    )
                    return resp.status_code
                except httpx.HTTPStatusError as e:
                    return e.response.status_code

            tasks = [rapid_exec(i) for i in range(num_requests)]
            status_codes = await asyncio.gather(*tasks)

            rate_limited = [s for s in status_codes if s == 429]
            if rate_limited:
                throttled = True
                print(
                    f"Rate limited: {len(rate_limited)}/{num_requests} "
                    f"requests returned 429"
                )

            assert throttled, (
                f"Expected some 429 responses from {num_requests} rapid requests, "
                f"but all returned: {set(status_codes)}"
            )
        finally:
            await destroy_computer(client, computer_id)
