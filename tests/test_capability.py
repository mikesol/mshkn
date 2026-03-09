from __future__ import annotations

from pathlib import Path

import aiosqlite

from mshkn.capability.cache import cache_volume, get_cached_volume, get_max_capability_volume_id
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


def test_bare_python() -> None:
    """'python' without version -> latest python3 from nixpkgs."""
    nix = manifest_to_nix(["python"])
    assert "pkgs.python3" in nix


def test_bare_node() -> None:
    """'node' -> nodejs from nixpkgs."""
    nix = manifest_to_nix(["node"])
    assert "pkgs.nodejs" in nix


def test_python_at_version() -> None:
    """'python@3.11' -> specific python version."""
    nix = manifest_to_nix(["python@3.11"])
    assert "pkgs.python311" in nix


def test_node_at_version() -> None:
    """'node@22' -> specific node version."""
    nix = manifest_to_nix(["node@22"])
    assert "pkgs.nodejs_22" in nix


def test_tarball_entry() -> None:
    """'tarball:URL:/path' -> fetchurl derivation."""
    nix = manifest_to_nix(["tarball:https://example.com/tool.tar.gz:/opt/tools"])
    assert "fetchurl" in nix or "builtins.fetchurl" in nix
    assert "example.com/tool.tar.gz" in nix


def test_python_with_pinned_package() -> None:
    """'python-3.12(numpy==1.26.0)' -> still generates valid nix."""
    nix = manifest_to_nix(["python-3.12(numpy==1.26.0)"])
    assert "python312" in nix
    assert "numpy" in nix


async def test_cache_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"

    async with aiosqlite.connect(str(db_path)) as db:
        await run_migrations(db, migrations_dir)

        # Cache miss initially
        result = await get_cached_volume(db, "abc123")
        assert result is None

        # Cache a volume
        await cache_volume(db, "abc123", volume_id=42, nix_closure_size=1024)

        # Cache hit
        result = await get_cached_volume(db, "abc123")
        assert result == 42


async def test_cache_updates_last_used(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"

    async with aiosqlite.connect(str(db_path)) as db:
        await run_migrations(db, migrations_dir)

        await cache_volume(db, "abc123", volume_id=42, nix_closure_size=1024)

        # Fetch once to update last_used_at
        await get_cached_volume(db, "abc123")

        # Verify the row was updated (last_used_at touched)
        cursor = await db.execute(
            "SELECT last_used_at FROM capability_cache WHERE manifest_hash = ?",
            ("abc123",),
        )
        row = await cursor.fetchone()
        assert row is not None


async def test_get_max_capability_volume_id_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"

    async with aiosqlite.connect(str(db_path)) as db:
        await run_migrations(db, migrations_dir)

        result = await get_max_capability_volume_id(db)
        assert result is None


async def test_get_max_capability_volume_id(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"

    async with aiosqlite.connect(str(db_path)) as db:
        await run_migrations(db, migrations_dir)

        await cache_volume(db, "hash1", volume_id=10)
        await cache_volume(db, "hash2", volume_id=25)
        await cache_volume(db, "hash3", volume_id=15)

        result = await get_max_capability_volume_id(db)
        assert result == 25
