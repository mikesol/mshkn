"""Every domain error's HTTP status through the real app, plus the per-key
exec rate limit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import ExecResult
from mshkn.ratelimit import RateLimiter

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from .conftest import Flow

_BAD_ACTION = 'def transform(req):\n  return {"action": "nope"}'


async def test_every_domain_error_maps_to_its_code(flow: Flow) -> None:
    c = flow.client
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    cid = (await c.post("/computers", json={})).json()["computer_id"]
    ckpt = (await c.post(f"/computers/{cid}/checkpoint", json={"label": "e"})).json()[
        "checkpoint_id"
    ]

    # 400 BadRequest: merging a checkpoint with itself, and operating on a
    # computer that is no longer running.
    self_merge = await c.post(
        f"/checkpoints/{ckpt}/merge", json={"checkpoint_a": ckpt, "checkpoint_b": ckpt}
    )
    assert self_merge.status_code == 400, self_merge.text
    assert self_merge.json()["detail"] == "Cannot merge a checkpoint with itself"
    assert (await c.delete(f"/computers/{cid}")).status_code == 200
    destroyed = await c.post(f"/computers/{cid}/checkpoint", json={})
    assert destroyed.status_code == 400
    assert destroyed.json()["detail"] == "Computer is destroyed"

    # 404 NotFound
    assert (await c.get("/computers/comp-nope/status")).status_code == 404
    assert (await c.post("/checkpoints/ckpt-nope/fork", json={})).status_code == 404
    assert (await c.post("/computers", json={"recipe_id": "rcp-nope"})).status_code == 404

    # 409 Conflict: a recipe that is not READY, and a second exclusive fork
    pending = await c.post("/recipes", json={"dockerfile": "FROM x"})
    rid = pending.json()["recipe_id"]
    assert (await c.post("/computers", json={"recipe_id": rid})).status_code == 409
    await flow.runtime.tasks.drain(timeout=5.0)  # the build fails without docker
    fork1 = await c.post(f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"})
    assert fork1.status_code == 200, fork1.text
    clash = await c.post(f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"})
    assert clash.status_code == 409

    # 422 InvalidInput: unparseable resources, and Starlark with no transform()
    assert (await c.post("/computers", json={"needs": {"ram": "lots"}})).status_code == 422
    bad = await c.post("/ingress_rules", json={"name": "x", "starlark_source": "def nope(): pass"})
    assert bad.status_code == 422
    assert "starlark_errors" in bad.json()["detail"]

    # 429 LimitExceeded: the account's vm_limit is 10 and the fork is the first
    assert (await c.get("/computers/" + fork1.json()["computer_id"] + "/status")).status_code == 200
    for _ in range(9):
        assert (await c.post("/computers", json={})).status_code == 200
    over = await c.post("/computers", json={})
    assert over.status_code == 429
    assert over.json()["detail"] == "VM limit reached"

    # 502 HostError: free a slot first so the limit cannot mask the boot failure
    assert (await c.delete(f"/computers/{fork1.json()['computer_id']}")).status_code == 200
    flow.host.hypervisor.fail_next("boot")
    # custom resources cold-boot rather than restoring from the template
    hosed = await c.post("/computers", json={"needs": {"ram": "512MB"}})
    assert hosed.status_code == 502
    assert hosed.json() == {"detail": "host operation failed"}

    # 502 TransformError: a transform whose result is not a valid action
    rule = (
        await c.post("/ingress_rules", json={"name": "t", "starlark_source": _BAD_ACTION})
    ).json()["id"]
    assert (await c.post(f"/ingress/{rule}")).status_code == 502


async def test_exec_is_rate_limited_per_key(
    flow_factory: Callable[..., AbstractAsyncContextManager[Flow]],
) -> None:
    async with flow_factory(rate_limit=RateLimiter(max_requests=2, window_seconds=60.0)) as flow:
        cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
        codes = []
        for _ in range(3):
            async with flow.client.stream(
                "POST", f"/computers/{cid}/exec", json={"command": "true"}
            ) as resp:
                await resp.aread()
                codes.append(resp.status_code)
        assert codes == [200, 200, 429]

        # the window is keyed by API key, so the other tenant is untouched
        ocid = (await flow.other_client.post("/computers", json={})).json()["computer_id"]
        async with flow.other_client.stream(
            "POST", f"/computers/{ocid}/exec", json={"command": "true"}
        ) as resp:
            await resp.aread()
            assert resp.status_code == 200
