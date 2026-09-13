"""GET /logs over the real app: a record is attributed to the account that
caused it, and one account never sees another's."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mshkn.observability.logging import account_id_var

if TYPE_CHECKING:
    from .conftest import Flow


async def test_a_request_stamps_the_account_on_every_record_it_causes(flow: Flow) -> None:
    token = account_id_var.set(None)
    try:
        created = await flow.client.post("/computers", json={})
        assert created.status_code == 200
    finally:
        account_id_var.reset(token)
    stamped = [r for r in flow.runtime.logs if r.get("mshkn.account_id") == "acct-1"]
    assert stamped, "the create logged nothing under the calling account"


async def test_the_endpoint_returns_ndjson_the_caller_caused(flow: Flow) -> None:
    created = await flow.client.post("/computers", json={})
    computer_id = created.json()["computer_id"]

    resp = await flow.client.get("/logs")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in resp.text.splitlines()]
    assert lines, "the create logged nothing"
    for entry in lines:
        assert entry["ecs.version"] and entry["@timestamp"] and entry["log.level"]
        assert entry["mshkn.account_id"] == "acct-1"
    assert any(e.get("mshkn.computer_id") == computer_id for e in lines), (
        "a specific computer's life is not reconstructable from the response"
    )


async def test_one_account_never_sees_another(flow: Flow) -> None:
    """The isolation test. Look here first if the filter is ever touched."""
    mine = (await flow.client.post("/computers", json={})).json()["computer_id"]
    theirs = (await flow.other_client.post("/computers", json={})).json()["computer_id"]

    ours = (await flow.client.get("/logs")).text
    assert mine in ours and theirs not in ours

    others = (await flow.other_client.get("/logs")).text
    assert theirs in others and mine not in others


async def test_a_scoped_key_is_refused(flow: Flow) -> None:
    """A scoped key is a narrowing; the account's whole stream would widen it."""
    made = await flow.client.post(
        "/keys", json={"scopes": {"computers": {"create_from": "*"}, "labels": ["verb/"]}}
    )
    assert made.status_code == 200
    secret = made.json()["secret"]
    resp = await flow.client.get("/logs", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 403


async def test_limit_keeps_the_newest_and_since_drops_the_old(flow: Flow) -> None:
    async def read(query: str = "") -> list[dict[str, object]]:
        resp = await flow.client.get(f"/logs{query}")
        assert resp.status_code == 200, resp.text
        return [json.loads(line) for line in resp.text.splitlines()]

    await flow.client.post("/computers", json={})
    everything = await read()
    assert len(everything) >= 2, "this test needs at least two records to order"

    one = await read("?limit=1")
    assert len(one) == 1
    assert str(one[0]["@timestamp"]) >= str(everything[-1]["@timestamp"]), (
        "a truncated response drops the oldest end, not the newest"
    )

    # Assertions are on the boundary rather than on equality with `everything`:
    # a record can land between the two reads, and a test that forbids that is
    # testing the fixture's quietness, not the endpoint.
    cutoff = str(everything[0]["@timestamp"])
    after = await read(f"?since={cutoff}")
    assert all(str(e["@timestamp"]) > cutoff for e in after), "since is exclusive"
    assert len(after) >= len(everything) - 1

    assert (await flow.client.get("/logs?since=not-a-timestamp")).status_code == 422


async def test_a_records_own_timestamp_pages_from_where_it_left_off(flow: Flow) -> None:
    """`+00:00` decodes to a space in a query string, so the endpoint would 422
    on the timestamps it hands out. Paging is the whole reason `since` exists."""
    await flow.client.post("/computers", json={})
    everything = [json.loads(line) for line in (await flow.client.get("/logs")).text.splitlines()]
    last = str(everything[-1]["@timestamp"])
    assert last.endswith("+00:00"), "the offset is what makes this a round trip"

    resp = await flow.client.get(f"/logs?since={last}")
    assert resp.status_code == 200, resp.text
    assert all(json.loads(line)["@timestamp"] > last for line in resp.text.splitlines())
