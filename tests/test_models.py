from mshkn.models import Checkpoint, Computer, Manifest, Recipe


def test_computer_creation() -> None:
    c = Computer(
        id="comp-abc",
        account_id="acct-1",
        thin_volume_id=5,
        tap_device="tap5",
        vm_ip="172.16.5.2",
        socket_path="/tmp/fc-comp-abc.socket",
        firecracker_pid=1234,
        manifest_hash="abc123",
        manifest_json='{"uses": []}',
        status="running",
        created_at="2026-03-08T12:00:00",
        last_exec_at=None,
    )
    assert c.id == "comp-abc"
    assert c.status == "running"


def test_manifest_hash_deterministic() -> None:
    m1 = Manifest(uses=["python-3.12(numpy)", "ffmpeg"])
    m2 = Manifest(uses=["python-3.12(numpy)", "ffmpeg"])
    m3 = Manifest(uses=["ffmpeg", "python-3.12(numpy)"])
    assert m1.content_hash() == m2.content_hash()
    assert m1.content_hash() == m3.content_hash(), "order should not matter"


def test_manifest_hash_changes_with_content() -> None:
    m1 = Manifest(uses=["python-3.12(numpy)"])
    m2 = Manifest(uses=["python-3.12(numpy, pandas)"])
    assert m1.content_hash() != m2.content_hash()


def test_recipe_dataclass() -> None:
    r = Recipe(
        id="rcp-abc123",
        account_id="acct-1",
        dockerfile="FROM ubuntu:24.04\nRUN apt-get update",
        content_hash="deadbeef",
        status="pending",
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    assert r.id == "rcp-abc123"
    assert r.account_id == "acct-1"
    assert r.status == "pending"
    assert r.build_log is None
    assert r.base_volume_id is None
    assert r.built_at is None


def test_computer_recipe_id() -> None:
    c = Computer(
        id="comp-abc",
        account_id="acct-1",
        thin_volume_id=5,
        tap_device="tap5",
        vm_ip="172.16.5.2",
        socket_path="/tmp/fc-comp-abc.socket",
        firecracker_pid=1234,
        manifest_hash="abc123",
        manifest_json='{"uses": []}',
        status="running",
        created_at="2026-03-13T00:00:00Z",
        last_exec_at=None,
        recipe_id="rcp-abc123",
    )
    assert c.recipe_id == "rcp-abc123"


def test_checkpoint_recipe_id() -> None:
    cp = Checkpoint(
        id="ckpt-abc",
        account_id="acct-1",
        parent_id=None,
        computer_id="comp-abc",
        thin_volume_id=5,
        manifest_hash="abc123",
        manifest_json='{"uses": []}',
        r2_prefix="checkpoints/ckpt-abc",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-03-13T00:00:00Z",
        recipe_id="rcp-abc123",
    )
    assert cp.recipe_id == "rcp-abc123"
