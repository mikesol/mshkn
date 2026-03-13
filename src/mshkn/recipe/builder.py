"""Recipe builder: Docker build → export → dm-thin inject."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import shutil
import subprocess
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.db import update_recipe_build_result, update_recipe_status
from mshkn.shell import run
from mshkn.vm.storage import create_snapshot, mount_volume, umount_volume

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config

logger = logging.getLogger(__name__)


def dockerfile_content_hash(dockerfile: str) -> str:
    """Return SHA256 hex digest of the dockerfile string."""
    return hashlib.sha256(dockerfile.encode()).hexdigest()


async def build_recipe(
    db: aiosqlite.Connection,
    config: Config,
    recipe_id: str,
    dockerfile: str,
    content_hash: str,
    allocate_volume_id: int,
) -> None:
    """Full recipe build pipeline.

    Phases:
      1. Docker Build
      2. Export (docker create → docker export → docker rm)
      3. Inject into dm-thin (snapshot from vol 0, mkfs, mount, tar, post-process, unmount)
      3.5. Cleanup (always, in finally)
      4. Ready (set status=ready)

    On failure: set status=failed with build_log containing error details.
    """
    build_dir = Path(f"/tmp/mshkn-build-{content_hash}")
    tar_path = build_dir / "rootfs.tar"
    container_name = f"tmp-{recipe_id}"
    volume_name = f"mshkn-recipe-{recipe_id}"
    mount_point: str | None = None
    volume_created = False
    image_tag = f"mshkn-recipe-img-{recipe_id}"

    build_log_lines: list[str] = []

    try:
        # ── Phase 1: Docker Build ─────────────────────────────────────────────
        await update_recipe_status(db, recipe_id, "building")
        logger.info("recipe %s: starting docker build", recipe_id)

        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "Dockerfile").write_text(dockerfile)

        # Copy SSH pub key into build context so Dockerfile can COPY it
        pub_key_path = config.ssh_key_path.with_suffix(".pub")
        if pub_key_path.exists():
            shutil.copy(pub_key_path, build_dir / "mshkn_key.pub")
        else:
            # Create a placeholder — _post_process_rootfs will fix authorized_keys
            (build_dir / "mshkn_key.pub").write_text("")

        build_cmd = (
            f"docker build --memory=4g --cpuset-cpus=0-1 "
            f"-t {image_tag} {build_dir}"
        )
        logger.debug("recipe %s: %s", recipe_id, build_cmd)

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    build_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                ),
                timeout=600,  # 10 minutes
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            build_output = stdout_bytes.decode(errors="replace")
            build_log_lines.append(build_output)

            if proc.returncode != 0:
                raise RuntimeError(
                    f"docker build failed (rc={proc.returncode}):\n{build_output}"
                )
        except TimeoutError as exc:
            raise RuntimeError("docker build timed out after 10 minutes") from exc

        logger.info("recipe %s: docker build complete", recipe_id)

        # ── Phase 2: Export ───────────────────────────────────────────────────
        await update_recipe_status(db, recipe_id, "exporting")
        logger.info("recipe %s: exporting filesystem", recipe_id)

        await run(f"docker create --name {container_name} {image_tag}")
        await run(f"docker export -o {tar_path} {container_name}")
        await run(f"docker rm {container_name}")

        logger.info("recipe %s: export complete (%d bytes)", recipe_id, tar_path.stat().st_size)

        # ── Phase 3: Inject into dm-thin ─────────────────────────────────────
        await update_recipe_status(db, recipe_id, "injecting")
        logger.info("recipe %s: injecting into dm-thin vol %d", recipe_id, allocate_volume_id)

        await create_snapshot(
            config.thin_pool_name,
            source_volume_id=0,
            new_volume_id=allocate_volume_id,
            new_volume_name=volume_name,
            sectors=config.thin_volume_sectors,
        )
        volume_created = True

        await run(f"mkfs.ext4 -F /dev/mapper/{volume_name}")

        mount_point = tempfile.mkdtemp(prefix="mshkn-recipe-mount-")
        await mount_volume(volume_name, mount_point)

        try:
            await run(f"tar xf {tar_path} -C {mount_point}")
            await _post_process_rootfs(mount_point, config)
        finally:
            await umount_volume(mount_point)
            Path(mount_point).rmdir()
            mount_point = None

        await run(f"dmsetup remove {volume_name}")
        volume_created = False

        logger.info("recipe %s: injection complete", recipe_id)

        # ── Phase 4: Ready ────────────────────────────────────────────────────
        built_at = datetime.now(UTC).isoformat()
        await update_recipe_build_result(
            db,
            recipe_id,
            status="ready",
            build_log="\n".join(build_log_lines),
            base_volume_id=allocate_volume_id,
            built_at=built_at,
        )
        logger.info("recipe %s: ready (vol %d)", recipe_id, allocate_volume_id)

    except Exception as exc:
        error_detail = traceback.format_exc()
        build_log_lines.append(f"\n--- BUILD FAILED ---\n{error_detail}")
        logger.error("recipe %s: build failed: %s", recipe_id, exc)
        await update_recipe_build_result(
            db,
            recipe_id,
            status="failed",
            build_log="\n".join(build_log_lines),
        )

    finally:
        # ── Phase 3.5: Cleanup ────────────────────────────────────────────────
        # Unmount if still mounted
        if mount_point is not None:
            with contextlib.suppress(Exception):
                await umount_volume(mount_point)
            with contextlib.suppress(Exception):
                Path(mount_point).rmdir()

        # Remove dm-thin volume if still active
        if volume_created:
            with contextlib.suppress(Exception):
                await run(f"dmsetup remove {volume_name}", check=False)

        # Remove build directory
        with contextlib.suppress(Exception):
            shutil.rmtree(build_dir, ignore_errors=True)

        # Remove tar
        with contextlib.suppress(Exception):
            tar_path.unlink(missing_ok=True)

        # Remove container (in case export failed after create)
        with contextlib.suppress(Exception):
            subprocess.run(
                ["docker", "rm", container_name],
                check=False,
                capture_output=True,
            )

        # Remove image
        with contextlib.suppress(Exception):
            subprocess.run(
                ["docker", "rmi", image_tag],
                check=False,
                capture_output=True,
            )


async def _post_process_rootfs(mount_point: str, config: Config) -> None:
    """Force-write Firecracker-required config into the rootfs."""
    mp = Path(mount_point)

    # Generate SSH host keys if missing
    etc_ssh = mp / "etc" / "ssh"
    etc_ssh.mkdir(parents=True, exist_ok=True)
    for key_type in ("rsa", "ecdsa", "ed25519"):
        host_key = etc_ssh / f"ssh_host_{key_type}_key"
        if not host_key.exists():
            subprocess.run(
                ["ssh-keygen", "-q", "-N", "", "-t", key_type, "-f", str(host_key)],
                check=False,
                capture_output=True,
            )

    # Write authorized_keys from config pub key
    pub_key_path = config.ssh_key_path.with_suffix(".pub")
    root_ssh = mp / "root" / ".ssh"
    root_ssh.mkdir(parents=True, exist_ok=True)
    root_ssh.chmod(0o700)
    authorized_keys = root_ssh / "authorized_keys"
    if pub_key_path.exists():
        authorized_keys.write_text(pub_key_path.read_text())
    else:
        # Ensure the file exists even if empty
        authorized_keys.touch()
    authorized_keys.chmod(0o600)

    # Fix sshd_config
    sshd_config_path = etc_ssh / "sshd_config"
    sshd_config = sshd_config_path.read_text() if sshd_config_path.exists() else ""

    # Ensure PermitRootLogin yes
    if "PermitRootLogin" not in sshd_config:
        sshd_config += "\nPermitRootLogin yes\n"
    else:
        import re
        sshd_config = re.sub(
            r"#?PermitRootLogin\s+\S+",
            "PermitRootLogin yes",
            sshd_config,
        )

    # Ensure PubkeyAuthentication yes
    if "PubkeyAuthentication" not in sshd_config:
        sshd_config += "PubkeyAuthentication yes\n"
    else:
        import re
        sshd_config = re.sub(
            r"#?PubkeyAuthentication\s+\S+",
            "PubkeyAuthentication yes",
            sshd_config,
        )

    sshd_config_path.write_text(sshd_config)

    # Create /sbin/init symlink
    sbin = mp / "sbin"
    sbin.mkdir(parents=True, exist_ok=True)
    init_link = sbin / "init"
    if init_link.is_symlink():
        init_link.unlink()
    if not init_link.exists():
        init_link.symlink_to("/lib/systemd/systemd")

    # Write /etc/environment with standard PATH
    etc = mp / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "environment").write_text(
        'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
    )

    # Remove .dockerenv so systemd doesn't detect Docker virtualization
    dockerenv = mp / ".dockerenv"
    if dockerenv.exists():
        dockerenv.unlink()

    # Install fcnet network setup (derives IP from MAC address — required for Firecracker)
    fcnet_script = mp / "usr" / "local" / "bin" / "fcnet-setup.sh"
    fcnet_script.parent.mkdir(parents=True, exist_ok=True)
    fcnet_script.write_text(
        '#!/bin/bash\n'
        '# Wait up to 1s for any non-loopback interface to appear.\n'
        'for i in $(seq 1 200); do\n'
        '    if [ "$(ls /sys/class/net | grep -v lo | wc -l)" -gt 0 ]; then\n'
        '        break\n'
        '    fi\n'
        '    sleep 0.005\n'
        'done\n'
        'for dev in $(ls /sys/class/net | grep -v lo); do\n'
        '    mac_ip=$(ip link show dev "$dev" | grep link/ether | '
        'grep -oP "(?<=06:00:)[0-9a-f:]{11}")\n'
        '    if [ -n "$mac_ip" ]; then\n'
        '        ip=$(printf "%d.%d.%d.%d" $(echo "0x${mac_ip}" | '
        'sed "s/:/ 0x/g"))\n'
        '        ip addr add "$ip/30" dev "$dev"\n'
        '        ip link set "$dev" up\n'
        '        gw=$(echo "$ip" | awk -F. \'{printf "%d.%d.%d.%d", '
        "$1, $2, $3, $4-1}')\n"
        '        ip route add default via "$gw"\n'
        '    fi\n'
        'done\n'
    )
    fcnet_script.chmod(0o755)

    # Install fcnet systemd service
    fcnet_unit = mp / "etc" / "systemd" / "system" / "fcnet.service"
    fcnet_unit.parent.mkdir(parents=True, exist_ok=True)
    fcnet_unit.write_text(
        "[Unit]\n"
        "Description=Firecracker network setup\n"
        "DefaultDependencies=no\n"
        "Before=network.target network-pre.target\n"
        "Wants=ssh.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/bin/fcnet-setup.sh\n"
        "RemainAfterExit=true\n"
    )

    # Enable fcnet.service in sysinit.target
    sysinit_wants = mp / "etc" / "systemd" / "system" / "sysinit.target.wants"
    sysinit_wants.mkdir(parents=True, exist_ok=True)
    fcnet_link = sysinit_wants / "fcnet.service"
    if not fcnet_link.exists():
        fcnet_link.symlink_to("/etc/systemd/system/fcnet.service")


async def ensure_base_image(config: Config) -> None:
    """Build the mshkn-base Docker image if it doesn't exist locally."""
    # Check if image already exists
    try:
        await run("docker image inspect mshkn-base", check=True)
        logger.info("mshkn-base image already exists, skipping build")
        return
    except Exception:
        pass

    logger.info("mshkn-base image not found, building...")

    # Find Dockerfile.mshkn-base relative to this file's package root
    # It lives at the repo root — walk up from src/mshkn/recipe/
    this_file = Path(__file__).resolve()
    # Go up: recipe/ -> mshkn/ -> src/ -> repo_root
    repo_root = this_file.parent.parent.parent.parent
    dockerfile_src = repo_root / "Dockerfile.mshkn-base"

    with tempfile.TemporaryDirectory(prefix="mshkn-base-build-") as build_ctx:
        build_ctx_path = Path(build_ctx)
        shutil.copy(dockerfile_src, build_ctx_path / "Dockerfile")

        pub_key_path = config.ssh_key_path.with_suffix(".pub")
        if pub_key_path.exists():
            shutil.copy(pub_key_path, build_ctx_path / "mshkn_key.pub")
        else:
            (build_ctx_path / "mshkn_key.pub").write_text("")

        await run(f"docker build -t mshkn-base {build_ctx}")

    logger.info("mshkn-base image built successfully")
