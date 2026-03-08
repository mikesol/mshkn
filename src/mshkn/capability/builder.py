"""Build a capability image from a Nix expression + base rootfs via overlayfs."""

from __future__ import annotations

import tempfile
from pathlib import Path

from mshkn.shell import run


async def build_capability_image(
    nix_expr: str, base_rootfs: Path, output_path: Path
) -> Path:
    """Build an ext4 image by composing a Nix closure on top of a base rootfs.

    1. Write *nix_expr* to a temp file and run ``nix-build``.
    2. Mount *base_rootfs* as the lower layer and the Nix closure as an overlay.
    3. Create a new ext4 image at *output_path* from the merged view.

    Returns *output_path* on success.
    """
    with tempfile.TemporaryDirectory(prefix="mshkn-cap-") as tmpdir:
        tmp = Path(tmpdir)

        # 1. Write nix expression and build
        nix_file = tmp / "capability.nix"
        nix_file.write_text(nix_expr)
        store_path = (await run(f"nix-build --no-out-link {nix_file}")).strip()

        # 2. Set up overlayfs: base + nix closure → merged
        lower = tmp / "lower"
        upper = tmp / "upper"
        work = tmp / "work"
        merged = tmp / "merged"
        for d in (lower, upper, work, merged):
            d.mkdir()

        await run(f"mount -o loop,ro {base_rootfs} {lower}")
        try:
            # Copy nix store path into upper layer so it appears in the merged view
            nix_dest = upper / "nix"
            nix_dest.mkdir(parents=True, exist_ok=True)
            await run(f"cp -a {store_path} {nix_dest}/")

            await run(
                f"mount -t overlay overlay "
                f"-o lowerdir={lower},upperdir={upper},workdir={work} {merged}"
            )
            try:
                # 3. Create ext4 image from merged view
                await run(
                    f"mke2fs -t ext4 -d {merged} {output_path} "
                    f"$(du -sm {merged} | cut -f1)M"
                )
            finally:
                await run(f"umount {merged}")
        finally:
            await run(f"umount {lower}")

    return output_path
