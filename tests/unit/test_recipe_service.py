from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import get_recipe, insert_account, insert_computer
from mshkn.errors import Conflict, NotFound
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import Account, Computer, ComputerStatus, RecipeStatus
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.recipes import RecipeService, dockerfile_content_hash

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")


class FakeShell:
    """Records commands; can be told to fail one of them."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            raise RuntimeError(f"failed: {cmd}")
        return ""


def _service(
    db: aiosqlite.Connection,
    tmp_path: Path,
    *,
    shell: FakeShell | None = None,
    build_ok: bool = True,
) -> tuple[RecipeService, FakeHostInstance, FakeShell]:
    host = FakeHost()
    shell = shell or FakeShell()

    async def build_image(cmd: str) -> str:
        if not build_ok:
            raise RuntimeError("docker build failed (rc=1):\nboom")
        return "Successfully built"

    config = Config(ssh_key_path=tmp_path / "id_ed25519", checkpoint_local_dir=tmp_path / "ckpts")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    service = RecipeService(
        config,
        db,
        host.blocks,
        host.hypervisor,
        SlotAllocator(),
        BackgroundTasks(),
        run=shell,
        build_image=build_image,
    )
    return service, host, shell


def test_dockerfile_content_hash() -> None:
    assert dockerfile_content_hash("FROM a") == dockerfile_content_hash("FROM a")
    assert dockerfile_content_hash("FROM a") != dockerfile_content_hash("FROM b")
    assert len(dockerfile_content_hash("x")) == 64


async def test_create_builds_through_the_state_machine_to_ready(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, shell = _service(db, tmp_path)
    recipe, created = await service.create(ACCOUNT, "FROM mshkn-base\nRUN true")
    assert created and recipe.status is RecipeStatus.PENDING
    await service.tasks.wait(service.build_task_name(recipe.id))
    stored = await get_recipe(db, recipe.id)
    assert stored is not None and stored.status is RecipeStatus.READY
    assert stored.base_volume_id == 100 and stored.built_at is not None
    assert host.blocks.volumes[100] == 0
    assert f"mshkn-recipe-{recipe.id}" not in host.blocks.active  # deactivated after inject
    assert ("mkfs", f"mshkn-recipe-{recipe.id}") in host.blocks.calls
    assert any(c.startswith("docker create --name tmp-") for c in shell.calls)
    assert any(c.startswith("docker export -o ") for c in shell.calls)
    assert any(c.startswith("tar xf ") and "-C " in c for c in shell.calls)


async def test_failed_build_records_the_log_and_leaves_no_device(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, _ = _service(db, tmp_path, build_ok=False)
    recipe, _ = await service.create(ACCOUNT, "FROM nope")
    await service.tasks.wait(service.build_task_name(recipe.id))
    stored = await get_recipe(db, recipe.id)
    assert stored is not None and stored.status is RecipeStatus.FAILED
    assert stored.build_log is not None and "boom" in stored.build_log
    assert stored.base_volume_id is None
    assert host.blocks.active == {}


async def test_inject_failure_after_activate_deactivates_the_device(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, _ = _service(db, tmp_path, shell=FakeShell(fail_on="tar xf"))
    recipe, _ = await service.create(ACCOUNT, "FROM x")
    await service.tasks.wait(service.build_task_name(recipe.id))
    stored = await get_recipe(db, recipe.id)
    assert stored is not None and stored.status is RecipeStatus.FAILED
    assert host.blocks.active == {}


async def test_create_dedupes_by_content_hash(db: aiosqlite.Connection, tmp_path: Path) -> None:
    await insert_account(db, ACCOUNT)
    service, _, _ = _service(db, tmp_path)
    first, created1 = await service.create(ACCOUNT, "FROM same")
    again, created2 = await service.create(ACCOUNT, "FROM same")
    assert created1 and not created2 and again.id == first.id


async def test_failed_recipe_is_replaced_on_retry(db: aiosqlite.Connection, tmp_path: Path) -> None:
    await insert_account(db, ACCOUNT)
    failing, _, _ = _service(db, tmp_path, build_ok=False)
    first, _ = await failing.create(ACCOUNT, "FROM retry")
    await failing.tasks.wait(failing.build_task_name(first.id))
    ok, _, _ = _service(db, tmp_path)
    second, created = await ok.create(ACCOUNT, "FROM retry")
    assert created and second.id != first.id
    assert await get_recipe(db, first.id) is None


async def test_resolve_rejects_unknown_and_not_ready(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, _, _ = _service(db, tmp_path, build_ok=False)
    with pytest.raises(NotFound):
        await service.resolve("rcp-nope")
    recipe, _ = await service.create(ACCOUNT, "FROM x")
    with pytest.raises(Conflict):
        await service.resolve(recipe.id)  # still pending


async def test_delete_refuses_referenced_recipes_and_removes_the_volume(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, _ = _service(db, tmp_path)
    recipe, _ = await service.create(ACCOUNT, "FROM x")
    await service.tasks.wait(service.build_task_name(recipe.id))
    await insert_computer(
        db,
        Computer(
            id="comp-1",
            account_id="acct-1",
            thin_volume_id=101,
            tap_device="tap1",
            vm_ip="172.16.1.2",
            socket_path="/tmp/s",
            firecracker_pid=1,
            status=ComputerStatus.RUNNING,
            created_at="t",
            last_exec_at=None,
            recipe_id=recipe.id,
        ),
    )
    with pytest.raises(Conflict):
        await service.delete(ACCOUNT, recipe.id)
    await db.execute("UPDATE computers SET status = 'destroyed'")
    await db.commit()
    await service.delete(ACCOUNT, recipe.id)
    assert ("remove", (100, f"mshkn-recipe-{recipe.id}")) in host.blocks.calls
    with pytest.raises(NotFound):
        await service.get(ACCOUNT, recipe.id)


async def test_ensure_template_builds_once_under_concurrency(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host, _ = _service(db, tmp_path)
    results = await asyncio.gather(*(service.ensure_template(None) for _ in range(3)))
    assert len(host.hypervisor.snapshots) == 1
    assert all(r is not None and r.vmstate.exists() for r in results)
    assert await service.ensure_template(None) == results[0]  # cached in the DB now


async def test_ensure_template_returns_none_when_the_build_fails(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host, _ = _service(db, tmp_path)
    host.hypervisor.fail_next("build_template")
    assert await service.ensure_template(None) is None
