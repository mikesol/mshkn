"""Every verb runs on its own computer (spec §3, §4): ephemeral ones from the
recipe, chain ones by fork-by-label; builds are polled once per turn (§5)."""

from __future__ import annotations

from typing import Any

import pytest
from membrane.declarations import parse_proposal, parse_verb, render_command
from membrane.mshkn import MshknError
from membrane.state import CatalogEntry, State
from membrane.verbs import chain_head, invoke, log_tail, poll_builds, submit_recipe, wait_for_recipe

from tests.support_embryo import FakeMshkn
from tests.unit.test_embryo_declarations import VERB

CHAIN_VERB: dict[str, Any] = {
    "name": "counter",
    "description": "counts",
    "params": {"type": "object", "properties": {}},
    "dockerfile": "FROM mshkn-base\nRUN true",
    "entrypoint": "/verb/count.sh",
    "effect": "local",
    "state": "chain",
}


async def _ready(api: FakeMshkn, dockerfile: str) -> str:
    info = await submit_recipe(api, dockerfile)
    await api.get_recipe(info.id)
    return info.id


async def test_ephemeral_invocation_creates_a_self_destructing_computer() -> None:
    api = FakeMshkn()
    verb = parse_verb(VERB)
    rid = await _ready(api, verb.dockerfile)
    cmd = render_command(verb, {"url": "https://example.com"})
    api.outputs[cmd] = (0, "Example Domain\n", "")
    result = await invoke(api, verb, {"url": "https://example.com"}, recipe_id=rid, remaining=100.0)
    assert result == {
        "verb": "page_title",
        "status": "ok",
        "computer_id": "comp-1",
        "exit_code": 0,
        "stdout": "Example Domain\n",
        "stderr": "",
    }
    call = api.calls[-1]
    assert (
        call[0] == "create_computer" and call[1]["label"] is None and call[1]["needs"] == verb.needs
    )
    assert call[1]["timeout"] == 100.0


async def test_chain_invocation_creates_then_forks_and_reports_the_head() -> None:
    api = FakeMshkn()
    verb = parse_verb(CHAIN_VERB)
    rid = await _ready(api, verb.dockerfile)
    cmd = render_command(verb, {})
    api.outputs[cmd] = (0, "1\n", "")
    first = await invoke(api, verb, {}, recipe_id=rid, remaining=50.0)
    assert first["status"] == "ok" and first["chain_head"] == "ckpt-2"
    assert api.calls[-1][0] == "create_computer" and api.calls[-1][1]["label"] == "verb/counter"
    api.outputs[cmd] = (0, "2\n", "")
    second = await invoke(api, verb, {}, recipe_id=rid, remaining=50.0)
    assert (
        second["status"] == "ok" and second["stdout"] == "2\n" and second["chain_head"] == "ckpt-3"
    )
    assert api.calls[-1][0] == "fork_label"
    assert await chain_head(api, verb) == ("ckpt-3", 2)


async def test_a_busy_chain_is_reported_as_deferred_not_awaited() -> None:
    api = FakeMshkn()
    verb = parse_verb(CHAIN_VERB)
    rid = await _ready(api, verb.dockerfile)
    await invoke(api, verb, {}, recipe_id=rid, remaining=50.0)
    api.busy_labels.add("verb/counter")
    result = await invoke(api, verb, {}, recipe_id=rid, remaining=50.0)
    assert result["status"] == "deferred" and result["deferred_id"].startswith("def-")


async def test_errors_and_the_deadline_become_results_not_exceptions() -> None:
    api = FakeMshkn()
    verb = parse_verb(VERB)
    result = await invoke(api, verb, {"url": "u"}, recipe_id="rcp-missing", remaining=10.0)
    assert result["status"] == "error" and "not ready" in result["error"]
    result = await invoke(api, verb, {"url": "u"}, recipe_id="rcp-x", remaining=0.0)
    assert result == {"verb": "page_title", "status": "error", "error": "out of time"}
    result = await invoke(api, verb, {}, recipe_id="rcp-x", remaining=10.0)
    assert result["status"] == "error" and "url" in result["error"]


async def test_wait_for_recipe_polls_until_terminal_or_deadline() -> None:
    api = FakeMshkn()
    info = await api.create_recipe("FROM mshkn-base\nRUN a")
    api.recipe_statuses[info.id] = ["building", "building", "ready"]
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    done = await wait_for_recipe(api, info.id, until=100.0, now=lambda: next(clock), sleep=sleep)
    assert done.status == "ready" and slept == [3.0, 3.0]

    other = await api.create_recipe("FROM mshkn-base\nRUN b")
    api.recipe_statuses[other.id] = ["building"]
    late = await wait_for_recipe(api, other.id, until=1.0, now=lambda: 5.0, sleep=sleep)
    assert late.status == "building"


async def test_submit_recipe_reraises_a_422() -> None:
    api = FakeMshkn()
    api.reject_dockerfiles["FROM python"] = "recipes must be built FROM mshkn-base"
    with pytest.raises(MshknError, match="mshkn-base"):
        await submit_recipe(api, "FROM python")


async def test_poll_builds_moves_verbs_to_ready_or_failed_and_writes_the_inbox() -> None:
    api = FakeMshkn()
    good = parse_verb(VERB)
    bad = parse_verb({**CHAIN_VERB, "dockerfile": "FROM mshkn-base\nRUN apt-get install nope"})
    g = await api.create_recipe(good.dockerfile)
    b = await api.create_recipe(bad.dockerfile)
    api.recipe_statuses[b.id] = ["failed"]
    state = State()
    for verb, rid, pid in ((good, g.id, "p-1"), (bad, b.id, "p-2")):
        state.proposals[pid] = parse_proposal(
            {
                "kind": "verb",
                "title": "t",
                "rationale": "r",
                "verb": verb.to_doc(),
                "status": "building",
                "recipe_id": rid,
            },
            id=pid,
        )
        state.catalog[verb.name] = CatalogEntry(
            verb=verb, status="building", recipe_id=rid, proposal_id=pid
        )
    items = await poll_builds(api, state)
    assert [i.kind for i in items] == ["build", "build"]
    assert items[0].text == "verb page_title is ready"
    assert (
        "verb counter failed to build (proposal p-2)" in items[1].text and "nope" in items[1].text
    )
    assert (
        state.catalog["page_title"].status == "ready" and state.proposals["p-1"].status == "ready"
    )
    assert state.catalog["counter"].status == "failed" and state.proposals["p-2"].status == "failed"
    assert state.proposals["p-2"].log is not None and "nope" in state.proposals["p-2"].log
    assert await poll_builds(api, state) == []


async def test_poll_builds_leaves_a_still_building_entry_untouched() -> None:
    api = FakeMshkn()
    verb = parse_verb(VERB)
    rid = (await api.create_recipe(verb.dockerfile)).id
    api.recipe_statuses[rid] = ["building"]
    state = State()
    state.proposals["p-1"] = parse_proposal(
        {
            "kind": "verb",
            "title": "t",
            "rationale": "r",
            "verb": verb.to_doc(),
            "status": "building",
            "recipe_id": rid,
        },
        id="p-1",
    )
    state.catalog[verb.name] = CatalogEntry(
        verb=verb, status="building", recipe_id=rid, proposal_id="p-1"
    )
    items = await poll_builds(api, state)
    assert items == []
    assert state.catalog[verb.name].status == "building"


async def test_poll_builds_tolerates_a_catalog_entry_with_no_matching_proposal() -> None:
    api = FakeMshkn()
    good = parse_verb(VERB)
    bad = parse_verb({**CHAIN_VERB, "dockerfile": "FROM mshkn-base\nRUN apt-get install nope"})
    g = await api.create_recipe(good.dockerfile)
    b = await api.create_recipe(bad.dockerfile)
    api.recipe_statuses[b.id] = ["failed"]
    state = State()
    state.catalog[good.name] = CatalogEntry(
        verb=good, status="building", recipe_id=g.id, proposal_id="missing-1"
    )
    state.catalog[bad.name] = CatalogEntry(
        verb=bad, status="building", recipe_id=b.id, proposal_id="missing-2"
    )
    items = await poll_builds(api, state)
    assert [i.kind for i in items] == ["build", "build"]
    assert state.catalog[good.name].status == "ready"
    assert state.catalog[bad.name].status == "failed"


def test_log_tail_keeps_the_end() -> None:
    assert log_tail(None) == ""
    assert log_tail("x" * 3000, limit=5) == "xxxxx"
