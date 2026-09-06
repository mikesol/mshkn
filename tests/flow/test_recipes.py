"""Recipes through HTTP: the build state machine, content-hash dedupe, creating a
computer from a ready recipe, and the reference check on delete."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

    from .conftest import Flow


async def test_recipe_build_state_machine_and_create_from_recipe(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def build_image(cmd: str) -> str:
        return "ok"

    async def run(cmd: str, check: bool = True) -> str:
        return ""

    monkeypatch.setattr(flow.runtime.recipes, "_build_image", build_image)
    monkeypatch.setattr(flow.runtime.recipes, "_run", run)
    (flow.runtime.config.ssh_key_path.parent).mkdir(parents=True, exist_ok=True)
    resp = await flow.client.post("/recipes", json={"dockerfile": "FROM mshkn-base\nRUN true"})
    assert resp.status_code == 202 and resp.json()["status"] == "pending"
    rid = resp.json()["recipe_id"]
    again = await flow.client.post("/recipes", json={"dockerfile": "FROM mshkn-base\nRUN true"})
    assert again.status_code == 200 and again.json()["recipe_id"] == rid
    await flow.runtime.tasks.wait(f"recipe_build:{rid}")
    assert (await flow.client.get(f"/recipes/{rid}")).json()["status"] == "ready"
    created = await flow.client.post("/computers", json={"recipe_id": rid})
    assert created.status_code == 200 and created.json()["recipe_id"] == rid
    assert (await flow.client.delete(f"/recipes/{rid}")).status_code == 409
    await flow.client.delete(f"/computers/{created.json()['computer_id']}")
    assert (await flow.client.delete(f"/recipes/{rid}")).status_code == 200
