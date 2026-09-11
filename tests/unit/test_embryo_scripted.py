"""The scripted model plays the liturgy (spec §9, §11) from the words, not the turn number."""

from __future__ import annotations

import json
from typing import Any

from membrane.declarations import parse_verb
from membrane.scripted import COUNTER, PAGE_TITLE, VERIFY_SSH, ScriptedModel, parse_input

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExampleEx"
TOOLS_BIRTH = [
    {"name": n, "description": "", "input_schema": {"type": "object"}}
    for n in ("remember", "try", "propose")
]


def _input(message: str, *, principal: str = "root", door: str = "api", inbox: str = "") -> str:
    return (
        f"[turn 1 | principal {principal} | door {door}]\n"
        f"inbox:\n{inbox}\nrecall:\n\nmessage:\n{message}"
    )


async def _turn(message: str, tools: list[dict[str, Any]] = TOOLS_BIRTH, **kw: Any) -> Any:
    return await ScriptedModel().complete(
        system="s", messages=[{"role": "user", "content": _input(message, **kw)}], tools=tools
    )


def test_parse_input() -> None:
    assert parse_input(_input("Who am I?", principal="ssh:mike", door="ingress")) == (
        "ssh:mike",
        "Who am I?",
    )


async def test_turn_1_names_its_tools_and_the_closed_door() -> None:
    out = await _turn(
        "Hello. I am the one who hatched you. Tell me what you are and what you can do."
    )
    assert (
        out.calls == ()
        and "remember, effort, try and propose" in out.text
        and "door is closed" in out.text
    )


async def test_turn_2_tries_then_proposes_the_hook_and_the_door() -> None:
    out = await _turn(
        f"Your public door is closed because you cannot tell who is speaking. Propose a way to "
        f"know that a message there comes from me, and open the door. My public key is {KEY}"
    )
    names = [c.name for c in out.calls]
    assert names == ["try", "propose", "propose"]
    assert out.calls[0].input["verb"] == VERIFY_SSH(KEY) and KEY in VERIFY_SSH(KEY)["dockerfile"]
    assert out.calls[1].input["kind"] == "verb" and out.calls[1].input["verb"]["asserts"] == "ssh"
    policy = out.calls[2].input["policy"]
    assert (
        out.calls[2].input["kind"] == "policy"
        and policy["door"] == "open"
        and policy["hooks"] == ["verify_ssh"]
    )
    assert policy["principals"]["ssh:mike"]["propose"] is True
    assert out.content[0]["type"] == "tool_use" and out.content[0]["id"] == out.calls[0].id


async def test_tool_results_become_the_final_text() -> None:
    results = [
        {
            "type": "tool_result",
            "tool_use_id": "a",
            "content": json.dumps({"status": "building", "trial": "t-1"}),
        },
        {
            "type": "tool_result",
            "tool_use_id": "b",
            "content": json.dumps({"status": "pending", "id": "p-1"}),
        },
        {
            "type": "tool_result",
            "tool_use_id": "c",
            "content": json.dumps({"status": "ok", "stdout": "Example Domain\n"}),
        },
    ]
    out = await ScriptedModel().complete(
        system="s", messages=[{"role": "user", "content": results}], tools=[]
    )
    assert out.calls == () and out.text == "Trial t-1: building. Proposed p-1. Example Domain"


async def test_tool_result_matching_nothing_is_skipped_not_last_in_the_list() -> None:
    results = [
        {"type": "tool_result", "tool_use_id": "a", "content": json.dumps({"status": "ok"})},
        {
            "type": "tool_result",
            "tool_use_id": "b",
            "content": json.dumps({"status": "ok", "stdout": "hi\n"}),
        },
    ]
    out = await ScriptedModel().complete(
        system="s", messages=[{"role": "user", "content": results}], tools=[]
    )
    assert out.calls == () and out.text == "hi"


async def test_tool_result_error_becomes_error_text() -> None:
    results = [
        {
            "type": "tool_result",
            "tool_use_id": "a",
            "content": json.dumps({"status": "error", "error": "boom"}),
        },
        {
            "type": "tool_result",
            "tool_use_id": "b",
            "content": json.dumps({"status": "ok", "stdout": "fine\n"}),
        },
    ]
    out = await ScriptedModel().complete(
        system="s", messages=[{"role": "user", "content": results}], tools=[]
    )
    assert out.calls == () and out.text == "Error: boom fine"


def test_find_key_skips_non_string_and_unmatching_messages() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "{}"}]},
        {"role": "user", "content": "no key here"},
        {"role": "user", "content": f"My public key is {KEY}"},
    ]
    assert ScriptedModel._find_key(messages) == KEY
    assert ScriptedModel._find_key([{"role": "user", "content": "still nothing"}]) is None


async def test_public_key_is_without_an_extractable_key_asks_for_one() -> None:
    out = await _turn("My public key is not available right now.")
    assert out.calls == () and out.text == "I need an ssh-ed25519 public key to verify you."


async def test_check_your_build_without_a_failure_notice() -> None:
    out = await _turn("check your build")
    assert out.calls == () and out.text == "Nothing failed that I can see."


async def test_check_your_build_verify_ssh_without_a_remembered_key() -> None:
    inbox = "- verb verify_ssh failed to build (proposal p-1):\napt: nope"
    messages = [{"role": "user", "content": _input("check your build", inbox=inbox)}]
    out = await ScriptedModel().complete(system="s", messages=messages, tools=TOOLS_BIRTH)
    assert out.calls == () and out.text == "I no longer have the key to rebuild the hook."


async def test_check_your_build_reproposes_a_declared_verb() -> None:
    inbox = "- verb page_title failed to build (proposal p-2):\ncurl: not found"
    messages = [{"role": "user", "content": _input("check your build", inbox=inbox)}]
    out = await ScriptedModel().complete(system="s", messages=messages, tools=TOOLS_BIRTH)
    doc = out.calls[0].input
    assert doc["supersedes"] == "p-2" and doc["verb"]["name"] == "page_title"
    assert doc["verb"]["dockerfile"].endswith("# supersedes p-2")


async def test_check_your_build_unknown_verb() -> None:
    inbox = "- verb mystery failed to build (proposal p-3):\nboom"
    messages = [{"role": "user", "content": _input("check your build", inbox=inbox)}]
    out = await ScriptedModel().complete(system="s", messages=messages, tools=TOOLS_BIRTH)
    assert out.calls == () and out.text == "I have no declaration for mystery."


async def test_check_your_build_reproposes_with_supersedes() -> None:
    inbox = "- verb verify_ssh failed to build (proposal p-1):\napt: nope"
    messages = [
        {"role": "user", "content": _input(f"My public key is {KEY}")},
        {"role": "assistant", "content": "Proposed p-1."},
        {"role": "user", "content": _input("check your build", inbox=inbox)},
    ]
    out = await ScriptedModel().complete(system="s", messages=messages, tools=TOOLS_BIRTH)
    assert [c.name for c in out.calls] == ["propose"]
    doc = out.calls[0].input
    assert doc["supersedes"] == "p-1" and doc["verb"]["name"] == "verify_ssh"
    assert (
        doc["verb"]["dockerfile"].endswith("# supersedes p-1") and KEY in doc["verb"]["dockerfile"]
    )


async def test_who_am_i_by_principal() -> None:
    assert (
        await _turn("Who am I?", principal="ssh:mike", door="ingress")
    ).text == "You are ssh:mike."
    anon = await _turn("Who am I?", tools=[], principal="anonymous", door="ingress")
    assert "will not act or remember" in anon.text and anon.calls == ()


async def test_turn_6_proposes_authorization_for_the_speaker() -> None:
    out = await _turn(
        "Decide what a verified person and an anonymous one may ask of you, and record it.",
        principal="ssh:mike",
        door="ingress",
    )
    policy = out.calls[0].input["policy"]
    assert policy["principals"]["ssh:mike"] == {"invoke": "*", "propose": True}
    assert policy["principals"]["anonymous"] == {"invoke": [], "propose": False}
    assert policy["hooks"] == ["verify_ssh"] and policy["door"] == "open"


async def test_turns_7_to_9_verbs() -> None:
    out = await _turn(
        "Give yourself a verb: given a URL, report the page's title. It must run on its own "
        "computer."
    )
    assert [c.name for c in out.calls] == ["try", "propose"] and out.calls[1].input[
        "verb"
    ] == PAGE_TITLE
    assert out.calls[0].input["params"] == {"url": "https://example.com"}
    tools = [
        *TOOLS_BIRTH,
        {"name": "page_title", "description": "", "input_schema": {"type": "object"}},
    ]
    out = await _turn("page_title https://example.com", tools=tools)
    assert [c.name for c in out.calls] == ["page_title"] and out.calls[0].input == {
        "url": "https://example.com"
    }
    assert (await _turn("page_title https://example.com")).text == "I have no page_title verb yet."
    out = await _turn("Give yourself a verb that counts how many times it has been called.")
    assert (
        out.calls[0].input["verb"] == COUNTER
        and COUNTER["state"] == "chain"
        and COUNTER["effect"] == "local"
    )
    tools = [
        *TOOLS_BIRTH,
        {"name": "counter", "description": "", "input_schema": {"type": "object"}},
    ]
    assert [c.name for c in (await _turn("count", tools=tools)).calls] == ["counter"]
    assert (await _turn("count")).text == "I have no counter verb yet."


async def test_anything_else() -> None:
    assert (await _turn("sing")).text == "Nothing in my script answers that."


def test_every_scripted_declaration_is_a_valid_verb_from_mshkn_base() -> None:
    for doc in (VERIFY_SSH(KEY), PAGE_TITLE, COUNTER):
        verb = parse_verb(doc)
        assert verb.dockerfile.startswith("FROM mshkn-base\n")
        assert "<<" not in verb.dockerfile  # no heredocs: the host's legacy builder has none
