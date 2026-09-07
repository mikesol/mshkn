"""Thin volume 0, the bare base, written from the mshkn-base image (spec §4).

Bare computers and recipe computers are produced by the same code: build the
image, export it, mkfs, untar, post-process. The only difference is that the
base device is already active (scripts/mshkn-pool-up maps it at boot) and is
not snapped from anything.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import mshkn
from mshkn.db import clear_bare_template
from mshkn.errors import ConfigError, Conflict
from mshkn.host.shell import ShellError
from mshkn.host.shell import run as shell_run
from mshkn.services.recipes import BASE_IMAGE as BASE_IMAGE  # re-exported: the CLI imports it here
from mshkn.services.recipes import docker_build_image, export_image, inject_tar

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import BlockStore
    from mshkn.host.shell import RunFn
    from mshkn.services.recipes import BuildImageFn

DEFAULT_DOCKERFILE = Path(mshkn.__file__).resolve().parents[2] / "Dockerfile.mshkn-base"
BASE_DEVICE = "mshkn-base"


async def write_base_volume(
    *,
    config: Config,
    db: aiosqlite.Connection,
    blocks: BlockStore,
    dockerfile: Path = DEFAULT_DOCKERFILE,
    image_tag: str = BASE_IMAGE,
    device: str = BASE_DEVICE,
    run: RunFn = shell_run,
    build_image: BuildImageFn = docker_build_image,
) -> str:
    """Build the base image, export it onto the active base device, drop the bare template.

    Returns the docker build output. Refuses while mshkn.service is active,
    because a create that ran meanwhile would snapshot a half-written origin.
    Existing checkpoint and recipe volumes are unaffected: a thin snapshot is
    independent of its origin once taken.

    The bare template is dropped as soon as validation passes, before build,
    export, or inject_tar's mkfs (the step that destroys volume 0's existing
    filesystem) run: the template is a memory snapshot of the old base, and it
    must never survive to describe a filesystem that mkfs has since erased.
    Losing the template on a failure that never touched the volume (build,
    export) costs one template rebuild on the next create; a failure inside
    inject_tar itself (mount, untar, post-process) leaves volume 0 not usable
    until a rerun succeeds.
    """
    try:
        await run("systemctl is-active --quiet mshkn")
    except ShellError:
        pass  # non-zero: inactive, or no systemctl here
    else:
        raise Conflict("mshkn is active; stop it before rewriting the base volume")
    pub_key = config.ssh_key_path.with_suffix(".pub")
    if not pub_key.exists():
        raise ConfigError(f"public key {pub_key} not found; VMs built without it are unreachable")
    if not dockerfile.exists():
        raise ConfigError(f"Dockerfile {dockerfile} not found")
    await clear_bare_template(db)
    await asyncio.to_thread(shutil.rmtree, config.checkpoint_local_dir / "templates" / "bare", True)
    build_dir = Path(tempfile.mkdtemp(prefix="mshkn-base-build-"))
    try:
        shutil.copy(dockerfile, build_dir / "Dockerfile")
        shutil.copy(pub_key, build_dir / "mshkn_key.pub")
        log = await build_image(
            f"docker build --memory=4g --cpuset-cpus=0-1 -t {image_tag} {build_dir}"
        )
        tar_path = build_dir / "rootfs.tar"
        await export_image(
            run, image_tag=image_tag, container_name=f"tmp-{device}", tar_path=tar_path
        )
        await inject_tar(run, blocks, config, volume_name=device, tar_path=tar_path)
        return log
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
