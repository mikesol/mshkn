"""export_image and inject_tar: the image-to-volume step shared by recipes and the base."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.host.fake import FakeHost
from mshkn.host.shell import ShellError
from mshkn.services.recipes import export_image, inject_tar
from tests.support import FakeShell

if TYPE_CHECKING:
    from pathlib import Path


async def test_export_image_creates_exports_and_removes_the_container(tmp_path: Path) -> None:
    shell = FakeShell()
    tar = tmp_path / "rootfs.tar"
    await export_image(shell, image_tag="img:1", container_name="tmp-x", tar_path=tar)
    assert shell.calls == [
        "docker create --name tmp-x img:1",
        f"docker export -o {tar} tmp-x",
        "docker rm tmp-x",
    ]


async def test_export_image_removes_the_container_when_export_fails(tmp_path: Path) -> None:
    shell = FakeShell(fail_on="docker export")
    with pytest.raises(ShellError):
        await export_image(
            shell, image_tag="img:1", container_name="tmp-x", tar_path=tmp_path / "r.tar"
        )
    assert shell.calls[-1] == "docker rm tmp-x"


async def test_inject_tar_formats_unpacks_and_post_processes(tmp_path: Path) -> None:
    host = FakeHost()
    await host.blocks.activate(volume_id=0, name="mshkn-base")
    config = Config(ssh_key_path=tmp_path / "id_ed25519")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    shell = FakeShell()
    tar = tmp_path / "rootfs.tar"
    await inject_tar(shell, host.blocks, config, volume_name="mshkn-base", tar_path=tar)
    root = host.blocks.mounts["mshkn-base"]
    assert ("mkfs", "mshkn-base") in host.blocks.calls
    assert shell.calls == [f"tar xf {tar} -C {root}"]
    # _post_process_rootfs ran on the mounted tree
    assert (root / "sbin" / "init").is_symlink()
    assert (root / "root" / ".ssh" / "authorized_keys").read_text() == "ssh-ed25519 AAAA test\n"
    assert (root / "etc" / "systemd" / "system" / "fcnet.service").exists()
    host.close()


async def test_inject_tar_fails_on_an_unmapped_device(tmp_path: Path) -> None:
    host = FakeHost()
    with pytest.raises(Exception, match="not"):
        await inject_tar(
            FakeShell(),
            host.blocks,
            Config(ssh_key_path=tmp_path / "k"),
            volume_name="nope",
            tar_path=tmp_path / "r.tar",
        )
    host.close()
