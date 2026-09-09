"""Root commands (spec §6) and the console script: one command per fork of brain."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from membrane.cli import USAGE, run
from membrane.commands import list_state, root
from membrane.declarations import parse_policy
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
        code == 0
        and listing["door"] == {"status": "closed", "hooks": [], "hooks_ready": []}
        and listing["turn"] == 0
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
        out.startswith("p-2 applied")
        and Brain(brain_dir).state().self_description == "I fetch titles."
    )
    out, _ = await _run(brain_dir, ["root", "revert", "p-2"], api)
    assert out.startswith("p-2 reverted") and Brain(brain_dir).state().self_description == ""
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
    listing = json.loads(await list_state(api, state))
    assert listing["catalog"]["counter"]["chain_head"] == "ckpt-b"
    assert listing["catalog"]["counter"]["chain_length"] == 2


async def test_list_state_separates_the_declared_hooks_from_the_ready_ones(
    brain_dir: Path,
) -> None:
    """The door is fail-closed while no hook can run (`principal_for` skips a
    hook that is not `ready`), but `door_is_open` looks only at the declared
    names, so `list` would say `open` with nothing able to name a caller.
    `hooks_ready` is the effective state root reads before trusting the door."""
    from membrane.declarations import parse_verb
    from membrane.state import CatalogEntry

    open_policy = {**CLOSED, "hooks": ["verify_ssh"], "door": "open"}
    api, state = FakeMshkn(), State(policy=parse_policy(open_policy))
    hook = parse_verb({**VERB, "name": "verify_ssh", "asserts": "ssh"})
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="building", recipe_id="r", proposal_id="p-1"
    )
    listing = json.loads(await list_state(api, state))
    assert listing["door"] == {"status": "open", "hooks": ["verify_ssh"], "hooks_ready": []}

    state.catalog["verify_ssh"].status = "ready"
    listing = json.loads(await list_state(api, state))
    assert listing["door"] == {
        "status": "open",
        "hooks": ["verify_ssh"],
        "hooks_ready": ["verify_ssh"],
    }

    state.catalog["verify_ssh"].status = "disabled"
    listing = json.loads(await list_state(api, state))
    assert listing["door"]["hooks_ready"] == []


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
    # Hold from Task 8's review: an approval must leave the new policy and
    # its bookkeeping (applied_policy, previous_policy) durable on disk
    # together, via cli.run's one brain.save(state).
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
    assert reloaded.previous_policy == parse_policy(CLOSED)
    assert reloaded.policy.to_doc()["principals"]["ssh:mike"]["propose"] is True


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


@pytest.mark.parametrize("kind", ["policy", "prompt"])
async def test_a_crash_before_save_leaves_no_half_applied_proposal(
    brain_dir: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100: every command runs in a fork that is checkpointed whatever its
    exit code, so a crash between applying a proposal and saving must not
    leave the document new and the bookkeeping old. Interrupt at the save and
    assert a reloaded brain sees both as they were: the proposal is still
    pending and `revert` has nothing to act on."""
    api = FakeMshkn()
    state = Brain(brain_dir).state()
    if kind == "policy":
        doc: dict[str, Any] = {"policy": {**CLOSED, "hooks": [], "door": "closed"}}
    else:
        doc = {"prompt": "I fetch titles."}
    propose(state, {"kind": kind, "title": "t", "rationale": "r", **doc})
    Brain(brain_dir).save(state)
    before = (brain_dir / "state.json").read_bytes()

    def crash(self: Brain, state: State) -> None:
        raise RuntimeError("killed between apply and save")

    monkeypatch.setattr(Brain, "save", crash)
    with pytest.raises(RuntimeError):
        await _run(brain_dir, ["root", "approve", "p-1"], api)
    monkeypatch.undo()
    assert (brain_dir / "state.json").read_bytes() == before
    reloaded = Brain(brain_dir).state()
    assert reloaded.policy == parse_policy(CLOSED) and reloaded.self_description == ""
    assert reloaded.applied_policy is None and reloaded.applied_prompt is None
    assert reloaded.proposals["p-1"].status == "pending"
    out, code = await _run(brain_dir, ["root", "revert", "p-1"], api)
    assert code == 0 and "not applied" in out


async def test_list_state_names_each_trials_recipe(brain_dir: Path) -> None:
    """A trial builds a recipe on the account (spec §5); `list` names it, so the
    measure's "no undeclared capability" check can tell a trial's recipe from a
    stray one (live run 2026-09-09-run-1 flagged the turn-1 trial as undeclared)."""
    from membrane.declarations import parse_verb
    from membrane.state import Trial

    state = State()
    state.trials["t-1"] = Trial(
        id="t-1", verb=parse_verb(VERB), params={}, recipe_id="rcp-trial", status="done", result={}
    )
    listing = json.loads(await list_state(FakeMshkn(), state))
    assert listing["trials"] == [
        {"id": "t-1", "verb": VERB["name"], "status": "done", "recipe_id": "rcp-trial"}
    ]
