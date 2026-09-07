"""Phase 10: The Generative Loop.

mshkn exists for a generative agent that writes its own recipes and the
programs those computers run. Phases 0 to 9 prove the primitives; this phase
proves the loop: write a recipe, build it, read the failure, fix it, run real
tools, checkpoint, fork, diverge, pick. Every test here is deterministic and
runs against the live host; none needs an LLM.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from .conftest import (
    checkpoint_computer,
    create_computer,
    create_recipe,
    delete_checkpoint,
    destroy_computer,
    exec_command,
    fork_checkpoint,
    port_url,
    upload_file,
)

if TYPE_CHECKING:
    from .conftest import ExecResult

BUILD_CAP_SECONDS = 600  # the documented docker build cap; every recipe must be ready within it


def _apt(*packages: str) -> str:
    return (
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        + " ".join(packages)
        + " && rm -rf /var/lib/apt/lists/*"
    )


def _ok(result: ExecResult) -> str:
    """stdout of a command that must have exited 0."""
    assert result.events and result.events[-1] == ("exit", "0"), (
        f"command failed: events={result.events[-6:]} stderr={result.stderr[-500:]}"
    )
    return result.stdout.strip()


async def _file_size(client: httpx.AsyncClient, cid: str, path: str) -> int:
    """-1 when the file does not exist."""
    out = _ok(await exec_command(client, cid, f"stat -c %s {path} 2>/dev/null || echo -1"))
    return int(out.splitlines()[-1])


# ---------------------------------------------------------------------------
# T10.1 — The ffmpeg Loop
# ---------------------------------------------------------------------------

FFMPEG_DOCKERFILE = "FROM mshkn-base\n" + _apt("ffmpeg") + "\n"


class TestT101FfmpegLoop:
    """Recipe → ready → create → run a real tool → checkpoint → fork → diverge."""

    async def test_recipe_create_run_checkpoint_fork_diverge(
        self, long_client: httpx.AsyncClient
    ) -> None:
        started = time.monotonic()
        recipe_id = await create_recipe(long_client, FFMPEG_DOCKERFILE, timeout=BUILD_CAP_SECONDS)
        print(f"T10.1 ffmpeg recipe ready in {time.monotonic() - started:.0f}s")

        cid = await create_computer(long_client, recipe_id=recipe_id)
        checkpoint_id: str | None = None
        forks: list[str] = []
        try:
            _ok(
                await exec_command(
                    long_client,
                    cid,
                    "ffmpeg -v error -f lavfi -i sine=frequency=440:duration=2 -y /root/tone.wav",
                    timeout_seconds=120,
                )
            )
            assert await _file_size(long_client, cid, "/root/tone.wav") > 0
            checkpoint_id = await checkpoint_computer(long_client, cid, label="ffmpeg-base")

            fork_a = await fork_checkpoint(long_client, checkpoint_id)
            forks.append(fork_a)
            fork_b = await fork_checkpoint(long_client, checkpoint_id)
            forks.append(fork_b)

            _ok(
                await exec_command(
                    long_client,
                    fork_a,
                    "ffmpeg -v error -i /root/tone.wav -y /root/tone.mp3",
                    timeout_seconds=120,
                )
            )
            _ok(
                await exec_command(
                    long_client,
                    fork_b,
                    "ffmpeg -v error -i /root/tone.wav -t 1 -y /root/short.wav",
                    timeout_seconds=120,
                )
            )

            # Each fork has its own output and not the other's.
            assert await _file_size(long_client, fork_a, "/root/tone.mp3") > 0
            assert await _file_size(long_client, fork_a, "/root/short.wav") == -1
            assert await _file_size(long_client, fork_b, "/root/short.wav") > 0
            assert await _file_size(long_client, fork_b, "/root/tone.mp3") == -1

            probe = "ffprobe -v error -show_entries format=duration -of csv=p=0 {}"
            mp3 = float(
                _ok(await exec_command(long_client, fork_a, probe.format("/root/tone.mp3")))
            )
            short = float(
                _ok(await exec_command(long_client, fork_b, probe.format("/root/short.wav")))
            )
            assert 1.8 <= mp3 <= 2.3, mp3
            assert 0.9 <= short <= 1.1, short
        finally:
            for fid in forks:
                await destroy_computer(long_client, fid)
            await destroy_computer(long_client, cid)
            if checkpoint_id:
                await delete_checkpoint(long_client, checkpoint_id)


# ---------------------------------------------------------------------------
# T10.2 — apt and pip Inside a Bare Computer
# ---------------------------------------------------------------------------

_ROWS = 200
_GROUPS = 4


def _dataset() -> list[tuple[int, int]]:
    """(group, value) rows the test and the VM agree on without a random source."""
    return [(i % _GROUPS, (i * 7) % 101) for i in range(_ROWS)]


_WRITE_CSV = """\
import csv
with open("/root/data.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["group", "value"])
    for i in range(%d):
        w.writerow([i %% %d, (i * 7) %% 101])
print("rows", %d)
""" % (_ROWS, _GROUPS, _ROWS)  # noqa: UP031 -- this is the VM-side script's own %% escaping, not ours

_MEAN = """\
import pandas as pd
df = pd.read_csv("/root/data.csv")
open("/root/mean.txt", "w").write(f"{df.value.mean():.4f}\\n")
print(f"mean={df.value.mean():.4f}")
"""

_GROUP_SUMS = """\
import pandas as pd
df = pd.read_csv("/root/data.csv")
sums = df.groupby("group").value.sum()
with open("/root/sums.txt", "w") as f:
    for g, s in sums.items():
        f.write(f"{g}={s}\\n")
        print(f"{g}={s}")
"""


class TestT102AptAndPipOnBare:
    """The other egress path: apt and pip run inside the VM, not in docker build."""

    async def test_apt_pip_checkpoint_fork_compare(self, long_client: httpx.AsyncClient) -> None:
        rows = _dataset()
        expected_mean = sum(v for _, v in rows) / len(rows)
        expected_sums = {g: sum(v for gg, v in rows if gg == g) for g in range(_GROUPS)}

        cid = await create_computer(long_client)
        checkpoint_id: str | None = None
        forks: list[str] = []
        try:
            started = time.monotonic()
            _ok(
                await exec_command(
                    long_client,
                    cid,
                    "apt-get update && apt-get install -y --no-install-recommends "
                    "python3 python3-pip python3-venv",
                    timeout_seconds=300,
                )
            )
            print(f"T10.2 apt in {time.monotonic() - started:.0f}s")
            started = time.monotonic()
            _ok(
                await exec_command(
                    long_client,
                    cid,
                    "python3 -m venv /root/venv && /root/venv/bin/pip install -q pandas",
                    timeout_seconds=300,
                )
            )
            print(f"T10.2 pip in {time.monotonic() - started:.0f}s")

            await upload_file(long_client, cid, "/root/write_csv.py", _WRITE_CSV.encode())
            assert _ok(await exec_command(long_client, cid, "python3 /root/write_csv.py")) == (
                f"rows {_ROWS}"
            )
            checkpoint_id = await checkpoint_computer(long_client, cid, label="data-ready")

            fork_a = await fork_checkpoint(long_client, checkpoint_id)
            forks.append(fork_a)
            fork_b = await fork_checkpoint(long_client, checkpoint_id)
            forks.append(fork_b)
            await upload_file(long_client, fork_a, "/root/mean.py", _MEAN.encode())
            await upload_file(long_client, fork_b, "/root/sums.py", _GROUP_SUMS.encode())

            mean_out = _ok(
                await exec_command(
                    long_client, fork_a, "/root/venv/bin/python /root/mean.py", timeout_seconds=120
                )
            )
            assert mean_out == f"mean={expected_mean:.4f}", mean_out

            sums_out = _ok(
                await exec_command(
                    long_client, fork_b, "/root/venv/bin/python /root/sums.py", timeout_seconds=120
                )
            )
            got = {int(k): int(v) for k, v in (line.split("=") for line in sums_out.splitlines())}
            assert got == expected_sums, sums_out

            # Neither fork has the other's file.
            assert await _file_size(long_client, fork_a, "/root/sums.txt") == -1
            assert await _file_size(long_client, fork_b, "/root/mean.txt") == -1
        finally:
            for fid in forks:
                await destroy_computer(long_client, fid)
            await destroy_computer(long_client, cid)
            if checkpoint_id:
                await delete_checkpoint(long_client, checkpoint_id)


# ---------------------------------------------------------------------------
# T10.3 — Parallel Exploration
# ---------------------------------------------------------------------------


class TestT103ParallelExploration:
    """Fork multiple times and explore different paths in parallel.

    Uses checkpoint/fork on bare VMs.
    """

    async def test_fork_three_ways_different_content(self, long_client: httpx.AsyncClient) -> None:
        """Create base state, fork 3 times, each writes different content."""
        computer_id = await create_computer(long_client)
        checkpoint_id = None
        forked_ids: list[str] = []
        try:
            # Write base state
            await exec_command(
                long_client,
                computer_id,
                "echo 'base_state' > /root/experiment.txt",
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
            for fid, experiment in zip(forked_ids, experiments, strict=True):
                await exec_command(
                    long_client,
                    fid,
                    f"echo '{experiment}' >> /root/experiment.txt",
                )

            # Verify each fork has different content
            contents: list[str] = []
            for fid in forked_ids:
                result = await exec_command(long_client, fid, "cat /root/experiment.txt")
                contents.append(result.stdout.strip())

            # All should have base_state
            for i, content in enumerate(contents):
                assert "base_state" in content, f"Fork {i} missing base_state: {content}"

            # Each should have its unique experiment line
            for i, (content, experiment) in enumerate(zip(contents, experiments, strict=True)):
                assert experiment in content, f"Fork {i} missing '{experiment}': {content}"

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
                long_client, forked_ids[best_idx], "cat /root/experiment.txt"
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
    """Checkpoint before risky operation, recover from failure by forking, on a bare VM."""

    async def test_recover_deleted_file_from_checkpoint(
        self, long_client: httpx.AsyncClient
    ) -> None:
        """Write important file, checkpoint, delete it, fork to recover."""
        computer_id = await create_computer(long_client)
        checkpoint_id = None
        recovered_id = None
        try:
            # Write important file
            await exec_command(
                long_client,
                computer_id,
                "echo 'critical_data_12345' > /root/important.txt",
            )

            # Verify it exists
            result = await exec_command(long_client, computer_id, "cat /root/important.txt")
            assert "critical_data_12345" in result.stdout

            # Checkpoint the good state
            checkpoint_id = await checkpoint_computer(
                long_client, computer_id, label="before-corruption"
            )

            # Corrupt the state: delete the important file
            await exec_command(long_client, computer_id, "rm /root/important.txt")

            # Verify it's gone
            result = await exec_command(
                long_client,
                computer_id,
                "cat /root/important.txt 2>&1 || echo FILE_MISSING",
            )
            assert (
                "FILE_MISSING" in result.stdout or "No such file" in result.stdout + result.stderr
            )

            # Fork from the checkpoint — file should be restored
            recovered_id = await fork_checkpoint(long_client, checkpoint_id)

            result = await exec_command(long_client, recovered_id, "cat /root/important.txt")
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
# T10.5 — The Scripted Agent Loop
# ---------------------------------------------------------------------------


class ScriptedAgent:
    """A deterministic agent: the tool table is the API, the policy is the test.

    Everything it creates is recorded so `cleanup` can tear it down whatever
    the outcome; the policy also cleans up on its own, which is part of what
    the test asserts.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.computers: list[str] = []
        self.checkpoints: list[str] = []

    async def create(self) -> str:
        cid = await create_computer(self.client)
        self.computers.append(cid)
        return cid

    async def run(self, cid: str, command: str) -> str:
        return _ok(await exec_command(self.client, cid, command, timeout_seconds=60))

    async def checkpoint(self, cid: str, label: str) -> str:
        ckpt = await checkpoint_computer(self.client, cid, label=label)
        self.checkpoints.append(ckpt)
        return ckpt

    async def fork(self, ckpt: str) -> str:
        cid = await fork_checkpoint(self.client, ckpt)
        self.computers.append(cid)
        return cid

    async def list_label(self, label: str) -> list[dict[str, Any]]:
        resp = await self.client.get("/checkpoints", params={"label": label})
        resp.raise_for_status()
        result: list[dict[str, Any]] = resp.json()
        return result

    async def destroy(self, cid: str) -> None:
        resp = await self.client.delete(f"/computers/{cid}")
        resp.raise_for_status()

    async def forget(self, ckpt: str) -> None:
        resp = await self.client.delete(f"/checkpoints/{ckpt}")
        resp.raise_for_status()

    async def cleanup(self) -> None:
        for cid in self.computers:
            await destroy_computer(self.client, cid)
        for ckpt in self.checkpoints:
            await delete_checkpoint(self.client, ckpt)


class TestT105ScriptedAgentLoop:
    """Explore, checkpoint, fork two attempts, compare, pick, discard."""

    async def test_explore_fork_compare_pick(self, long_client: httpx.AsyncClient) -> None:
        run_id = uuid.uuid4().hex[:8]
        agent = ScriptedAgent(long_client)
        try:
            # Explore: the data the task is about, with three distinct sizes.
            root = await agent.create()
            await agent.run(
                root,
                "mkdir -p /root/data && head -c 1000 /dev/zero > /root/data/small.bin "
                "&& head -c 20000 /dev/zero > /root/data/medium.bin "
                "&& head -c 300000 /dev/zero > /root/data/large.bin",
            )
            listing = await agent.run(root, "ls /root/data")
            assert set(listing.split()) == {"small.bin", "medium.bin", "large.bin"}, listing
            explored = await agent.checkpoint(root, f"explored-{run_id}")

            # Two attempts at "the largest file under /root/data", by different means.
            attempt_a = await agent.fork(explored)
            attempt_b = await agent.fork(explored)
            answer_a = await agent.run(
                attempt_a,
                "find /root/data -type f -printf '%s %p\\n' | sort -rn | head -1 "
                "| awk '{print $2}' | tee /root/answer.txt",
            )
            answer_b = await agent.run(
                attempt_b,
                "find /root/data -type f -exec du -b {} + | sort -rn | head -1 "
                "| awk '{print $2}' | tee /root/answer.txt",
            )
            assert answer_a == answer_b == "/root/data/large.bin", (answer_a, answer_b)

            # Pick: the attempts agree; keep the first, discard the second.
            winner, loser = attempt_a, attempt_b
            answer_ckpt = await agent.checkpoint(winner, f"answer-{run_id}")
            loser_ckpt = await agent.checkpoint(loser, f"discarded-{run_id}")
            await agent.destroy(loser)
            await agent.forget(loser_ckpt)

            # The kept work is reachable by label and forks to the answer.
            kept = await agent.list_label(f"answer-{run_id}")
            assert [c["id"] for c in kept] == [answer_ckpt], kept
            verify = await agent.fork(answer_ckpt)
            assert await agent.run(verify, "cat /root/answer.txt") == "/root/data/large.bin"

            # The discarded attempt is gone: its computer and its checkpoint.
            status = await long_client.get(f"/computers/{loser}/status")
            assert status.status_code == 404 or status.json().get("status") == "destroyed", (
                status.text
            )
            assert await agent.list_label(f"discarded-{run_id}") == []
        finally:
            await agent.cleanup()


# ---------------------------------------------------------------------------
# T10.6 — A Listener Survives Checkpoint and Fork
# ---------------------------------------------------------------------------

PYTHON_DOCKERFILE = "FROM mshkn-base\nRUN apt-get update && apt-get install -y python3"
# Same text as test_phase4_networking's recipe, so the host has it built already.

_COUNTER_SERVER = """\
import http.server

count = 0


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global count
        count += 1
        body = str(count).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# 0.0.0.0 on purpose: a restore gives the VM a new address, and a socket bound
# to the old one would not answer on the new slot.
http.server.HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""


async def _count(url: str) -> int:
    """GET url until it answers 200; the body is the server's request count."""
    async with httpx.AsyncClient(timeout=20.0) as public:
        for _ in range(30):
            try:
                resp = await public.get(url)
                if resp.status_code == 200:
                    return int(resp.text.strip())
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise AssertionError(f"{url} never answered 200")


class TestT106ListenerSurvives:
    """An in-memory counter behind a bound socket survives checkpoint and fork."""

    async def test_counter_continues_on_the_fork_and_the_original(
        self, long_client: httpx.AsyncClient
    ) -> None:
        recipe_id = await create_recipe(long_client, PYTHON_DOCKERFILE, timeout=BUILD_CAP_SECONDS)
        cid = await create_computer(long_client, recipe_id=recipe_id)
        checkpoint_id: str | None = None
        fork_id: str | None = None
        try:
            await upload_file(long_client, cid, "/root/counter.py", _COUNTER_SERVER.encode())
            bg = await long_client.post(
                f"/computers/{cid}/exec/bg", json={"command": "python3 /root/counter.py"}
            )
            bg.raise_for_status()
            status = await long_client.get(f"/computers/{cid}/status")
            status.raise_for_status()
            original_url = port_url(status.json()["url"], 8080)

            assert await _count(original_url) == 1
            assert await _count(original_url) == 2
            assert await _count(original_url) == 3

            checkpoint_id = await checkpoint_computer(long_client, cid, label="listening")
            fork_id = await fork_checkpoint(long_client, checkpoint_id)
            fork_status = await long_client.get(f"/computers/{fork_id}/status")
            fork_status.raise_for_status()
            fork_url = port_url(fork_status.json()["url"], 8080)
            assert fork_url != original_url

            # Memory state and the bound socket came with the snapshot.
            assert await _count(fork_url) == 4
            # The original kept running, independently.
            assert await _count(original_url) == 4
            assert await _count(fork_url) == 5
        finally:
            if fork_id:
                await destroy_computer(long_client, fork_id)
            await destroy_computer(long_client, cid)
            if checkpoint_id:
                await delete_checkpoint(long_client, checkpoint_id)
