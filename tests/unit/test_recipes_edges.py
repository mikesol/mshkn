"""RecipeService and docker_build_image branches the main recipe tests do not reach."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import (
    get_recipe,
    insert_account,
    insert_computer,
    insert_recipe,
    update_recipe_template,
)
from mshkn.errors import Conflict, NotFound
from mshkn.host import SnapshotFiles
from mshkn.models import RecipeStatus
from mshkn.services.recipes import _post_process_rootfs, docker_build_image
from tests.support import account_row, computer_row, recipe_row
from tests.unit.test_recipe_service import ACCOUNT, _service

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

OTHER = account_row("acct-2", api_key="k2")


async def test_docker_build_image_returns_output_and_raises_on_failure() -> None:
    assert "built" in await docker_build_image("echo built")
    with pytest.raises(RuntimeError, match=r"docker build failed \(rc=2\)"):
        await docker_build_image("echo bad >&2; exit 2")


async def test_docker_build_image_kills_a_build_that_outlives_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build left running would keep its 4 GB reservation until it finished."""
    import mshkn.services.recipes as recipes

    pidfile = tmp_path / "build.pid"
    monkeypatch.setattr(recipes, "_DOCKER_BUILD_TIMEOUT_SECONDS", 0.3)

    with pytest.raises(RuntimeError, match="timed out"):
        # exec so the pid the shell records is the long-running process itself,
        # not a wrapper whose death would leave the real one orphaned.
        await docker_build_image(f"echo $$ > {pidfile}; exec sleep 30")

    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


async def test_docker_build_image_times_out_before_the_process_even_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no handle to kill when the spawn itself is what overran."""
    import mshkn.services.recipes as recipes

    async def slow_spawn(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(5)

    monkeypatch.setattr(recipes, "_DOCKER_BUILD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", slow_spawn)

    with pytest.raises(RuntimeError, match="timed out"):
        await docker_build_image("docker build .")


async def test_delete_without_a_base_volume_removes_nothing(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, _ = _service(db, tmp_path, build_ok=False)
    recipe, _ = await service.create(ACCOUNT, "FROM x")
    await service.tasks.wait(service.build_task_name(recipe.id))  # failed → base_volume_id None

    await service.delete(ACCOUNT, recipe.id)

    assert not any(name == "remove" for name, _ in host.blocks.calls)
    with pytest.raises(NotFound):
        await service.get(ACCOUNT, recipe.id)


async def test_ensure_template_uses_a_cached_recipe_template(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, _ = _service(db, tmp_path)
    await insert_recipe(db, recipe_row("rcp-c", base_volume_id=0))
    await update_recipe_template(db, "rcp-c", str(tmp_path / "vm"), str(tmp_path / "mem"))

    recipe = await service.resolve("rcp-c")
    files = await service.ensure_template(recipe)

    assert files == SnapshotFiles(vmstate=tmp_path / "vm", memory=tmp_path / "mem")
    assert host.hypervisor.snapshots == []


async def test_get_and_list_are_scoped_to_the_account(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    await insert_account(db, OTHER)
    service, _, _ = _service(db, tmp_path)
    mine, _ = await service.create(ACCOUNT, "FROM mine")
    await service.tasks.wait(service.build_task_name(mine.id))

    with pytest.raises(NotFound):
        await service.get(OTHER, mine.id)
    assert [r.id for r in await service.list(ACCOUNT)] == [mine.id]
    assert await service.list(OTHER) == []


async def test_a_referenced_recipe_cannot_be_deleted(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, _, _ = _service(db, tmp_path)
    await insert_recipe(db, recipe_row("rcp-used", base_volume_id=0))
    await insert_computer(db, computer_row(1, recipe_id="rcp-used"))

    with pytest.raises(Conflict, match="referenced by 1"):
        await service.delete(ACCOUNT, "rcp-used")


async def test_resolve_rejects_a_ready_recipe_with_no_base_volume(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, _, _ = _service(db, tmp_path)
    await insert_recipe(db, recipe_row("rcp-novol", base_volume_id=None))

    with pytest.raises(Conflict, match="has no base volume"):
        await service.resolve("rcp-novol")


async def test_ensure_template_rejects_a_recipe_with_no_base_volume(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """resolve() already refuses this; ensure_template checks again on its own."""
    await insert_account(db, ACCOUNT)
    service, _, _ = _service(db, tmp_path)
    recipe = recipe_row("rcp-novol", base_volume_id=None)
    await insert_recipe(db, recipe)

    with pytest.raises(Conflict, match="has no base volume"):
        await service.ensure_template(recipe)


async def test_build_writes_an_empty_key_when_the_host_has_no_public_key(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, _, _ = _service(db, tmp_path)
    (tmp_path / "id_ed25519.pub").unlink()

    recipe, _ = await service.create(ACCOUNT, "FROM keyless")
    await service.tasks.wait(service.build_task_name(recipe.id))

    stored = await get_recipe(db, recipe.id)
    assert stored is not None and stored.status is RecipeStatus.READY


def _rootfs(tmp_path: Path) -> Path:
    """A minimal exported rootfs whose SSH host keys already exist."""
    mp = tmp_path / "rootfs"
    etc_ssh = mp / "etc" / "ssh"
    etc_ssh.mkdir(parents=True)
    for key_type in ("rsa", "ecdsa", "ed25519"):
        (etc_ssh / f"ssh_host_{key_type}_key").write_text("key")
    return mp


def test_post_process_rewrites_an_already_configured_rootfs(tmp_path: Path) -> None:
    mp = _rootfs(tmp_path)
    (mp / "etc" / "ssh" / "sshd_config").write_text(
        "#PermitRootLogin prohibit-password\n#PubkeyAuthentication no\n"
    )
    (mp / "sbin").mkdir()
    (mp / "sbin" / "init").symlink_to("/bin/busybox")
    (mp / ".dockerenv").touch()
    wants = mp / "etc" / "systemd" / "system" / "sysinit.target.wants"
    wants.mkdir(parents=True)
    (wants / "fcnet.service").write_text("already enabled")
    config = Config(ssh_key_path=tmp_path / "absent")  # no .pub alongside it

    _post_process_rootfs(mp, config)

    sshd_config = (mp / "etc" / "ssh" / "sshd_config").read_text()
    assert "PermitRootLogin yes" in sshd_config
    assert "PubkeyAuthentication yes" in sshd_config
    assert "prohibit-password" not in sshd_config
    assert (mp / "root" / ".ssh" / "authorized_keys").read_text() == ""
    assert str((mp / "sbin" / "init").readlink()) == "/lib/systemd/systemd"
    assert not (mp / ".dockerenv").exists()
    assert (wants / "fcnet.service").read_text() == "already enabled"


def test_post_process_leaves_a_real_init_binary_alone(tmp_path: Path) -> None:
    mp = _rootfs(tmp_path)
    (mp / "sbin").mkdir()
    (mp / "sbin" / "init").write_text("#!/bin/sh\n")
    (tmp_path / "id.pub").write_text("ssh-ed25519 AAAA test\n")

    _post_process_rootfs(mp, Config(ssh_key_path=tmp_path / "id"))

    assert (mp / "sbin" / "init").read_text() == "#!/bin/sh\n"
    assert (mp / "root" / ".ssh" / "authorized_keys").read_text() == "ssh-ed25519 AAAA test\n"
