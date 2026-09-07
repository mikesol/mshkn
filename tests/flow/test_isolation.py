"""Tenant isolation: the second account sees none of the first's resources and
cannot touch any of them, and unauthenticated calls stop at the door."""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.host import ExecResult

if TYPE_CHECKING:
    from .conftest import Flow

_RULE_SOURCE = "def transform(req):\n  return None"


async def test_second_tenant_cannot_see_or_touch_the_first_tenants_resources(flow: Flow) -> None:
    mine, other = flow.client, flow.other_client
    flow.host.guest.script["sync"] = ExecResult(0, "", "")

    cid = (await mine.post("/computers", json={})).json()["computer_id"]
    ckpt = (await mine.post(f"/computers/{cid}/checkpoint", json={"label": "iso"})).json()[
        "checkpoint_id"
    ]
    rule = (
        await mine.post("/ingress_rules", json={"name": "r", "starlark_source": _RULE_SOURCE})
    ).json()["id"]
    created = await mine.post("/recipes", json={"dockerfile": "FROM mshkn-base"})
    assert created.status_code == 202, created.text
    recipe = created.json()["recipe_id"]
    # The build needs docker, which this tier does not have; let it finish failing
    # so no task is still in flight when the assertions run.
    await flow.runtime.tasks.drain(timeout=5.0)

    for method, url, body in [
        ("GET", f"/computers/{cid}/status", None),
        ("POST", f"/computers/{cid}/exec/bg", {"command": "true"}),
        ("POST", f"/computers/{cid}/checkpoint", {}),
        ("DELETE", f"/computers/{cid}", None),
        ("POST", f"/checkpoints/{ckpt}/fork", {}),
        ("DELETE", f"/checkpoints/{ckpt}", None),
        ("POST", f"/checkpoints/{ckpt}/merge", {"checkpoint_a": ckpt, "checkpoint_b": ckpt}),
        ("GET", f"/ingress_rules/{rule}", None),
        ("PUT", f"/ingress_rules/{rule}", {"name": "stolen"}),
        ("DELETE", f"/ingress_rules/{rule}", None),
        ("POST", f"/ingress_rules/{rule}/rotate", None),
        ("POST", f"/ingress_rules/{rule}/test", {"method": "POST", "path": "/"}),
        ("GET", f"/ingress_rules/{rule}/logs", None),
        ("GET", f"/recipes/{recipe}", None),
        ("DELETE", f"/recipes/{recipe}", None),
    ]:
        resp = await other.request(method, url, json=body)
        assert resp.status_code == 404, f"{method} {url} -> {resp.status_code}: {resp.text}"

    # the second tenant's own listings are empty
    assert (await other.get("/checkpoints")).json() == []
    assert (await other.get("/ingress_rules")).json() == []
    assert (await other.get("/recipes")).json() == []

    # and nothing the owner has changed under it
    assert (await mine.get(f"/computers/{cid}/status")).status_code == 200
    assert (await mine.get(f"/ingress_rules/{rule}")).json()["name"] == "r"
    assert [c["id"] for c in (await mine.get("/checkpoints")).json()] == [ckpt]
    assert [r["recipe_id"] for r in (await mine.get("/recipes")).json()] == [recipe]
    assert flow.host.hypervisor.alive != {}
    assert flow.host.proxy.routes.keys() == {cid}


async def test_missing_and_malformed_auth_are_401(flow: Flow) -> None:
    bare = AsyncClient(transport=ASGITransport(app=flow.app), base_url="http://flow")
    try:
        assert (await bare.get("/checkpoints")).status_code == 401
        # wrong scheme, wrong key, and the scheme spelled in the wrong case
        for header in ("", "Token x", "Bearer nope", "bearer test-key"):
            resp = await bare.get("/checkpoints", headers={"Authorization": header})
            assert resp.status_code == 401, header
        assert (await bare.get("/health")).status_code == 200
    finally:
        await bare.aclose()
