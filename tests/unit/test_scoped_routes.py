"""Route by route, what a scoped key may do (#88). The account key is unaffected.

Every check is a 403 raised in the route, naming the scope, before a service
runs. Ownership of a computer is `computers.api_key_id`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from mshkn.config import Config
from mshkn.db import get_computer, insert_account
from mshkn.host.fake import FakeHost
from tests.support import account_row
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import aiosqlite

    from mshkn.runtime import Runtime

ACCOUNT = {"Authorization": "Bearer test-key"}
BRAIN = {
    "recipes": {"create": True, "read": True},
    "computers": {"create_from": "*"},
    "labels": ["verb/"],
}
RULE = {"name": "r", "starlark_source": "def transform(req):\n  return None"}


@pytest.fixture
async def runtime(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    """A Runtime whose recipe builds succeed without docker, so a recipe can be ready."""
    config = Config(
        domain="test.dev",
        checkpoint_local_dir=tmp_path / "ckpts",
        ssh_key_path=tmp_path / "id_ed25519",
    )
    config.ssh_key_path.parent.mkdir(parents=True, exist_ok=True)
    config.ssh_key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAA t\n")
    host = FakeHost()
    rt = make_runtime(db, config=config, host=host)

    async def build_image(cmd: str) -> str:
        return "ok"

    async def run(cmd: str, check: bool = True) -> str:
        return ""

    monkeypatch.setattr(rt.recipes, "_build_image", build_image)
    monkeypatch.setattr(rt.recipes, "_run", run)
    try:
        yield rt
    finally:
        await rt.tasks.drain(timeout=2.0)
        await rt.http.aclose()
        host.close()


@pytest.fixture
async def client(db: aiosqlite.Connection, runtime: Runtime) -> AsyncIterator[AsyncClient]:
    await insert_account(db, account_row())
    app = make_app(runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=ACCOUNT
    ) as c:
        yield c


async def _key(client: AsyncClient, scopes: dict[str, Any]) -> tuple[str, dict[str, str]]:
    resp = await client.post("/keys", json={"scopes": scopes, "label": "t"}, headers=ACCOUNT)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"], {"Authorization": f"Bearer {resp.json()['secret']}"}


async def _ready_recipe(client: AsyncClient, runtime: Runtime) -> str:
    resp = await client.post("/recipes", json={"dockerfile": "FROM mshkn-base\nRUN true"})
    assert resp.status_code == 202, resp.text
    rid: str = resp.json()["recipe_id"]
    await runtime.tasks.wait(f"recipe_build:{rid}")
    return rid


async def _computer(client: AsyncClient, headers: dict[str, str], **body: Any) -> str:
    resp = await client.post("/computers", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    cid: str = resp.json()["computer_id"]
    return cid


async def _checkpoint(client: AsyncClient, cid: str, label: str | None) -> str:
    resp = await client.post(f"/computers/{cid}/checkpoint", json={"label": label})
    assert resp.status_code == 200, resp.text
    ckpt: str = resp.json()["checkpoint_id"]
    return ckpt


# --- recipes -----------------------------------------------------------------


async def test_recipes_create_and_read_are_separate_scopes(client: AsyncClient) -> None:
    _, create_only = await _key(client, {"recipes": {"create": True}})
    _, read_only = await _key(client, {"recipes": {"read": True}})
    dockerfile = {"dockerfile": "FROM mshkn-base"}
    resp = await client.post("/recipes", json=dockerfile, headers=read_only)
    assert resp.status_code == 403 and "recipes.create" in resp.json()["detail"]
    resp = await client.post("/recipes", json=dockerfile, headers=create_only)
    assert resp.status_code == 202, resp.text
    rid = resp.json()["recipe_id"]
    assert (await client.get("/recipes", headers=create_only)).status_code == 403
    assert (await client.get(f"/recipes/{rid}", headers=create_only)).status_code == 403
    assert (await client.get("/recipes", headers=read_only)).status_code == 200
    assert (await client.get(f"/recipes/{rid}", headers=read_only)).status_code == 200


async def test_a_scoped_key_never_deletes_a_recipe(client: AsyncClient) -> None:
    _, scoped = await _key(client, BRAIN)
    resp = await client.post("/recipes", json={"dockerfile": "FROM mshkn-base"}, headers=scoped)
    rid = resp.json()["recipe_id"]
    resp = await client.delete(f"/recipes/{rid}", headers=scoped)
    assert resp.status_code == 403 and "account key" in resp.json()["detail"]


# --- computers: create -------------------------------------------------------


async def test_create_from_star_means_any_recipe_but_not_bare(
    client: AsyncClient, runtime: Runtime
) -> None:
    rid = await _ready_recipe(client, runtime)
    key_id, scoped = await _key(client, BRAIN)
    resp = await client.post("/computers", json={}, headers=scoped)
    assert resp.status_code == 403 and "computers.create_from" in resp.json()["detail"]
    cid = await _computer(client, scoped, recipe_id=rid)
    row = await get_computer(runtime.db, cid)
    assert row is not None and row.api_key_id == key_id


async def test_create_from_list_names_recipes_and_bare(
    client: AsyncClient, runtime: Runtime
) -> None:
    rid = await _ready_recipe(client, runtime)
    _, bare_only = await _key(client, {"computers": {"create_from": ["bare"]}})
    _, recipe_only = await _key(client, {"computers": {"create_from": [rid]}})
    _, none = await _key(client, {})
    await _computer(client, bare_only)
    assert (
        await client.post("/computers", json={"recipe_id": rid}, headers=bare_only)
    ).status_code == 403
    await _computer(client, recipe_only, recipe_id=rid)
    assert (await client.post("/computers", json={}, headers=recipe_only)).status_code == 403
    assert (await client.post("/computers", json={}, headers=none)).status_code == 403


async def test_create_with_a_label_must_fall_under_labels(client: AsyncClient) -> None:
    _, scoped = await _key(client, {"computers": {"create_from": ["bare"]}, "labels": ["verb/"]})
    await _computer(client, scoped, label="verb/x")
    resp = await client.post("/computers", json={"label": "brain"}, headers=scoped)
    assert resp.status_code == 403 and "labels" in resp.json()["detail"]


async def test_the_account_key_creates_without_a_key_id(
    client: AsyncClient, runtime: Runtime
) -> None:
    cid = await _computer(client, ACCOUNT, label="brain")
    row = await get_computer(runtime.db, cid)
    assert row is not None and row.api_key_id is None


# --- computers: per-computer routes ------------------------------------------

COMPUTER_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/computers/{cid}/exec", {"command": "true"}),
    ("POST", "/computers/{cid}/exec/bg", {"command": "true"}),
    ("GET", "/computers/{cid}/exec/logs/4000", None),
    ("POST", "/computers/{cid}/exec/kill/4000", None),
    ("POST", "/computers/{cid}/upload?path=/root/x", None),
    ("GET", "/computers/{cid}/download?path=/root/x", None),
    ("GET", "/computers/{cid}/status", None),
    ("GET", "/computers/{cid}/exec_log", None),
    ("POST", "/computers/{cid}/checkpoint", {}),
    ("DELETE", "/computers/{cid}", None),
]


async def test_computer_routes_need_the_key_that_created_it(client: AsyncClient) -> None:
    _, scoped = await _key(client, {"computers": {"create_from": ["bare"]}, "labels": ["verb/"]})
    _, other = await _key(client, {"computers": {"create_from": ["bare"]}, "labels": ["verb/"]})
    by_account = await _computer(client, ACCOUNT, exec="true")
    by_other = await _computer(client, other, exec="true")
    for cid in (by_account, by_other):
        for method, path, body in COMPUTER_ROUTES:
            resp = await client.request(method, path.format(cid=cid), json=body, headers=scoped)
            assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}: {resp.text}"
            assert "computers" in resp.json()["detail"]
    # the account key and the other key still see their computers
    assert (await client.get(f"/computers/{by_account}/status")).status_code == 200
    assert (await client.get(f"/computers/{by_other}/status", headers=other)).status_code == 200


async def test_computer_routes_work_on_the_keys_own_computer(client: AsyncClient) -> None:
    _, scoped = await _key(client, {"computers": {"create_from": ["bare"]}, "labels": ["verb/"]})
    cid = await _computer(client, scoped, exec="true")
    for method, path, body in COMPUTER_ROUTES[:-1]:
        resp = await client.request(method, path.format(cid=cid), json=body, headers=scoped)
        assert resp.status_code != 403, f"{method} {path} -> {resp.status_code}: {resp.text}"
    resp = await client.delete(f"/computers/{cid}", headers=scoped)
    assert resp.status_code == 200, resp.text


async def test_a_nonexistent_computer_is_404_not_403(client: AsyncClient) -> None:
    _, scoped = await _key(client, BRAIN)
    assert (await client.get("/computers/comp-nope/status", headers=scoped)).status_code == 404


async def test_checkpoint_label_must_fall_under_labels(client: AsyncClient) -> None:
    _, scoped = await _key(client, {"computers": {"create_from": ["bare"]}, "labels": ["verb/"]})
    cid = await _computer(client, scoped)
    ok = await client.post(f"/computers/{cid}/checkpoint", json={"label": "verb/x"}, headers=scoped)
    assert ok.status_code == 200, ok.text
    bad = await client.post(f"/computers/{cid}/checkpoint", json={"label": "brain"}, headers=scoped)
    assert bad.status_code == 403 and "labels" in bad.json()["detail"]
    # an unlabelled checkpoint of its own computer is fine to take; it is just never reachable
    assert (await client.post(f"/computers/{cid}/checkpoint", headers=scoped)).status_code == 200


# --- checkpoints -------------------------------------------------------------


async def test_list_shows_only_checkpoints_under_the_prefixes(client: AsyncClient) -> None:
    _, scoped = await _key(client, BRAIN)
    cid = await _computer(client, ACCOUNT)
    verb = await _checkpoint(client, cid, "verb/x")
    await _checkpoint(client, cid, "brain")
    await _checkpoint(client, cid, None)
    await _checkpoint(client, cid, "verbose")
    listed = await client.get("/checkpoints", headers=scoped)
    assert [c["checkpoint_id"] for c in listed.json()] == [verb]
    brain = await client.get("/checkpoints", params={"label": "brain"}, headers=scoped)
    assert brain.json() == []
    exact = await client.get("/checkpoints", params={"label": "verb/x"}, headers=scoped)
    assert [c["checkpoint_id"] for c in exact.json()] == [verb]
    assert len((await client.get("/checkpoints")).json()) == 4


async def test_fork_and_delete_need_a_covered_label(client: AsyncClient, runtime: Runtime) -> None:
    key_id, scoped = await _key(client, BRAIN)
    cid = await _computer(client, ACCOUNT)
    verb = await _checkpoint(client, cid, "verb/x")
    brain = await _checkpoint(client, cid, "brain")
    bare = await _checkpoint(client, cid, None)
    for ckpt in (brain, bare):
        resp = await client.post(f"/checkpoints/{ckpt}/fork", json={}, headers=scoped)
        assert resp.status_code == 403 and "labels" in resp.json()["detail"], resp.text
        resp = await client.delete(f"/checkpoints/{ckpt}", headers=scoped)
        assert resp.status_code == 403 and "labels" in resp.json()["detail"], resp.text
    forked = await client.post(f"/checkpoints/{verb}/fork", json={}, headers=scoped)
    assert forked.status_code == 200, forked.text
    row = await get_computer(runtime.db, forked.json()["computer_id"])
    assert row is not None and row.api_key_id == key_id
    assert (await client.delete(f"/checkpoints/{verb}", headers=scoped)).status_code == 200
    # the account key forks and deletes anything
    assert (await client.post(f"/checkpoints/{brain}/fork", json={})).status_code == 200
    assert (await client.delete(f"/checkpoints/{bare}")).status_code == 200


async def test_merge_is_never_allowed(client: AsyncClient) -> None:
    _, scoped = await _key(client, BRAIN)
    cid = await _computer(client, ACCOUNT)
    parent = await _checkpoint(client, cid, "verb/p")
    resp = await client.post(
        f"/checkpoints/{parent}/merge",
        json={"checkpoint_a": "ckpt-a", "checkpoint_b": "ckpt-b"},
        headers=scoped,
    )
    assert resp.status_code == 403 and "account key" in resp.json()["detail"]


# --- ingress rules and alerts ------------------------------------------------


async def test_ingress_rules_are_never_allowed(client: AsyncClient) -> None:
    _, scoped = await _key(client, BRAIN)
    rule = (await client.post("/ingress_rules", json=RULE)).json()["id"]
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("POST", "/ingress_rules", RULE),
        ("GET", "/ingress_rules", None),
        ("GET", f"/ingress_rules/{rule}", None),
        ("PUT", f"/ingress_rules/{rule}", {"name": "x"}),
        ("DELETE", f"/ingress_rules/{rule}", None),
        ("POST", f"/ingress_rules/{rule}/rotate", None),
        ("POST", f"/ingress_rules/{rule}/test", {"method": "POST", "path": "/"}),
        ("GET", f"/ingress_rules/{rule}/logs", None),
    ]
    for method, url, body in calls:
        resp = await client.request(method, url, json=body, headers=scoped)
        assert resp.status_code == 403, f"{method} {url} -> {resp.status_code}: {resp.text}"
        assert "account key" in resp.json()["detail"]
    assert (await client.get("/ingress_rules")).status_code == 200


async def test_fork_by_label_needs_a_covered_label(client: AsyncClient, runtime: Runtime) -> None:
    key_id, scoped = await _key(client, BRAIN)
    cid = await _computer(client, ACCOUNT)
    await _checkpoint(client, cid, "verb/x")
    await _checkpoint(client, cid, "brain")
    # the prefix check runs before the lookup: an uncovered label is 403 whether or not it exists
    for label in ("brain", "nope/"):
        resp = await client.post("/checkpoints/fork", json={"label": label}, headers=scoped)
        assert resp.status_code == 403 and "labels" in resp.json()["detail"], resp.text
    missing = await client.post("/checkpoints/fork", json={"label": "verb/missing"}, headers=scoped)
    assert missing.status_code == 404, missing.text
    forked = await client.post("/checkpoints/fork", json={"label": "verb/x"}, headers=scoped)
    assert forked.status_code == 200, forked.text
    row = await get_computer(runtime.db, forked.json()["computer_id"])
    assert row is not None and row.api_key_id == key_id
    assert (await client.post("/checkpoints/fork", json={"label": "brain"})).status_code == 200


# --- relay ---------------------------------------------------------------


async def test_relay_needs_its_own_scope_and_hides_other_keys_jobs(client: AsyncClient) -> None:
    """#110: a key with no `relay` section never reaches the service, and a
    job it did not create is 404, not 403 -- it must not even learn one exists."""
    _, scoped = await _key(client, BRAIN)
    refused = await client.post("/relay", json={"target": "https://model.example/"}, headers=scoped)
    assert refused.status_code == 403 and "relay" in refused.json()["detail"]
    assert (await client.get("/relay/rj-000000000001", headers=scoped)).status_code == 404
    _, relay_scoped = await _key(
        client,
        {
            "relay": {
                "targets": ["https://model.example/"],
                "deliver": {"label": "brain", "exec": "membrane resume"},
            }
        },
    )
    outside = await client.post(
        "/relay", json={"target": "https://other.example/"}, headers=relay_scoped
    )
    assert outside.status_code == 403 and "relay.targets" in outside.json()["detail"]
    with_deliver = await client.post(
        "/relay",
        json={"target": "https://model.example/x", "deliver": {"label": "verb/x", "exec": "true"}},
        headers=relay_scoped,
    )
    assert with_deliver.status_code == 422
