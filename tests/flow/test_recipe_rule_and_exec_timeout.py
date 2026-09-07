"""Two agent-facing rules through HTTP: recipes must end FROM mshkn-base, and
the exec time limit belongs to the caller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import list_recipes_by_account

if TYPE_CHECKING:
    import pytest

    from .conftest import Flow


async def test_a_recipe_from_another_base_is_422_before_any_build(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The accepted recipe at the end would otherwise start a real docker build;
    # the flow tier replaces the build and the shell on the service instance,
    # as tests/flow/test_recipes.py does.
    async def build_image(cmd: str) -> str:
        return "ok"

    async def run(cmd: str, check: bool = True) -> str:
        return ""

    monkeypatch.setattr(flow.runtime.recipes, "_build_image", build_image)
    monkeypatch.setattr(flow.runtime.recipes, "_run", run)

    resp = await flow.client.post("/recipes", json={"dockerfile": "FROM python:3.12\nRUN true"})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "mshkn-base" in detail and "python:3.12" in detail
    assert await list_recipes_by_account(flow.runtime.db, "acct-1") == []
    assert flow.host.blocks.calls == []  # no snap, no mkfs: nothing was built

    resp = await flow.client.post("/recipes", json={"dockerfile": "RUN true"})
    assert resp.status_code == 422
    assert "no FROM" in resp.json()["detail"]

    # A multi-stage build whose final stage is mshkn-base is accepted.
    ok = await flow.client.post(
        "/recipes",
        json={"dockerfile": "FROM golang:1.22 AS build\nFROM mshkn-base\nCOPY --from=build /a /a"},
    )
    assert ok.status_code == 202, ok.text
    await flow.runtime.tasks.wait(f"recipe_build:{ok.json()['recipe_id']}")
    assert (await flow.client.get(f"/recipes/{ok.json()['recipe_id']}")).json()["status"] == "ready"
