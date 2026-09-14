"""Phase 9: Economics — "Cost Targets and Resource Efficiency"

These tests run against a LIVE server with real Firecracker VMs. Both are
skipped outright, with reasons, because they require a week- or month-long
measurement window (NVMe wear, sleep-cost billing).

T9.1 (R2 storage cost per GB) was dropped from the definition of done on
2026-09-13: it measured Cloudflare's price list rather than mshkn's behaviour.
See the Phase 9 note in docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# T9.2 — NVMe Wear Leveling
# ---------------------------------------------------------------------------


class TestT92NvmeWear:
    """Verify NVMe write amplification stays sustainable."""

    @pytest.mark.skip(reason="Requires week-long NVMe wear measurement")
    async def test_nvme_tbw_projection(self) -> None:
        """Project NVMe Total Bytes Written over expected device lifetime.

        Test plan:
        1. Record initial SMART TBW counter
        2. Run a realistic workload for 7 days:
           - Create/destroy 1000 computers per day
           - Each does ~50MB of writes
           - Checkpoint every 10 minutes
        3. Record final SMART TBW counter
        4. Extrapolate to 5-year lifetime
        5. Verify projected TBW < device rated endurance
           (Samsung 970 EVO Plus 512GB: ~300 TBW)
        """
        pass


# ---------------------------------------------------------------------------
# T9.3 — $0 Sleep Cost
# ---------------------------------------------------------------------------


class TestT93ZeroDollarSleep:
    """Verify that sleeping (checkpointed) computers cost $0 in compute."""

    @pytest.mark.skip(reason="Requires 30-day wait to measure sleep cost")
    async def test_sleeping_computer_zero_compute_cost(self) -> None:
        """A checkpointed computer should consume zero compute resources.

        Test plan:
        1. Create a computer, do some work, checkpoint it
        2. Destroy the running VM (only checkpoint remains)
        3. Wait 30 days
        4. Verify:
           - No CPU usage attributed to the checkpointed computer
           - No RAM allocated
           - No network bandwidth consumed
           - Only storage cost (R2 at $0.015/GB-month)
        5. Fork from the checkpoint — should resume instantly
        6. Verify total cost for the 30-day sleep was ~$0 + storage
        """
        pass
