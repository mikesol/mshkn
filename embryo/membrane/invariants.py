"""What the membrane is (spec §10): the things no policy, proposal or approval
can change. Every command runs these; the policy language cannot express
their negation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from membrane.declarations import EMBRYO_EFFECTS, RESERVED_NAMESPACES
from membrane.principals import ANONYMOUS, ROOT

if TYPE_CHECKING:
    from membrane.declarations import Policy, Proposal, Verb
    from membrane.state import State

# §10.3: only these fields of State change on approval; all three live in
# state.json. The membrane, the seed, the invariants and the scoped key are not
# proposal kinds, so nothing can name them.
MUTABLE = ("self_description", "policy", "catalog")


def door_is_open(policy: Policy) -> bool:
    """§10.6: closed while policy declares no pre-turn hook, whatever door says."""
    return policy.door == "open" and bool(policy.hooks)


def may_invoke(principal: str, verb: Verb, policy: Policy) -> bool:
    """§4: a verb's `allow` says who may invoke it, and "policy may widen or
    narrow this". So policy answers for every principal it names — widening
    past `allow`, or narrowing to nothing — and `allow` answers only where
    policy is silent. Root is neither's to refuse (§10.1)."""
    if principal == ROOT:
        return True
    if principal in policy.principals:
        return policy.grant(principal).allows(verb.name)
    return principal in verb.allow


def may_propose(principal: str, policy: Policy) -> bool:
    if principal == ROOT:
        return True
    if principal == ANONYMOUS:
        return False  # §10.7
    return policy.grant(principal).propose


def refuse_approval(proposal: Proposal, state: State) -> str | None:
    if proposal.kind == "verb":
        verb = proposal.verb
        assert verb is not None
        if verb.effect not in EMBRYO_EFFECTS:
            return (
                f"effect {verb.effect} is not approved by the embryo (only local and read, §10.8)"
            )
        if verb.asserts in RESERVED_NAMESPACES:
            return (
                f"a hook may not assert {verb.asserts}; "
                f"the reserved namespaces are {sorted(RESERVED_NAMESPACES)} (§10.1)"
            )
        existing = state.catalog.get(verb.name)
        if (
            existing is not None
            and existing.status != "disabled"
            and proposal.supersedes != existing.proposal_id
        ):
            return (
                f"verb {verb.name} already exists as {existing.proposal_id}; "
                "propose with supersedes"
            )
        if verb.name in state.policy.hooks:
            properties = verb.params.get("properties", {})
            if len(properties) != 1 or verb.asserts is None:
                return (
                    f"verb {verb.name} is the policy's live hook; a hook must keep exactly "
                    "one parameter, which receives the decoded payload, and a non-null "
                    f"asserts, but this declaration has {sorted(properties)} as parameters "
                    f"and asserts {verb.asserts!r}"
                )
        return None
    if proposal.kind == "policy":
        new = proposal.policy
        assert new is not None
        if new.door == "open" and not new.hooks:
            return "the public door cannot open with no pre-turn hook (§10.6)"
        for hook in new.hooks:
            entry = state.catalog.get(hook)
            if entry is None:
                return (
                    f"hook {hook} is not a verb in the catalog; "
                    f"the catalog has {sorted(state.catalog)}"
                )
            if entry.verb.asserts is None:
                return f"hook {hook} declares no asserts namespace"
            properties = entry.verb.params.get("properties", {})
            if len(properties) != 1:
                return (
                    f"hook {hook} declares {sorted(properties)} as parameters; "
                    "a hook takes exactly one parameter, which receives the decoded payload"
                )
        if new.grant(ANONYMOUS).propose:
            return "anonymous may never propose (§10.7)"
        return None
    return None
