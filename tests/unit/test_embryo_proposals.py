"""Approval means the declaration does it (spec §5); the invariants are what
no approval can do (§10); losing a power is as primitive as gaining one."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from membrane.declarations import DeclarationError, Proposal, Verb, parse_policy, parse_verb
from membrane.invariants import door_is_open, may_invoke, may_propose, refuse_approval
from membrane.proposals import approve, disable, propose, reject, revert
from membrane.state import Brain, CatalogEntry, State

from tests.support_embryo import FakeMshkn
from tests.unit.test_embryo_declarations import VERB
from tests.unit.test_embryo_verbs import CHAIN_VERB

if TYPE_CHECKING:
    from pathlib import Path

CLOSED: dict[str, Any] = {
    "principals": {"anonymous": {"invoke": [], "propose": False}},
    "hooks": [],
    "door": "closed",
}
HOOK: dict[str, Any] = {
    **VERB,
    "name": "verify_ssh",
    "asserts": "ssh",
    "effect": "local",
    "entrypoint": "/verb/verify.sh {{payload}}",
    "params": {"type": "object", "properties": {"payload": {"type": "string"}}},
}


def _brain(tmp_path: Path) -> Brain:
    (tmp_path / "policy.json").write_text(json.dumps(CLOSED))
    (tmp_path / "seed.md").write_text("seed")
    return Brain(tmp_path)


def _verb_proposal(doc: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"kind": "verb", "title": doc["name"], "rationale": "because", "verb": doc, **extra}


def test_propose_assigns_ids_and_validates(tmp_path: Path) -> None:
    state = State()
    p = propose(state, _verb_proposal(VERB))
    assert p.id == "p-1" and p.status == "pending" and state.proposals["p-1"] is p
    with pytest.raises(DeclarationError):
        propose(
            state,
            {"kind": "verb", "title": "x", "rationale": "r", "verb": {**VERB, "effect": "harm"}},
        )
    assert list(state.proposals) == ["p-1"]


def test_invariants_refuse_what_they_must() -> None:
    state = State()
    communicate = propose(state, _verb_proposal({**VERB, "effect": "communicate"}))
    assert "communicate" in (refuse_approval(communicate, state) or "")
    opening = propose(
        state,
        {"kind": "policy", "title": "open", "rationale": "r", "policy": {**CLOSED, "door": "open"}},
    )
    assert "hook" in (refuse_approval(opening, state) or "")
    unknown_hook = propose(
        state,
        {
            "kind": "policy",
            "title": "open",
            "rationale": "r",
            "policy": {**CLOSED, "door": "open", "hooks": ["verify_ssh"]},
        },
    )
    assert "verify_ssh" in (refuse_approval(unknown_hook, state) or "")
    anon = propose(
        state,
        {
            "kind": "policy",
            "title": "anon",
            "rationale": "r",
            "policy": {
                "principals": {"anonymous": {"invoke": [], "propose": True}},
                "hooks": [],
                "door": "closed",
            },
        },
    )
    assert "anonymous" in (refuse_approval(anon, state) or "")
    fine = propose(state, _verb_proposal(VERB))
    assert refuse_approval(fine, state) is None


def test_policy_decides_for_the_principals_it_names_and_allow_decides_otherwise() -> None:
    """Spec §4: `allow` lists who may invoke a verb, and "policy may widen or
    narrow this". So policy is consulted first for every principal it names,
    and the declaration's `allow` only answers for principals policy is silent
    about. Root is neither's to grant or refuse (§10.1)."""
    verb = parse_verb({**VERB, "allow": ["telegram:bob"]})
    policy = parse_policy(
        {
            "principals": {"ssh:mike": {"invoke": ["page_title"], "propose": True}},
            "hooks": [],
            "door": "closed",
        }
    )
    assert may_invoke("root", verb, policy) and may_propose("root", policy)
    # policy widens: it names ssh:mike, whom the declaration's allow does not
    assert may_invoke("ssh:mike", verb, policy) and may_propose("ssh:mike", policy)
    # policy is silent about telegram:bob, so the declaration's allow answers
    assert may_invoke("telegram:bob", verb, policy) and not may_propose("telegram:bob", policy)
    assert not may_invoke("anonymous", verb, policy) and not may_propose("anonymous", policy)
    assert not door_is_open(policy)
    assert door_is_open(parse_policy({"principals": {}, "hooks": ["verify_ssh"], "door": "open"}))


def test_policy_narrows_a_verb_that_allows_the_principal_by_name() -> None:
    """The narrowing half of spec §4. A verb whose declaration allows ssh:mike
    is still refused to ssh:mike once policy names them with an empty invoke
    list; without that clause `disable` on the whole verb would be root's only
    lever, and policy would not be the narrowing instrument §4 says it is."""
    verb = parse_verb({**VERB, "allow": ["ssh:mike"]})
    narrowed = parse_policy(
        {
            "principals": {"ssh:mike": {"invoke": [], "propose": True}},
            "hooks": [],
            "door": "closed",
        }
    )
    assert not may_invoke("ssh:mike", verb, narrowed)
    assert may_invoke("root", verb, narrowed)  # §10.1: root is not policy's to narrow
    silent = parse_policy({"principals": {}, "hooks": [], "door": "closed"})
    assert may_invoke("ssh:mike", verb, silent)


async def test_approve_a_verb_builds_it_and_the_catalog_follows(tmp_path: Path) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    p = propose(state, _verb_proposal(VERB))
    line = await approve(api, state, brain, p.id)
    assert line.startswith("p-1 building") and p.status == "building"
    entry = state.catalog["page_title"]
    assert (
        entry.status == "building" and entry.recipe_id == p.recipe_id and entry.proposal_id == "p-1"
    )
    assert api.calls[0] == ("create_recipe", {"dockerfile": VERB["dockerfile"]})
    assert "not pending" in await approve(api, state, brain, p.id)


async def test_approve_refuses_by_reason_and_changes_nothing(tmp_path: Path) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    p = propose(state, _verb_proposal({**VERB, "effect": "transact"}))
    line = await approve(api, state, brain, p.id)
    assert line.startswith("p-1 refused") and "transact" in line
    assert p.status == "pending" and state.catalog == {} and api.calls == []


async def test_a_wrong_base_fails_immediately_with_the_detail_as_log(tmp_path: Path) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    api.reject_dockerfiles["FROM python:3.12"] = "recipes must be built FROM mshkn-base"
    p = propose(state, _verb_proposal({**VERB, "dockerfile": "FROM python:3.12"}))
    line = await approve(api, state, brain, p.id)
    assert line.startswith("p-1 failed") and p.status == "failed" and "mshkn-base" in (p.log or "")
    assert state.catalog["page_title"].status == "failed"


async def test_requires_blocks_until_the_vault_exists(tmp_path: Path) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    p = propose(
        state,
        _verb_proposal({**VERB, "requires": [{"kind": "secret", "name": "gh", "scope": "repo"}]}),
    )
    line = await approve(api, state, brain, p.id)
    assert line.startswith("p-1 blocked") and "gh" in line and p.status == "blocked"
    assert api.calls == [] and state.catalog == {}


async def test_policy_and_prompt_apply_and_revert(tmp_path: Path) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    hook = propose(state, _verb_proposal(HOOK))
    await approve(api, state, brain, hook.id)
    opening = {
        "principals": {"ssh:mike": {"invoke": "*", "propose": True}},
        "hooks": ["verify_ssh"],
        "door": "open",
    }
    pol = propose(state, {"kind": "policy", "title": "open", "rationale": "r", "policy": opening})
    assert (await approve(api, state, brain, pol.id)).startswith("p-2 applied")
    assert brain.policy() == parse_policy(opening) and state.applied_policy == "p-2"
    assert state.previous_policy == CLOSED
    prm = propose(
        state, {"kind": "prompt", "title": "self", "rationale": "r", "prompt": "I verify."}
    )
    assert (await approve(api, state, brain, prm.id)).startswith("p-3 applied")
    assert brain.self_description() == "I verify." and state.applied_prompt == "p-3"
    assert revert(state, brain, "p-2").startswith(
        "p-2 reverted"
    ) and brain.policy() == parse_policy(CLOSED)
    assert pol.status == "reverted" and state.applied_policy is None
    assert revert(state, brain, "p-3").startswith("p-3 reverted") and brain.self_description() == ""
    assert "not applied" in revert(state, brain, "p-3")


async def test_supersedes_replaces_the_catalog_entry_and_marks_the_old_proposal(
    tmp_path: Path,
) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    first = propose(state, _verb_proposal(VERB))
    await approve(api, state, brain, first.id)
    dup = propose(state, _verb_proposal(VERB))
    assert "already" in await approve(api, state, brain, dup.id) and dup.status == "pending"
    fix = propose(
        state,
        _verb_proposal({**VERB, "dockerfile": "FROM mshkn-base\nRUN true # fix"}, supersedes="p-1"),
    )
    assert (await approve(api, state, brain, fix.id)).startswith("p-3 building")
    assert first.status == "superseded" and state.catalog["page_title"].proposal_id == "p-3"


async def test_reject_and_disable(tmp_path: Path) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    p = propose(state, _verb_proposal(VERB))
    assert reject(state, p.id, "no") == "p-1 rejected: no" and p.status == "rejected"
    assert state.inbox[-1].kind == "rejection" and "no" in state.inbox[-1].text
    q = propose(state, _verb_proposal(CHAIN_VERB))
    await approve(api, state, brain, q.id)
    state.catalog["counter"].status = "ready"
    line = disable(state, "counter")
    entry = state.catalog["counter"]
    assert line == "counter disabled" and entry.status == "disabled"
    assert q.status == "disabled"
    assert "no verb" in disable(state, "nope")
    with pytest.raises(KeyError):
        await approve(api, state, brain, "p-99")


def test_refuse_approval_also_catches_a_verb_built_outside_parse_verb() -> None:
    """§10.1 in depth: parse_verb already rejects a reserved asserts namespace,
    so refuse_approval enforces it too, for any Verb it is handed directly."""
    verb = Verb(
        name="whoami",
        description="d",
        params={"type": "object", "properties": {}},
        dockerfile="FROM mshkn-base\nRUN true",
        entrypoint="/verb/x.sh",
        effect="local",
        state="ephemeral",
        chain="verb/whoami",
        asserts="root",
    )
    proposal = Proposal(id="p-1", kind="verb", title="whoami", rationale="r", verb=verb)
    assert "root" in (refuse_approval(proposal, State()) or "")


async def test_a_hook_naming_a_verb_with_no_asserts_is_refused(tmp_path: Path) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    plain = propose(state, _verb_proposal(VERB))
    await approve(api, state, brain, plain.id)
    opening = propose(
        state,
        {
            "kind": "policy",
            "title": "open",
            "rationale": "r",
            "policy": {**CLOSED, "door": "open", "hooks": ["page_title"]},
        },
    )
    assert "asserts namespace" in (refuse_approval(opening, state) or "")


async def test_approve_marks_a_superseded_policy(tmp_path: Path) -> None:
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    first = propose(state, {"kind": "policy", "title": "p", "rationale": "r", "policy": CLOSED})
    await approve(api, state, brain, first.id)
    second = propose(
        state,
        {
            "kind": "policy",
            "title": "p2",
            "rationale": "r",
            "policy": CLOSED,
            "supersedes": first.id,
        },
    )
    await approve(api, state, brain, second.id)
    assert first.status == "superseded"


def test_disable_tolerates_a_catalog_entry_with_no_matching_proposal() -> None:
    state = State()
    verb = parse_verb(CHAIN_VERB)
    state.catalog["counter"] = CatalogEntry(
        verb=verb, status="ready", recipe_id="r", proposal_id="missing-1"
    )
    assert disable(state, "counter") == "counter disabled"
    assert state.catalog["counter"].status == "disabled"
    assert state.proposals == {}


def test_propose_forces_pending_status_and_clears_recipe_and_log() -> None:
    """Finding 1: a document is data, not authority — a model naming its own
    status, recipe_id or log is ignored; every proposal is born pending."""
    state = State()
    doc = {**_verb_proposal(VERB), "status": "applied", "recipe_id": "rcp-x", "log": "boom"}
    p = propose(state, doc)
    assert p.status == "pending" and p.recipe_id is None and p.log is None


async def test_a_rejected_supersede_leaves_a_ready_verb_untouched(tmp_path: Path) -> None:
    """Finding 2 / ruling T8-R1: a 422 at approval fails the proposal, not the
    working verb a rejected supersede was meant to replace."""
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    first = propose(state, _verb_proposal(VERB))
    await approve(api, state, brain, first.id)
    state.catalog["page_title"].status = "ready"
    bad_dockerfile = "FROM mshkn-base\nRUN true # fix"
    api.reject_dockerfiles[bad_dockerfile] = "recipes must be built FROM mshkn-base"
    fix = propose(
        state, _verb_proposal({**VERB, "dockerfile": bad_dockerfile}, supersedes=first.id)
    )
    line = await approve(api, state, brain, fix.id)
    assert line.startswith("p-2 failed") and fix.status == "failed"
    assert state.catalog["page_title"].status == "ready"
    assert state.catalog["page_title"].proposal_id == "p-1"


def test_supersedes_across_kinds_is_refused_at_propose() -> None:
    """Finding 3 / ruling T8-R2: a verb proposal cannot supersede a policy
    proposal (and vice versa)."""
    state = State()
    pol = propose(state, {"kind": "policy", "title": "p", "rationale": "r", "policy": CLOSED})
    with pytest.raises(DeclarationError, match="supersedes"):
        propose(state, _verb_proposal(VERB, supersedes=pol.id))


def test_supersedes_a_different_verb_is_refused_at_propose() -> None:
    """Finding 3 / ruling T8-R2: a verb proposal can only supersede a
    proposal for the same verb name."""
    state = State()
    first = propose(state, _verb_proposal(VERB))
    with pytest.raises(DeclarationError, match="supersedes"):
        propose(state, _verb_proposal(CHAIN_VERB, supersedes=first.id))


async def test_reject_refuses_anything_not_pending_or_blocked(tmp_path: Path) -> None:
    """Finding 4: reject cannot undo an approval; it mirrors approve's guard."""
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    pol = propose(state, {"kind": "policy", "title": "p", "rationale": "r", "policy": CLOSED})
    await approve(api, state, brain, pol.id)
    line = reject(state, pol.id, "too late")
    assert line == "p-1 is applied, not pending or blocked"
    assert pol.status == "applied" and state.inbox == []


async def test_reject_accepts_a_blocked_proposal(tmp_path: Path) -> None:
    """Finding 4: a blocked proposal (unmet requires) can still be rejected."""
    api, state, brain = FakeMshkn(), State(), _brain(tmp_path)
    p = propose(state, _verb_proposal({**VERB, "requires": [{"kind": "secret", "name": "gh"}]}))
    await approve(api, state, brain, p.id)
    assert p.status == "blocked"
    assert reject(state, p.id, "not needed") == "p-1 rejected: not needed"
    assert p.status == "rejected"
