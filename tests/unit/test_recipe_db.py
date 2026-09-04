from pathlib import Path

import aiosqlite
import pytest

from mshkn.db import (
    count_recipe_references,
    delete_recipe,
    get_max_recipe_volume_id,
    get_recipe,
    get_recipe_by_content_hash,
    insert_checkpoint,
    insert_computer,
    insert_recipe,
    list_recipes_by_account,
    run_migrations,
    update_recipe_build_result,
    update_recipe_status,
)
from mshkn.models import Checkpoint, Computer, Recipe


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await run_migrations(conn, Path("migrations"))
    await conn.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        ("acct-test", "key-test", 10, "2026-01-01T00:00:00Z"),
    )
    await conn.commit()
    yield conn
    await conn.close()


def _make_recipe(
    recipe_id: str = "rcp-001",
    account_id: str = "acct-test",
    content_hash: str = "deadbeef",
    status: str = "pending",
) -> Recipe:
    return Recipe(
        id=recipe_id,
        account_id=account_id,
        dockerfile="FROM ubuntu:24.04\nRUN apt-get update",
        content_hash=content_hash,
        status=status,
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-13T00:00:00Z",
        built_at=None,
    )


async def test_insert_and_get_recipe(db: aiosqlite.Connection) -> None:
    recipe = _make_recipe()
    await insert_recipe(db, recipe)
    result = await get_recipe(db, "rcp-001")
    assert result is not None
    assert result.id == "rcp-001"
    assert result.account_id == "acct-test"
    assert result.dockerfile == "FROM ubuntu:24.04\nRUN apt-get update"
    assert result.content_hash == "deadbeef"
    assert result.status == "pending"
    assert result.build_log is None
    assert result.base_volume_id is None
    assert result.built_at is None


async def test_get_recipe_returns_none_for_missing(db: aiosqlite.Connection) -> None:
    result = await get_recipe(db, "rcp-nonexistent")
    assert result is None


async def test_list_recipes_by_account(db: aiosqlite.Connection) -> None:
    await insert_recipe(db, _make_recipe("rcp-001", content_hash="hash1"))
    await insert_recipe(db, _make_recipe("rcp-002", content_hash="hash2"))
    results = await list_recipes_by_account(db, "acct-test")
    assert len(results) == 2
    ids = {r.id for r in results}
    assert ids == {"rcp-001", "rcp-002"}


async def test_update_recipe_status(db: aiosqlite.Connection) -> None:
    await insert_recipe(db, _make_recipe())
    await update_recipe_status(db, "rcp-001", "building")
    result = await get_recipe(db, "rcp-001")
    assert result is not None
    assert result.status == "building"


async def test_update_recipe_build_result(db: aiosqlite.Connection) -> None:
    await insert_recipe(db, _make_recipe())
    await update_recipe_build_result(
        db,
        "rcp-001",
        status="ready",
        build_log="Build successful",
        base_volume_id=42,
        built_at="2026-03-13T01:00:00Z",
    )
    result = await get_recipe(db, "rcp-001")
    assert result is not None
    assert result.status == "ready"
    assert result.build_log == "Build successful"
    assert result.base_volume_id == 42
    assert result.built_at == "2026-03-13T01:00:00Z"


async def test_get_recipe_by_content_hash(db: aiosqlite.Connection) -> None:
    await insert_recipe(db, _make_recipe(status="ready"))
    result = await get_recipe_by_content_hash(db, "acct-test", "deadbeef")
    assert result is not None
    assert result.id == "rcp-001"

    # Non-existent hash returns None
    result = await get_recipe_by_content_hash(db, "acct-test", "notexist")
    assert result is None


async def test_get_recipe_by_content_hash_excludes_failed(
    db: aiosqlite.Connection,
) -> None:
    await insert_recipe(db, _make_recipe(status="failed"))
    result = await get_recipe_by_content_hash(db, "acct-test", "deadbeef")
    assert result is None


async def test_get_max_recipe_volume_id_none_when_empty(
    db: aiosqlite.Connection,
) -> None:
    result = await get_max_recipe_volume_id(db)
    assert result is None


async def test_get_max_recipe_volume_id_returns_max(db: aiosqlite.Connection) -> None:
    await insert_recipe(db, _make_recipe("rcp-001", content_hash="hash1"))
    await insert_recipe(db, _make_recipe("rcp-002", content_hash="hash2"))
    await insert_recipe(db, _make_recipe("rcp-003", content_hash="hash3"))
    await update_recipe_build_result(db, "rcp-001", status="ready", base_volume_id=10)
    await update_recipe_build_result(db, "rcp-002", status="ready", base_volume_id=25)
    await update_recipe_build_result(db, "rcp-003", status="ready", base_volume_id=7)
    result = await get_max_recipe_volume_id(db)
    assert result == 25


async def test_delete_recipe(db: aiosqlite.Connection) -> None:
    await insert_recipe(db, _make_recipe())
    await delete_recipe(db, "rcp-001")
    result = await get_recipe(db, "rcp-001")
    assert result is None


async def test_count_recipe_references_zero(db: aiosqlite.Connection) -> None:
    await insert_recipe(db, _make_recipe())
    count = await count_recipe_references(db, "rcp-001")
    assert count == 0


async def test_count_recipe_references_with_computer(
    db: aiosqlite.Connection,
) -> None:
    await insert_recipe(db, _make_recipe())
    computer = Computer(
        id="comp-001",
        account_id="acct-test",
        thin_volume_id=1,
        tap_device="tap1",
        vm_ip="172.16.1.2",
        socket_path="/tmp/fc-comp-001.socket",
        firecracker_pid=None,
        manifest_hash="abc",
        manifest_json='{"uses": []}',
        status="running",
        created_at="2026-03-13T00:00:00Z",
        last_exec_at=None,
        recipe_id="rcp-001",
    )
    await insert_computer(db, computer)
    count = await count_recipe_references(db, "rcp-001")
    assert count == 1

    # Destroyed computer should not count
    await db.execute(
        "UPDATE computers SET status = 'destroyed' WHERE id = 'comp-001'"
    )
    await db.commit()
    count = await count_recipe_references(db, "rcp-001")
    assert count == 0


async def test_count_recipe_references_with_checkpoint(
    db: aiosqlite.Connection,
) -> None:
    await insert_recipe(db, _make_recipe())
    checkpoint = Checkpoint(
        id="ckpt-001",
        account_id="acct-test",
        parent_id=None,
        computer_id=None,
        thin_volume_id=None,
        manifest_hash="abc",
        manifest_json='{"uses": []}',
        r2_prefix="checkpoints/ckpt-001",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-03-13T00:00:00Z",
        recipe_id="rcp-001",
    )
    await insert_checkpoint(db, checkpoint)
    count = await count_recipe_references(db, "rcp-001")
    assert count == 1
