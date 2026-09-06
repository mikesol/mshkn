from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from mshkn.config import Config
from mshkn.db import claim_deferred_by_label, get_computer, insert_account
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


def test_validate_transform_result_preflights_needs() -> None:
    unparseable = validate_transform_result({"action": "create", "needs": {"ram": "lots"}})
    assert any("needs" in e and "ram" in e for e in unparseable)
    unknown = validate_transform_result({"action": "create", "needs": {"bogus": 1}})
    assert any("needs" in e and "bogus" in e for e in unknown)
    out_of_range = validate_transform_result({"action": "create", "needs": {"cores": 999}})
    assert any("needs" in e and "cores" in e for e in out_of_range)


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


async def test_async_action_failure_logs_a_failed_row(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    ingress, _, _, _ = await _ingress(db, tmp_path)
    rule = await ingress.create_rule(
        ACCOUNT,
        name="doomed",
        starlark_source=(
            'def transform(req):\n  return {"action": "fork", "label": "missing", "exec": "true"}'
        ),
        response_mode="async",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    assert (await ingress.trigger(rule.id, REQ)).status_code == 202
    await ingress.tasks.drain(timeout=2.0)
    logs = await ingress.logs(ACCOUNT, rule.id)
    assert sorted(log.status.value for log in logs) == ["accepted", "failed"]
    failed = next(log for log in logs if log.status is IngressLogStatus.FAILED)
    assert failed.error_message is not None and "missing" in failed.error_message


async def test_fork_defers_when_the_label_already_has_a_running_computer(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    ingress, computers, checkpoints, host = await _ingress(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    ckpt = await checkpoints.create(base, label="busy", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    holder = await computers.fork(ACCOUNT, ckpt, recipe_id=None)
    assert holder.status is ComputerStatus.RUNNING
    restores_before = len(host.hypervisor.restored)
    rule = await ingress.create_rule(
        ACCOUNT,
        name="defer",
        starlark_source=(
            'def transform(req):\n  return {"action": "fork", "label": "busy",'
            ' "exec": "true", "exclusive": "defer_on_conflict"}'
        ),
        response_mode="sync",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    outcome = await ingress.trigger(rule.id, REQ)
    assert outcome.status_code == 200 and outcome.body is not None
    assert outcome.body["status"] == "queued"
    deferred_id = str(outcome.body["deferred_id"])
    assert deferred_id.startswith("def-")
    assert len(host.hypervisor.restored) == restores_before  # nothing was forked
    queued = await claim_deferred_by_label(db, "busy")
    assert [d.id for d in queued] == [deferred_id]
    assert json.loads(queued[0].request_payload)["checkpoint_id"] == ckpt.id
    logs = await ingress.logs(ACCOUNT, rule.id)
    assert [log.status for log in logs] == [IngressLogStatus.COMPLETED]


async def test_list_and_delete_rules(db: aiosqlite.Connection, tmp_path: Path) -> None:
    ingress, _, _, _ = await _ingress(db, tmp_path)
    assert await ingress.list_rules(ACCOUNT) == []
    first = await ingress.create_rule(
        ACCOUNT,
        name="first",
        starlark_source="def transform(req):\n  return None",
        response_mode="async",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    second = await ingress.create_rule(
        ACCOUNT,
        name="second",
        starlark_source="def transform(req):\n  return None",
        response_mode="sync",
        max_body_bytes=2048,
        rate_limit_rpm=30,
    )
    assert sorted(rule.name for rule in await ingress.list_rules(ACCOUNT)) == ["first", "second"]
    limiter = ingress.limiter_for(first)
    assert ingress.limiter_for(first) is limiter  # cached while the rule lives
    await ingress.delete_rule(ACCOUNT, first.id)
    assert ingress.limiter_for(first) is not limiter  # the cached limiter was evicted
    assert [rule.name for rule in await ingress.list_rules(ACCOUNT)] == ["second"]
    with pytest.raises(NotFound):
        await ingress.get_rule(ACCOUNT, first.id)
    with pytest.raises(NotFound):
        await ingress.delete_rule(ACCOUNT, first.id)
    assert (await ingress.get_rule(ACCOUNT, second.id)).name == "second"


async def test_update_rule(db: aiosqlite.Connection, tmp_path: Path) -> None:
    ingress, _, _, _ = await _ingress(db, tmp_path)
    rule = await ingress.create_rule(
        ACCOUNT,
        name="before",
        starlark_source="def transform(req):\n  return None",
        response_mode="async",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    updated = await ingress.update_rule(
        ACCOUNT,
        rule.id,
        name="after",
        response_mode="sync",
        max_body_bytes=4096,
        rate_limit_rpm=7,
    )
    assert updated.updated_at >= rule.created_at
    stored = await ingress.get_rule(ACCOUNT, rule.id)
    assert (stored.name, stored.response_mode) == ("after", "sync")
    assert (stored.max_body_bytes, stored.rate_limit_rpm) == (4096, 7)
    with pytest.raises(InvalidInput) as info:
        await ingress.update_rule(
            ACCOUNT, rule.id, starlark_source="def other(req):\n  return None"
        )
    assert isinstance(info.value.detail, dict) and "starlark_errors" in info.value.detail
    assert (await ingress.get_rule(ACCOUNT, rule.id)).starlark_source.startswith("def transform")
    await ingress.update_rule(ACCOUNT, rule.id, enabled=False)
    assert not (await ingress.get_rule(ACCOUNT, rule.id)).enabled
    with pytest.raises(NotFound):
        await ingress.trigger(rule.id, REQ)
    await ingress.update_rule(ACCOUNT, rule.id, enabled=True)
    assert (await ingress.trigger(rule.id, REQ)).status_code == 204


async def test_test_rule_reports_results_and_starlark_failures(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    ingress, _, _, _ = await _ingress(db, tmp_path)
    good = await ingress.create_rule(
        ACCOUNT,
        name="good",
        starlark_source=(
            'def transform(req):\n  return {"action": "fork", "checkpoint_id": req["path"]}'
        ),
        response_mode="sync",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    result, errors, elapsed = ingress.test_rule(good, REQ)
    assert result == {"action": "fork", "checkpoint_id": "/hook"}
    assert errors == [] and elapsed >= 0
    invalid = await ingress.create_rule(
        ACCOUNT,
        name="invalid",
        starlark_source='def transform(req):\n  return {"action": "create", "uses": ["x"]}',
        response_mode="sync",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    result, errors, elapsed = ingress.test_rule(invalid, REQ)
    assert result == {"action": "create", "uses": ["x"]}
    assert any("uses" in e for e in errors) and elapsed >= 0
    boom = await ingress.create_rule(
        ACCOUNT,
        name="boom",
        starlark_source='def transform(req):\n  return req["x"]["y"]',
        response_mode="sync",
        max_body_bytes=1024,
        rate_limit_rpm=60,
    )
    result, errors, elapsed = ingress.test_rule(boom, REQ)
    assert result is None and len(errors) == 1 and elapsed >= 0
    assert (await ingress.logs(ACCOUNT, boom.id)) == []  # a dry run logs nothing
