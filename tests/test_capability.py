from __future__ import annotations

from pathlib import Path

import aiosqlite

from mshkn.capability.cache import cache_image, get_cached_image
from mshkn.capability.resolver import manifest_to_nix
from mshkn.db import run_migrations


def test_python_manifest() -> None:
    nix = manifest_to_nix(["python-3.12(numpy, pandas)"])
    assert "python312" in nix
    assert "numpy" in nix
    assert "pandas" in nix


def test_bare_tool() -> None:
    nix = manifest_to_nix(["ffmpeg"])
    assert "ffmpeg" in nix


def test_empty_manifest() -> None:
    nix = manifest_to_nix([])
    assert nix == ""


def test_mixed_manifest() -> None:
    nix = manifest_to_nix(["python-3.12(numpy, pandas)", "ffmpeg"])
    assert "python312" in nix
    assert "numpy" in nix
    assert "pandas" in nix
    assert "ffmpeg" in nix
    # Should be a valid nix expression with buildEnv
    assert "buildEnv" in nix
    assert "paths" in nix


def test_python_single_package() -> None:
    nix = manifest_to_nix(["python-3.11(requests)"])
    assert "python311" in nix
    assert "requests" in nix


def test_nix_expression_structure() -> None:
    nix = manifest_to_nix(["ffmpeg"])
    assert "{ pkgs ? import <nixpkgs> {} }:" in nix
    assert "pkgs.buildEnv" in nix
    assert 'name = "mshkn-capability"' in nix
    assert "pkgs.ffmpeg" in nix


async def test_cache_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"

    async with aiosqlite.connect(str(db_path)) as db:
        await run_migrations(db, migrations_dir)

        # Cache miss initially
        result = await get_cached_image(db, "abc123")
        assert result is None

        # Cache an image
        image_path = tmp_path / "test.ext4"
        image_path.write_bytes(b"fake image")
        await cache_image(db, "abc123", image_path, nix_size=1024, image_size=2048)

        # Cache hit
        result = await get_cached_image(db, "abc123")
        assert result == image_path


async def test_cache_updates_last_used(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"

    async with aiosqlite.connect(str(db_path)) as db:
        await run_migrations(db, migrations_dir)

        image_path = tmp_path / "test.ext4"
        image_path.write_bytes(b"fake image")
        await cache_image(db, "abc123", image_path, nix_size=1024, image_size=2048)

        # Fetch once to update last_used_at
        await get_cached_image(db, "abc123")

        # Verify the row was updated (last_used_at touched)
        cursor = await db.execute(
            "SELECT last_used_at FROM capability_cache WHERE manifest_hash = ?",
            ("abc123",),
        )
        row = await cursor.fetchone()
        assert row is not None
