"""A verb is one JSON document (spec §4); policy is data, not code (§10); a
proposal is a whole document, not a diff (§5)."""

from __future__ import annotations

import shlex
from typing import Any

import pytest
from membrane.declarations import (
    DeclarationError,
    Policy,
    parse_policy,
    parse_proposal,
    parse_verb,
    render_command,
)

VERB: dict[str, Any] = {
    "name": "page_title",
    "description": "The <title> of a page",
    "params": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    "dockerfile": "FROM mshkn-base\nRUN true",
    "entrypoint": "/verb/title.sh {{url}}",
    "effect": "read",
    "state": "ephemeral",
}


def test_parse_verb_fills_defaults() -> None:
    verb = parse_verb(VERB)
    assert verb.chain == "verb/page_title"
    assert verb.needs == {"ram": "256MB", "cores": 1}
    assert verb.timeout_seconds == 60
    assert verb.allow == () and verb.requires == () and verb.asserts is None
    assert verb.tool() == {
        "name": "page_title",
        "description": "The <title> of a page",
        "input_schema": VERB["params"],
    }
    assert parse_verb(verb.to_doc()) == verb


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"name": "Page"}, "name"),
        ({"name": "try"}, "reserved"),
        ({"effect": "harm"}, "effect"),
        ({"state": "event"}, "not in the embryo"),
        ({"params": {"type": "string"}}, "params"),
        ({"entrypoint": "run {{nope}}"}, "nope"),
        ({"timeout_seconds": 500}, "timeout_seconds"),
        ({"asserts": "root"}, "reserved"),
        ({"requires": [{"kind": "secret"}]}, "requires"),
        ({"chain": "other/x"}, "verb/"),
        ({"description": ""}, "description"),
        # §4: allow lists namespaced principals. root is never a declaration's
        # to grant (§10.1) and what anonymous may do is policy's to say (§6).
        ({"allow": ["root"]}, "allow"),
        ({"allow": ["anonymous"]}, "allow"),
        ({"allow": ["mike"]}, "allow"),
    ],
)
def test_parse_verb_refuses(patch: dict[str, Any], reason: str) -> None:
    with pytest.raises(DeclarationError, match=reason):
        parse_verb({**VERB, **patch})


def test_render_command_quotes_every_value_and_bounds_the_run() -> None:
    verb = parse_verb({**VERB, "timeout_seconds": 30})
    cmd = render_command(verb, {"url": "https://x/?a=1&b='2'"})
    inner = "/verb/title.sh " + shlex.quote("https://x/?a=1&b='2'")
    assert cmd == f"timeout --preserve-status -s KILL 30 bash -c {shlex.quote(inner)}"


def test_render_command_serialises_objects_and_rejects_missing_params() -> None:
    verb = parse_verb(
        {
            **VERB,
            "entrypoint": "/verb/x {{url}} {{opts}}",
            "params": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "opts": {"type": "object"}},
            },
        }
    )
    cmd = render_command(verb, {"url": "u", "opts": {"a": [1, 2]}})
    assert '{"a": [1, 2]}' in cmd  # the quoting wraps it; the characters survive
    with pytest.raises(DeclarationError, match="opts"):
        render_command(verb, {"url": "u"})


def test_parse_policy_and_grants() -> None:
    policy = parse_policy(
        {
            "principals": {
                "ssh:mike": {"invoke": "*", "propose": True},
                "anonymous": {"invoke": [], "propose": False},
            },
            "hooks": ["verify_ssh"],
            "door": "open",
        }
    )
    assert policy.grant("ssh:mike").invoke == "*" and policy.grant("ssh:mike").propose
    assert policy.grant("anonymous").invoke == () and not policy.grant("anonymous").propose
    assert policy.grant("telegram:bob").invoke == () and not policy.grant("telegram:bob").propose
    assert policy.hooks == ("verify_ssh",) and policy.door == "open"
    assert parse_policy(policy.to_doc()) == policy


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        ({"principals": {}, "hooks": [], "door": "ajar"}, "door"),
        (
            {
                "principals": {"ssh:x": {"invoke": "*", "propose": "yes"}},
                "hooks": [],
                "door": "closed",
            },
            "propose",
        ),
        (
            {
                "principals": {"bad name": {"invoke": "*", "propose": False}},
                "hooks": [],
                "door": "closed",
            },
            "principal",
        ),
        ({"principals": {}, "hooks": ["Not-A-Verb"], "door": "closed"}, "hooks"),
        ({"principals": {}, "hooks": [], "door": "closed", "code": "x"}, "unknown"),
        ("def transform(): pass", "object"),
    ],
)
def test_parse_policy_refuses(doc: object, reason: str) -> None:
    with pytest.raises(DeclarationError, match=reason):
        parse_policy(doc)


def test_parse_proposal_validates_its_kind() -> None:
    p = parse_proposal({"kind": "verb", "title": "t", "rationale": "r", "verb": VERB}, id="p-1")
    assert (
        p.id == "p-1"
        and p.status == "pending"
        and p.verb is not None
        and p.verb.name == "page_title"
    )
    q = parse_proposal(
        {"kind": "prompt", "title": "t", "rationale": "r", "prompt": "I am"}, id="p-2"
    )
    assert q.prompt == "I am"
    r = parse_proposal(
        {
            "kind": "policy",
            "title": "t",
            "rationale": "r",
            "supersedes": "p-1",
            "policy": Policy(principals={}, hooks=(), door="closed").to_doc(),
        },
        id="p-3",
    )
    assert r.policy is not None and r.supersedes == "p-1"
    assert parse_proposal(p.to_doc(), id="p-1") == p


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        ({"kind": "verb", "title": "t", "rationale": "r"}, "verb"),
        ({"kind": "policy", "title": "t", "rationale": "r", "verb": VERB}, "policy"),
        ({"kind": "diff", "title": "t", "rationale": "r"}, "kind"),
        ({"kind": "prompt", "title": "", "rationale": "r", "prompt": "x"}, "title"),
        ({"kind": "prompt", "title": "t", "rationale": "r", "prompt": ""}, "prompt"),
    ],
)
def test_parse_proposal_refuses(doc: dict[str, Any], reason: str) -> None:
    with pytest.raises(DeclarationError, match=reason):
        parse_proposal(doc, id="p-9")
