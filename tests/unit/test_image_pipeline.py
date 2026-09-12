"""export_image and inject_tar: the image-to-volume step shared by recipes and the base."""

from __future__ import annotations

from pathlib import Path

import pytest

from mshkn.config import Config
from mshkn.errors import HostError
from mshkn.host.fake import FakeHost
from mshkn.host.shell import ShellError
from mshkn.services.recipes import export_image, inject_tar
from tests.support import FakeShell


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


async def test_export_image_succeeds_when_the_container_removal_fails(tmp_path: Path) -> None:
    shell = FakeShell(fail_on="docker rm")
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
    assert host.blocks.calls == [("mkfs", "mshkn-base"), ("mounted", ("mshkn-base", False))]
    assert shell.calls == [f"tar xf {tar} -C {root}"]
    # _post_process_rootfs ran on the mounted tree
    assert (root / "sbin" / "init").is_symlink()
    assert (root / "root" / ".ssh" / "authorized_keys").read_text() == "ssh-ed25519 AAAA test\n"
    assert (root / "etc" / "systemd" / "system" / "fcnet.service").exists()
    host.close()


async def test_inject_tar_fails_on_an_unmapped_device(tmp_path: Path) -> None:
    host = FakeHost()
    with pytest.raises(HostError, match="not active"):
        await inject_tar(
            FakeShell(),
            host.blocks,
            Config(ssh_key_path=tmp_path / "k"),
            volume_name="nope",
            tar_path=tmp_path / "r.tar",
        )
    host.close()


async def test_post_process_disables_pam_and_login_time_work(tmp_path: Path) -> None:
    """A new SSH connection must not run Ubuntu's login stack (#143, #149).

    On the live host the first session on every connection took 50 ms between
    channel open and command, flat across 23k samples: sshd's post-auth PAM
    pass, with `pam_motd` running `/etc/update-motd.d/*`. `UsePAM no` skips the
    whole stack for pubkey root login. The apt, motd-news, fstrim, e2scrub and
    dpkg timers are masked because a restored guest whose clock `date -s` moves
    forward by days would otherwise fire them all at once inside a user's exec.
    """
    host = FakeHost()
    await host.blocks.activate(volume_id=0, name="mshkn-base")
    config = Config(ssh_key_path=tmp_path / "id_ed25519")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    # What the ubuntu:24.04 image ships, reduced to what the post-process rewrites.
    root = host.blocks.mounts["mshkn-base"]
    (root / "etc" / "ssh").mkdir(parents=True)
    (root / "etc" / "ssh" / "sshd_config").write_text(
        "Include /etc/ssh/sshd_config.d/*.conf\n#PermitRootLogin prohibit-password\n"
        "KbdInteractiveAuthentication no\nUsePAM yes\nPrintMotd no\n"
    )
    motd = root / "etc" / "update-motd.d"
    motd.mkdir(parents=True)
    (motd / "00-header").write_text("#!/bin/sh\n")
    (motd / "50-motd-news").write_text("#!/bin/sh\n")
    (motd / "vendor").mkdir()  # a recipe's Dockerfile may leave a directory here
    (motd / "vendor" / "99-x").write_text("#!/bin/sh\n")
    await inject_tar(
        FakeShell(), host.blocks, config, volume_name="mshkn-base", tar_path=tmp_path / "r.tar"
    )
    sshd_config = (root / "etc" / "ssh" / "sshd_config").read_text()
    assert "UsePAM no" in sshd_config and "UsePAM yes" not in sshd_config
    assert list(motd.iterdir()) == [], "pam_motd has nothing left to run at login"
    units = root / "etc" / "systemd" / "system"
    for timer in (
        "apt-daily.timer",
        "apt-daily-upgrade.timer",
        "dpkg-db-backup.timer",
        "e2scrub_all.timer",
        "fstrim.timer",
        "motd-news.timer",
    ):
        link = units / timer
        assert link.is_symlink() and link.readlink() == Path("/dev/null"), f"{timer} is masked"
    host.close()


async def test_post_process_adds_use_pam_no_when_the_directive_is_absent(tmp_path: Path) -> None:
    host = FakeHost()
    await host.blocks.activate(volume_id=0, name="mshkn-base")
    config = Config(ssh_key_path=tmp_path / "id_ed25519")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    await inject_tar(
        FakeShell(), host.blocks, config, volume_name="mshkn-base", tar_path=tmp_path / "r.tar"
    )
    root = host.blocks.mounts["mshkn-base"]
    assert "UsePAM no" in (root / "etc" / "ssh" / "sshd_config").read_text()
    host.close()


async def test_post_process_installs_the_vsock_shell_listener(tmp_path: Path) -> None:
    """The guest answers the host's reconfiguration over vsock (#55): a socat
    listener on port 52 handing each connection to a shell, enabled at boot so
    every template and checkpoint carries it."""
    host = FakeHost()
    await host.blocks.activate(volume_id=0, name="mshkn-base")
    config = Config(ssh_key_path=tmp_path / "id_ed25519")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    await inject_tar(
        FakeShell(), host.blocks, config, volume_name="mshkn-base", tar_path=tmp_path / "r.tar"
    )
    root = host.blocks.mounts["mshkn-base"]
    unit = root / "etc" / "systemd" / "system" / "mshkn-vsock.service"
    text = unit.read_text()
    assert "socat" in text and "VSOCK-LISTEN:52" in text and "/bin/sh" in text
    link = root / "etc" / "systemd" / "system" / "multi-user.target.wants" / "mshkn-vsock.service"
    assert link.is_symlink()
    host.close()
