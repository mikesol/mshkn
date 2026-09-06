from dataclasses import FrozenInstanceError

import pytest

from mshkn.models import (
    Checkpoint,
    CheckpointTrigger,
    Computer,
    ComputerStatus,
    DeferredRequest,
    ExecSpec,
    Recipe,
    RecipeStatus,
    checkpoint_volume_name,
    computer_volume_name,
    recipe_volume_name,
)


def test_computer_creation() -> None:
    c = Computer(
        id="comp-abc",
        account_id="acct-1",
        thin_volume_id=5,
        tap_device="tap5",
        vm_ip="172.16.5.2",
        socket_path="/tmp/fc-comp-abc.socket",
        firecracker_pid=1234,
        status=ComputerStatus.RUNNING,
        created_at="2026-03-08T12:00:00",
        last_exec_at=None,
    )
    assert c.id == "comp-abc"
    assert c.status == ComputerStatus.RUNNING
    assert c.volume_name == computer_volume_name("comp-abc")


def test_recipe_dataclass() -> None:
    r = Recipe(
        id="rcp-abc123",
        account_id="acct-1",
        dockerfile="FROM ubuntu:24.04\nRUN apt-get update",
        content_hash="deadbeef",
        status=RecipeStatus.PENDING,
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )
    assert r.id == "rcp-abc123"
    assert r.account_id == "acct-1"
    assert r.status == RecipeStatus.PENDING
    assert r.build_log is None
    assert r.base_volume_id is None
    assert r.built_at is None
    assert r.volume_name == recipe_volume_name("rcp-abc123")


def test_computer_recipe_id() -> None:
    c = Computer(
        id="comp-abc",
        account_id="acct-1",
        thin_volume_id=5,
        tap_device="tap5",
        vm_ip="172.16.5.2",
        socket_path="/tmp/fc-comp-abc.socket",
        firecracker_pid=1234,
        status=ComputerStatus.RUNNING,
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
        r2_prefix="checkpoints/ckpt-abc",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-03-13T00:00:00Z",
        recipe_id="rcp-abc123",
    )
    assert cp.recipe_id == "rcp-abc123"
    assert cp.volume_name == checkpoint_volume_name("ckpt-abc")


def test_status_enums_are_strings() -> None:
    assert ComputerStatus.RUNNING == "running"  # type: ignore[comparison-overlap]
    assert str(ComputerStatus.DESTROYED) == "destroyed"
    assert ComputerStatus("running") is ComputerStatus.RUNNING
    assert {s.value for s in RecipeStatus} == {
        "pending",
        "building",
        "exporting",
        "injecting",
        "ready",
        "failed",
    }


def test_deferred_request_is_frozen() -> None:
    d = DeferredRequest(
        id="def-1",
        label="l",
        account_id="a",
        request_payload="{}",
        created_at="t",
    )
    with pytest.raises(FrozenInstanceError):
        d.label = "other"  # type: ignore[misc]


def test_computer_and_checkpoint_have_no_manifest_fields() -> None:
    assert "manifest_hash" not in Computer.__dataclass_fields__
    assert "manifest_json" not in Checkpoint.__dataclass_fields__


def test_checkpoint_trigger_values() -> None:
    assert [t.value for t in CheckpointTrigger] == ["api", "self_destruct", "idle"]
    assert CheckpointTrigger.IDLE == "idle"  # type: ignore[comparison-overlap]


def test_exec_spec_is_frozen() -> None:
    spec = ExecSpec(
        command="ls", self_destruct=False, callback_url=None, label=None, meta_exec=None
    )
    with pytest.raises(FrozenInstanceError):
        spec.command = "rm"  # type: ignore[misc]


def test_volume_name_helpers_and_the_properties_that_delegate() -> None:
    """scripts/e2e.sh cleans orphans by these prefixes, so the formats are load-bearing."""
    assert computer_volume_name("comp-abc") == "mshkn-comp-abc"
    assert checkpoint_volume_name("ckpt-abc") == "mshkn-ckpt-ckpt-abc"
    assert recipe_volume_name("rcp-abc") == "mshkn-recipe-rcp-abc"
