"""Three-way merge over HTTP: real bytes on the three volumes, conflicts resolved
to fork_a, the merged checkpoint's row and volume, and the 400/404 family."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import ExecResult

if TYPE_CHECKING:
    from .conftest import Flow


async def test_merge_over_http_reports_conflicts_and_writes_the_output_volume(
    flow: Flow,
) -> None:
    host = flow.host
    host.guest.script["sync"] = ExecResult(0, "", "")
    c = flow.client
    cid = (await c.post("/computers", json={})).json()["computer_id"]
    parent = (await c.post(f"/computers/{cid}/checkpoint", json={"label": "p"})).json()[
        "checkpoint_id"
    ]
    fa = (await c.post(f"/checkpoints/{parent}/fork", json={})).json()["computer_id"]
    fb = (await c.post(f"/checkpoints/{parent}/fork", json={})).json()["computer_id"]
    a = (await c.post(f"/computers/{fa}/checkpoint", json={})).json()["checkpoint_id"]
    b = (await c.post(f"/computers/{fb}/checkpoint", json={})).json()["checkpoint_id"]
    for name, files in (
        (f"mshkn-ckpt-{parent}", {"f": "v0"}),
        (f"mshkn-ckpt-{a}", {"f": "A"}),
        (f"mshkn-ckpt-{b}", {"f": "B", "only_b": "b"}),
    ):
        async with host.blocks.mounted(name) as mount:
            for fname, content in files.items():
                (mount / fname).write_text(content)
    resp = await c.post(f"/checkpoints/{parent}/merge", json={"checkpoint_a": a, "checkpoint_b": b})
    assert resp.status_code == 200
    body = resp.json()
    assert body["conflicts"] == [{"path": "f", "resolution": "fork_a"}]
    assert body["auto_merged"] == 1 and body["unchanged"] == 0
    out = host.blocks.mounts[f"mshkn-ckpt-{body['checkpoint_id']}"]
    assert (out / "f").read_text() == "A" and (out / "only_b").read_text() == "b"
    listed = (await c.get("/checkpoints")).json()
    merged = next(x for x in listed if x["id"] == body["checkpoint_id"])
    assert merged["label"] == "merge"
    assert merged["parent_id"] == parent and merged["computer_id"] is None
    # a merge checkpoint has no memory snapshot, so forking it cold-boots
    booted_before, restored_before = len(host.hypervisor.booted), len(host.hypervisor.restored)
    forked = await c.post(f"/checkpoints/{body['checkpoint_id']}/fork", json={})
    assert forked.status_code == 200
    assert len(host.hypervisor.booted) == booted_before + 1
    assert len(host.hypervisor.restored) == restored_before


async def test_merge_validation_codes(flow: Flow) -> None:
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    c = flow.client
    cid = (await c.post("/computers", json={})).json()["computer_id"]
    parent = (await c.post(f"/computers/{cid}/checkpoint", json={})).json()["checkpoint_id"]
    # the second checkpoint of the same computer chains off the first
    child = (await c.post(f"/computers/{cid}/checkpoint", json={})).json()["checkpoint_id"]
    missing = await c.post(
        "/checkpoints/ckpt-nope/merge", json={"checkpoint_a": child, "checkpoint_b": child}
    )
    assert missing.status_code == 404
    same = await c.post(
        f"/checkpoints/{parent}/merge", json={"checkpoint_a": child, "checkpoint_b": child}
    )
    assert same.status_code == 400
    unknown_b = await c.post(
        f"/checkpoints/{parent}/merge", json={"checkpoint_a": child, "checkpoint_b": "ckpt-nope"}
    )
    assert unknown_b.status_code == 404
    wrong_parent = await c.post(
        f"/checkpoints/{child}/merge", json={"checkpoint_a": parent, "checkpoint_b": child}
    )
    assert wrong_parent.status_code == 400
