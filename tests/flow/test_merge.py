"""Three-way merge over HTTP: real bytes on the three volumes, conflicts resolved
to fork_a, the merged checkpoint's row and volume, and the 400/404 family."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import ExecResult

if TYPE_CHECKING:
    from pathlib import Path

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


async def test_merge_reproduces_symlinks_instead_of_writing_through_them(
    flow: Flow, tmp_path: Path
) -> None:
    """An absolute symlink on a volume points at the host's tree, not the guest's.

    Every live merge against the `mshkn-base` volume failed on
    `/usr/sbin/init -> /lib/systemd/systemd`: the merge read the host's file
    through the link and then wrote the winning bytes back through the output
    volume's copy of it, onto the host.
    """
    host = flow.host
    host.guest.script["sync"] = ExecResult(0, "", "")
    c = flow.client
    host_systemd = tmp_path / "host-systemd"
    host_systemd.write_bytes(b"PARENT-SYSTEMD")
    host_systemd_a = tmp_path / "host-systemd-a"
    host_systemd_a.write_bytes(b"A-SYSTEMD")
    mtimes = (host_systemd.stat().st_mtime_ns, host_systemd_a.stat().st_mtime_ns)

    cid = (await c.post("/computers", json={})).json()["computer_id"]
    parent = (await c.post(f"/computers/{cid}/checkpoint", json={"label": "p"})).json()[
        "checkpoint_id"
    ]
    fa = (await c.post(f"/checkpoints/{parent}/fork", json={})).json()["computer_id"]
    fb = (await c.post(f"/checkpoints/{parent}/fork", json={})).json()["computer_id"]
    a = (await c.post(f"/computers/{fa}/checkpoint", json={})).json()["checkpoint_id"]
    b = (await c.post(f"/computers/{fb}/checkpoint", json={})).json()["checkpoint_id"]
    for name, target in (
        (f"mshkn-ckpt-{parent}", host_systemd),
        (f"mshkn-ckpt-{a}", host_systemd_a),  # changed only in A
        (f"mshkn-ckpt-{b}", host_systemd),
    ):
        async with host.blocks.mounted(name) as mount:
            (mount / "usr" / "sbin").mkdir(parents=True, exist_ok=True)
            (mount / "usr" / "sbin" / "init").symlink_to(target)

    resp = await c.post(f"/checkpoints/{parent}/merge", json={"checkpoint_a": a, "checkpoint_b": b})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conflicts"] == [] and body["auto_merged"] == 1
    out = host.blocks.mounts[f"mshkn-ckpt-{body['checkpoint_id']}"]
    assert host_systemd.read_bytes() == b"PARENT-SYSTEMD", "the merge wrote onto the host"
    assert host_systemd_a.read_bytes() == b"A-SYSTEMD"
    assert (host_systemd.stat().st_mtime_ns, host_systemd_a.stat().st_mtime_ns) == mtimes
    init = out / "usr" / "sbin" / "init"
    assert init.is_symlink() and init.readlink() == host_systemd_a


async def test_merge_replaces_a_directory_with_a_forks_symlink(flow: Flow, tmp_path: Path) -> None:
    """A fork that replaced a directory with an absolute link wins the path.

    The output volume is snapped from the parent, so it carries the parent's
    real `usr/sbin` directory: writing the link there has to remove it, and the
    parent's `usr/sbin/init` must not then be deleted through the new link.
    """
    host = flow.host
    host.guest.script["sync"] = ExecResult(0, "", "")
    c = flow.client
    host_root = tmp_path / "hostroot"
    host_root.mkdir()
    (host_root / "init").write_bytes(b"HOST-INIT")
    mtime = (host_root / "init").stat().st_mtime_ns

    cid = (await c.post("/computers", json={})).json()["computer_id"]
    parent = (await c.post(f"/computers/{cid}/checkpoint", json={})).json()["checkpoint_id"]
    fa = (await c.post(f"/checkpoints/{parent}/fork", json={})).json()["computer_id"]
    fb = (await c.post(f"/checkpoints/{parent}/fork", json={})).json()["computer_id"]
    a = (await c.post(f"/computers/{fa}/checkpoint", json={})).json()["checkpoint_id"]
    b = (await c.post(f"/computers/{fb}/checkpoint", json={})).json()["checkpoint_id"]
    for name in (f"mshkn-ckpt-{parent}", f"mshkn-ckpt-{b}"):
        async with host.blocks.mounted(name) as mount:
            (mount / "usr" / "sbin").mkdir(parents=True, exist_ok=True)
            (mount / "usr" / "sbin" / "init").write_bytes(b"GUEST-INIT")
    async with host.blocks.mounted(f"mshkn-ckpt-{a}") as mount:
        (mount / "usr").mkdir(parents=True, exist_ok=True)
        (mount / "usr" / "sbin").symlink_to(host_root)

    resp = await c.post(f"/checkpoints/{parent}/merge", json={"checkpoint_a": a, "checkpoint_b": b})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conflicts"] == []
    assert (host_root / "init").read_bytes() == b"HOST-INIT", "the merge wrote onto the host"
    assert (host_root / "init").stat().st_mtime_ns == mtime
    assert sorted(p.name for p in host_root.iterdir()) == ["init"], "the merge deleted on the host"
    out = host.blocks.mounts[f"mshkn-ckpt-{body['checkpoint_id']}"]
    sbin = out / "usr" / "sbin"
    assert sbin.is_symlink() and sbin.readlink() == host_root


async def test_merge_replaces_a_stale_symlink_on_the_volume_with_a_forks_directory(
    flow: Flow, tmp_path: Path
) -> None:
    """The mirror of the previous case: a fork replaced the link with a directory.

    The output volume is snapped from the parent, so it still carries the
    parent's `usr/sbin` link. That link is stale — the merge result has a real
    directory there — and until it goes it shadows the children about to be
    copied, while the delete pass removes the link itself: neither survives.
    """
    host = flow.host
    host.guest.script["sync"] = ExecResult(0, "", "")
    c = flow.client
    host_root = tmp_path / "hostroot"
    host_root.mkdir()
    (host_root / "init").write_bytes(b"HOST-INIT")
    mtime = (host_root / "init").stat().st_mtime_ns

    cid = (await c.post("/computers", json={})).json()["computer_id"]
    parent = (await c.post(f"/computers/{cid}/checkpoint", json={})).json()["checkpoint_id"]
    fa = (await c.post(f"/checkpoints/{parent}/fork", json={})).json()["computer_id"]
    fb = (await c.post(f"/checkpoints/{parent}/fork", json={})).json()["computer_id"]
    a = (await c.post(f"/computers/{fa}/checkpoint", json={})).json()["checkpoint_id"]
    b = (await c.post(f"/computers/{fb}/checkpoint", json={})).json()["checkpoint_id"]
    for name in (f"mshkn-ckpt-{parent}", f"mshkn-ckpt-{b}"):
        async with host.blocks.mounted(name) as mount:
            (mount / "usr").mkdir(parents=True, exist_ok=True)
            (mount / "usr" / "sbin").symlink_to(host_root)
    async with host.blocks.mounted(f"mshkn-ckpt-{a}") as mount:
        (mount / "usr" / "sbin").mkdir(parents=True, exist_ok=True)
        (mount / "usr" / "sbin" / "init").write_bytes(b"A-INIT")

    resp = await c.post(f"/checkpoints/{parent}/merge", json={"checkpoint_a": a, "checkpoint_b": b})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conflicts"] == []
    out = host.blocks.mounts[f"mshkn-ckpt-{body['checkpoint_id']}"]
    sbin = out / "usr" / "sbin"
    assert not sbin.is_symlink(), "the parent's link is stale once a fork puts a directory there"
    assert (sbin / "init").read_bytes() == b"A-INIT"
    assert (host_root / "init").read_bytes() == b"HOST-INIT"
    assert (host_root / "init").stat().st_mtime_ns == mtime
    assert sorted(p.name for p in host_root.iterdir()) == ["init"]
