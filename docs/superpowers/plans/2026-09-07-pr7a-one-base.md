# PR 7a: One Base — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thin volume 0, the bare base every computer without a recipe boots from, becomes the export of the `mshkn-base` Docker image, written by `python -m mshkn base-volume` through the same code path recipes use; the Nix-era debootstrap rootfs, its purity shims and its private init go.

**Architecture:** The docker-export and tar-inject halves of `RecipeService.build` are extracted into two module-level coroutines in `src/mshkn/services/recipes.py` (`export_image`, `inject_tar`). A new `src/mshkn/services/base_volume.py` composes them for volume 0: refuse while the service is active, build the image with the VM public key in the context, export, inject onto the already-active `mshkn-base` device, drop the bare template. The CLI gains a `base-volume` subcommand wired to the production dm-thin block store. `scripts/build-rootfs.sh` and `Config.base_rootfs_path` are deleted; `DEPLOY.md` sections 5 to 7 collapse into a pool section and a base section.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, asyncio subprocesses, Docker (legacy builder on the host), dm-thin, pytest 9 / pytest-asyncio / pytest-cov, uv, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-07-generative-agent-workload-proof-design.md` §4 (PR 7a), §7 (documents), §8 (validation). Deviation decided here:

1. **Two shared functions instead of one.** The spec names one `write_image_to_volume`. `RecipeService.build` sets the recipe's status to `exporting` before the docker export and to `injecting` before the thin-volume work, and those statuses are part of the API. Splitting the step into `export_image` (docker create, export, rm) and `inject_tar` (mkfs, mount, untar, post-process) keeps both statuses where they are; the CLI calls the two in sequence. Same code path, same commands, one more name.

## Global Constraints

- Python `>=3.12`; uv only; every command runs as `uv run <tool>` inside the worktree.
- The gate, identical to CI: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov`. Green at the end of every task; zero warnings; the coverage floor (`fail_under` in `pyproject.toml`, 98 %) holds.
- No xfail, no new skips, no `# type: ignore` in tests, no assertion-free tests. Flow tests never patch module attributes.
- Product changes in this PR are exactly: the two extracted functions, `base_volume.py`, the `base-volume` CLI command, `clear_bare_template`, the removal of `Config.base_rootfs_path`, and the deletion of `scripts/build-rootfs.sh`. Nothing else under `src/` changes behaviour.
- `tests/unit/test_docs.py` must stay green: every backticked path, module, route, metric and variable in the documents must exist.
- Live E2E gate for this PR: **144 passed, 6 skipped, 7 failed**, the seven being the `Not implemented` tests in #65. Any other failure is a regression: fix it or stop and discuss.
- Commit messages end with the trailer block (Co-Authored-By and Claude-Session lines). Never merge; open the PR and request authorization.

---

## File Structure

**Created**
- `src/mshkn/services/base_volume.py` — `write_base_volume(...)`: the volume-0 pipeline; `DEFAULT_DOCKERFILE`, `BASE_IMAGE`, `BASE_DEVICE`.
- `tests/unit/test_base_volume.py` — tests for `write_base_volume`, `clear_bare_template` and the CLI's `base_volume` function.
- `tests/unit/test_image_pipeline.py` — tests for `export_image` and `inject_tar`.
- `docs/superpowers/plans/2026-09-07-pr7a-baseline.txt` — test count and coverage before the PR.

**Modified**
- `src/mshkn/services/recipes.py` — `export_image`, `inject_tar` extracted; `build` calls them.
- `src/mshkn/db/templates.py`, `src/mshkn/db/__init__.py` — `clear_bare_template`.
- `src/mshkn/cli.py` — `base-volume` subcommand, `base_volume(...)`.
- `src/mshkn/config.py` — `base_rootfs_path` removed.
- `tests/support.py` — `FakeShell` (moved from `tests/unit/test_recipe_service.py`, raises `ShellError`, accepts several `fail_on` patterns).
- `tests/unit/test_recipe_service.py` — imports `FakeShell` from support.
- `tests/unit/test_entrypoints.py` — help lists `base-volume`.
- `DEPLOY.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/infrastructure.md`, `docs/plans/README.md`.

**Deleted**
- `scripts/build-rootfs.sh`.

---

### Task 1: Worktree and baseline

**Files:**
- Create: `docs/superpowers/plans/2026-09-07-pr7a-baseline.txt`

- [ ] **Step 1:** Use `superpowers:using-git-worktrees` to create `../mshkn-pr7a` on branch `pr7a-one-base`, from `main` if the #76 brainstorm PR has merged, else from `pr7-brainstorm` (the spec and this plan live there). `cd ../mshkn-pr7a && uv sync --frozen`.

- [ ] **Step 2:** Record the baseline:

```bash
{ echo "Baseline before PR 7a ($(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD), $(date -I))"; uv run ruff check . | tail -1; uv run ruff format --check . | tail -1; uv run mypy | tail -1; uv run pytest -q -p no:cacheprovider 2>&1 | tail -1; uv run pytest --cov -q -p no:cacheprovider 2>&1 | grep TOTAL; } | tee docs/superpowers/plans/2026-09-07-pr7a-baseline.txt
git add docs/superpowers/plans/2026-09-07-pr7a-baseline.txt && git commit -m "chore: record pre-PR7a baseline"
```

Expected: clean; `493 passed, 157 deselected`; coverage TOTAL at or above 98 %. Report the actual numbers.

---

### Task 2: `clear_bare_template`

**Files:**
- Modify: `src/mshkn/db/templates.py`
- Modify: `src/mshkn/db/__init__.py` (the `from mshkn.db.templates import …` line and `__all__`)
- Test: `tests/unit/test_base_volume.py` (created here; later tasks add to it)

**Interfaces:**
- Produces: `async def clear_bare_template(db: aiosqlite.Connection) -> None` — deletes the `snapshot_templates` row whose `manifest_hash` is `'bare'` and commits; a no-op when there is none. Exported from `mshkn.db`.

- [ ] **Step 1: Write the failing test**

```python
"""The base-volume pipeline: build mshkn-base, export it, write volume 0, drop the bare template."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import cache_bare_template, clear_bare_template, get_bare_template

if TYPE_CHECKING:
    import aiosqlite


async def test_clear_bare_template_removes_the_row_and_tolerates_none(
    db: aiosqlite.Connection,
) -> None:
    await cache_bare_template(db, "/t/vmstate", "/t/memory")
    assert await get_bare_template(db) == ("/t/vmstate", "/t/memory")
    await clear_bare_template(db)
    assert await get_bare_template(db) is None
    await clear_bare_template(db)  # nothing to delete is not an error
    assert await get_bare_template(db) is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_base_volume.py -v`
Expected: FAIL with `ImportError: cannot import name 'clear_bare_template'`.

- [ ] **Step 3: Implement**

Append to `src/mshkn/db/templates.py`:

```python
async def clear_bare_template(db: aiosqlite.Connection) -> None:
    """Forget the bare template; the next bare create rebuilds it from the current base."""
    await db.execute("DELETE FROM snapshot_templates WHERE manifest_hash = 'bare'")
    await db.commit()
```

In `src/mshkn/db/__init__.py`, extend the import line to `from mshkn.db.templates import cache_bare_template, clear_bare_template, get_bare_template` and add `"clear_bare_template"` to `__all__` in alphabetical position.

- [ ] **Step 4: Run the test and the gate**

Run: `uv run pytest tests/unit/test_base_volume.py -v` → PASS. Then `uv run ruff check . && uv run ruff format --check . && uv run mypy` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/mshkn/db/templates.py src/mshkn/db/__init__.py tests/unit/test_base_volume.py
git commit -m "feat(db): clear_bare_template forgets the bare L3 template"
```

---

### Task 3: Extract `export_image` and `inject_tar` from the recipe build

**Files:**
- Modify: `src/mshkn/services/recipes.py:234-296` (`RecipeService.build`) and the module level below the imports
- Modify: `tests/support.py` (add `FakeShell`)
- Modify: `tests/unit/test_recipe_service.py:25-36` (remove the local `FakeShell`, import it from support)
- Test: `tests/unit/test_image_pipeline.py`

**Interfaces:**
- Produces, in `mshkn.services.recipes`:

```python
async def export_image(
    run: RunFn, *, image_tag: str, container_name: str, tar_path: Path
) -> None:
    """docker create → export → rm: the image's root filesystem as a tar at tar_path."""

async def inject_tar(
    run: RunFn, blocks: BlockStore, config: Config, *, volume_name: str, tar_path: Path
) -> None:
    """Format the active device volume_name, unpack tar_path onto it, post-process for Firecracker."""
```

- Produces, in `tests.support`:

```python
class FakeShell:
    """Records commands; raises ShellError for any command containing one of fail_on."""
    def __init__(self, fail_on: str | tuple[str, ...] | None = None) -> None: ...
    calls: list[str]
    async def __call__(self, cmd: str, check: bool = True) -> str: ...
```

- [ ] **Step 1: Move `FakeShell` to `tests/support.py`**

Add to `tests/support.py` (keep the existing row builders):

```python
from mshkn.host.shell import ShellError


class FakeShell:
    """Records commands; raises ShellError for any command containing one of fail_on.

    ShellError rather than a bare exception, because that is what the real
    runner raises and what callers are written to catch.
    """

    def __init__(self, fail_on: str | tuple[str, ...] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on: tuple[str, ...] = (
            () if fail_on is None else (fail_on,) if isinstance(fail_on, str) else fail_on
        )

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append(cmd)
        if any(pattern in cmd for pattern in self.fail_on):
            raise ShellError(cmd, 1, f"failed: {cmd}")
        return ""
```

In `tests/unit/test_recipe_service.py` delete the local `class FakeShell` and add `FakeShell` to the `from tests.support import …` line.

Run: `uv run pytest tests/unit/test_recipe_service.py -q` → all pass (the build's `except Exception` catches `ShellError` as it caught `RuntimeError`).

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_image_pipeline.py`:

```python
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
```

Check `FakeBlockStore.mounted` before running: it must yield `self.mounts[name]` for an activated device and raise `HostError` for an unknown one (PR 5 made mounts stable per device). If the yielded path differs from `mounts[name]`, assert on the path the context manager yields instead; do not change the fake.

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_image_pipeline.py -v`
Expected: FAIL with `ImportError: cannot import name 'export_image'`.

- [ ] **Step 4: Extract the two functions**

In `src/mshkn/services/recipes.py`, add `BlockStore` to the `TYPE_CHECKING` imports if it is not already imported at runtime (it is imported under `TYPE_CHECKING` today: `from mshkn.host import BlockStore, Hypervisor`), and add these two module-level coroutines above `class RecipeService` (after `docker_build_image`):

```python
async def export_image(
    run: RunFn, *, image_tag: str, container_name: str, tar_path: Path
) -> None:
    """docker create → export → rm: the image's root filesystem as a tar at tar_path.

    The container is removed even when the export fails, so a broken build
    does not leave a stopped container behind for the next attempt to trip on.
    """
    try:
        await run(f"docker create --name {container_name} {image_tag}")
        await run(f"docker export -o {tar_path} {container_name}")
    finally:
        with contextlib.suppress(Exception):
            await run(f"docker rm {container_name}", check=False)


async def inject_tar(
    run: RunFn, blocks: BlockStore, config: Config, *, volume_name: str, tar_path: Path
) -> None:
    """Format the active device volume_name, unpack tar_path onto it, post-process for Firecracker.

    Used for every recipe volume and for the bare base (volume 0), so the two
    kinds of computer are produced by the same code.
    """
    await blocks.mkfs(volume_name)
    async with blocks.mounted(volume_name) as mount_point:
        await run(f"tar xf {tar_path} -C {mount_point}")
        await asyncio.to_thread(_post_process_rootfs, mount_point, config)
```

`RunFn` is only imported under `TYPE_CHECKING`; with `from __future__ import annotations` that is fine for the signatures.

Then replace lines 257 to 271 of `build` (from `await update_recipe_status(self.db, recipe_id, RecipeStatus.EXPORTING)` through `device_active = False`) with:

```python
            await update_recipe_status(self.db, recipe_id, RecipeStatus.EXPORTING)
            await export_image(
                self._run, image_tag=image_tag, container_name=container_name, tar_path=tar_path
            )

            await update_recipe_status(self.db, recipe_id, RecipeStatus.INJECTING)
            await self.blocks.snap(source_volume_id=0, new_volume_id=volume_id)
            await self.blocks.activate(volume_id=volume_id, name=volume_name)
            device_active = True
            await inject_tar(
                self._run, self.blocks, self.config, volume_name=volume_name, tar_path=tar_path
            )
            await self.blocks.deactivate(volume_name)
            device_active = False
```

In the `finally` block of `build`, delete the two lines that `docker rm {container_name}` (`export_image` owns that now); keep the `docker rmi` and the `rmtree`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_image_pipeline.py tests/unit/test_recipe_service.py tests/unit/test_recipes_edges.py -v`
Expected: all PASS. `test_create_builds_through_the_state_machine_to_ready` still sees `docker create`, `docker export`, `tar xf` and `("mkfs", …)`; `test_inject_failure_after_activate_deactivates_the_device` still ends with no active device.

- [ ] **Step 6: Gate and commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov -q` → clean, floor held.

```bash
git add src/mshkn/services/recipes.py tests/support.py tests/unit/test_recipe_service.py tests/unit/test_image_pipeline.py
git commit -m "refactor(recipes): export_image and inject_tar are the shared image-to-volume step"
```

---

### Task 4: `write_base_volume`

**Files:**
- Create: `src/mshkn/services/base_volume.py`
- Test: `tests/unit/test_base_volume.py` (extend)

**Interfaces:**
- Consumes: `export_image`, `inject_tar`, `docker_build_image` from `mshkn.services.recipes`; `clear_bare_template` from `mshkn.db`; `FakeShell` from `tests.support`.
- Produces, in `mshkn.services.base_volume`:

```python
DEFAULT_DOCKERFILE: Path   # <repository root>/Dockerfile.mshkn-base, found relative to the package
BASE_IMAGE = "mshkn-base"
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

    Returns the docker build output. Raises Conflict while mshkn.service is
    active and ConfigError when the Dockerfile or the VM public key is missing.
    """
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_base_volume.py` (extend the imports at the top accordingly):

```python
from pathlib import Path

import pytest

from mshkn.config import Config
from mshkn.errors import ConfigError, Conflict
from mshkn.host.fake import FakeHost
from mshkn.services.base_volume import BASE_DEVICE, BASE_IMAGE, DEFAULT_DOCKERFILE, write_base_volume
from tests.support import FakeShell

INACTIVE = "systemctl is-active"  # FakeShell(fail_on=INACTIVE): the service is not running


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
            config=_config(tmp_path), db=db, blocks=host.blocks,
            dockerfile=_dockerfile(tmp_path), run=shell,
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
            config=_config(tmp_path, with_key=False), db=db, blocks=host.blocks,
            dockerfile=_dockerfile(tmp_path), run=FakeShell(fail_on=INACTIVE),
        )
    with pytest.raises(ConfigError, match="Dockerfile"):
        await write_base_volume(
            config=_config(tmp_path), db=db, blocks=host.blocks,
            dockerfile=tmp_path / "missing", run=FakeShell(fail_on=INACTIVE),
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
        config=config, db=db, blocks=host.blocks, dockerfile=_dockerfile(tmp_path),
        run=shell, build_image=build_image,
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
            config=_config(tmp_path), db=db, blocks=host.blocks, dockerfile=_dockerfile(tmp_path),
            run=FakeShell(fail_on=(INACTIVE, "docker export")), build_image=build_image,
        )
    assert not seen["build_dir"].exists()
    assert ("mkfs", "mshkn-base") not in host.blocks.calls
    assert await get_bare_template(db) == ("/t/vmstate", "/t/memory")
    host.close()
```

Add `from mshkn.host.shell import ShellError` and `import aiosqlite` under `TYPE_CHECKING` as needed. Format the file with `uv run ruff format tests/unit/test_base_volume.py` (the multi-line call layouts above are illustrative; ruff decides).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_base_volume.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mshkn.services.base_volume'`.

- [ ] **Step 3: Implement**

`src/mshkn/services/base_volume.py`:

```python
"""Thin volume 0, the bare base, written from the mshkn-base image (spec §4).

Bare computers and recipe computers are produced by the same code: build the
image, export it, mkfs, untar, post-process. The only difference is that the
base device is already active (scripts/mshkn-pool-up maps it at boot) and is
not snapped from anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import mshkn
from mshkn.db import clear_bare_template
from mshkn.errors import ConfigError, Conflict
from mshkn.host.shell import ShellError
from mshkn.host.shell import run as shell_run
from mshkn.services.recipes import docker_build_image, export_image, inject_tar

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import BlockStore
    from mshkn.host.shell import RunFn
    from mshkn.services.recipes import BuildImageFn

DEFAULT_DOCKERFILE = Path(mshkn.__file__).resolve().parents[2] / "Dockerfile.mshkn-base"
BASE_IMAGE = "mshkn-base"
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
    """
    with contextlib.suppress(ShellError):  # non-zero: inactive, or no systemctl here
        await run("systemctl is-active --quiet mshkn")
        raise Conflict("mshkn is active; stop it before rewriting the base volume")
    pub_key = config.ssh_key_path.with_suffix(".pub")
    if not pub_key.exists():
        raise ConfigError(f"public key {pub_key} not found; VMs built without it are unreachable")
    if not dockerfile.exists():
        raise ConfigError(f"Dockerfile {dockerfile} not found")
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
        await clear_bare_template(db)
        await asyncio.to_thread(
            shutil.rmtree, config.checkpoint_local_dir / "templates" / "bare", True
        )
        return log
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
```

`BuildImageFn` is defined in `recipes.py` under `TYPE_CHECKING`; import it the same way. If mypy objects to the `contextlib.suppress` block containing the `raise`, rewrite it as an explicit `try/except ShellError: pass / else: raise Conflict(...)`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_base_volume.py -v` → all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov -q` → clean, floor held.

```bash
git add src/mshkn/services/base_volume.py tests/unit/test_base_volume.py
git commit -m "feat: write_base_volume builds mshkn-base and writes it into thin volume 0"
```

---

### Task 5: The `base-volume` CLI command

**Files:**
- Modify: `src/mshkn/cli.py`
- Test: `tests/unit/test_base_volume.py` (extend), `tests/unit/test_entrypoints.py:9-16`

**Interfaces:**
- Consumes: `write_base_volume`, `DEFAULT_DOCKERFILE`, `BASE_IMAGE`, `BASE_DEVICE` from `mshkn.services.base_volume`; `DmThinBlockStore(pool_name, sectors)` from `mshkn.host.dmthin`.
- Produces, in `mshkn.cli`:

```python
async def base_volume(
    args: argparse.Namespace,
    config: Config,
    db: aiosqlite.Connection,
    *,
    blocks: BlockStore,
    run: RunFn = shell_run,
    build_image: BuildImageFn = docker_build_image,
) -> int:
    """The base-volume subcommand: 0 on success; 1 with the reason on stderr."""
```

`python -m mshkn base-volume [--dockerfile PATH] [--image TAG] [--device NAME]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_base_volume.py`:

```python
from mshkn.cli import _parser, base_volume


def test_base_volume_parser_defaults() -> None:
    args = _parser().parse_args(["base-volume"])
    assert args.command == "base-volume"
    assert args.dockerfile == DEFAULT_DOCKERFILE
    assert args.image == BASE_IMAGE and args.device == BASE_DEVICE
    custom = _parser().parse_args(
        ["base-volume", "--dockerfile", "/x/Dockerfile", "--image", "i", "--device", "d"]
    )
    assert custom.dockerfile == Path("/x/Dockerfile")
    assert custom.image == "i" and custom.device == "d"


async def test_base_volume_command_reports_a_refusal(
    db: aiosqlite.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = FakeHost()
    args = _parser().parse_args(["base-volume", "--dockerfile", str(_dockerfile(tmp_path))])
    assert await base_volume(args, _config(tmp_path), db, blocks=host.blocks, run=FakeShell()) == 1
    assert "stop it" in capsys.readouterr().err
    host.close()


async def test_base_volume_command_succeeds(
    db: aiosqlite.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = FakeHost()
    await host.blocks.activate(volume_id=0, name="mshkn-base")

    async def build_image(cmd: str) -> str:
        return "built"

    args = _parser().parse_args(["base-volume", "--dockerfile", str(_dockerfile(tmp_path))])
    code = await base_volume(
        args, _config(tmp_path), db, blocks=host.blocks,
        run=FakeShell(fail_on=INACTIVE), build_image=build_image,
    )
    assert code == 0
    assert "base volume mshkn-base written from mshkn-base" in capsys.readouterr().out
    assert ("mkfs", "mshkn-base") in host.blocks.calls
    host.close()
```

In `tests/unit/test_entrypoints.py::test_python_dash_m_mshkn_prints_help` add `assert "base-volume" in proc.stdout`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_base_volume.py tests/unit/test_entrypoints.py -v`
Expected: FAIL with `ImportError: cannot import name 'base_volume'` and the help assertion.

- [ ] **Step 3: Implement**

In `src/mshkn/cli.py`:

Imports (runtime): `from pathlib import Path`, `from mshkn.errors import ConfigError, Conflict`, `from mshkn.host.dmthin import DmThinBlockStore`, `from mshkn.host.shell import run as shell_run`, `from mshkn.services.base_volume import BASE_DEVICE, BASE_IMAGE, DEFAULT_DOCKERFILE, write_base_volume`, `from mshkn.services.recipes import docker_build_image`. Under `TYPE_CHECKING`: `import aiosqlite`, `from mshkn.host import BlockStore`, `from mshkn.host.shell import RunFn`, `from mshkn.services.recipes import BuildImageFn`.

In `_parser()`, after the `migrate` parser:

```python
    base = sub.add_parser(
        "base-volume", help="build mshkn-base and write it into thin volume 0 (stop mshkn first)"
    )
    base.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
    base.add_argument("--image", default=BASE_IMAGE)
    base.add_argument("--device", default=BASE_DEVICE)
```

A new module-level function:

```python
async def base_volume(
    args: argparse.Namespace,
    config: Config,
    db: aiosqlite.Connection,
    *,
    blocks: BlockStore,
    run: RunFn = shell_run,
    build_image: BuildImageFn = docker_build_image,
) -> int:
    """The base-volume subcommand: 0 on success; 1 with the reason on stderr."""
    try:
        await write_base_volume(
            config=config,
            db=db,
            blocks=blocks,
            dockerfile=args.dockerfile,
            image_tag=args.image,
            device=args.device,
            run=run,
            build_image=build_image,
        )
    except (Conflict, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"base volume {args.device} written from {args.image}")
    return 0
```

In `_run`, after the `migrate` early return:

```python
        if args.command == "base-volume":
            blocks = DmThinBlockStore(config.thin_pool_name, config.thin_volume_sectors)
            return await base_volume(args, config, db, blocks=blocks)
```

Update the module docstring: `"""`python -m mshkn`: operator commands on the configured database and the base volume."""`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_base_volume.py tests/unit/test_entrypoints.py tests/unit/test_cli.py -v` → all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov -q` → clean; the two production-wiring lines in `_run` are the only new uncovered lines, and the floor holds (report the number).

```bash
git add src/mshkn/cli.py tests/unit/test_base_volume.py tests/unit/test_entrypoints.py
git commit -m "feat(cli): python -m mshkn base-volume writes the bare base from mshkn-base"
```

---

### Task 6: Remove the old rootfs and update every document

**Files:**
- Delete: `scripts/build-rootfs.sh`
- Modify: `src/mshkn/config.py:35-37` (remove `base_rootfs_path`)
- Modify: `DEPLOY.md` (§1, §5 to §7, section numbers, §13, Teardown), `README.md:27,52`, `docs/ARCHITECTURE.md:115,158,205`, `docs/infrastructure.md` (after the table), `docs/plans/README.md` (the PR 7a row)
- Test: `tests/unit/test_docs.py` (unchanged; must stay green), `tests/unit/test_config.py` (unchanged; must stay green)

- [ ] **Step 1: Delete the script and the config field**

```bash
git rm scripts/build-rootfs.sh
```

In `src/mshkn/config.py` delete the three lines defining `base_rootfs_path`. Run `uv run pytest tests/unit/test_config.py -q` → pass (`test_generic_names_map_every_field` iterates the dataclass fields).

- [ ] **Step 2: DEPLOY.md**

1. §1: remove `debootstrap` from the `apt-get install` list. Change the sentence after the block to: `Docker builds the base image and every recipe; \`sqlite3\` is handy for inspecting \`/opt/mshkn/mshkn.db\` directly; uv installs the project.`
2. Delete §5 "Base rootfs" entirely.
3. Old §6 becomes `## 5. dm-thin pool and VM egress`: keep the paragraph and the first code block (ending `dmsetup ls            # mshkn-pool and mshkn-base`); delete the "Write the rootfs into the base volume…" paragraph and its `dd`/`resize2fs`/`e2fsck` block.
4. Old §7 becomes:

````markdown
## 6. Base image and base volume

Bare computers and recipes share one filesystem: the `mshkn-base` image, built from `Dockerfile.mshkn-base` with the key from step 3. This command builds the image, exports it, and writes it into thin volume 0 (the `mshkn-base` device from step 5) through the same mkfs, untar and post-processing a recipe volume gets. It refuses to run while `mshkn.service` is active. Takes a few minutes.

```bash
cd /opt/mshkn && .venv/bin/python -m mshkn base-volume
docker images mshkn-base
e2fsck -fn /dev/mapper/mshkn-base
```

Rerun it after changing `Dockerfile.mshkn-base` or the key, with the service stopped. Existing checkpoint and recipe volumes are unaffected (a thin snapshot is independent of its origin), and the bare template is rebuilt on the next create.
````

5. Renumber old §8 to §13 as §7 to §12 (`## 7. Environment and R2` … `## 12. Verify`).
6. Teardown's last line becomes: `Then, with mshkn stopped, redo step 6 (\`python -m mshkn base-volume\`) and step 9.`
7. Search for stale cross-references and fix each: `grep -n "step [0-9]\+\|§[0-9]\+" DEPLOY.md docs/ARCHITECTURE.md docs/plans/README.md README.md CLAUDE.md docs/infrastructure.md`. Known ones: `docs/ARCHITECTURE.md` §8 says `DEPLOY.md §7` (now §6); `docs/plans/README.md` says `DEPLOY.md §12` for Litestream (now §11).

- [ ] **Step 3: README.md**

Line 27: `  cli.py             python -m mshkn accounts create|list, migrate, base-volume`.
Line 52: `scripts/             deploy.sh, e2e.sh, mshkn-pool-up`.
In "What exists", the Computers bullet: replace `boots a VM from the bare base volume or from a recipe's volume` with `boots a VM from the base volume (the export of the \`mshkn-base\` image) or from a recipe's volume`.

- [ ] **Step 4: docs/ARCHITECTURE.md**

Line 115: `base volume 0 (the bare rootfs)` → `base volume 0 (the export of the \`mshkn-base\` image, written by \`python -m mshkn base-volume\`)`.

Line 158, replace the first sentence with: `A recipe is a Dockerfile, by convention starting \`FROM mshkn-base\` (the image built from \`Dockerfile.mshkn-base\` by \`python -m mshkn base-volume\`, \`DEPLOY.md\` §6, which also writes that image's export into volume 0 so bare and recipe computers share one filesystem); the service does not check that line, and nothing verifies the result boots until the first computer is created from it.` In the same paragraph, replace `exports the container filesystem, unpacks it into a thin volume snapped from volume 0, and post-processes it for Firecracker (init symlink, network unit, SSH keys) in a worker thread` with `exports the container filesystem (\`mshkn.services.recipes.export_image\`), unpacks it into a thin volume snapped from volume 0 and post-processes it for Firecracker (init symlink, network unit, SSH keys) in a worker thread (\`mshkn.services.recipes.inject_tar\`, the same step \`mshkn.services.base_volume.write_base_volume\` uses for volume 0)`.

Line 205: `| \`kernel_path\` | \`/opt/firecracker/vmlinux.bin\` | \`MSHKN_KERNEL_PATH\` |`.

- [ ] **Step 5: docs/infrastructure.md**

After the "Minimum host" table add:

`Docker on Ubuntu 24.04 (\`docker.io\`) ships without the buildx plugin, so \`docker build\` runs the deprecated legacy builder; mshkn keeps each recipe's image so that builder's layer cache serves rebuilds. If a Docker upgrade removes the legacy builder, install \`docker-buildx\` and re-check the recipe build log format, which BuildKit changes.`

- [ ] **Step 6: docs/plans/README.md**

In the "Generative-agent workload proof (2026-09)" table, the PR 7a row becomes:
`| \`docs/superpowers/plans/2026-09-07-pr7a-one-base.md\` | this PR | in progress (this PR) |`

- [ ] **Step 7: Docs tests and the gate**

Run: `uv run pytest tests/unit/test_docs.py -v` → all pass (every path, module and variable named above exists: `mshkn.services.recipes.export_image`, `mshkn.services.recipes.inject_tar`, `mshkn.services.base_volume.write_base_volume`, `MSHKN_KERNEL_PATH`). Then the full gate: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov` → clean, zero warnings, floor held.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "docs: one base; DEPLOY writes volume 0 with python -m mshkn base-volume; build-rootfs.sh removed"
```

---

### Task 7: PR, CI, live migration, live E2E

**Files:** none in the repository beyond the PR body.

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin pr7a-one-base
gh pr create --title "PR 7a: one base — volume 0 is the export of mshkn-base" --body-file - <<'EOF'
Part of #76 (spec: docs/superpowers/specs/2026-09-07-generative-agent-workload-proof-design.md §4). Closes #<the PR 7a issue>.

**What this does**

Thin volume 0 becomes the export of the `mshkn-base` image, written by `python -m mshkn base-volume` through the same `export_image` and `inject_tar` step recipes use, so bare and recipe computers are produced by identical code. The debootstrap rootfs, its purity shims (which told the agent to use the removed `uses` manifest), the private `mshkn-init` and the `/nix` directories go, along with the unread `base_rootfs_path` config field.

**Design alignment**

- Architecture §3 (host boundary): the CLI reaches the pool only through `BlockStore` (`DmThinBlockStore`) and the shell runner; no new host code.
- Architecture §5 (state ownership): volume 0 stays the origin of every bare snapshot; thin snapshots are independent of their origin, so existing checkpoint and recipe volumes are untouched. The bare template row and directory are dropped because they were booted from the old base.
- Architecture §8 (recipes): the recipe pipeline is unchanged in commands and statuses; its export and inject halves are now the named functions the base uses.
- Spec §4.2 deviation: two functions (`export_image`, `inject_tar`) instead of one, so `exporting` and `injecting` keep their places in the build.

**Validation performed**

- Gate: <paste the five command results>
- CI: <link>
- Live migration: deploy, stop, `base-volume`, start (output below)
- Live E2E: <summary line>, failing set: <the seven of #65>
- Journal tracebacks after the run: <count>

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
EOF
gh pr checks --watch
```

If no issue for PR 7a exists yet, create it first: `gh issue create --title "One base: volume 0 is the export of mshkn-base" --body "Spec §4 of docs/superpowers/specs/2026-09-07-generative-agent-workload-proof-design.md. The bare rootfs still carries the Nix-era purity shims, has no apt, and boots mshkn-init instead of systemd; recipe computers are exports of mshkn-base. One base, written by python -m mshkn base-volume. Part of #76."` and use its number.

- [ ] **Step 2: Deploy and rewrite the base on the live host**

```bash
export MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000
scripts/deploy.sh
ssh mshkn 'systemctl stop mshkn && cd /opt/mshkn && .venv/bin/python -m mshkn base-volume && e2fsck -fn /dev/mapper/mshkn-base && docker images mshkn-base && rm -f /opt/firecracker/rootfs.ext4; systemctl start mshkn && sleep 3 && curl -fsS localhost:8000/health'
```

Expected: `base volume mshkn-base written from mshkn-base`, a clean `e2fsck`, the image listed, health `ok`. Keep the output for the PR body.

- [ ] **Step 3: Spot-check a bare computer**

```bash
H="Authorization: Bearer mk-test-key-2026"
ID=$(curl -s -X POST "$MSHKN_API_URL/computers" -H "$H" -H "Content-Type: application/json" -d '{}' | jq -r .computer_id)
curl -s -N -X POST "$MSHKN_API_URL/computers/$ID/exec" -H "$H" -H "Content-Type: application/json" \
  -d '{"command":"readlink /sbin/init; apt-get --version | head -1; command -v pip npm || echo no-shims; ps -p 1 -o comm="}'
curl -s -X DELETE "$MSHKN_API_URL/computers/$ID" -H "$H"
```

Expected: `/lib/systemd/systemd`, an `apt` version line, `no-shims`, `systemd`. Anything else means the base was not rewritten or the template still points at the old one; stop and investigate before running the suite.

- [ ] **Step 4: Run the live suite, detached**

```bash
setsid nohup scripts/e2e.sh > /tmp/e2e-pr7a.log 2>&1 < /dev/null &
```

Poll `tail -3 /tmp/e2e-pr7a.log` until the summary line appears (about 14 minutes). Expected: `144 passed, 6 skipped, 7 failed`, and `grep -E "^FAILED" /tmp/e2e-pr7a.log` lists exactly the seven of #65 (phase 8 T8.6, phase 9 T9.1, phase 10 T10.1/T10.2/T10.5, phase 11 T11.2 and the audit check). Then:

```bash
ssh mshkn "journalctl -u mshkn --since '20 min ago' --no-pager | grep -ci traceback"
```

Expected: `0`.

- [ ] **Step 5: Fill in the PR body**

`gh pr edit <N> --body-file -` with the gate output, the CI link, the migration output, the summary line with the seven named, and the traceback count. Then triage bot review comments per CLAUDE.md (reply to every one, resolve every thread, fix only what is actually wrong). Do not merge; ask for authorization.

---

## Self-review

**Spec coverage (§4):** 4.1 steps 1 to 4 → Task 4 (refusal, image build with key, export and inject on the active device, template row and directory dropped); options → Task 5; 4.2 shared step → Task 3 (as two functions, deviation 1); 4.3 removals → Task 6 (script, config field, DEPLOY sections, README line); 4.4 consequences are documented in DEPLOY §6 and Architecture §5/§8 (Task 6); 4.5 tests → Tasks 3, 4, 5 and the docs tests in Task 6; 4.6 live migration → Task 7. §7's PR 7a document list → Task 6. §8 validation → Task 7.

**Placeholders:** none; every step carries its code or its command. The PR issue number in Task 7 is created in the same step when it does not exist.

**Type consistency:** `export_image(run, *, image_tag, container_name, tar_path)` and `inject_tar(run, blocks, config, *, volume_name, tar_path)` are used with the same keyword names in Tasks 3, 4 and the recipe build; `write_base_volume` and `base_volume` share the keyword set `config, db, blocks, dockerfile, image_tag, device, run, build_image`; `FakeShell(fail_on: str | tuple[str, ...] | None)` raises `ShellError` everywhere it is used; `clear_bare_template(db)` matches Task 2's definition.
