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
        # A refusal names what would have been valid (#123): the reserved set,
        # the legal state kinds, the reserved namespaces, the params that exist.
        ({"name": "try"}, "remember"),
        ({"state": "event"}, "ephemeral"),
        ({"asserts": "system"}, "root"),
        ({"entrypoint": "run {{nope}}"}, "url"),
        ({"requires": [{"kind": "secret"}]}, "name"),
        # requires that is not a list at all reaches the reworded message
        # (#123 final review) rather than the per-entry shape check above.
        ({"requires": "secret"}, "optional scope"),
    ],
)
def test_parse_verb_refuses(patch: dict[str, Any], reason: str) -> None:
    with pytest.raises(DeclarationError, match=reason):
        parse_verb({**VERB, **patch})


def test_a_verb_missing_fields_is_told_the_whole_shape_at_once() -> None:
    """One field per refusal is a serial walk that costs a round trip each
    (#123). The refusal names every required field, and what is missing."""
    with pytest.raises(DeclarationError) as exc:
        parse_verb({"name": "x"})
    message = str(exc.value)
    for field_name in ("description", "params", "dockerfile", "entrypoint", "effect", "state"):
        assert field_name in message, field_name
    assert "name" in message


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


def test_an_unknown_policy_field_is_told_the_fields_that_exist() -> None:
    """The seed no longer carries the policy schema (#123), so the refusal is
    the only thing that can teach it."""
    with pytest.raises(DeclarationError) as exc:
        parse_policy({"grants": {}})
    message = str(exc.value)
    assert "grants" in message
    for field_name in ("principals", "hooks", "door"):
        assert field_name in message, field_name


def test_a_bad_principal_is_told_the_form_a_principal_takes() -> None:
    with pytest.raises(DeclarationError) as exc:
        parse_policy({"principals": {"Mike": {"invoke": [], "propose": False}}})
    message = str(exc.value)
    assert "anonymous" in message and "<namespace>:<name>" in message


def test_a_policy_naming_root_is_refused() -> None:
    """Nothing refused this before, and may_invoke/may_propose short-circuit on
    root anyway (invariants.py), so an embryo that wrote a policy restricting
    root was silently wrong. The seed used to assert it; now the refusal does."""
    with pytest.raises(DeclarationError, match="root"):
        parse_policy({"principals": {"root": {"invoke": [], "propose": False}}})


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


def test_a_proposal_missing_fields_is_told_the_whole_shape_at_once() -> None:
    """The proposal mirror of test_a_verb_missing_fields_is_told_the_whole_shape_at_once
    above: one field per refusal is a serial walk that costs a round trip
    each (#123). The refusal names every required field, not just the first
    one missing."""
    with pytest.raises(DeclarationError) as exc:
        parse_proposal({}, id="p-9")
    message = str(exc.value)
    for field_name in ("kind", "title", "rationale"):
        assert field_name in message, field_name
