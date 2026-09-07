"""Recipes: Docker build → export → dm-thin inject, and L3 template caching (spec §6.6)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import shutil
import subprocess
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.db import (
    cache_bare_template,
    count_recipe_references,
    delete_failed_recipes_by_hash,
    delete_recipe,
    get_bare_template,
    get_recipe,
    get_recipe_by_content_hash,
    insert_recipe,
    list_recipes_by_account,
    update_recipe_build_result,
    update_recipe_status,
    update_recipe_template,
)
from mshkn.errors import Conflict, NotFound
from mshkn.host import SnapshotFiles
from mshkn.host.shell import run as shell_run
from mshkn.models import Recipe, RecipeStatus, recipe_volume_name
from mshkn.observability.metrics import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import BlockStore, Hypervisor
    from mshkn.host.shell import RunFn
    from mshkn.models import Account
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.allocator import SlotAllocator

    BuildImageFn = Callable[[str], Awaitable[str]]

logger = logging.getLogger(__name__)

_DOCKER_BUILD_TIMEOUT_SECONDS = 600


def dockerfile_content_hash(dockerfile: str) -> str:
    """SHA-256 hex digest of the Dockerfile text."""
    return hashlib.sha256(dockerfile.encode()).hexdigest()


async def docker_build_image(cmd: str) -> str:
    """Run `docker build …`, returning its combined output; raise on failure or timeout.

    A build that overruns the timeout is killed before the error is raised. An
    abandoned one would hold its memory reservation and its share of the CPU set
    for as long as it took to finish, long after the caller gave up on it.
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            ),
            timeout=_DOCKER_BUILD_TIMEOUT_SECONDS,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_DOCKER_BUILD_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        if proc is not None:
            logger.warning("docker build timed out; killing pid %d", proc.pid)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        raise RuntimeError("docker build timed out after 10 minutes") from exc
    output = stdout.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"docker build failed (rc={proc.returncode}):\n{output}")
    return output


class RecipeService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        blocks: BlockStore,
        hypervisor: Hypervisor,
        allocator: SlotAllocator,
        tasks: BackgroundTasks,
        *,
        run: RunFn = shell_run,
        build_image: BuildImageFn = docker_build_image,
    ) -> None:
        self.config = config
        self.db = db
        self.blocks = blocks
        self.hypervisor = hypervisor
        self.allocator = allocator
        self.tasks = tasks
        self._run = run
        self._build_image = build_image
        self._build_locks: dict[str, asyncio.Lock] = {}  # per account
        self._template_locks: dict[str, asyncio.Lock] = {}  # per recipe id / "bare"

    @staticmethod
    def build_task_name(recipe_id: str) -> str:
        return f"recipe_build:{recipe_id}"

    # -- CRUD ----------------------------------------------------------------

    async def create(self, account: Account, dockerfile: str) -> tuple[Recipe, bool]:
        content_hash = dockerfile_content_hash(dockerfile)
        existing = await get_recipe_by_content_hash(self.db, account.id, content_hash)
        if existing is not None:
            return existing, False
        await delete_failed_recipes_by_hash(self.db, account.id, content_hash)
        recipe = Recipe(
            id=f"rcp-{uuid.uuid4().hex[:12]}",
            account_id=account.id,
            dockerfile=dockerfile,
            content_hash=content_hash,
            status=RecipeStatus.PENDING,
            build_log=None,
            base_volume_id=None,
            template_vmstate=None,
            template_memory=None,
            created_at=datetime.now(UTC).isoformat(),
            built_at=None,
        )
        await insert_recipe(self.db, recipe)
        volume_id = await self.allocator.acquire_volume_id()
        lock = self._build_locks.setdefault(account.id, asyncio.Lock())

        async def _run_build() -> None:
            async with lock, timed("recipe_build"):
                await self.build(recipe.id, dockerfile, content_hash, volume_id)

        task_name = self.build_task_name(recipe.id)
        self.tasks.spawn(_run_build(), name=task_name, key=task_name)
        return recipe, True

    async def get(self, account: Account, recipe_id: str) -> Recipe:
        recipe = await get_recipe(self.db, recipe_id)
        if recipe is None or recipe.account_id != account.id:
            raise NotFound("Recipe not found")
        return recipe

    async def list(self, account: Account) -> list[Recipe]:
        return await list_recipes_by_account(self.db, account.id)

    async def delete(self, account: Account, recipe_id: str) -> None:
        recipe = await self.get(account, recipe_id)
        refs = await count_recipe_references(self.db, recipe_id)
        if refs > 0:
            raise Conflict(f"Recipe is referenced by {refs} computer(s)/checkpoint(s)")
        if recipe.base_volume_id is not None:
            await self.blocks.remove(volume_id=recipe.base_volume_id, name=recipe.volume_name)
        await delete_recipe(self.db, recipe_id)

    async def resolve(self, recipe_id: str) -> Recipe:
        """The recipe a computer can be created from, or the reason it cannot."""
        recipe = await get_recipe(self.db, recipe_id)
        if recipe is None:
            raise NotFound(f"Recipe {recipe_id} not found")
        if recipe.status != RecipeStatus.READY:
            raise Conflict(f"Recipe {recipe_id} is not ready (status={recipe.status})")
        if recipe.base_volume_id is None:
            raise Conflict(f"Recipe {recipe_id} has no base volume")
        return recipe

    # -- L3 templates --------------------------------------------------------

    async def ensure_template(self, recipe: Recipe | None) -> SnapshotFiles | None:
        """The template snapshot for a recipe (or the bare base), built at most once.

        Concurrent first callers share one build through a per-key lock; a
        build failure logs a warning and returns None so the caller cold-boots.
        """
        key = recipe.id if recipe is not None else "bare"
        lock = self._template_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await self._cached_template(recipe)
            if cached is not None:
                return cached
            source_volume_id = 0
            if recipe is not None:
                if recipe.base_volume_id is None:  # resolve() rejects this; belt and braces
                    raise Conflict(f"Recipe {recipe.id} has no base volume")
                source_volume_id = recipe.base_volume_id
            dest_dir = self.config.checkpoint_local_dir / "templates" / key
            try:
                files = await self.hypervisor.build_template(
                    disk_volume_id=source_volume_id, dest_dir=dest_dir
                )
            except Exception:
                logger.warning("L3 template build failed for %s, will cold-boot", key)
                return None
            if recipe is not None:
                await update_recipe_template(
                    self.db, recipe.id, str(files.vmstate), str(files.memory)
                )
            else:
                await cache_bare_template(self.db, str(files.vmstate), str(files.memory))
            logger.info("Built L3 template for %s", key)
            return files

    async def _cached_template(self, recipe: Recipe | None) -> SnapshotFiles | None:
        if recipe is not None:
            fresh = await get_recipe(self.db, recipe.id)  # another caller may have cached it
            if fresh is not None and fresh.template_vmstate and fresh.template_memory:
                return SnapshotFiles(
                    vmstate=Path(fresh.template_vmstate), memory=Path(fresh.template_memory)
                )
            return None
        bare = await get_bare_template(self.db)
        if bare is not None:
            return SnapshotFiles(vmstate=Path(bare[0]), memory=Path(bare[1]))
        return None

    # -- build pipeline ------------------------------------------------------

    async def build(
        self, recipe_id: str, dockerfile: str, content_hash: str, volume_id: int
    ) -> None:
        """Docker build → export → inject into dm-thin → ready; failed with a log otherwise."""
        build_dir = Path(f"/tmp/mshkn-build-{content_hash}")
        tar_path = build_dir / "rootfs.tar"
        container_name = f"tmp-{recipe_id}"
        volume_name = recipe_volume_name(recipe_id)
        image_tag = f"mshkn-recipe-img-{recipe_id}"
        device_active = False
        build_log_lines: list[str] = []
        try:
            await update_recipe_status(self.db, recipe_id, RecipeStatus.BUILDING)
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "Dockerfile").write_text(dockerfile)
            pub_key_path = self.config.ssh_key_path.with_suffix(".pub")
            if pub_key_path.exists():
                shutil.copy(pub_key_path, build_dir / "mshkn_key.pub")
            else:
                (build_dir / "mshkn_key.pub").write_text("")
            build_cmd = f"docker build --memory=4g --cpuset-cpus=0-1 -t {image_tag} {build_dir}"
            build_log_lines.append(await self._build_image(build_cmd))

            await update_recipe_status(self.db, recipe_id, RecipeStatus.EXPORTING)
            await self._run(f"docker create --name {container_name} {image_tag}")
            await self._run(f"docker export -o {tar_path} {container_name}")
            await self._run(f"docker rm {container_name}")

            await update_recipe_status(self.db, recipe_id, RecipeStatus.INJECTING)
            await self.blocks.snap(source_volume_id=0, new_volume_id=volume_id)
            await self.blocks.activate(volume_id=volume_id, name=volume_name)
            device_active = True
            await self.blocks.mkfs(volume_name)
            async with self.blocks.mounted(volume_name) as mount_point:
                await self._run(f"tar xf {tar_path} -C {mount_point}")
                await asyncio.to_thread(_post_process_rootfs, mount_point, self.config)
            await self.blocks.deactivate(volume_name)
            device_active = False

            await update_recipe_build_result(
                self.db,
                recipe_id,
                status=RecipeStatus.READY,
                build_log="\n".join(build_log_lines),
                base_volume_id=volume_id,
                built_at=datetime.now(UTC).isoformat(),
            )
            logger.info("recipe %s: ready (vol %d)", recipe_id, volume_id)
        except Exception as exc:
            build_log_lines.append(f"\n--- BUILD FAILED ---\n{traceback.format_exc()}")
            logger.error("recipe %s: build failed: %s", recipe_id, exc)
            await update_recipe_build_result(
                self.db, recipe_id, status=RecipeStatus.FAILED, build_log="\n".join(build_log_lines)
            )
        finally:
            if device_active:
                with contextlib.suppress(Exception):
                    await self.blocks.deactivate(volume_name)
            shutil.rmtree(build_dir, ignore_errors=True)
            with contextlib.suppress(Exception):
                await self._run(f"docker rm {container_name}", check=False)
            with contextlib.suppress(Exception):
                await self._run(f"docker rmi {image_tag}", check=False)


def _post_process_rootfs(mount_point: Path, config: Config) -> None:
    """Force-write the Firecracker-required config into an exported rootfs."""
    mp = mount_point

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
        sshd_config = re.sub(
            r"#?PermitRootLogin\s+\S+",
            "PermitRootLogin yes",
            sshd_config,
        )

    # Ensure PubkeyAuthentication yes
    if "PubkeyAuthentication" not in sshd_config:
        sshd_config += "PubkeyAuthentication yes\n"
    else:
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

    # Write /etc/resolv.conf (Docker export strips it)
    (etc / "resolv.conf").write_text("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")

    # Remove .dockerenv so systemd doesn't detect Docker virtualization
    dockerenv = mp / ".dockerenv"
    if dockerenv.exists():
        dockerenv.unlink()

    # Install fcnet network setup (derives IP from MAC address — required for Firecracker)
    fcnet_script = mp / "usr" / "local" / "bin" / "fcnet-setup.sh"
    fcnet_script.parent.mkdir(parents=True, exist_ok=True)
    fcnet_script.write_text(
        "#!/bin/bash\n"
        "# Wait up to 1s for any non-loopback interface to appear.\n"
        "for i in $(seq 1 200); do\n"
        '    if [ "$(ls /sys/class/net | grep -v lo | wc -l)" -gt 0 ]; then\n'
        "        break\n"
        "    fi\n"
        "    sleep 0.005\n"
        "done\n"
        "for dev in $(ls /sys/class/net | grep -v lo); do\n"
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
        "    fi\n"
        "done\n"
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
