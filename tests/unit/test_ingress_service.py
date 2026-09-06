from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from mshkn.config import Config
from mshkn.db import get_computer, insert_account
from mshkn.errors import InvalidInput, LimitExceeded, NotFound, TransformError
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import Account, CheckpointTrigger, ComputerStatus, IngressLogStatus
from mshkn.resources import DEFAULT_RESOURCES, Resources
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.ingress import IngressService, validate_transform_result
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")
REQ: dict[str, object] = {
    "method": "POST",
    "path": "/hook",
    "headers": {},
    "query_params": {},
    "body_json": None,
    "body_form": None,
    "body_raw": "",
    "content_type": "",
}


async def _ingress(
    db: aiosqlite.Connection, tmp_path: Path
) -> tuple[IngressService, ComputerService, CheckpointService, FakeHostInstance]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    allocator = SlotAllocator()
    tasks = BackgroundTasks()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
    computers = ComputerService(config, db, host, allocator, recipes)
    checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
    lifecycle = Lifecycle(db, computers, checkpoints, tasks, httpx.AsyncClient())
    return (
        IngressService(config, db, computers, checkpoints, lifecycle, tasks),
        computers,
        checkpoints,
        host,
    )


def test_validate_transform_result_accepts_recipe_id_and_needs_and_rejects_uses() -> None:
    assert validate_transform_result(None) == []
    assert validate_transform_result({"action": "fork", "checkpoint_id": "cp_1"}) == []
    assert (
        validate_transform_result(
            {"action": "create", "recipe_id": "rcp-1", "needs": {"ram": "1GB"}}
        )
        == []
    )
    assert any(
        "checkpoint_id" in e or "label" in e for e in validate_transform_result({"action": "fork"})
    )
    assert validate_transform_result({"action": "restart"})
    assert any(
        "bogus" in e
        for e in validate_transform_result({"action": "fork", "checkpoint_id": "x", "bogus": 1})
    )
    assert any(
        "uses" in e for e in validate_transform_result({"action": "create", "uses": ["python"]})
    )
    assert any(
        "capabilities" in e
        for e in validate_transform_result({"action": "create", "capabilities": []})
    )
    assert any(
        "exclusive" in e
        for e in validate_transform_result(
            {"action": "fork", "checkpoint_id": "x", "exclusive": "wrong"}
        )
    )
    assert validate_transform_result("not a dict") == ["transform must return a dict or None"]


async def test_create_rule_validates_starlark(db: aiosqlite.Connection, tmp_path: Path) -> None:
    ingress, _, _, _ = await _ingress(db, tmp_path)
    with pytest.raises(InvalidInput) as info:
        await ingress.create_rule(
            ACCOUNT,
            name="bad",
            starlark_source="def other(req):\n  return None",
            response_mode="async",
            max_body_bytes=10485760,
            rate_limit_rpm=60,
        )
    assert isinstance(info.value.detail, dict) and "starlark_errors" in info.value.detail
    rule = await ingress.create_rule(
        ACCOUNT,
        name="ok",
        starlark_source="def transform(req):\n  return None",
        response_mode="sync",
        max_body_bytes=2048,
        rate_limit_rpm=5,
    )
    assert rule.id.startswith("ir_") and (await ingress.get_rule(ACCOUNT, rule.id)).name == "ok"
    rotated = await ingress.rotate_rule(ACCOUNT, rule.id)
    assert rotated.id != rule.id
    with pytest.raises(NotFound):
        await ingress.get_rule(ACCOUNT, rule.id)


async def test_trigger_outcomes(db: aiosqlite.Connection, tmp_path: Path) -> None:
    ingress, _, _, _ = await _ingress(db, tmp_path)
    with pytest.raises(NotFound):
        await ingress.trigger("ir_nope", REQ)
    none_rule = await ingress.create_rule(
        ACCOUNT,
        name="none",
        starlark_source="def transform(req):\n  return None",
        response_mode="async",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    assert (await ingress.trigger(none_rule.id, REQ)).status_code == 204
    boom = await ingress.create_rule(
        ACCOUNT,
        name="boom",
        starlark_source='def transform(req):\n  return req["x"]["y"]',
        response_mode="async",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    with pytest.raises(TransformError):
        await ingress.trigger(boom.id, REQ)
    logs = await ingress.logs(ACCOUNT, boom.id)
    assert logs and logs[0].status is IngressLogStatus.FAILED
    bad = await ingress.create_rule(
        ACCOUNT,
        name="bad",
        starlark_source='def transform(req):\n  return {"action": "create", "uses": ["x"]}',
        response_mode="sync",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    with pytest.raises(TransformError) as info:
        await ingress.trigger(bad.id, REQ)
    assert isinstance(info.value.detail, dict) and "errors" in info.value.detail
    limited = await ingress.create_rule(
        ACCOUNT,
        name="limited",
        starlark_source="def transform(req):\n  return None",
        response_mode="async",
        max_body_bytes=1024,
        rate_limit_rpm=1,
    )
    assert (await ingress.trigger(limited.id, REQ)).status_code == 204
    with pytest.raises(LimitExceeded):
        await ingress.trigger(limited.id, REQ)


async def test_sync_create_honours_recipe_id_and_needs_through_the_lifecycle(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    ingress, _, _, host = await _ingress(db, tmp_path)
    host.guest.script["echo hi"] = ExecResult(0, "hi\n", "")
    rule = await ingress.create_rule(
        ACCOUNT,
        name="create",
        starlark_source=(
            'def transform(req):\n  return {"action": "create",'
            ' "needs": {"ram": "1GB", "cores": 4},'
            ' "exec": "echo hi", "self_destruct": True, "label": "ing"}'
        ),
        response_mode="sync",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    outcome = await ingress.trigger(rule.id, REQ)
    assert outcome.status_code == 200 and outcome.body is not None
    assert outcome.body["exec_stdout"] == "hi\n" and outcome.body["created_checkpoint_id"]
    assert host.hypervisor.booted == [
        (host.hypervisor.booted[0][0], Resources(mem_mib=1024, vcpus=4))
    ]
    stored = await get_computer(db, str(outcome.body["computer_id"]))
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    logs = await ingress.logs(ACCOUNT, rule.id)
    assert logs[0].status is IngressLogStatus.COMPLETED


async def test_async_fork_by_label_runs_in_the_background(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    ingress, computers, checkpoints, host = await _ingress(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    rule = await ingress.create_rule(
        ACCOUNT,
        name="fork",
        starlark_source=(
            'def transform(req):\n  return {"action": "fork", "label": "chain", "exec": "true"}'
        ),
        response_mode="async",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    outcome = await ingress.trigger(rule.id, REQ)
    assert outcome.status_code == 202 and outcome.body == {"status": "accepted"}
    await ingress.tasks.drain(timeout=2.0)
    assert len(host.hypervisor.restored) == 2  # base's template restore + the fork
    assert [log.status for log in await ingress.logs(ACCOUNT, rule.id)] == [
        IngressLogStatus.ACCEPTED
    ]
