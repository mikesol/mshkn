"""A say turn is a chain of forks (relay design §6): say posts the request and
acknowledges; resume appends the answer, runs its calls, posts the next request
or closes; every command settles a pending turn first; a say while one is
pending is queued and runs next."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from membrane.config import Settings
from membrane.declarations import parse_policy, parse_verb, render_command
from membrane.hooks import principal_for
from membrane.loop import CAP_REACHED, OUT_OF_TOKENS
from membrane.memory import Provenance
from membrane.model import zero_usage
from membrane.mshkn import MshknError, RelayJob
from membrane.principals import ROOT
from membrane.proposals import approve, propose
from membrane.state import Brain, CatalogEntry, Exchange, InboxItem, State
from membrane.turn import (
    BAD_PAYLOAD,
    DOOR_CLOSED,
    MODEL_FAILED,
    Context,
    Door,
    compose_input,
    decode_payload,
    history_from,
    resume,
    say,
    settle,
)
from membrane.verbs import poll_builds

from tests.support_embryo import (
    FakeMshkn,
    ListMemory,
    b64,
    failed_job,
    http_error_job,
    in_progress_job,
    message_of,
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

Answer = dict[str, Any] | RelayJob


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "brain": tmp_path,
        "api_url": "http://api",
        "api_key": "mk-scoped",
        "model": "scripted",
        "model_id": "claude-opus-5",
        "anthropic_api_key": None,
        "openai_api_key": None,
    }
    fields.update(overrides)
    return Settings(**fields)


async def _no_sleep(seconds: float) -> None:
    return None


def _ctx(
    tmp_path: Path,
    *,
    policy: dict[str, Any] = CLOSED,
    api: FakeMshkn | None = None,
    memory: ListMemory | None = None,
    answers: list[Answer] | None = None,
    **settings: Any,
) -> Context:
    (tmp_path / "policy.json").write_text(json.dumps(policy))
    (tmp_path / "seed.md").write_text("SEED")
    brain = Brain(tmp_path)
    api = api or FakeMshkn()
    if answers:
        api.relay_answers.extend(answers)
    return Context(
        brain=brain,
        state=brain.state(),
        api=api,
        settings=_settings(tmp_path, **settings),
        deadline=1e9,
        memory=memory or ListMemory(),
        now=lambda: 0.0,
        sleep=_no_sleep,
    )


def _fake(ctx: Context) -> FakeMshkn:
    """The relay and mshkn behind a Context built by `_ctx`."""
    api = ctx.api
    assert isinstance(api, FakeMshkn)
    return api


def _posted(ctx: Context, index: int = -1) -> dict[str, Any]:
    """The body of a request the membrane handed the relay, newest last. The
    fake's job ids share a counter with its computers, so a turn whose hook ran
    first is not `rj-1`; the order of the posts is what the tests are about."""
    return dict(list(_fake(ctx).relay_jobs.values())[index]["body"])


def _ack(out: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A say's output: its start audit line and the acknowledgement."""
    audit, rest = split_output(out)
    return audit, dict(json.loads(rest))


async def _turn(
    ctx: Context, payload: Any, door: Door = "api", *, answers: list[Answer] | None = None
) -> str:
    """A whole turn in one call: say, then resume until the turn closes. Returns
    the output of the fork that closed it (the closing audit line and the reply)."""
    if answers:
        _fake(ctx).relay_answers.extend(answers)
    out = await say(ctx, payload_b64=b64(payload), door=door)
    audit, _ = split_output(out)
    if "job" not in audit:
        return out
    while (pending := ctx.state.pending) is not None:
        out = await resume(ctx, pending.job)
    return out


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
    live = parse_policy({**CLOSED, "hooks": ["verify_ssh"], "door": "open"})
    text = compose_input(
        turn=3,
        principal="ssh:mike",
        door="ingress",
        policy=live.to_doc(),
        inbox=[InboxItem("build", "verb x is ready")],
        recalled=["mike likes tea"],
        message="hello",
    )
    assert text.startswith("[turn 3 | principal ssh:mike | door ingress]\n")
    # The policy is part of the turn's environment (#123): the embryo may be
    # asked to replace it as a whole document, so it must be able to read it.
    assert '"door": "open"' in text and '"hooks": ["verify_ssh"]' in text
    assert (
        "inbox:\n- verb x is ready\n" in text
        and "recall:\n- mike likes tea\n" in text
        and text.endswith("message:\nhello")
    )


def test_history_is_the_last_ten_exchanges() -> None:
    window = [
        Exchange(turn=i, principal="root", door="api", input=f"in{i}", reply=f"out{i}")
        for i in range(12)
    ]
    history = history_from(window)
    assert len(history) == 20 and history[0]["content"] == "[root via api] in2"
    assert history[-1]["content"] == "out11"


async def test_a_say_posts_the_request_and_acknowledges_without_a_model_call(
    tmp_path: Path,
) -> None:
    ctx = _ctx(
        tmp_path,
        anthropic_api_key="sk-test",
        model="anthropic",
        openai_api_key="o",
        default_effort="medium",
    )
    out = await say(ctx, payload_b64=b64("Hello. I am the one who hatched you."), door="api")
    audit, ack = _ack(out)
    assert audit["started"] is True and audit["turn"] == 1
    assert audit["principal"] == "root" and audit["door"] == "api"
    assert audit["offered"] == ["effort", "propose", "remember", "try"]
    assert audit["job"] == "rj-1"
    assert ack == {"turn": 1, "job": "rj-1"}
    posted = _fake(ctx).relay_jobs["rj-1"]
    assert posted["target"] == "https://api.anthropic.com/v1/messages"
    assert posted["headers"]["x-api-key"] == "sk-test"
    assert posted["headers"]["anthropic-version"] == "2023-06-01"
    body = posted["body"]
    assert body["system"] == "SEED" and body["stream"] is True
    assert body["output_config"] == {"effort": "medium"}
    # insertion order
    assert [t["name"] for t in body["tools"]] == ["remember", "effort", "try", "propose"]
    assert body["messages"][-1]["role"] == "user"
    assert "hatched you" in body["messages"][-1]["content"]
    pending = ctx.state.pending
    assert pending is not None and pending.job == "rj-1" and pending.turn == 1
    assert pending.forks == 1 and pending.model_calls == 1
    assert pending.write_memory is True and pending.started_at
    assert ctx.state.window == [] and ctx.state.turn == 1
    assert not any(name == "get_relay_job" for name, _ in _fake(ctx).calls)


async def test_system_prompt_is_seed_then_self(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.state.self_description = "I verify."
    await say(ctx, payload_b64=b64("x"), door="api")
    assert _posted(ctx)["system"] == "SEED\n\nI verify."


async def test_a_text_answer_closes_the_turn_with_the_full_audit_and_the_reply(
    tmp_path: Path,
) -> None:
    memory = ListMemory()
    ctx = _ctx(tmp_path, memory=memory, answers=[message_of(text_completion("I am an embryo."))])
    await say(ctx, payload_b64=b64("Hello. I am the one who hatched you."), door="api")
    out = await resume(ctx, "rj-1")
    audit, reply = split_output(out)
    assert reply == "I am an embryo.\n"
    assert audit["turn"] == 1 and audit["stopped"] == "done" and audit["model_calls"] == 1
    assert audit["usage"] == zero_usage() and audit["forks"] == 1 and audit["job"] == "rj-1"
    assert audit["tools"] == [] and audit["proposals"] == [] and audit["memory_written"] is True
    assert ctx.state.pending is None
    entry = ctx.state.window[-1]
    assert entry.reply == "I am an embryo." and entry.output == "I am an embryo.\n"
    assert entry.audit["stopped"] == "done"
    assert memory.entries[0][1] == Provenance(principal=ROOT, door="api", turn=1)
    assert "hatched" in memory.entries[0][0] and "I am an embryo." in memory.entries[0][0]


async def test_memory_written_reports_what_the_store_did_not_what_was_intended(
    tmp_path: Path,
) -> None:
    """#107: mem0 catches its own extraction failure and stores nothing; the audit
    line said `memory_written: true` anyway."""
    memory = ListMemory(stores=False)
    ctx = _ctx(tmp_path, memory=memory, answers=[message_of(text_completion("I am an embryo."))])
    await say(ctx, payload_b64=b64("Hello. I am the one who hatched you."), door="api")
    audit, _ = split_output(await resume(ctx, "rj-1"))
    assert audit["memory_written"] is False
    assert memory.entries == []


async def test_tool_calls_run_and_the_next_request_is_posted_in_a_new_fork(
    tmp_path: Path,
) -> None:
    memory = ListMemory()
    proposal = {"kind": "verb", "title": "page_title", "rationale": "r", "verb": VERB}
    ctx = _ctx(
        tmp_path,
        memory=memory,
        answers=[
            message_of(tool_call_completion("remember", text="mike hatched me")),
            message_of(tool_call_completion("propose", **proposal)),
            message_of(text_completion("Proposed p-1.")),
        ],
    )
    await say(ctx, payload_b64=b64("go"), door="api")
    first = await resume(ctx, "rj-1")
    audit, rest = split_output(first)
    assert rest == "" and audit["continued"] is True
    assert audit["job"] == "rj-1" and audit["next_job"] == "rj-2"
    assert audit["calls"] == [{"name": "remember", "status": "remembered"}]
    assert memory.entries[0][0] == "mike hatched me"
    pending = ctx.state.pending
    assert pending is not None and pending.job == "rj-2"
    assert pending.forks == 2 and pending.model_calls == 2
    assert pending.messages[-2]["role"] == "assistant"
    assert pending.messages[-2]["content"][0]["type"] == "tool_use"
    assert pending.messages[-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_remember",
                "content": '{"status": "remembered"}',
            }
        ],
    }
    assert _posted(ctx)["messages"] == pending.messages
    await resume(ctx, "rj-2")
    out = await resume(ctx, "rj-3")
    audit, reply = split_output(out)
    assert reply.startswith("Proposed p-1.\nproposal p-1\n")
    assert json.loads(reply.split("proposal p-1\n", 1)[1])["verb"]["name"] == "page_title"
    assert [c["name"] for c in audit["tools"]] == ["remember", "propose"]
    assert audit["proposals"][0]["id"] == "p-1" and len(audit["proposals"][0]["sha256"]) == 64
    assert audit["forks"] == 3 and audit["model_calls"] == 3
    assert ctx.state.proposals["p-1"].status == "pending"
    # the audit sink: the last exec's stdout carries the whole turn's audit line
    assert ctx.state.window[-1].audit["tools"] == audit["tools"]


async def test_a_forged_or_stale_job_id_does_nothing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert await resume(ctx, "rj-77") == "no pending turn for job rj-77\n"
    await say(ctx, payload_b64=b64("go"), door="api")
    assert await resume(ctx, "rj-77") == "no pending turn for job rj-77\n"
    assert ctx.state.pending is not None and ctx.state.pending.job == "rj-1"
    assert not any(name == "get_relay_job" for name, _ in _fake(ctx).calls)


async def test_resume_and_settle_leave_a_job_still_in_progress(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, answers=[in_progress_job(), message_of(text_completion("later"))])
    await say(ctx, payload_b64=b64("go"), door="api")
    assert await settle(ctx) == "" and ctx.state.pending is not None
    # the fake answers every read of a job the same way until its results are cleared
    assert await resume(ctx, "rj-1") == "job rj-1 is in_progress\n"
    _fake(ctx).relay_results.clear()
    audit, reply = split_output(await resume(ctx, "rj-1"))
    assert reply == "later\n" and audit["stopped"] == "done" and ctx.state.pending is None


async def test_a_failed_job_and_a_non_2xx_answer_end_the_turn_with_the_error(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path, answers=[failed_job("HTTP 529 after 3 attempts")])
    await say(ctx, payload_b64=b64("go"), door="api")
    audit, reply = split_output(await settle(ctx))
    assert audit["stopped"] == "error" and reply.startswith(MODEL_FAILED) and "529" in reply
    assert ctx.state.pending is None and ctx.state.window[-1].audit["stopped"] == "error"
    _fake(ctx).relay_answers.append(
        http_error_job(400, {"error": {"message": "max_tokens too large"}})
    )
    await say(ctx, payload_b64=b64("again"), door="api")
    audit, reply = split_output(await resume(ctx, "rj-2"))
    assert audit["stopped"] == "error" and "HTTP 400" in reply
    assert "max_tokens too large" in reply


async def test_max_tokens_and_the_cap_end_the_turn_honestly(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        answers=[
            message_of(tool_call_completion("remember", text="never"), stop_reason="max_tokens")
        ],
    )
    memory = ctx.memory
    assert isinstance(memory, ListMemory)
    await say(ctx, payload_b64=b64("go"), door="api")
    audit, reply = split_output(await resume(ctx, "rj-1"))
    assert audit["stopped"] == "max_tokens" and reply.startswith(OUT_OF_TOKENS)
    assert audit["tools"] == []
    assert not any(text == "never" for text, _ in memory.entries), (
        "calls of a truncated answer never run"
    )
    # the cap: 20 calls over as many forks, then the 21st ends the turn
    _fake(ctx).relay_answers.extend(
        message_of(tool_call_completion("remember", text=f"fact {i}")) for i in range(21)
    )
    await say(ctx, payload_b64=b64("count"), door="api")
    out = ""
    while (pending := ctx.state.pending) is not None:
        out = await resume(ctx, pending.job)
    audit, reply = split_output(out)
    assert audit["stopped"] == "cap" and reply.startswith(CAP_REACHED)
    assert len(audit["tools"]) == 20 and audit["forks"] == 21


async def test_a_job_the_relay_has_forgotten_ends_the_turn_instead_of_stranding_it(
    tmp_path: Path,
) -> None:
    """A relay job is expired and reaped (relay design §11). Neither the wake-up
    nor the next command may leave the brain pending on a job that is gone."""
    ctx = _ctx(tmp_path)
    await say(ctx, payload_b64=b64("go"), door="api")
    del _fake(ctx).relay_jobs["rj-1"]  # the relay reaped it
    audit, reply = split_output(await resume(ctx, "rj-1"))
    assert audit["stopped"] == "error" and reply == f"{MODEL_FAILED} job rj-1 is gone\n"
    assert ctx.state.pending is None
    await say(ctx, payload_b64=b64("again"), door="api")
    del _fake(ctx).relay_jobs["rj-2"]
    audit, reply = split_output(await settle(ctx))
    assert audit["stopped"] == "error" and reply == f"{MODEL_FAILED} job rj-2 is gone\n"
    assert ctx.state.pending is None


async def test_a_relay_that_is_itself_down_leaves_the_turn_for_the_next_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything but a 404 is the relay's problem, not this turn's: `settle` says
    nothing and leaves the turn pending, and `resume` raises so the fork's exit
    code and traceback reach the exec log."""
    ctx = _ctx(tmp_path)
    await say(ctx, payload_b64=b64("go"), door="api")

    async def down(job_id: str) -> RelayJob:
        raise MshknError(503, "relay unavailable")

    monkeypatch.setattr(_fake(ctx), "get_relay_job", down)
    assert await settle(ctx) == "" and ctx.state.pending is not None
    with pytest.raises(MshknError):
        await resume(ctx, "rj-1")
    assert ctx.state.pending is not None


async def test_a_2xx_answer_that_is_not_a_message_ends_the_turn(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, answers=[http_error_job(200, "<html>gateway</html>")])
    await say(ctx, payload_b64=b64("go"), door="api")
    audit, reply = split_output(await resume(ctx, "rj-1"))
    assert audit["stopped"] == "error"
    assert reply == f"{MODEL_FAILED} the response is not a message\n"


async def test_the_try_tool_trials_a_declaration_without_installing_it(tmp_path: Path) -> None:
    api = FakeMshkn()
    ctx = _ctx(tmp_path, api=api)
    api.outputs[render_command(parse_verb(VERB), {"url": "u"})] = (0, "Example Domain", "")
    out = await _turn(
        ctx,
        "try it",
        answers=[
            message_of(tool_call_completion("try", verb=VERB, params={"url": "u"})),
            message_of(text_completion("It works.")),
        ],
    )
    audit, _ = split_output(out)
    assert audit["tools"][0]["name"] == "try" and audit["tools"][0]["status"] == "done"
    assert ctx.state.trials["t-1"].status == "done"
    # a trial installs nothing: no catalog entry, no proposal
    assert ctx.state.catalog == {} and ctx.state.proposals == {}


async def test_a_say_while_a_turn_is_pending_is_queued_and_runs_next(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        answers=[
            message_of(text_completion("first reply")),
            message_of(text_completion("second reply")),
        ],
    )
    await say(ctx, payload_b64=b64("first"), door="api")
    audit, ack = _ack(await say(ctx, payload_b64=b64("second"), door="api"))
    assert ack == {"queued": 1} and audit["queued"] == 1 and audit["principal"] == "root"
    assert len(ctx.state.queue) == 1 and ctx.state.turn == 1
    out = await resume(ctx, "rj-1")
    lines = out.splitlines()
    assert lines[0].startswith("audit ") and lines[1] == "first reply"
    started = json.loads(lines[2][len("audit ") :])
    assert started["started"] is True and started["turn"] == 2 and started["job"] == "rj-2"
    assert json.loads(lines[3]) == {"turn": 2, "job": "rj-2"}
    assert ctx.state.queue == [] and ctx.state.pending is not None
    assert ctx.state.pending.message == "second"
    audit, reply = split_output(await resume(ctx, "rj-2"))
    assert reply == "second reply\n"
    assert [e.reply for e in ctx.state.window] == ["first reply", "second reply"]
    assert _posted(ctx)["messages"][0]["content"] == "[root via api] first"


async def test_a_queued_turns_closing_audit_carries_the_hooks_that_named_it(
    tmp_path: Path,
) -> None:
    """The hooks of a queued message run at queue time, in a different fork from
    the one that closes its turn. The closing audit line is what authorization is
    read from (§10.5), so it must carry them rather than deny them."""
    api = FakeMshkn()
    ctx = _ctx(tmp_path, policy=OPEN, api=api)
    hook = parse_verb(HOOK)
    info = await api.create_recipe(hook.dockerfile)
    await api.get_recipe(info.id)
    ctx.state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="ready", recipe_id=info.id, proposal_id="p-1"
    )
    good = json.dumps({"msg": "Who am I?", "sig": "good"})
    api.outputs[render_command(hook, {"payload": good})] = (0, "mike\n", "")
    api.relay_answers.extend(
        [message_of(text_completion("first")), message_of(text_completion("You are ssh:mike."))]
    )
    await say(ctx, payload_b64=b64("root goes first"), door="api")
    queued, ack = _ack(
        await say(ctx, payload_b64=b64({"msg": "Who am I?", "sig": "good"}), door="ingress")
    )
    assert ack == {"queued": 1} and queued["principal"] == "ssh:mike"
    assert queued["hooks"][0]["name"] == "verify_ssh"
    assert ctx.state.queue[0].hooks == queued["hooks"]
    await resume(ctx, "rj-1")  # closes root's turn and starts the queued one
    pending = ctx.state.pending
    assert pending is not None and pending.turn == 2
    audit, reply = split_output(await resume(ctx, pending.job))
    assert reply == "You are ssh:mike.\n" and audit["principal"] == "ssh:mike"
    assert audit["hooks"] == queued["hooks"]


async def test_tools_are_rebuilt_from_the_current_catalog_on_every_fork(tmp_path: Path) -> None:
    api = FakeMshkn()
    ctx = _ctx(tmp_path, api=api)
    await say(ctx, payload_b64=b64("go"), door="api")
    assert ctx.state.pending is not None and "page_title" not in ctx.state.pending.offered
    # root approves a verb while the model thinks, and its build is polled by root's next command
    p = propose(ctx.state, {"kind": "verb", "title": "t", "rationale": "r", "verb": VERB})
    await approve(api, ctx.state, p.id)
    ctx.state.inbox.extend(await poll_builds(api, ctx.state))
    assert ctx.state.catalog["page_title"].status == "ready"
    verb = parse_verb(VERB)
    api.outputs[render_command(verb, {"url": "https://example.com"})] = (0, "Example Domain\n", "")
    api.relay_answers.extend(
        [
            message_of(tool_call_completion("page_title", url="https://example.com")),
            message_of(text_completion("Example Domain")),
        ]
    )
    await resume(ctx, "rj-1")
    assert ctx.state.pending is not None
    assert ctx.state.pending.calls[0]["result"]["stdout"] == "Example Domain\n"
    assert [t["name"] for t in _posted(ctx)["tools"]] == [
        "remember",
        "effort",
        "try",
        "propose",
        "page_title",
    ]
    # the closing audit names every tool the turn offered, not only the first fork's
    audit, _ = split_output(await resume(ctx, ctx.state.pending.job))
    assert audit["offered"] == ["effort", "page_title", "propose", "remember", "try"]


async def test_a_turn_that_ends_on_the_cap_claims_no_tools_it_never_offered(
    tmp_path: Path,
) -> None:
    """The fork that hits the cap builds tools for a request it never posts. The
    audit must not deny a verb it offered, and must not claim one it did not."""
    api = FakeMshkn()
    ctx = _ctx(tmp_path, api=api)
    api.relay_answers.extend(
        message_of(tool_call_completion("remember", text=f"fact {i}")) for i in range(21)
    )
    await say(ctx, payload_b64=b64("count"), door="api")
    # root approves a verb after the first request went out; the fork that would
    # have offered it is the one the cap ends.
    p = propose(ctx.state, {"kind": "verb", "title": "t", "rationale": "r", "verb": VERB})
    await approve(api, ctx.state, p.id)
    ctx.state.inbox.extend(await poll_builds(api, ctx.state))
    out = ""
    while (pending := ctx.state.pending) is not None:
        out = await resume(ctx, pending.job)
    audit, _ = split_output(out)
    assert audit["stopped"] == "cap" and len(audit["tools"]) == 20
    # page_title rode on the 20 requests that were posted, but not on the 21st
    assert audit["offered"] == ["effort", "page_title", "propose", "remember", "try"]


async def test_closed_door_and_bad_payload_answer_one_line_and_post_nothing(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    out = await say(ctx, payload_b64=b64("hi"), door="ingress")
    audit, reply = split_output(out)
    assert audit["closed"] is True and audit["principal"] is None and reply == DOOR_CLOSED + "\n"
    out = await say(ctx, payload_b64="not base64!", door="api")
    audit, reply = split_output(out)
    assert audit["error"] == "bad payload" and reply == BAD_PAYLOAD + "\n"
    assert ctx.state.pending is None and ctx.state.turn == 0 and _fake(ctx).relay_jobs == {}


# --- kept from the synchronous turn, driven through the chain of forks -----------


async def test_the_hook_names_the_principal_and_anonymous_gets_nothing(tmp_path: Path) -> None:
    api, memory = FakeMshkn(), ListMemory()
    ctx = _ctx(tmp_path, policy=OPEN, api=api, memory=memory)
    state = ctx.state
    hook = parse_verb(HOOK)
    info = await api.create_recipe(hook.dockerfile)
    await api.get_recipe(info.id)
    state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="ready", recipe_id=info.id, proposal_id="p-1"
    )
    payload = json.dumps({"msg": "Who am I?", "sig": "good"})
    api.outputs[render_command(hook, {"payload": payload})] = (0, "mike\n", "")
    memory.add("who hatched me: mike", Provenance("root", "api", 0))  # shares a word with the query
    out = await _turn(
        ctx,
        {"msg": "Who am I?", "sig": "good"},
        "ingress",
        answers=[message_of(text_completion("You are ssh:mike."))],
    )
    audit, _ = split_output(out)
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
    assert audit["offered"] == ["effort", "propose", "remember", "try", "verify_ssh"]
    body = _posted(ctx)
    assert "recall:\n- who hatched me: mike" in body["messages"][-1]["content"]
    assert {t["name"] for t in body["tools"]} == {
        "remember",
        "effort",
        "try",
        "propose",
        "verify_ssh",
    }
    assert "ssh:mike" in state.principals and memory.entries[-1][1].principal == "ssh:mike"

    bad = json.dumps({"msg": "Who am I?", "sig": "bad"})
    api.outputs[render_command(hook, {"payload": bad})] = (1, "", "verify failed")
    before = len(memory.entries)
    out = await _turn(
        ctx,
        {"msg": "Who am I?", "sig": "bad"},
        "ingress",
        answers=[message_of(text_completion("I do not know you."))],
    )
    audit, _ = split_output(out)
    assert audit["principal"] == "anonymous"
    assert audit["offered"] == []  # §10.7: anonymous is offered no reserved tool
    body = _posted(ctx)
    # the current turn's composed input is the last message; the ones before it
    # are history from the first turn.
    assert body["messages"][-1]["content"].startswith(
        "[turn 2 | principal anonymous | door ingress]\n"
    )
    # compose_request omits an empty tool list, so an anonymous turn offers none
    assert "tools" not in body and "recall:\n\n" in body["messages"][-1]["content"]
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
    # parse_verb refuses this shape at propose time now (#123), so it is built
    # directly: hooks.py must still fail closed on a Verb that reaches it anyway.
    two = replace(
        hook,
        params={
            "type": "object",
            "properties": {"payload": {"type": "string"}, "extra": {}},
        },
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


async def test_anonymous_turn_polls_but_leaves_the_inbox_for_a_later_authenticated_turn(
    tmp_path: Path,
) -> None:
    """Ruling P2: the inbox is input for a principal who can act on it (§10.7
    denies anonymous `try`/`propose`). An anonymous turn still polls builds
    and trials so a failure lands in the inbox, but composes its input with
    an empty inbox and leaves `state.inbox` untouched for root's next turn."""
    api, memory = FakeMshkn(), ListMemory()
    ctx = _ctx(tmp_path, policy=OPEN, api=api, memory=memory)
    state = ctx.state
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

    out = await _turn(
        ctx,
        {"msg": "hi", "sig": "bad"},
        "ingress",
        answers=[message_of(text_completion("I do not know you."))],
    )
    audit, _ = split_output(out)
    assert audit["principal"] == "anonymous"
    assert "inbox:\n\n" in _posted(ctx, 0)["messages"][-1]["content"]
    assert state.catalog["page_title"].status == "failed"
    assert len(state.inbox) == 1 and "page_title failed to build" in state.inbox[0].text

    # Root's next turn drains it.
    await _turn(ctx, "go", answers=[message_of(text_completion("Noted."))])
    composed = _posted(ctx, 1)["messages"][-1]["content"]
    assert composed.startswith("[turn 2 | principal root | door api]\n")
    assert "inbox:\n- verb page_title failed to build" in composed
    assert state.inbox == []


async def test_authenticated_without_propose_rights_gets_remember_and_effort_only(
    tmp_path: Path,
) -> None:
    no_propose = {
        "principals": {"ssh:mike": {"invoke": [], "propose": False}},
        "hooks": ["verify_ssh"],
        "door": "open",
    }
    api = FakeMshkn()
    ctx = _ctx(tmp_path, policy=no_propose, api=api)
    hook = parse_verb(HOOK)
    info = await api.create_recipe(hook.dockerfile)
    await api.get_recipe(info.id)
    ctx.state.catalog["verify_ssh"] = CatalogEntry(
        verb=hook, status="ready", recipe_id=info.id, proposal_id="p-1"
    )
    good = json.dumps({"msg": "hi", "sig": "good"})
    api.outputs[render_command(hook, {"payload": good})] = (0, "mike\n", "")
    out = await _turn(
        ctx, {"msg": "hi", "sig": "good"}, "ingress", answers=[message_of(text_completion("ok"))]
    )
    audit, _ = split_output(out)
    assert audit["principal"] == "ssh:mike"
    # authenticated but may_propose is false: remember and effort, neither try nor propose
    assert audit["offered"] == ["effort", "remember"]
    assert {t["name"] for t in _posted(ctx)["tools"]} == {"remember", "effort"}


async def test_propose_tool_reports_a_declaration_error_as_invalid(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = await _turn(
        ctx,
        "go",
        answers=[
            message_of(tool_call_completion("propose", title="verb with no kind")),
            message_of(text_completion("noted")),
        ],
    )
    audit, _ = split_output(out)
    assert audit["tools"][0]["name"] == "propose" and audit["tools"][0]["status"] == "invalid"
    assert "kind" in audit["tools"][0]["error"]
    assert ctx.state.proposals == {} and audit["proposals"] == []


async def test_a_ready_verb_is_a_tool_and_builds_are_polled_first(tmp_path: Path) -> None:
    api = FakeMshkn()
    ctx = _ctx(tmp_path, api=api)
    p = propose(ctx.state, {"kind": "verb", "title": "t", "rationale": "r", "verb": VERB})
    await approve(api, ctx.state, p.id)
    verb = parse_verb(VERB)
    api.outputs[render_command(verb, {"url": "https://example.com"})] = (0, "Example Domain\n", "")
    out = await _turn(
        ctx,
        "page_title https://example.com",
        answers=[
            message_of(tool_call_completion("page_title", url="https://example.com")),
            message_of(text_completion("Example Domain")),
        ],
    )
    first = _posted(ctx, 0)
    assert "inbox:\n- verb page_title is ready" in first["messages"][-1]["content"]
    assert "page_title" in {t["name"] for t in first["tools"]}
    audit, reply = split_output(out)
    # comp-1 went to the fork's relay job: the fake numbers jobs and computers
    # from one counter, and the request is posted before the verb runs.
    assert audit["tools"][0] == {
        "name": "page_title",
        "status": "ok",
        "exit_code": 0,
        "computer_id": "comp-2",
    }
    assert reply.startswith("Example Domain")


async def test_the_live_policy_reaches_the_model_on_every_turn(tmp_path: Path) -> None:
    """§10.3 lets approval replace the policy, and §5 says a proposal is a whole
    document rather than a diff -- so an embryo asked to replace its policy must
    be able to read the one it is replacing. The self-description is already in
    the system prompt and the catalog is the tool list; the policy was the one
    mutable thing invisible to it. Three of five runs against the reduced seed
    stalled on exactly that (#123)."""
    api = FakeMshkn()
    ctx = _ctx(tmp_path, api=api, answers=[message_of(text_completion("hello"))])
    await say(ctx, payload_b64=b64("Hello."), door="api")
    sent = next(iter(api.relay_jobs.values()))["body"]["messages"][-1]["content"]
    assert '"door": "closed"' in sent and '"hooks": []' in sent
    assert '"anonymous": {"invoke": [], "propose": false}' in sent
    # It is the live document, not the prior: a policy applied on an earlier turn
    # is what the next turn sees.
    ctx.state.policy = parse_policy({**CLOSED, "hooks": ["verify_ssh"], "door": "open"})
    await settle(ctx)
    api.relay_answers.append(message_of(text_completion("again")))
    await say(ctx, payload_b64=b64("Again."), door="api")
    latest = api.relay_jobs[sorted(api.relay_jobs)[-1]]["body"]["messages"][-1]["content"]
    assert '"door": "open"' in latest and '"hooks": ["verify_ssh"]' in latest


async def test_a_failed_turn_gives_its_inbox_back(tmp_path: Path) -> None:
    """start_turn drains the inbox unconditionally, and a turn that ends in
    error never showed the model what it drained. Before #123 that lost a build
    log; after it, a refusal too, and the dedupe means nothing regenerates it.
    2026-09-10-postcut-run-3: a relay DNS failure swallowed the very refusal
    that turn 3 existed to deliver, and the next turn reported an empty inbox."""
    api = FakeMshkn(relay_refusal=MshknError(503, "No address associated with host"))
    ctx = _ctx(tmp_path, api=api)
    ctx.state.inbox.append(InboxItem(kind="refusal", text="proposal p-1 refused: because"))
    out = await say(ctx, payload_b64=b64("hello"), door="api")
    audit, _ = split_output(out)
    assert audit["stopped"] == "error" and ctx.state.pending is None
    assert [(i.kind, i.text) for i in ctx.state.inbox] == [
        ("refusal", "proposal p-1 refused: because")
    ]

    # A turn that succeeds consumes it, as before: restoring is the error path only.
    ctx2 = _ctx(tmp_path, answers=[message_of(text_completion("hi"))])
    ctx2.state.inbox.append(InboxItem(kind="build", text="verb x is ready"))
    await say(ctx2, payload_b64=b64("hello"), door="api")
    await settle(ctx2)
    assert ctx2.state.inbox == []


async def test_a_refused_relay_post_ends_the_turn_instead_of_crashing(tmp_path: Path) -> None:
    """A `POST /relay` the host refuses (a target outside the key's scope, the relay
    down) is a failure like any other: the audit line is written, the reply names it,
    and the pending turn is closed rather than left behind a traceback."""
    api = FakeMshkn(relay_refusal=MshknError(403, "Scope relay.targets does not allow it"))
    ctx = _ctx(tmp_path, api=api)
    out = await say(ctx, payload_b64=b64("Hello."), door="api")
    audit, reply = split_output(out)
    assert audit["stopped"] == "error" and audit["turn"] == 1 and audit["job"] == ""
    assert MODEL_FAILED in reply and "403" in reply
    assert ctx.state.pending is None
    assert ctx.state.window[-1].reply.startswith(MODEL_FAILED)


async def test_a_refusal_on_the_next_request_ends_the_turn_after_the_calls_ran(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path, answers=[message_of(tool_call_completion("remember", text="a fact"))])
    await say(ctx, payload_b64=b64("Remember a fact."), door="api")
    pending = ctx.state.pending
    assert pending is not None
    _fake(ctx).relay_refusal = MshknError(0, "ConnectError: the relay is unreachable")
    out = await resume(ctx, pending.job)
    audit, reply = split_output(out)
    assert audit["stopped"] == "error" and [t["name"] for t in audit["tools"]] == ["remember"]
    assert MODEL_FAILED in reply and "ConnectError" in reply
    assert ctx.state.pending is None


async def test_every_model_call_records_the_effort_it_was_made_at(tmp_path: Path) -> None:
    """#122: effort is per call now, so the audit says what each call actually spent.
    One entry per call is what makes the measure's cost tables readable."""
    ctx = _ctx(tmp_path, default_effort="medium")
    out = await _turn(
        ctx,
        "go",
        answers=[
            message_of(tool_call_completion("remember", text="a fact")),
            message_of(text_completion("noted")),
        ],
    )
    audit, _ = split_output(out)
    assert audit["effort"] == ["medium", "medium"]
    assert len(audit["effort"]) == audit["model_calls"] == 2


async def test_the_model_can_ask_for_more_effort_for_the_rest_of_the_turn(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, default_effort="medium")
    out = await _turn(
        ctx,
        "this is hard",
        answers=[
            message_of(tool_call_completion("effort", level="max")),
            message_of(text_completion("done")),
        ],
    )
    audit, _ = split_output(out)
    assert audit["tools"] == [{"name": "effort", "status": "set"}]
    assert _posted(ctx)["output_config"] == {"effort": "max"}
    assert audit["effort"] == ["medium", "max"]


async def test_a_request_for_more_effort_does_not_survive_the_turn(tmp_path: Path) -> None:
    """A turn is a life. What one turn asked for is not the next turn's floor."""
    ctx = _ctx(tmp_path, default_effort="medium")
    await _turn(
        ctx,
        "this is hard",
        answers=[
            message_of(tool_call_completion("effort", level="max")),
            message_of(text_completion("done")),
        ],
    )
    out = await _turn(ctx, "and this is easy", answers=[message_of(text_completion("ok"))])
    audit, _ = split_output(out)
    assert audit["effort"] == ["medium"]


async def test_the_model_cannot_ask_for_less_effort_than_the_run_was_given(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path, default_effort="high")
    out = await _turn(
        ctx,
        "go",
        answers=[
            message_of(tool_call_completion("effort", level="low")),
            message_of(text_completion("done")),
        ],
    )
    audit, _ = split_output(out)
    assert ctx.state.window[-1].audit["effort"] == ["high", "high"]
    assert _posted(ctx)["output_config"] == {"effort": "high"}
    assert audit["tools"] == [{"name": "effort", "status": "set"}]


async def test_an_unknown_effort_level_is_refused_with_the_ones_that_would_work(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path, default_effort="medium")
    out = await _turn(
        ctx,
        "go",
        answers=[
            message_of(tool_call_completion("effort", level="turbo")),
            message_of(text_completion("done")),
        ],
    )
    audit, _ = split_output(out)
    assert audit["tools"][0]["status"] == "invalid"
    assert "low, medium, high, xhigh, max" in audit["tools"][0]["error"]
    # a refusal changes nothing: the second call is still made at the run's default
    assert audit["effort"] == ["medium", "medium"]


async def test_an_irreversible_verb_in_the_tool_list_raises_the_turn_effort(
    tmp_path: Path,
) -> None:
    """The reversibility prior (#122). §10.8 keeps the embryo's own approvals to
    `local` and `read`, so this catalog entry is placed directly: the prior is built
    for the effects that exist once the invocation-time confirmation protocol does."""
    api = FakeMshkn()
    ctx = _ctx(tmp_path, default_effort="medium", api=api)
    sender = parse_verb({**VERB, "name": "send_mail", "effect": "communicate"})
    info = await api.create_recipe(sender.dockerfile)
    await api.get_recipe(info.id)
    ctx.state.catalog["send_mail"] = CatalogEntry(
        verb=sender, status="ready", recipe_id=info.id, proposal_id="p-1"
    )
    out = await _turn(ctx, "go", answers=[message_of(text_completion("ok"))])
    audit, _ = split_output(out)
    assert "send_mail" in audit["offered"]
    assert audit["effort"] == ["high"]
    assert _posted(ctx)["output_config"] == {"effort": "high"}


async def test_a_request_above_a_low_run_default_is_granted(tmp_path: Path) -> None:
    """The model's standing request accumulates against the other requests, not
    against the API's default: asking for more than a low floor must reach the wire."""
    ctx = _ctx(tmp_path, default_effort="low")
    out = await _turn(
        ctx,
        "go",
        answers=[
            message_of(tool_call_completion("effort", level="medium")),
            message_of(text_completion("done")),
        ],
    )
    audit, _ = split_output(out)
    assert audit["effort"] == ["low", "medium"]
    assert _posted(ctx)["output_config"] == {"effort": "medium"}


async def test_a_second_request_cannot_walk_the_effort_back_down(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, default_effort="low")
    out = await _turn(
        ctx,
        "go",
        answers=[
            message_of(tool_call_completion("effort", level="xhigh")),
            message_of(tool_call_completion("effort", level="medium")),
            message_of(text_completion("done")),
        ],
    )
    audit, _ = split_output(out)
    assert audit["effort"] == ["low", "xhigh", "xhigh"]
    # the second call is told what it actually got, not what it asked for
    assert ctx.state.window[-1].audit["tools"][1] == {"name": "effort", "status": "set"}
