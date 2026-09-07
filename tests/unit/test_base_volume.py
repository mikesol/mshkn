"""The base-volume pipeline: build mshkn-base, export it, write volume 0, drop the bare template."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import cache_bare_template, clear_bare_template, get_bare_template
from mshkn.errors import ConfigError, Conflict
from mshkn.host.fake import FakeHost
from mshkn.host.shell import ShellError
from mshkn.services.base_volume import (
    BASE_DEVICE,
    BASE_IMAGE,
    DEFAULT_DOCKERFILE,
    write_base_volume,
)
from tests.support import FakeShell

if TYPE_CHECKING:
    import aiosqlite

INACTIVE = "systemctl is-active"  # FakeShell(fail_on=INACTIVE): the service is not running


async def test_clear_bare_template_removes_the_row_and_tolerates_none(
    db: aiosqlite.Connection,
) -> None:
    await cache_bare_template(db, "/t/vmstate", "/t/memory")
    assert await get_bare_template(db) == ("/t/vmstate", "/t/memory")
    await clear_bare_template(db)
    assert await get_bare_template(db) is None
    await clear_bare_template(db)  # nothing to delete is not an error
    assert await get_bare_template(db) is None


def _config(tmp_path: Path, *, with_key: bool = True) -> Config:
    config = Config(ssh_key_path=tmp_path / "id_ed25519", checkpoint_local_dir=tmp_path / "ckpts")
    if with_key:
        (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    return config


def _dockerfile(tmp_path: Path) -> Path:
    path = tmp_path / "Dockerfile.mshkn-base"
    path.write_text("FROM ubuntu:24.04\nCOPY mshkn_key.pub /root/.ssh/authorized_keys\n")
    return path


def test_default_dockerfile_is_the_repository_one() -> None:
    assert DEFAULT_DOCKERFILE.name == "Dockerfile.mshkn-base"
    assert DEFAULT_DOCKERFILE.exists()
    assert BASE_IMAGE == "mshkn-base" and BASE_DEVICE == "mshkn-base"


async def test_refuses_while_the_service_is_active(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    host = FakeHost()
    shell = FakeShell()  # every command succeeds, including is-active
    with pytest.raises(Conflict, match="stop it"):
        await write_base_volume(
            config=_config(tmp_path),
            db=db,
            blocks=host.blocks,
            dockerfile=_dockerfile(tmp_path),
            run=shell,
        )
    assert shell.calls == ["systemctl is-active --quiet mshkn"]
    assert host.blocks.calls == []
    host.close()


async def test_refuses_without_the_public_key_or_the_dockerfile(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    host = FakeHost()
    with pytest.raises(ConfigError, match="public key"):
        await write_base_volume(
            config=_config(tmp_path, with_key=False),
            db=db,
            blocks=host.blocks,
            dockerfile=_dockerfile(tmp_path),
            run=FakeShell(fail_on=INACTIVE),
        )
    with pytest.raises(ConfigError, match="Dockerfile"):
        await write_base_volume(
            config=_config(tmp_path),
            db=db,
            blocks=host.blocks,
            dockerfile=tmp_path / "missing",
            run=FakeShell(fail_on=INACTIVE),
        )
    assert host.blocks.calls == []
    host.close()


async def test_builds_exports_writes_volume_zero_and_drops_the_bare_template(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    host = FakeHost()
    await host.blocks.activate(volume_id=0, name="mshkn-base")  # what mshkn-pool-up does
    config = _config(tmp_path)
    bare_dir = config.checkpoint_local_dir / "templates" / "bare"
    bare_dir.mkdir(parents=True)
    (bare_dir / "vmstate").write_bytes(b"old")
    await cache_bare_template(db, str(bare_dir / "vmstate"), str(bare_dir / "memory"))
    shell = FakeShell(fail_on=INACTIVE)
    seen: dict[str, object] = {}

    async def build_image(cmd: str) -> str:
        build_dir = Path(cmd.split()[-1])
        seen["cmd"] = cmd
        seen["context"] = sorted(p.name for p in build_dir.iterdir())
        seen["key"] = (build_dir / "mshkn_key.pub").read_text()
        seen["build_dir"] = build_dir
        return "Successfully built base"

    log = await write_base_volume(
        config=config,
        db=db,
        blocks=host.blocks,
        dockerfile=_dockerfile(tmp_path),
        run=shell,
        build_image=build_image,
    )
    assert log == "Successfully built base"
    cmd = str(seen["cmd"])
    assert cmd.startswith("docker build --memory=4g --cpuset-cpus=0-1 -t mshkn-base ")
    assert seen["context"] == ["Dockerfile", "mshkn_key.pub"]
    assert seen["key"] == "ssh-ed25519 AAAA test\n"
    build_dir = seen["build_dir"]
    assert isinstance(build_dir, Path) and not build_dir.exists()  # context removed afterwards
    root = host.blocks.mounts["mshkn-base"]
    assert shell.calls == [
        "systemctl is-active --quiet mshkn",
        "docker create --name tmp-mshkn-base mshkn-base",
        f"docker export -o {build_dir / 'rootfs.tar'} tmp-mshkn-base",
        "docker rm tmp-mshkn-base",
        f"tar xf {build_dir / 'rootfs.tar'} -C {root}",
    ]
    assert ("mkfs", "mshkn-base") in host.blocks.calls
    assert host.blocks.volumes == {0: None}  # no snapshot: volume 0 itself was written
    assert (root / "sbin" / "init").is_symlink()
    assert await get_bare_template(db) is None
    assert not bare_dir.exists()
    host.close()


async def test_a_failed_export_removes_the_context_and_keeps_the_template(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    host = FakeHost()
    await host.blocks.activate(volume_id=0, name="mshkn-base")
    await cache_bare_template(db, "/t/vmstate", "/t/memory")
    seen: dict[str, Path] = {}

    async def build_image(cmd: str) -> str:
        seen["build_dir"] = Path(cmd.split()[-1])
        return "ok"

    with pytest.raises(ShellError):
        await write_base_volume(
            config=_config(tmp_path),
            db=db,
            blocks=host.blocks,
            dockerfile=_dockerfile(tmp_path),
            run=FakeShell(fail_on=(INACTIVE, "docker export")),
            build_image=build_image,
        )
    assert not seen["build_dir"].exists()
    assert ("mkfs", "mshkn-base") not in host.blocks.calls
    assert await get_bare_template(db) == ("/t/vmstate", "/t/memory")
    host.close()
