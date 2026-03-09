"""Build a capability base volume by composing a Nix closure onto a dm-thin volume."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from mshkn.shell import run

logger = logging.getLogger(__name__)


async def nix_build(nix_expr: str) -> str:
    """Write a Nix expression to a temp file, build it, return the store path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".nix", prefix="mshkn-cap-", delete=False
    ) as f:
        f.write(nix_expr)
        nix_file = f.name

    try:
        store_path = (await run(f"nix-build --no-out-link {nix_file}")).strip()
    finally:
        Path(nix_file).unlink(missing_ok=True)

    return store_path


async def get_closure_paths(store_path: str) -> list[str]:
    """Get the full closure (all transitive deps) of a Nix store path."""
    output = await run(f"nix-store -qR {store_path}")
    return [p.strip() for p in output.strip().splitlines() if p.strip()]


async def inject_closure_into_volume(
    volume_name: str,
    store_path: str,
    manifest_uses: list[str],
) -> int:
    """Mount a dm-thin volume, copy Nix closure into it, install shims.

    Returns the total size of the Nix closure in bytes.
    """
    from mshkn.vm.storage import mount_volume, umount_volume

    mount_point = tempfile.mkdtemp(prefix="mshkn-cap-mount-")

    try:
        # Resize ext4 filesystem to fill the 8GB volume (rootfs is only 1GB)
        await run(f"e2fsck -fy /dev/mapper/{volume_name}", check=False)
        await run(f"resize2fs /dev/mapper/{volume_name}")
        logger.info("Resized filesystem on %s", volume_name)

        await mount_volume(volume_name, mount_point)

        try:
            # Get full closure
            closure_paths = await get_closure_paths(store_path)
            logger.info(
                "Injecting %d store paths into %s", len(closure_paths), volume_name
            )

            # Ensure /nix/store exists
            nix_store = Path(mount_point) / "nix" / "store"
            nix_store.mkdir(parents=True, exist_ok=True)

            # Copy each store path into the volume
            for cp in closure_paths:
                dest = Path(mount_point) / cp.lstrip("/")
                if not dest.exists():
                    await run(f"cp -a {cp} {dest}")

            # Create symlinks in /usr/local/bin for top-level binaries
            local_bin = Path(mount_point) / "usr" / "local" / "bin"
            local_bin.mkdir(parents=True, exist_ok=True)

            store_bin = Path(store_path) / "bin"
            if store_bin.is_dir():
                for binary in store_bin.iterdir():
                    link_target = f"/nix/store/{Path(store_path).name}/bin/{binary.name}"
                    link_path = local_bin / binary.name
                    # Don't overwrite existing shims
                    if not link_path.exists():
                        link_path.symlink_to(link_target)

            # Handle tarball extract directories — copy extracted files
            # into their target paths in the rootfs
            extract_dir = Path(store_path) / "extract"
            if extract_dir.is_dir():
                await run(f"cp -a {extract_dir}/. {mount_point}/")
                logger.info("Copied tarball extract into rootfs")

            # Install pip/npm shims (overwrites the base rootfs shims with
            # manifest-aware versions that include the current uses list)
            _install_shims(Path(mount_point), manifest_uses)

            # Make /nix/store immutable (even root can't modify)
            # Use find to skip symlinks — chattr doesn't support them
            await run(
                f"find {mount_point}/nix/store -not -type l -exec chattr +i {{}} +"
            )

            # Calculate closure size
            size_output = await run(f"du -sb {mount_point}/nix/store")
            closure_size = int(size_output.split()[0])

            logger.info(
                "Injected closure into %s (%d bytes)", volume_name, closure_size
            )

        finally:
            await umount_volume(mount_point)

    finally:
        Path(mount_point).rmdir()

    return closure_size


def _install_shims(rootfs: Path, manifest_uses: list[str]) -> None:
    """Install purity shim scripts for pip/npm inside the rootfs.

    These shims override the base rootfs shims with manifest-aware versions
    that include the current uses list in the suggested_action.
    """
    local_bin = rootfs / "usr" / "local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)

    uses_str = ", ".join(f'"{u}"' for u in manifest_uses)

    has_python = any(u.startswith("python") for u in manifest_uses)
    if has_python:
        pip_shim = local_bin / "pip"
        pip_shim.write_text(
            '#!/bin/bash\n'
            'PKG="${@: -1}"\n'
            'cat >&2 <<JSON\n'
            '{\n'
            '  "error": "Package installation not permitted. '
            'Use the uses capability manifest instead.",\n'
            '  "suggested_action": {\n'
            '    "tool": "checkpoint_fork",\n'
            f'    "args": {{"uses": [{uses_str}, "$PKG"]}}\n'
            '  }\n'
            '}\n'
            'JSON\n'
            'exit 1\n'
        )
        pip_shim.chmod(0o755)
        pip3_shim = local_bin / "pip3"
        if pip3_shim.exists() or pip3_shim.is_symlink():
            pip3_shim.unlink()
        pip3_shim.symlink_to("pip")

    has_node = any(u.startswith("node") for u in manifest_uses)
    if has_node:
        npm_shim = local_bin / "npm"
        npm_shim.write_text(
            '#!/bin/bash\n'
            'PKG="${@: -1}"\n'
            'cat >&2 <<JSON\n'
            '{\n'
            '  "error": "Package installation not permitted. '
            'Use the uses capability manifest instead.",\n'
            '  "suggested_action": {\n'
            '    "tool": "checkpoint_fork",\n'
            f'    "args": {{"uses": [{uses_str}, "$PKG"]}}\n'
            '  }\n'
            '}\n'
            'JSON\n'
            'exit 1\n'
        )
        npm_shim.chmod(0o755)
