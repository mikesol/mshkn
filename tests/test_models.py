from mshkn.models import Computer, Manifest


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
