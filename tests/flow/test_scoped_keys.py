"""The brain's key (#88), end to end over the fake host: it builds a recipe,
creates a computer from it, checkpoints and forks under `verb/`, and is stopped
at everything the embryo must keep it away from."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import get_computer
from mshkn.host import ExecResult

if TYPE_CHECKING:
    import pytest

    from .conftest import Flow

BRAIN_SCOPES = {
    "recipes": {"create": True, "read": True},
    "computers": {"create_from": "*"},
    "labels": ["verb/"],
}


async def test_the_brain_key_runs_verbs_and_nothing_else(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def build_image(cmd: str) -> str:
        return "ok"

    async def run(cmd: str, check: bool = True) -> str:
        return ""

    monkeypatch.setattr(flow.runtime.recipes, "_build_image", build_image)
    monkeypatch.setattr(flow.runtime.recipes, "_run", run)
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    root = flow.client

    # root: the brain chain and its key
    brain_cid = (await root.post("/computers", json={})).json()["computer_id"]
    brain_ckpt = (
        await root.post(f"/computers/{brain_cid}/checkpoint", json={"label": "brain"})
    ).json()["checkpoint_id"]
    minted = await root.post("/keys", json={"scopes": BRAIN_SCOPES, "label": "brain"})
    assert minted.status_code == 200, minted.text
    key_id = minted.json()["id"]
    brain = {"Authorization": f"Bearer {minted.json()['secret']}"}

    # the brain builds a verb and runs it on a computer of its own
    built = await root.post(
        "/recipes", json={"dockerfile": "FROM mshkn-base\nRUN true"}, headers=brain
    )
    assert built.status_code == 202, built.text
    rid = built.json()["recipe_id"]
    await flow.runtime.tasks.wait(f"recipe_build:{rid}")
    assert (await root.get(f"/recipes/{rid}", headers=brain)).json()["status"] == "ready"
    created = await root.post("/computers", json={"recipe_id": rid}, headers=brain)
    assert created.status_code == 200, created.text
    verb_cid = created.json()["computer_id"]
    row = await get_computer(flow.runtime.db, verb_cid)
    assert row is not None and row.api_key_id == key_id
    execd = await root.post(
        f"/computers/{verb_cid}/exec/bg", json={"command": "true"}, headers=brain
    )
    assert execd.status_code == 200, execd.text

    # it checkpoints under verb/ and forks the chain
    saved = await root.post(
        f"/computers/{verb_cid}/checkpoint", json={"label": "verb/x"}, headers=brain
    )
    assert saved.status_code == 200, saved.text
    verb_ckpt = saved.json()["checkpoint_id"]
    forked = await root.post(f"/checkpoints/{verb_ckpt}/fork", json={}, headers=brain)
    assert forked.status_code == 200, forked.text
    fork_row = await get_computer(flow.runtime.db, forked.json()["computer_id"])
    assert fork_row is not None and fork_row.api_key_id == key_id

    # the brain chain does not exist for it
    assert (await root.get("/checkpoints", params={"label": "brain"}, headers=brain)).json() == []
    seen = {c["checkpoint_id"] for c in (await root.get("/checkpoints", headers=brain)).json()}
    assert seen == {verb_ckpt}
    denied = await root.post(f"/checkpoints/{brain_ckpt}/fork", json={}, headers=brain)
    assert denied.status_code == 403 and "labels" in denied.json()["detail"]
    denied = await root.post(
        f"/computers/{brain_cid}/exec/bg", json={"command": "true"}, headers=brain
    )
    assert denied.status_code == 403 and "computers" in denied.json()["detail"]

    # nor do the account's door, its recipes' lifetime, or its keys
    rule = {"name": "hook", "starlark_source": "def transform(req):\n  return None"}
    denied = await root.post("/ingress_rules", json=rule, headers=brain)
    assert denied.status_code == 403 and "account key" in denied.json()["detail"]
    denied = await root.delete(f"/recipes/{rid}", headers=brain)
    assert denied.status_code == 403 and "account key" in denied.json()["detail"]
    denied = await root.post("/keys", json={"scopes": {}}, headers=brain)
    assert denied.status_code == 403

    # root still sees and does everything
    assert len((await root.get("/checkpoints")).json()) == 2
    assert (await root.post(f"/checkpoints/{brain_ckpt}/fork", json={})).status_code == 200
