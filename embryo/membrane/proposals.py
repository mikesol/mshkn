"""Proposals (spec §5): propose, approve, reject, disable, revert. Approval
executes the declaration exactly as written, with the brain's scoped key."""

from __future__ import annotations

from typing import TYPE_CHECKING

from membrane.declarations import DeclarationError, parse_proposal
from membrane.invariants import MUTABLE, refuse_approval
from membrane.mshkn import MshknError
from membrane.state import CatalogEntry, CatalogStatus, InboxItem
from membrane.verbs import submit_recipe

if TYPE_CHECKING:
    from membrane.declarations import Proposal
    from membrane.mshkn import MshknApi
    from membrane.state import State


# §10.3: the one mutable thing each proposal kind writes, named as the State
# field it is. The invariant is structural — these are the only three kinds
# `parse_proposal` accepts, so nothing can name the membrane, the seed, the
# invariants or the scoped key — and this is the map `approve` checks itself
# against MUTABLE with. Every write lands in state.json, committed by the one
# atomic save at the end of the command (#100); nothing is written here.
WRITES: dict[str, str] = {"verb": "catalog", "policy": "policy", "prompt": "self_description"}


def _supersede_target(state: State, proposal: Proposal) -> Proposal | None:
    """The proposal `proposal.supersedes` names, only when it is a genuine
    match: same kind, and for a verb, the same verb name (finding 3 / T8-R2).
    `None` for no supersedes, an unknown id, a kind mismatch or a verb-name
    mismatch alike — propose() refuses the last three; approve() uses this to
    decide whether to mark the target superseded."""
    if proposal.supersedes is None:
        return None
    target = state.proposals.get(proposal.supersedes)
    if target is None or target.kind != proposal.kind:
        return None
    if proposal.kind == "verb" and (
        proposal.verb is None or target.verb is None or target.verb.name != proposal.verb.name
    ):
        return None
    return target


def propose(state: State, doc: object) -> Proposal:
    proposal = parse_proposal(doc, id=f"p-{state.next_proposal}")
    # A document is data, not authority: whatever status/recipe_id/log a model
    # names is discarded. Every proposal is born pending (finding 1).
    proposal.status = "pending"
    proposal.recipe_id = None
    proposal.log = None
    if proposal.supersedes is not None and _supersede_target(state, proposal) is None:
        for_verb = (
            f" for verb {proposal.verb.name!r}" if proposal.kind == "verb" and proposal.verb else ""
        )
        raise DeclarationError(
            f"proposal.supersedes {proposal.supersedes!r} does not name an existing "
            f"{proposal.kind} proposal{for_verb}"
        )
    state.new_proposal_id()
    state.proposals[proposal.id] = proposal
    return proposal


def _get(state: State, proposal_id: str) -> Proposal:
    if proposal_id not in state.proposals:
        raise KeyError(proposal_id)
    return state.proposals[proposal_id]


async def approve(api: MshknApi, state: State, proposal_id: str) -> str:
    proposal = _get(state, proposal_id)
    if proposal.status != "pending":
        return f"{proposal.id} is {proposal.status}, not pending"
    reason = refuse_approval(proposal, state)
    if reason is not None:
        return f"{proposal.id} refused: {reason}"
    # Everything below writes exactly one of MUTABLE and nothing else (§10.3).
    assert WRITES[proposal.kind] in MUTABLE
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
            # §5 step 3: a rejected Dockerfile fails the proposal, not the
            # verb it was meant to (re)build. A working, non-disabled entry
            # for this name — e.g. the one a rejected supersede targeted —
            # is left exactly as it was (finding 2 / T8-R1).
            existing = state.catalog.get(verb.name)
            if existing is None or existing.status == "disabled":
                state.catalog[verb.name] = CatalogEntry(
                    verb=verb, status="failed", recipe_id=None, proposal_id=proposal.id
                )
            return f"{proposal.id} failed: {exc.detail}"
        proposal.recipe_id = info.id
        status: CatalogStatus = "ready" if info.status == "ready" else "building"
        proposal.status = status
        target = _supersede_target(state, proposal)
        if target is not None:
            target.status = "superseded"
        state.catalog[verb.name] = CatalogEntry(
            verb=verb, status=status, recipe_id=info.id, proposal_id=proposal.id
        )
        return f"{proposal.id} {status}: verb {verb.name} recipe {info.id}"
    if proposal.kind == "policy":
        assert proposal.policy is not None
        state.previous_policy = state.policy
        state.policy = proposal.policy
        state.applied_policy = proposal.id
    else:
        assert proposal.prompt is not None
        state.previous_prompt = state.self_description
        state.self_description = proposal.prompt
        state.applied_prompt = proposal.id
    proposal.status = "applied"
    target = _supersede_target(state, proposal)
    if target is not None:
        target.status = "superseded"
    return f"{proposal.id} applied: {proposal.kind} replaced; effective from the next turn"


def reject(state: State, proposal_id: str, reason: str) -> str:
    proposal = _get(state, proposal_id)
    if proposal.status not in ("pending", "blocked"):
        return f"{proposal.id} is {proposal.status}, not pending or blocked"
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


def revert(state: State, proposal_id: str) -> str:
    proposal = _get(state, proposal_id)
    if (
        proposal.kind == "policy"
        and state.applied_policy == proposal_id
        and state.previous_policy is not None
    ):
        state.policy = state.previous_policy
        state.applied_policy = None
        state.previous_policy = None
    elif (
        proposal.kind == "prompt"
        and state.applied_prompt == proposal_id
        and state.previous_prompt is not None
    ):
        state.self_description = state.previous_prompt
        state.applied_prompt = None
        state.previous_prompt = None
    else:
        return f"{proposal.id} is not applied; nothing to revert"
    proposal.status = "reverted"
    return f"{proposal.id} reverted: {proposal.kind} restored to the previous applied one"
