"""Root commands (spec §6) and the console script: one command per fork of brain."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from membrane.cli import USAGE, run
from membrane.commands import list_state, root
from membrane.proposals import propose
from membrane.state import Brain, State

from tests.support_embryo import FakeMshkn, ListMemory, StubModel, b64, text_completion
from tests.unit.test_embryo_declarations import VERB
from tests.unit.test_embryo_proposals import CLOSED
from tests.unit.test_embryo_verbs import CHAIN_VERB

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def brain_dir(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text(
        "MSHKN_API_URL=http://x\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n"
    )
    (tmp_path / "policy.json").write_text(json.dumps(CLOSED))
    (tmp_path / "seed.md").write_text("SEED")
    return tmp_path


async def _run(
    brain_dir: Path, argv: list[str], api: FakeMshkn, model: StubModel | None = None
) -> tuple[str, int]:
    return await run(
        argv, brain_dir=brain_dir, api=api, model=model or StubModel(), memory=ListMemory()
    )


async def test_usage(brain_dir: Path) -> None:
    api = FakeMshkn()
    for argv in ([], ["dance"], ["root"], ["root", "approve"], ["say"]):
        out, code = await _run(brain_dir, argv, api)
        assert code == 2 and out == USAGE


async def test_public_say_and_root_say_differ_by_door(brain_dir: Path) -> None:
    api = FakeMshkn()
    out, code = await _run(brain_dir, ["say", b64("hi")], api)
    assert code == 0 and "The public door is closed." in out
    out, code = await _run(
        brain_dir, ["root", "say", b64("hi")], api, StubModel([text_completion("hello")])
    )
    assert code == 0 and out.endswith("hello\n") and '"principal": "root"' in out.splitlines()[0]
    assert Brain(brain_dir).state().turn == 1  # state was saved


async def test_list_approve_reject_disable_revert_round_trip(brain_dir: Path) -> None:
    api = FakeMshkn()
    state = Brain(brain_dir).state()
    propose(state, {"kind": "verb", "title": "page_title", "rationale": "r", "verb": VERB})
    propose(
        state, {"kind": "prompt", "title": "self", "rationale": "r", "prompt": "I fetch titles."}
    )
    Brain(brain_dir).save(state)
    out, code = await _run(brain_dir, ["root", "list"], api)
    listing = json.loads(out)
    assert (
        code == 0 and listing["door"] == {"status": "closed", "hooks": []} and listing["turn"] == 0
    )
    assert [p["id"] for p in listing["proposals"]] == ["p-1", "p-2"] and listing["catalog"] == {}
    out, code = await _run(brain_dir, ["root", "approve", "p-1"], api)
    assert code == 0 and out.startswith("p-1 building")
    out, _ = await _run(brain_dir, ["root", "list"], api)
    listing = json.loads(out)
    entry = listing["catalog"]["page_title"]
    assert entry["status"] == "ready" and entry["chain_head"] is None and entry["chain_length"] == 0
    assert listing["inbox"] == 1  # the build transition waits for the next say
    out, _ = await _run(brain_dir, ["root", "approve", "p-2"], api)
    assert (
        out.startswith("p-2 applied") and Brain(brain_dir).self_description() == "I fetch titles."
    )
    out, _ = await _run(brain_dir, ["root", "revert", "p-2"], api)
    assert out.startswith("p-2 reverted") and Brain(brain_dir).self_description() == ""
    out, _ = await _run(brain_dir, ["root", "disable", "page_title"], api)
    assert out == "page_title disabled\n"
    # p-1 is ready (poll_builds) then disabled (disable's proposal coupling,
    # pinned by test_reject_and_disable) so it can no longer be rejected; a
    # fresh, still-pending proposal exercises the reject path instead.
    extra_state = Brain(brain_dir).state()
    propose(
        extra_state,
        {
            "kind": "verb",
            "title": "other_verb",
            "rationale": "r",
            "verb": {**VERB, "name": "other_verb"},
        },
    )
    Brain(brain_dir).save(extra_state)
    out, _ = await _run(brain_dir, ["root", "reject", "p-3", b64("too broad")], api)
    assert out == "p-3 rejected: too broad\n"
    out, code = await _run(brain_dir, ["root", "approve", "p-77"], api)
    assert code == 1 and out == "no proposal p-77\n"


async def test_list_state_reports_chain_heads(brain_dir: Path) -> None:
    api = FakeMshkn()
    api.chains["verb/counter"] = ["ckpt-a", "ckpt-b"]
    state = State()
    from membrane.declarations import parse_verb
    from membrane.state import CatalogEntry

    state.catalog["counter"] = CatalogEntry(
        verb=parse_verb(CHAIN_VERB), status="ready", recipe_id="r", proposal_id="p-1"
    )
    listing = json.loads(await list_state(api, state, Brain(brain_dir)))
    assert listing["catalog"]["counter"]["chain_head"] == "ckpt-b"
    assert listing["catalog"]["counter"]["chain_length"] == 2


async def test_root_rejects_unknown_command_directly(brain_dir: Path) -> None:
    # commands.root must be safe even when called with a command cli._valid()
    # would never let through (P13): no fall-through to revert, USAGE and 2.
    api = FakeMshkn()
    brain = Brain(brain_dir)
    state = brain.state()
    out, code = await root(
        ["bogus"],
        brain=brain,
        state=state,
        api=api,
        model=None,
        memory=None,
        deadline=1e9,
        now=lambda: 0.0,
        sleep=asyncio.sleep,
    )
    assert code == 2 and out == USAGE


async def test_approve_policy_persists_state_through_cli_run(brain_dir: Path) -> None:
    # Hold from Task 8's review: approve (which writes policy.json/self.md
    # through Brain immediately) must also leave state.applied_policy and
    # state.previous_policy durable on disk, via cli.run's brain.save(state).
    api = FakeMshkn()
    state = Brain(brain_dir).state()
    new_policy = {
        "principals": {
            "anonymous": {"invoke": [], "propose": False},
            "ssh:mike": {"invoke": ["page_title"], "propose": True},
        },
        "hooks": [],
        "door": "closed",
    }
    propose(
        state, {"kind": "policy", "title": "grant mike", "rationale": "r", "policy": new_policy}
    )
    Brain(brain_dir).save(state)
    out, code = await _run(brain_dir, ["root", "approve", "p-1"], api)
    assert code == 0 and out.startswith("p-1 applied")
    reloaded = Brain(brain_dir).state()
    assert reloaded.applied_policy == "p-1"
    assert reloaded.previous_policy == CLOSED
    assert Brain(brain_dir).policy().to_doc()["principals"]["ssh:mike"]["propose"] is True


async def test_run_builds_and_closes_the_real_clients(
    brain_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When run() is not handed an api/model/memory it must build the real
    # ones (Mshkn.connect, build_model, Mem0Store.open) and close what it
    # owns in `finally`. The public door is closed, so `say` returns before
    # any request would reach mshkn; the mock transport guarantees that even
    # if it did, no real network call would occur.
    import httpx
    from membrane.memory import Mem0Store
    from membrane.mshkn import Mshkn

    closed: list[str] = []
    real_connect = Mshkn.connect

    def fake_connect(settings: Any) -> Mshkn:
        client = real_connect(settings)
        client.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
            base_url=client.http.base_url,
            headers=client.http.headers,
        )
        return client

    real_aclose = Mshkn.aclose

    async def tracking_aclose(self: Mshkn) -> None:
        closed.append("api")
        await real_aclose(self)

    real_close = Mem0Store.close

    def tracking_close(self: Mem0Store) -> None:
        closed.append("memory")
        real_close(self)

    monkeypatch.setattr(Mshkn, "connect", staticmethod(fake_connect))
    monkeypatch.setattr(Mshkn, "aclose", tracking_aclose)
    monkeypatch.setattr(Mem0Store, "close", tracking_close)

    out, code = await run(["say", b64("hi")], brain_dir=brain_dir)
    assert code == 0 and "The public door is closed." in out
    assert closed == ["memory", "api"]


def test_main_prints_and_exits(
    brain_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import membrane.cli as cli

    async def fake_run(argv: list[str], **kwargs: Any) -> tuple[str, int]:
        return f"ran {argv}\n", 3

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["membrane", "root", "list"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3 and capsys.readouterr().out == "ran ['root', 'list']\n"
