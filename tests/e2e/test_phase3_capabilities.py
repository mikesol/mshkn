"""Phase 3: Recipe System E2E tests.

Tests the Docker-based recipe system: CRUD, build lifecycle,
content-hash dedup, computer boot from recipe, and bare rootfs boot.

These tests run against a LIVE server with real Firecracker VMs.
Every computer costs real resources — clean up in finally blocks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .conftest import (
    create_computer,
    create_recipe,
    destroy_computer,
    exec_command,
    managed_computer,
)

if TYPE_CHECKING:
    import httpx


# ---------------------------------------------------------------------------
# T3.1 — Recipe CRUD
# ---------------------------------------------------------------------------


class TestRecipeCRUD:
    """Recipe create, get, list, delete operations."""

    async def test_create_recipe(self, long_client: httpx.AsyncClient) -> None:
        """POST /recipes creates a recipe and builds it to ready."""
        recipe_id = await create_recipe(
            long_client,
            "FROM mshkn-base\nRUN apt-get update && apt-get install -y jq",
        )

        resp = await long_client.get(f"/recipes/{recipe_id}")
        resp.raise_for_status()
        data = resp.json()
        assert data["status"] == "ready"

    async def test_list_recipes(self, long_client: httpx.AsyncClient) -> None:
        """GET /recipes returns a list."""
        resp = await long_client.get("/recipes")
        resp.raise_for_status()
        recipes = resp.json()
        assert isinstance(recipes, list)

    async def test_get_recipe(self, long_client: httpx.AsyncClient) -> None:
        """GET /recipes/{id} returns the recipe."""
        recipe_id = await create_recipe(
            long_client,
            "FROM mshkn-base\nRUN echo get-test-unique",
        )
        resp = await long_client.get(f"/recipes/{recipe_id}")
        resp.raise_for_status()
        data = resp.json()
        assert data["recipe_id"] == recipe_id
        assert data["status"] == "ready"

    async def test_delete_recipe(self, long_client: httpx.AsyncClient) -> None:
        """DELETE /recipes/{id} removes the recipe."""
        recipe_id = await create_recipe(
            long_client,
            "FROM mshkn-base\nRUN echo delete-test-unique",
        )
        resp = await long_client.delete(f"/recipes/{recipe_id}")
        resp.raise_for_status()

        resp2 = await long_client.get(f"/recipes/{recipe_id}")
        assert resp2.status_code == 404

    async def test_delete_referenced_recipe_blocked(
        self, long_client: httpx.AsyncClient
    ) -> None:
        """DELETE /recipes/{id} returns 409 if a computer references it."""
        recipe_id = await create_recipe(
            long_client,
            "FROM mshkn-base\nRUN echo ref-test-unique",
        )
        comp_id = await create_computer(long_client, recipe_id=recipe_id)
        try:
            resp = await long_client.delete(f"/recipes/{recipe_id}")
            assert resp.status_code == 409
        finally:
            await destroy_computer(long_client, comp_id)


# ---------------------------------------------------------------------------
# T3.2 — Content-hash dedup
# ---------------------------------------------------------------------------


class TestContentHashDedup:
    """Same Dockerfile text should return the same recipe_id."""

    async def test_dedup_same_dockerfile(
        self, long_client: httpx.AsyncClient
    ) -> None:
        dockerfile = "FROM mshkn-base\nRUN echo dedup-test-unique-e2e"
        id1 = await create_recipe(long_client, dockerfile)
        # Second post with identical Dockerfile
        resp2 = await long_client.post("/recipes", json={"dockerfile": dockerfile})
        resp2.raise_for_status()
        assert resp2.json()["recipe_id"] == id1


# ---------------------------------------------------------------------------
# T3.3 — Build failure
# ---------------------------------------------------------------------------


class TestBuildFailure:
    """Bad Dockerfile produces status=failed with build_log."""

    async def test_bad_dockerfile_fails(
        self, long_client: httpx.AsyncClient
    ) -> None:
        resp = await long_client.post(
            "/recipes",
            json={"dockerfile": "FROM nonexistent-image-that-does-not-exist-12345"},
        )
        resp.raise_for_status()
        recipe_id = resp.json()["recipe_id"]

        # Poll until terminal
        import asyncio
        import time

        deadline = time.time() + 120
        while time.time() < deadline:
            r = await long_client.get(f"/recipes/{recipe_id}")
            r.raise_for_status()
            data = r.json()
            if data["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(3)

        assert data["status"] == "failed"
        assert data.get("build_log") is not None
        assert len(data["build_log"]) > 0


# ---------------------------------------------------------------------------
# T3.4 — Computer boots from recipe
# ---------------------------------------------------------------------------


class TestComputerFromRecipe:
    """Computer created with recipe_id boots with the recipe's tools."""

    async def test_boot_with_recipe_and_verify_tool(
        self, long_client: httpx.AsyncClient
    ) -> None:
        """Create recipe with jq, boot computer, verify jq is available."""
        recipe_id = await create_recipe(
            long_client,
            "FROM mshkn-base\nRUN apt-get update && apt-get install -y jq",
        )
        comp_id = await create_computer(long_client, recipe_id=recipe_id)
        try:
            result = await exec_command(long_client, comp_id, "jq --version")
            assert "jq" in result.stdout.lower()
        finally:
            await destroy_computer(long_client, comp_id)

    async def test_boot_bare_no_recipe(
        self, long_client: httpx.AsyncClient
    ) -> None:
        """Computer without recipe_id boots from bare rootfs."""
        async with managed_computer(long_client) as comp_id:
            result = await exec_command(long_client, comp_id, "uname -a")
            assert "Linux" in result.stdout


# ---------------------------------------------------------------------------
# T3.5 — Multi-tool Dockerfile
# ---------------------------------------------------------------------------


class TestMultiToolRecipe:
    """Dockerfile with multiple tools all available in booted computer."""

    async def test_multi_tool(self, long_client: httpx.AsyncClient) -> None:
        recipe_id = await create_recipe(
            long_client,
            "FROM mshkn-base\nRUN apt-get update && apt-get install -y python3 curl jq",
        )
        comp_id = await create_computer(long_client, recipe_id=recipe_id)
        try:
            result = await exec_command(
                long_client,
                comp_id,
                "python3 --version && curl --version | head -1 && jq --version",
            )
            assert "Python" in result.stdout
            assert "curl" in result.stdout
            assert "jq" in result.stdout.lower()
        finally:
            await destroy_computer(long_client, comp_id)
