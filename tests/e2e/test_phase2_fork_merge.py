"""Phase 2: Fork & Merge E2E tests.

These tests run against a LIVE server with real Firecracker VMs.
Every computer costs real resources — clean up in finally blocks.
"""

from __future__ import annotations

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
# T2.1 — Fork Isolation
# ---------------------------------------------------------------------------


async def test_fork_isolation(long_client: httpx.AsyncClient) -> None:
    """Forks from the same checkpoint see the checkpoint state, not each other's writes."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        # Create original computer, write a file
        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)

        await exec_command(
            long_client, comp_origin, 'echo -n "A" > /root/original.txt'
        )

        # Checkpoint
        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="base")
        checkpoints.append(ckpt_base)

        # Fork A: overwrite file with "B"
        comp_a = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_a)

        await exec_command(long_client, comp_a, 'echo -n "B" > /root/original.txt')

        result_a = await exec_command(long_client, comp_a, "cat /root/original.txt")
        assert result_a.stdout.strip() == "B", f"Fork A should see 'B', got {result_a.stdout!r}"

        # Fork B: must still see "A" (isolated from Fork A's write)
        comp_b = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_b)

        result_b = await exec_command(long_client, comp_b, "cat /root/original.txt")
        assert result_b.stdout.strip() == "A", (
            f"Fork B should see 'A' (from checkpoint), got {result_b.stdout!r}"
        )

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.2 — Merge: Non-Overlapping Files
# ---------------------------------------------------------------------------


async def test_merge_non_overlapping_files(long_client: httpx.AsyncClient) -> None:
    """Merge two forks where each created a different file — merged result has both."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        # Common base
        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)

        await exec_command(long_client, comp_origin, "mkdir -p /data")

        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="base")
        checkpoints.append(ckpt_base)

        # Fork A: create /data/a.txt
        comp_a = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_a)
        await exec_command(long_client, comp_a, 'echo -n "from_a" > /data/a.txt')
        ckpt_a = await checkpoint_computer(long_client, comp_a, label="fork_a")
        checkpoints.append(ckpt_a)

        # Fork B: create /data/b.txt
        comp_b = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_b)
        await exec_command(long_client, comp_b, 'echo -n "from_b" > /data/b.txt')
        ckpt_b = await checkpoint_computer(long_client, comp_b, label="fork_b")
        checkpoints.append(ckpt_b)

        # Merge
        resp = await long_client.post(
            f"/checkpoints/{ckpt_base}/merge",
            json={"checkpoint_a": ckpt_a, "checkpoint_b": ckpt_b},
        )
        resp.raise_for_status()
        merged = resp.json()
        merged_ckpt = merged["checkpoint_id"]
        checkpoints.append(merged_ckpt)

        # Boot from merged checkpoint and verify both files
        comp_merged = await fork_checkpoint(long_client, merged_ckpt)
        computers.append(comp_merged)

        result_a = await exec_command(long_client, comp_merged, "cat /data/a.txt")
        assert result_a.stdout.strip() == "from_a"

        result_b = await exec_command(long_client, comp_merged, "cat /data/b.txt")
        assert result_b.stdout.strip() == "from_b"

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.3 — Merge: Same File Modified in Both Forks
# ---------------------------------------------------------------------------


async def test_merge_same_file_conflict(long_client: httpx.AsyncClient) -> None:
    """Merge two forks that both modify the same file — should produce a conflict or resolution."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)

        await exec_command(long_client, comp_origin, 'echo -n "original" > /data/shared.txt')
        await exec_command(long_client, comp_origin, "mkdir -p /data")

        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="base")
        checkpoints.append(ckpt_base)

        # Fork A modifies shared.txt
        comp_a = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_a)
        await exec_command(long_client, comp_a, 'echo -n "version_a" > /data/shared.txt')
        ckpt_a = await checkpoint_computer(long_client, comp_a, label="fork_a")
        checkpoints.append(ckpt_a)

        # Fork B modifies shared.txt differently
        comp_b = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_b)
        await exec_command(long_client, comp_b, 'echo -n "version_b" > /data/shared.txt')
        ckpt_b = await checkpoint_computer(long_client, comp_b, label="fork_b")
        checkpoints.append(ckpt_b)

        # Merge — should either fail with conflict or have a defined resolution strategy
        resp = await long_client.post(
            f"/checkpoints/{ckpt_base}/merge",
            json={"checkpoint_a": ckpt_a, "checkpoint_b": ckpt_b},
        )
        resp.raise_for_status()
        data = resp.json()
        # The exact conflict resolution behavior is TBD; just verify we get a response
        assert "checkpoint_id" in data or "conflict" in data

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.4 — Merge: File Created in Both at Same Path
# ---------------------------------------------------------------------------


async def test_merge_file_created_both_forks(long_client: httpx.AsyncClient) -> None:
    """Both forks create a NEW file at the same path (neither existed in base)."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)
        await exec_command(long_client, comp_origin, "mkdir -p /data")

        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="base")
        checkpoints.append(ckpt_base)

        # Fork A creates /data/new.txt
        comp_a = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_a)
        await exec_command(long_client, comp_a, 'echo -n "created_by_a" > /data/new.txt')
        ckpt_a = await checkpoint_computer(long_client, comp_a, label="fork_a")
        checkpoints.append(ckpt_a)

        # Fork B also creates /data/new.txt with different content
        comp_b = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_b)
        await exec_command(long_client, comp_b, 'echo -n "created_by_b" > /data/new.txt')
        ckpt_b = await checkpoint_computer(long_client, comp_b, label="fork_b")
        checkpoints.append(ckpt_b)

        resp = await long_client.post(
            f"/checkpoints/{ckpt_base}/merge",
            json={"checkpoint_a": ckpt_a, "checkpoint_b": ckpt_b},
        )
        resp.raise_for_status()
        data = resp.json()
        assert "checkpoint_id" in data or "conflict" in data

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.5 — Merge: Deletion in One Fork
# ---------------------------------------------------------------------------


async def test_merge_deletion_in_one_fork(long_client: httpx.AsyncClient) -> None:
    """One fork deletes a file, the other leaves it untouched — merge should delete it."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)
        await exec_command(long_client, comp_origin, "mkdir -p /data")
        await exec_command(
            long_client, comp_origin, 'echo -n "to_be_deleted" > /data/target.txt'
        )

        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="base")
        checkpoints.append(ckpt_base)

        # Fork A: delete the file
        comp_a = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_a)
        await exec_command(long_client, comp_a, "rm /data/target.txt")
        ckpt_a = await checkpoint_computer(long_client, comp_a, label="fork_a")
        checkpoints.append(ckpt_a)

        # Fork B: leave file untouched
        comp_b = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_b)
        ckpt_b = await checkpoint_computer(long_client, comp_b, label="fork_b")
        checkpoints.append(ckpt_b)

        # Merge
        resp = await long_client.post(
            f"/checkpoints/{ckpt_base}/merge",
            json={"checkpoint_a": ckpt_a, "checkpoint_b": ckpt_b},
        )
        resp.raise_for_status()
        merged = resp.json()
        merged_ckpt = merged["checkpoint_id"]
        checkpoints.append(merged_ckpt)

        # Boot merged — file should be gone
        comp_merged = await fork_checkpoint(long_client, merged_ckpt)
        computers.append(comp_merged)

        result = await exec_command(
            long_client, comp_merged, "test -f /data/target.txt && echo EXISTS || echo GONE"
        )
        assert result.stdout.strip() == "GONE"

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.6 — Merge: Deletion vs Modification
# ---------------------------------------------------------------------------


async def test_merge_deletion_vs_modification(long_client: httpx.AsyncClient) -> None:
    """One fork deletes a file, the other modifies it — conflict or defined behavior."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)
        await exec_command(long_client, comp_origin, "mkdir -p /data")
        await exec_command(
            long_client, comp_origin, 'echo -n "original" > /data/contested.txt'
        )

        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="base")
        checkpoints.append(ckpt_base)

        # Fork A: delete the file
        comp_a = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_a)
        await exec_command(long_client, comp_a, "rm /data/contested.txt")
        ckpt_a = await checkpoint_computer(long_client, comp_a, label="fork_a")
        checkpoints.append(ckpt_a)

        # Fork B: modify the file
        comp_b = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_b)
        await exec_command(long_client, comp_b, 'echo -n "modified" > /data/contested.txt')
        ckpt_b = await checkpoint_computer(long_client, comp_b, label="fork_b")
        checkpoints.append(ckpt_b)

        resp = await long_client.post(
            f"/checkpoints/{ckpt_base}/merge",
            json={"checkpoint_a": ckpt_a, "checkpoint_b": ckpt_b},
        )
        resp.raise_for_status()
        data = resp.json()
        # Conflict expected — exact behavior TBD
        assert "checkpoint_id" in data or "conflict" in data

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.7 — Merge Has No Process State
# ---------------------------------------------------------------------------


async def test_merge_no_process_state(long_client: httpx.AsyncClient) -> None:
    """Merged checkpoint should only contain filesystem state, not process state."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)

        # Start a background process, then checkpoint
        await exec_command(
            long_client,
            comp_origin,
            "nohup sleep 9999 &>/dev/null & echo $! > /root/bg.pid",
        )

        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="base")
        checkpoints.append(ckpt_base)

        # Fork A and B, checkpoint both
        comp_a = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_a)
        await exec_command(long_client, comp_a, 'echo -n "a_data" > /root/a.txt')
        ckpt_a = await checkpoint_computer(long_client, comp_a, label="fork_a")
        checkpoints.append(ckpt_a)

        comp_b = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_b)
        await exec_command(long_client, comp_b, 'echo -n "b_data" > /root/b.txt')
        ckpt_b = await checkpoint_computer(long_client, comp_b, label="fork_b")
        checkpoints.append(ckpt_b)

        # Merge
        resp = await long_client.post(
            f"/checkpoints/{ckpt_base}/merge",
            json={"checkpoint_a": ckpt_a, "checkpoint_b": ckpt_b},
        )
        resp.raise_for_status()
        merged = resp.json()
        merged_ckpt = merged["checkpoint_id"]
        checkpoints.append(merged_ckpt)

        # Boot from merged checkpoint
        comp_merged = await fork_checkpoint(long_client, merged_ckpt)
        computers.append(comp_merged)

        # The old background sleep process should NOT be running
        result = await exec_command(
            long_client, comp_merged, "pgrep -c 'sleep 9999' || true"
        )
        assert result.stdout.strip() == "0", (
            "Merged checkpoint should have no process state from parent forks"
        )

        # But filesystem state should be present
        result_a = await exec_command(long_client, comp_merged, "cat /root/a.txt")
        assert result_a.stdout.strip() == "a_data"

        result_b = await exec_command(long_client, comp_merged, "cat /root/b.txt")
        assert result_b.stdout.strip() == "b_data"

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.8 — Deep Fork Chains
# ---------------------------------------------------------------------------


async def test_deep_fork_chains(long_client: httpx.AsyncClient) -> None:
    """3-level deep fork chain: create→ckpt→fork→ckpt→fork→ckpt. CoW stacking works."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        # Level 0: create original, write file, checkpoint
        comp_0 = await create_computer(long_client)
        computers.append(comp_0)
        await exec_command(long_client, comp_0, 'echo -n "level0" > /root/level0.txt')
        ckpt_0 = await checkpoint_computer(long_client, comp_0, label="level0")
        checkpoints.append(ckpt_0)

        # Level 1: fork from ckpt_0, write another file, checkpoint
        comp_1 = await fork_checkpoint(long_client, ckpt_0)
        computers.append(comp_1)
        await exec_command(long_client, comp_1, 'echo -n "level1" > /root/level1.txt')
        ckpt_1 = await checkpoint_computer(long_client, comp_1, label="level1")
        checkpoints.append(ckpt_1)

        # Level 2: fork from ckpt_1, write another file, checkpoint
        comp_2 = await fork_checkpoint(long_client, ckpt_1)
        computers.append(comp_2)
        await exec_command(long_client, comp_2, 'echo -n "level2" > /root/level2.txt')
        ckpt_2 = await checkpoint_computer(long_client, comp_2, label="level2")
        checkpoints.append(ckpt_2)

        # Fork from deepest checkpoint — verify ALL files from all levels
        comp_final = await fork_checkpoint(long_client, ckpt_2)
        computers.append(comp_final)

        result_0 = await exec_command(long_client, comp_final, "cat /root/level0.txt")
        assert result_0.stdout.strip() == "level0", (
            f"Level 0 file missing or wrong: {result_0.stdout!r}"
        )

        result_1 = await exec_command(long_client, comp_final, "cat /root/level1.txt")
        assert result_1.stdout.strip() == "level1", (
            f"Level 1 file missing or wrong: {result_1.stdout!r}"
        )

        result_2 = await exec_command(long_client, comp_final, "cat /root/level2.txt")
        assert result_2.stdout.strip() == "level2", (
            f"Level 2 file missing or wrong: {result_2.stdout!r}"
        )

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.9 — Diamond Merge
# ---------------------------------------------------------------------------


async def test_diamond_merge(long_client: httpx.AsyncClient) -> None:
    """Diamond: base → A, base → B, merge(A, B) → C, then fork C and verify.

    This tests that the merge correctly handles the diamond topology where
    both forks share the same base checkpoint.
    """
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)
        await exec_command(long_client, comp_origin, "mkdir -p /data")
        await exec_command(
            long_client, comp_origin, 'echo -n "base_content" > /data/base.txt'
        )

        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="diamond_base")
        checkpoints.append(ckpt_base)

        # Left branch
        comp_a = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_a)
        await exec_command(long_client, comp_a, 'echo -n "left" > /data/left.txt')
        ckpt_a = await checkpoint_computer(long_client, comp_a, label="diamond_left")
        checkpoints.append(ckpt_a)

        # Right branch
        comp_b = await fork_checkpoint(long_client, ckpt_base)
        computers.append(comp_b)
        await exec_command(long_client, comp_b, 'echo -n "right" > /data/right.txt')
        ckpt_b = await checkpoint_computer(long_client, comp_b, label="diamond_right")
        checkpoints.append(ckpt_b)

        # Merge left + right
        resp = await long_client.post(
            f"/checkpoints/{ckpt_base}/merge",
            json={"checkpoint_a": ckpt_a, "checkpoint_b": ckpt_b},
        )
        resp.raise_for_status()
        merged = resp.json()
        merged_ckpt = merged["checkpoint_id"]
        checkpoints.append(merged_ckpt)

        # Fork from merged checkpoint and verify all three files
        comp_c = await fork_checkpoint(long_client, merged_ckpt)
        computers.append(comp_c)

        result_base = await exec_command(long_client, comp_c, "cat /data/base.txt")
        assert result_base.stdout.strip() == "base_content"

        result_left = await exec_command(long_client, comp_c, "cat /data/left.txt")
        assert result_left.stdout.strip() == "left"

        result_right = await exec_command(long_client, comp_c, "cat /data/right.txt")
        assert result_right.stdout.strip() == "right"

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)


# ---------------------------------------------------------------------------
# T2.10 — Concurrent Merges on Shared Parent
# ---------------------------------------------------------------------------


async def test_concurrent_merges_shared_parent(long_client: httpx.AsyncClient) -> None:
    """Two merges happening concurrently on the same base checkpoint must not corrupt data."""
    computers: list[str] = []
    checkpoints: list[str] = []

    try:
        import asyncio

        comp_origin = await create_computer(long_client)
        computers.append(comp_origin)
        await exec_command(long_client, comp_origin, "mkdir -p /data")

        ckpt_base = await checkpoint_computer(long_client, comp_origin, label="conc_base")
        checkpoints.append(ckpt_base)

        # Create 4 forks (A, B, C, D) — we'll merge (A,B) and (C,D) concurrently
        fork_ids: list[str] = []
        for i, name in enumerate(["a", "b", "c", "d"]):
            comp = await fork_checkpoint(long_client, ckpt_base)
            computers.append(comp)
            await exec_command(
                long_client, comp, f'echo -n "{name}_data" > /data/{name}.txt'
            )
            ckpt = await checkpoint_computer(long_client, comp, label=f"conc_{name}")
            checkpoints.append(ckpt)
            fork_ids.append(ckpt)

        ckpt_a, ckpt_b, ckpt_c, ckpt_d = fork_ids

        # Issue two merges concurrently
        async def do_merge(ca: str, cb: str) -> dict:
            resp = await long_client.post(
                f"/checkpoints/{ckpt_base}/merge",
                json={"checkpoint_a": ca, "checkpoint_b": cb},
            )
            resp.raise_for_status()
            return resp.json()

        merge_ab, merge_cd = await asyncio.gather(
            do_merge(ckpt_a, ckpt_b),
            do_merge(ckpt_c, ckpt_d),
        )

        # Verify merge AB
        merged_ab_ckpt = merge_ab["checkpoint_id"]
        checkpoints.append(merged_ab_ckpt)
        comp_ab = await fork_checkpoint(long_client, merged_ab_ckpt)
        computers.append(comp_ab)

        result = await exec_command(long_client, comp_ab, "cat /data/a.txt /data/b.txt")
        assert "a_data" in result.stdout
        assert "b_data" in result.stdout

        # Verify merge CD
        merged_cd_ckpt = merge_cd["checkpoint_id"]
        checkpoints.append(merged_cd_ckpt)
        comp_cd = await fork_checkpoint(long_client, merged_cd_ckpt)
        computers.append(comp_cd)

        result = await exec_command(long_client, comp_cd, "cat /data/c.txt /data/d.txt")
        assert "c_data" in result.stdout
        assert "d_data" in result.stdout

    finally:
        for cid in computers:
            await destroy_computer(long_client, cid)
        for ckid in checkpoints:
            await delete_checkpoint(long_client, ckid)
