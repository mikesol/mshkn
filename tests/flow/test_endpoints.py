"""Endpoints no other flow file reaches: the exec family (background, logs,
kill), file transfer, live status metrics, and the checkpoint listing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import get_computer
from mshkn.host import ExecResult

if TYPE_CHECKING:
    from .conftest import Flow

_CHECKPOINT_SUMMARY_FIELDS = {
    "id",
    "checkpoint_id",
    "parent_id",
    "computer_id",
    "recipe_id",
    "r2_prefix",
    "disk_delta_size_bytes",
    "memory_size_bytes",
    "label",
    "pinned",
    "created_at",
}


def _parse_sse(body: bytes) -> list[tuple[str, str]]:
    """(event, data) pairs from a fully-read SSE body."""
    events: list[tuple[str, str]] = []
    current = "stdout"
    for raw in body.decode().splitlines():
        line = raw.strip()
        if line.startswith("event: "):
            current = line[7:]
        elif line.startswith("data: "):
            events.append((current, line[6:]))
    return events


async def test_exec_family_and_file_transfer(flow: Flow) -> None:
    host = flow.host
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    row = await get_computer(flow.runtime.db, cid)
    assert row is not None

    # exec/bg hands back the pid the guest minted, and the command reached the VM
    bg = await flow.client.post(f"/computers/{cid}/exec/bg", json={"command": "sleep 60"})
    assert bg.status_code == 200, bg.text
    assert bg.json() == {"pid": 4000}
    assert (row.vm_ip, "sleep 60") in host.guest.commands

    # exec/logs replays the pid's log file as one stdout event per line, then exit 0
    host.guest.script["cat /tmp/bg-4000.log 2>/dev/null || echo ''"] = ExecResult(
        0, "line1\nline2\n", ""
    )
    async with flow.client.stream("GET", f"/computers/{cid}/exec/logs/4000") as resp:
        assert resp.status_code == 200
        events = _parse_sse(await resp.aread())
    assert events == [("stdout", "line1"), ("stdout", "line2"), ("exit", "0")]

    # exec/kill reports killed on exit 0 and not_found with the guest's stderr otherwise
    host.guest.script["kill 4000"] = ExecResult(0, "", "")
    killed = await flow.client.post(f"/computers/{cid}/exec/kill/4000")
    assert killed.status_code == 200
    assert killed.json() == {"status": "killed", "stderr": None}
    host.guest.script["kill 9999"] = ExecResult(1, "", "kill: (9999): No such process\n")
    missing = await flow.client.post(f"/computers/{cid}/exec/kill/9999")
    assert missing.status_code == 200
    assert missing.json() == {
        "status": "not_found",
        "stderr": "kill: (9999): No such process\n",
    }

    # upload lands the raw body in the guest; download hands the same bytes back
    up = await flow.client.post(
        f"/computers/{cid}/upload", params={"path": "/root/x.bin"}, content=b"\x00\x01"
    )
    assert up.status_code == 200, up.text
    assert up.json() == {"status": "uploaded", "path": "/root/x.bin"}
    assert host.guest.files[(row.vm_ip, "/root/x.bin")] == b"\x00\x01"
    down = await flow.client.get(f"/computers/{cid}/download", params={"path": "/root/x.bin"})
    assert down.status_code == 200
    assert down.content == b"\x00\x01"
    assert down.headers["content-type"] == "application/octet-stream"

    # a missing file is a 404, not a 500
    absent = await flow.client.get(f"/computers/{cid}/download", params={"path": "/root/gone"})
    assert absent.status_code == 404

    stored = await get_computer(flow.runtime.db, cid)
    assert stored is not None
    assert stored.last_exec_at is not None, "exec_bg touched last_exec_at"


async def test_status_carries_live_metrics_for_a_running_computer(flow: Flow) -> None:
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    resp = await flow.client.get(f"/computers/{cid}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["recipe_id"] is None
    assert "manifest_hash" not in body
    assert (body["cpu_pct"], body["ram_usage_mb"], body["ram_total_mb"]) == (1.5, 64, 230)
    assert (body["disk_usage_mb"], body["disk_total_mb"]) == (200, 7800)
    assert body["processes"] == [{"pid": 1, "command": "systemd"}]
    assert body["url"] == f"https://{cid}.test.dev"


async def test_status_without_metrics_carries_nulls_and_still_200(flow: Flow) -> None:
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    flow.host.guest.fail_next("metrics")
    resp = await flow.client.get(f"/computers/{cid}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["cpu_pct"] is None
    assert body["ram_usage_mb"] is None
    assert body["processes"] is None


async def test_checkpoint_list_shape_and_label_filter(flow: Flow) -> None:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    a = (
        await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "x", "pin": True})
    ).json()["checkpoint_id"]
    b = (await flow.client.post(f"/computers/{cid}/checkpoint", json={})).json()["checkpoint_id"]

    listed = (await flow.client.get("/checkpoints")).json()
    assert {c["id"] for c in listed} == {a, b}
    entry = next(c for c in listed if c["id"] == a)
    assert set(entry) == _CHECKPOINT_SUMMARY_FIELDS
    assert entry["checkpoint_id"] == a
    assert entry["computer_id"] == cid
    assert entry["pinned"] is True
    assert entry["label"] == "x"
    assert entry["r2_prefix"] == f"acct-1/{a}"

    only_x = (await flow.client.get("/checkpoints", params={"label": "x"})).json()
    assert [c["id"] for c in only_x] == [a]

    deleted = await flow.client.delete(f"/checkpoints/{b}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted"}
    assert [c["id"] for c in (await flow.client.get("/checkpoints")).json()] == [a]


async def test_exec_stream_reports_a_non_zero_exit(flow: Flow) -> None:
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    flow.host.guest.stream_script["exit 42"] = [("exit", "42")]
    async with flow.client.stream(
        "POST", f"/computers/{cid}/exec", json={"command": "exit 42"}
    ) as resp:
        assert resp.status_code == 200
        events = _parse_sse(await resp.aread())
    assert events == [("exit", "42")]
