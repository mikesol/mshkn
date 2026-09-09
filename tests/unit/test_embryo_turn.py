"""A say turn in order (spec §6): principal, builds, input, tools, loop, close."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from membrane.declarations import parse_policy, parse_verb, render_command
from membrane.hooks import principal_for
from membrane.memory import Provenance
from membrane.model import zero_usage
from membrane.proposals import approve, propose
from membrane.scripted import ScriptedModel
from membrane.state import Brain, CatalogEntry, Exchange, InboxItem, State
from membrane.turn import DOOR_CLOSED, compose_input, decode_payload, history_from, say

from tests.support_embryo import (
    FakeMshkn,
    ListMemory,
    StubModel,
    b64,
    split_output,
    text_completion,
    tool_call_completion,
)
from tests.unit.test_embryo_declarations import VERB
from tests.unit.test_embryo_proposals import CLOSED, HOOK

if TYPE_CHECKING:
    from pathlib import Path

OPEN = {
    "principals": {"ssh:mike": {"invoke": "*", "propose": True}},
    "hooks": ["verify_ssh"],
    "door": "open",
}


def _brain(tmp_path: Path, policy: dict[str, Any] = CLOSED) -> Brain:
    (tmp_path / "policy.json").write_text(json.dumps(policy))
    (tmp_path / "seed.md").write_text("SEED")
    return Brain(tmp_path)


def _brain_state(tmp_path: Path, policy: dict[str, Any] = CLOSED) -> tuple[Brain, State]:
    brain = _brain(tmp_path, policy)
    return brain, brain.state()


def _audit(out: str) -> dict[str, Any]:
    audit, _ = split_output(out)
    return audit


def _reply(out: str) -> str:
    _, reply = split_output(out)
    return reply


def test_decode_payload_and_compose_input() -> None:
    assert decode_payload(b64("hi")) == ("hi", "hi")
    assert decode_payload(b64({"msg": "Who am I?", "sig": "s"})) == (
        "Who am I?",
        json.dumps({"msg": "Who am I?", "sig": "s"}),
    )
    assert decode_payload("not base64!") is None
    # Valid JSON that is not a {"msg": <str>} object: the whole text is both
    # the message and the payload_text a hook receives.
    other = json.dumps({"sig": "s"})
    assert decode_payload(b64({"sig": "s"})) == (other, other)
    text = compose_input(
        turn=3,
        principal="ssh:mike",
        door="ingress",
        inbox=[InboxItem("build", "verb x is ready")],
        recalled=["mike likes tea"],
        message="hello",
    )
    assert text.startswith("[turn 3 | principal ssh:mike | door ingress]\n")
    assert (
        "inbox:\n- verb x is ready\n" in text
        and "recall:\n- mike likes tea\n" in text
        and text.endswith("message:\nhello")
    )


def test_history_is_the_last_ten_exchanges() -> None:
    window = [Exchange(i, "root", "api", f"in{i}", f"out{i}") for i in range(15)]
    history = history_from(window)
    assert len(history) == 20 and history[0] == {"role": "user", "content": "[root via api] in5"}
    assert history[-1] == {"role": "assistant", "content": "out14"}


async def test_root_turn_with_no_calls(tmp_path: Path) -> None:
    brain, state, api, memory = *_brain_state(tmp_path), FakeMshkn(), ListMemory()
    model = StubModel([text_completion("I am an embryo.")])
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64("Hello. I am the one who hatched you."),
        door="api",
        deadline=1e9,
    )
    audit = _audit(out)
    assert audit["principal"] == "root" and audit["door"] == "api" and audit["turn"] == 1
    # The token counts of the turn ride in the audit line (#101), so the cost of a
    # run is read from mshkn's exec_log, not from a side channel.
    assert audit["model_calls"] == 1 and audit["usage"] == zero_usage()
    assert _reply(out) == "I am an embryo.\n"
    system, messages, tools = model.calls[0]
    assert system == "SEED" and [t["name"] for t in tools] == ["remember", "try", "propose"]
    assert "message:\nHello. I am the one who hatched you." in messages[0]["content"]
    assert state.window[-1].reply == "I am an embryo." and state.turn == 1
    assert memory.entries[0][1].principal == "root" and "hatched" in memory.entries[0][0]


async def test_system_prompt_is_seed_then_self(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    state = brain.state()
    state.self_description = "I verify."
    model = StubModel([text_completion("ok")])
    await say(
        brain=brain,
        state=state,
        api=FakeMshkn(),
        model=model,
        memory=ListMemory(),
        payload_b64=b64("x"),
        door="api",
        deadline=1e9,
    )
    assert model.calls[0][0] == "SEED\n\nI verify."


async def test_closed_door_answers_one_line_and_never_calls_the_model(tmp_path: Path) -> None:
    brain, state, model = *_brain_state(tmp_path), StubModel([text_completion("never")])
    out = await say(
        brain=brain,
        state=state,
        api=FakeMshkn(),
        model=model,
        memory=ListMemory(),
        payload_b64=b64("Who am I?"),
        door="ingress",
        deadline=1e9,
    )
    assert (
        _reply(out) == DOOR_CLOSED + "\n"
        and model.calls == []
        and state.turn == 0
        and state.window == []
    )


async def test_the_hook_names_the_principal_and_anonymous_gets_nothing(tmp_path: Path) -> None:
    brain, state, api, memory = *_brain_state(tmp_path, OPEN), FakeMshkn(), ListMemory()
    hook = parse_verb(HOOK)
    info = await api.create_recipe(hook.dockerfile)
    await api.get_recipe(info.id)
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="ready", recipe_id=info.id, proposal_id="p-1"
    )
    payload = json.dumps({"msg": "Who am I?", "sig": "good"})
    api.outputs[render_command(hook, {"payload": payload})] = (0, "mike\n", "")
    memory.add("who hatched me: mike", Provenance("root", "api", 0))  # shares a word with the query
    model = StubModel([text_completion("You are ssh:mike.")])
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64({"msg": "Who am I?", "sig": "good"}),
        door="ingress",
        deadline=1e9,
    )
    audit = _audit(out)
    assert audit["principal"] == "ssh:mike"
    # the hook that named the caller is on the audit line, with its computer (#101)
    assert audit["hooks"] == [
        {
            "name": "verify_ssh",
            "status": "ok",
            "computer_id": audit["hooks"][0]["computer_id"],
            "exit_code": 0,
            "principal": "ssh:mike",
        }
    ]
    assert audit["hooks"][0]["computer_id"].startswith("comp-")
    # §10.7 is provable from the audit line alone: what was offered, not only
    # what was called. An authenticated principal who may propose gets all three.
    assert audit["offered"] == ["propose", "remember", "try", "verify_ssh"]
    _, messages, tools = model.calls[0]
    assert "recall:\n- who hatched me: mike" in messages[0]["content"]
    assert {t["name"] for t in tools} == {"remember", "try", "propose", "verify_ssh"}
    assert "ssh:mike" in state.principals and memory.entries[-1][1].principal == "ssh:mike"

    model = StubModel([text_completion("I do not know you.")])
    bad = json.dumps({"msg": "Who am I?", "sig": "bad"})
    api.outputs[render_command(hook, {"payload": bad})] = (1, "", "verify failed")
    before = len(memory.entries)
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64({"msg": "Who am I?", "sig": "bad"}),
        door="ingress",
        deadline=1e9,
    )
    audit = _audit(out)
    assert audit["principal"] == "anonymous"
    assert audit["offered"] == []  # §10.7: anonymous is offered no reserved tool
    _, messages, tools = model.calls[0]
    # messages[0] is now history from the first turn; the current turn's
    # composed input is the last message. Assert its header so this doesn't
    # depend on there being exactly one history exchange.
    assert messages[-1]["content"].startswith("[turn 2 | principal anonymous | door ingress]\n")
    assert tools == [] and "recall:\n\n" in messages[-1]["content"]
    assert len(memory.entries) == before and state.window[-1].principal == "anonymous"


async def test_hooks_fail_closed_when_not_ready_or_malformed() -> None:
    api, state = FakeMshkn(), State()
    policy = parse_policy(OPEN)
    assert await principal_for(api, state, policy, "p", remaining=10.0) == "anonymous"
    hook = parse_verb(HOOK)
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="building", recipe_id="r", proposal_id="p-1"
    )
    assert await principal_for(api, state, policy, "p", remaining=10.0) == "anonymous"
    two = parse_verb(
        {
            **HOOK,
            "params": {
                "type": "object",
                "properties": {"payload": {"type": "string"}, "extra": {}},
            },
        }
    )
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=two, status="ready", recipe_id="r", proposal_id="p-1"
    )
    assert await principal_for(api, state, policy, "p", remaining=10.0) == "anonymous"
    # Hold from Task 8's review: a ready hook with no `asserts` also fails closed,
    # without being invoked (door_is_open looks only at declared hook names).
    no_assert = parse_verb({**HOOK, "asserts": None})
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=no_assert, status="ready", recipe_id="r", proposal_id="p-1"
    )
    assert api.calls == []
    runs: list[dict[str, Any]] = []
    assert await principal_for(api, state, policy, "p", remaining=10.0, runs=runs) == "anonymous"
    assert api.calls == [] and runs == []  # never invoked, so nothing to record
    # A ready hook whose invocation itself errors (here: its recipe_id names
    # no recipe FakeMshkn knows) also yields anonymous: invoke()'s non-"ok"
    # status is skipped, not treated as a principal.
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="ready", recipe_id="no-such-recipe", proposal_id="p-1"
    )
    assert await principal_for(api, state, policy, "p", remaining=10.0) == "anonymous"


async def test_propose_tool_reports_a_declaration_error_as_invalid(tmp_path: Path) -> None:
    brain, state, api, memory = *_brain_state(tmp_path), FakeMshkn(), ListMemory()
    model = StubModel(
        [tool_call_completion("propose", title="verb with no kind"), text_completion("noted")]
    )
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64("go"),
        door="api",
        deadline=1e9,
    )
    audit = _audit(out)
    assert audit["tools"][0]["name"] == "propose" and audit["tools"][0]["status"] == "invalid"
    assert "kind" in audit["tools"][0]["error"]
    assert state.proposals == {} and audit["proposals"] == []


async def test_authenticated_without_propose_rights_gets_remember_only(tmp_path: Path) -> None:
    no_propose = {
        "principals": {"ssh:mike": {"invoke": [], "propose": False}},
        "hooks": ["verify_ssh"],
        "door": "open",
    }
    brain, state, api, memory = *_brain_state(tmp_path, no_propose), FakeMshkn(), ListMemory()
    hook = parse_verb(HOOK)
    info = await api.create_recipe(hook.dockerfile)
    await api.get_recipe(info.id)
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="ready", recipe_id=info.id, proposal_id="p-1"
    )
    good = json.dumps({"msg": "hi", "sig": "good"})
    api.outputs[render_command(hook, {"payload": good})] = (0, "mike\n", "")
    model = StubModel([text_completion("ok")])
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64({"msg": "hi", "sig": "good"}),
        door="ingress",
        deadline=1e9,
    )
    audit = _audit(out)
    assert audit["principal"] == "ssh:mike"
    # authenticated but may_propose is false: remember, and neither try nor propose
    assert audit["offered"] == ["remember"]
    _, _, tools = model.calls[0]
    assert {t["name"] for t in tools} == {"remember"}


async def test_anonymous_turn_polls_but_leaves_the_inbox_for_a_later_authenticated_turn(
    tmp_path: Path,
) -> None:
    """Ruling P2: the inbox is input for a principal who can act on it (§10.7
    denies anonymous `try`/`propose`). An anonymous turn still polls builds
    and trials so a failure lands in the inbox, but composes its input with
    an empty inbox and leaves `state.inbox` untouched for root's next turn."""
    brain, state, api, memory = *_brain_state(tmp_path, OPEN), FakeMshkn(), ListMemory()
    hook = parse_verb(HOOK)
    hook_info = await api.create_recipe(hook.dockerfile)
    await api.get_recipe(hook_info.id)
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="ready", recipe_id=hook_info.id, proposal_id="p-1"
    )
    other = parse_verb(VERB)
    other_info = await api.create_recipe(other.dockerfile)
    api.recipe_statuses[other_info.id] = ["failed"]
    state.catalog["page_title"] = CatalogEntry(
        verb=other, status="building", recipe_id=other_info.id, proposal_id="p-2"
    )
    bad = json.dumps({"msg": "hi", "sig": "bad"})
    api.outputs[render_command(hook, {"payload": bad})] = (1, "", "verify failed")

    model = StubModel([text_completion("I do not know you.")])
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64({"msg": "hi", "sig": "bad"}),
        door="ingress",
        deadline=1e9,
    )

    assert _audit(out)["principal"] == "anonymous"
    _, messages, _ = model.calls[0]
    assert "inbox:\n\n" in messages[0]["content"]
    assert state.catalog["page_title"].status == "failed"
    assert len(state.inbox) == 1 and "page_title failed to build" in state.inbox[0].text

    # Root's next turn drains it.
    model = StubModel([text_completion("Noted.")])
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64("go"),
        door="api",
        deadline=1e9,
    )
    _, messages, _ = model.calls[0]
    # messages[0] is history from the anonymous turn above; the composed
    # input for this turn is the last message. Assert its header so this
    # doesn't depend on there being exactly one history exchange.
    assert messages[-1]["content"].startswith("[turn 2 | principal root | door api]\n")
    assert "inbox:\n- verb page_title failed to build" in messages[-1]["content"]
    assert state.inbox == []


async def test_tools_run_and_proposals_are_appended_in_full(tmp_path: Path) -> None:
    brain, state, api, memory = *_brain_state(tmp_path), FakeMshkn(), ListMemory()
    proposal = {"kind": "verb", "title": "page_title", "rationale": "r", "verb": VERB}
    model = StubModel(
        [
            tool_call_completion("remember", text="mike hatched me"),
            tool_call_completion("propose", **proposal),
            text_completion("Proposed p-1."),
        ]
    )
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64("go"),
        door="api",
        deadline=1e9,
    )
    assert memory.entries[0][0] == "mike hatched me"
    assert state.proposals["p-1"].status == "pending"
    assert (
        "\nproposal p-1\n" in out
        and json.loads(out.split("proposal p-1\n", 1)[1])["verb"]["name"] == "page_title"
    )
    audit = _audit(out)
    assert [c["name"] for c in audit["tools"]] == ["remember", "propose"]
    assert audit["proposals"][0]["id"] == "p-1" and len(audit["proposals"][0]["sha256"]) == 64


async def test_a_ready_verb_is_a_tool_and_builds_are_polled_first(tmp_path: Path) -> None:
    brain, state, api, memory = *_brain_state(tmp_path), FakeMshkn(), ListMemory()
    p = propose(state, {"kind": "verb", "title": "t", "rationale": "r", "verb": VERB})
    await approve(api, state, p.id)
    verb = parse_verb(VERB)
    api.outputs[render_command(verb, {"url": "https://example.com"})] = (0, "Example Domain\n", "")
    model = StubModel(
        [
            tool_call_completion("page_title", url="https://example.com"),
            text_completion("Example Domain"),
        ]
    )
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64("page_title https://example.com"),
        door="api",
        deadline=1e9,
    )
    _, messages, tools = model.calls[0]
    assert "inbox:\n- verb page_title is ready" in messages[0]["content"]
    assert "page_title" in {t["name"] for t in tools}
    audit = _audit(out)
    assert audit["tools"][0] == {
        "name": "page_title",
        "status": "ok",
        "exit_code": 0,
        "computer_id": "comp-1",
    }
    assert _reply(out).startswith("Example Domain")


async def test_invalid_payload(tmp_path: Path) -> None:
    out = await say(
        brain=_brain(tmp_path),
        state=State(),
        api=FakeMshkn(),
        model=StubModel(),
        memory=ListMemory(),
        payload_b64="***",
        door="api",
        deadline=1e9,
    )
    assert "not base64" in _reply(out)


async def test_scripted_model_liturgy_turn_2_tries_and_proposes(tmp_path: Path) -> None:
    """Hold from Task 12's review: the `try` and `propose` tool handlers
    consume `ToolCall.input` exactly as `membrane.scripted.ScriptedModel`
    emits it. Liturgy turn 2 (spec §9): a real declaration is `try`-ed, then
    two full proposal documents are `propose`-d."""
    brain, state, api, memory = *_brain_state(tmp_path), FakeMshkn(), ListMemory()
    message = (
        "Your public door is closed because you cannot tell who is speaking. "
        "Propose a way to know that a message there comes from me, and open the door. "
        "My public key is ssh-ed25519 AAAAC3NzaC1lZDI1NTE5abcdef mike@laptop"
    )
    model = ScriptedModel()
    out = await say(
        brain=brain,
        state=state,
        api=api,
        model=model,
        memory=memory,
        payload_b64=b64(message),
        door="api",
        deadline=1e9,
    )
    audit = _audit(out)
    assert audit["principal"] == "root"
    assert [c["name"] for c in audit["tools"]] == ["try", "propose", "propose"]
    assert audit["tools"][0]["status"] == "done"

    proposal_ids = list(state.proposals)
    assert len(proposal_ids) == 2
    verb_proposal, policy_proposal = (
        state.proposals[proposal_ids[0]],
        state.proposals[proposal_ids[1]],
    )
    assert verb_proposal.kind == "verb"
    assert verb_proposal.verb is not None and verb_proposal.verb.name == "verify_ssh"
    assert verb_proposal.verb.asserts == "ssh"
    assert policy_proposal.kind == "policy"
    assert policy_proposal.policy is not None
    assert policy_proposal.policy.door == "open" and policy_proposal.policy.hooks == ("verify_ssh",)
    assert policy_proposal.policy.grant("ssh:mike").propose is True
    assert f"\nproposal {proposal_ids[0]}\n" in out and f"\nproposal {proposal_ids[1]}\n" in out
