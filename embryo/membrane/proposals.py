"""Proposals (spec §5): propose, approve, reject, disable, revert. Approval
executes the declaration exactly as written, with the brain's scoped key."""

from __future__ import annotations

from typing import TYPE_CHECKING

from membrane.declarations import parse_policy, parse_proposal
from membrane.invariants import refuse_approval
from membrane.mshkn import MshknError
from membrane.state import CatalogEntry, CatalogStatus, InboxItem
from membrane.verbs import submit_recipe

if TYPE_CHECKING:
    from membrane.declarations import Proposal
    from membrane.mshkn import MshknApi
    from membrane.state import Brain, State


def propose(state: State, doc: object) -> Proposal:
    proposal = parse_proposal(doc, id=f"p-{state.next_proposal}")
    state.new_proposal_id()
    state.proposals[proposal.id] = proposal
    return proposal


def _get(state: State, proposal_id: str) -> Proposal:
    if proposal_id not in state.proposals:
        raise KeyError(proposal_id)
    return state.proposals[proposal_id]


async def approve(api: MshknApi, state: State, brain: Brain, proposal_id: str) -> str:
    proposal = _get(state, proposal_id)
    if proposal.status != "pending":
        return f"{proposal.id} is {proposal.status}, not pending"
    reason = refuse_approval(proposal, state)
    if reason is not None:
        return f"{proposal.id} refused: {reason}"
    if proposal.kind == "verb":
        verb = proposal.verb
        assert verb is not None
        if verb.requires:
            missing = ", ".join(
                f"{r.kind} {r.name}" + (f" ({r.scope})" if r.scope else "") for r in verb.requires
            )
            proposal.status = "blocked"
            return f"{proposal.id} blocked: requires {missing}; the embryo has no vault (#91)"
        try:
            info = await submit_recipe(api, verb.dockerfile)
        except MshknError as exc:
            proposal.status = "failed"
            proposal.log = exc.detail
            state.catalog[verb.name] = CatalogEntry(
                verb=verb, status="failed", recipe_id=None, proposal_id=proposal.id
            )
            return f"{proposal.id} failed: {exc.detail}"
        proposal.recipe_id = info.id
        status: CatalogStatus = "ready" if info.status == "ready" else "building"
        proposal.status = status
        if proposal.supersedes in state.proposals:
            state.proposals[proposal.supersedes].status = "superseded"
        state.catalog[verb.name] = CatalogEntry(
            verb=verb, status=status, recipe_id=info.id, proposal_id=proposal.id
        )
        return f"{proposal.id} {status}: verb {verb.name} recipe {info.id}"
    if proposal.kind == "policy":
        assert proposal.policy is not None
        state.previous_policy = brain.policy().to_doc()
        brain.write_policy(proposal.policy)
        state.applied_policy = proposal.id
    else:
        assert proposal.prompt is not None
        state.previous_prompt = brain.self_description()
        brain.write_self(proposal.prompt)
        state.applied_prompt = proposal.id
    proposal.status = "applied"
    if proposal.supersedes in state.proposals:
        state.proposals[proposal.supersedes].status = "superseded"
    return f"{proposal.id} applied: {proposal.kind} replaced; effective from the next turn"


def reject(state: State, proposal_id: str, reason: str) -> str:
    proposal = _get(state, proposal_id)
    proposal.status = "rejected"
    state.inbox.append(
        InboxItem(
            kind="rejection",
            text=f"proposal {proposal.id} ({proposal.title}) was rejected: {reason}",
        )
    )
    return f"{proposal.id} rejected: {reason}"


def disable(state: State, verb_name: str) -> str:
    entry = state.catalog.get(verb_name)
    if entry is None:
        return f"no verb {verb_name}"
    entry.status = "disabled"
    if entry.proposal_id in state.proposals:
        state.proposals[entry.proposal_id].status = "disabled"
    return f"{verb_name} disabled"


def revert(state: State, brain: Brain, proposal_id: str) -> str:
    proposal = _get(state, proposal_id)
    if (
        proposal.kind == "policy"
        and state.applied_policy == proposal_id
        and state.previous_policy is not None
    ):
        brain.write_policy(parse_policy(state.previous_policy))
        state.applied_policy = None
        state.previous_policy = None
    elif (
        proposal.kind == "prompt"
        and state.applied_prompt == proposal_id
        and state.previous_prompt is not None
    ):
        brain.write_self(state.previous_prompt)
        state.applied_prompt = None
        state.previous_prompt = None
    else:
        return f"{proposal.id} is not applied; nothing to revert"
    proposal.status = "reverted"
    return f"{proposal.id} reverted: {proposal.kind} restored to the previous applied one"
