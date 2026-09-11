"""The verb model (spec §4), the policy document (§10) and proposals (§5).

Everything here is validation of JSON documents into frozen values. Nothing
here talks to mshkn or to a model.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Literal

Effect = Literal["local", "read", "communicate", "transact", "administer"]
StateKind = Literal["ephemeral", "chain"]
ProposalKind = Literal["verb", "policy", "prompt"]

EFFECTS: frozenset[str] = frozenset({"local", "read", "communicate", "transact", "administer"})
EMBRYO_EFFECTS: frozenset[str] = frozenset({"local", "read"})
STATE_KINDS: frozenset[str] = frozenset({"ephemeral", "chain"})
RESERVED_TOOL_NAMES: frozenset[str] = frozenset({"remember", "propose", "try", "effort"})
RESERVED_NAMESPACES: frozenset[str] = frozenset({"root", "system"})
POLICY_FIELDS: frozenset[str] = frozenset({"principals", "hooks", "door"})
VERB_REQUIRED = ("name", "description", "params", "dockerfile", "entrypoint", "effect", "state")
PROPOSAL_REQUIRED = ("kind", "title", "rationale")
CHAIN_PREFIX = "verb/"
# trials.py reserves this prefix for a trial's own scratch chain (#118); a
# declared chain under it would let an unrelated trial's sweep delete the
# verb's live chain. Kept as a literal here, not imported from trials.py:
# declarations.py talks to nothing.
TRIAL_CHAIN_PREFIX = "verb/trial/"
TIMEOUT_DEFAULT = 60
# A verb runs in its own computer under mshkn's 300 s exec budget, but it is
# awaited inside a turn whose deadline is 240 s; 200 leaves 40 s for the loop
# and the close.
TIMEOUT_MAX = 200
DEFAULT_NEEDS: dict[str, Any] = {"ram": "256MB", "cores": 1}

NAME_RE = re.compile(r"^[a-z0-9_]+$")
NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
NAMESPACED_PRINCIPAL_RE = re.compile(r"^[a-z][a-z0-9_]*:[A-Za-z0-9_.@+-]+$")
PRINCIPAL_RE = re.compile(r"^(root|anonymous|[a-z][a-z0-9_]*:[A-Za-z0-9_.@+-]+)$")
PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
PROPOSAL_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "blocked",
        "rejected",
        "superseded",
        "disabled",
        "building",
        "ready",
        "failed",
        "applied",
        "reverted",
    }
)


class DeclarationError(ValueError):
    """A document that is not a declaration, with the reason."""


def _obj(doc: object, what: str) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise DeclarationError(f"{what} must be a JSON object")
    return doc


def _str(doc: dict[str, Any], key: str, what: str, *, required: bool = True) -> str | None:
    value = doc.get(key)
    if value is None:
        if required:
            raise DeclarationError(f"{what}.{key} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise DeclarationError(f"{what}.{key} must be a non-empty string")
    return value


def _require(doc: dict[str, Any], what: str, fields: tuple[str, ...]) -> None:
    """One refusal that names the whole shape, not the first field missing: a
    serial walk teaches a document one field per round trip (#123)."""
    missing = [f for f in fields if doc.get(f) is None]
    if missing:
        raise DeclarationError(f"{what} is missing {missing}; {what} requires {list(fields)}")


@dataclass(frozen=True)
class Requirement:
    kind: str
    name: str
    scope: str | None = None

    def to_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"kind": self.kind, "name": self.name}
        if self.scope is not None:
            doc["scope"] = self.scope
        return doc


@dataclass(frozen=True)
class Verb:
    name: str
    description: str
    params: dict[str, Any]
    dockerfile: str
    entrypoint: str
    effect: Effect
    state: StateKind
    chain: str
    asserts: str | None = None
    needs: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_NEEDS))
    timeout_seconds: int = TIMEOUT_DEFAULT
    allow: tuple[str, ...] = ()
    requires: tuple[Requirement, ...] = ()

    def tool(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.params}

    def to_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "params": self.params,
            "dockerfile": self.dockerfile,
            "entrypoint": self.entrypoint,
            "effect": self.effect,
            "state": self.state,
            "chain": self.chain,
            "needs": self.needs,
            "timeout_seconds": self.timeout_seconds,
            "allow": list(self.allow),
            "requires": [r.to_doc() for r in self.requires],
        }
        if self.asserts is not None:
            doc["asserts"] = self.asserts
        return doc


def _requirements(raw: object) -> tuple[Requirement, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeclarationError(
            "verb.requires must be a list of objects with kind, name and optional scope"
        )
    out: list[Requirement] = []
    for item in raw:
        entry = _obj(item, "verb.requires[]")
        kind = _str(entry, "kind", "verb.requires[]")
        name = _str(entry, "name", "verb.requires[]")
        assert kind is not None and name is not None
        out.append(
            Requirement(
                kind=kind, name=name, scope=_str(entry, "scope", "verb.requires[]", required=False)
            )
        )
    return tuple(out)


def _namespaced_principals(raw: object, where: str) -> tuple[str, ...]:
    """§4: a verb's `allow` names namespaced principals only. `root` is never a
    declaration's to grant (§10.1), and what `anonymous` may invoke is the
    policy document's to say (§6 step 4), so neither may appear here."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise DeclarationError(f"{where} must be a list of principals")
    for p in raw:
        if not NAMESPACED_PRINCIPAL_RE.match(p):
            raise DeclarationError(f"{where}: {p!r} is not a namespaced principal (<ns>:<name>)")
    return tuple(raw)


def parse_verb(doc: object) -> Verb:
    d = _obj(doc, "verb")
    _require(d, "verb", VERB_REQUIRED)
    name = _str(d, "name", "verb")
    assert name is not None
    if not NAME_RE.match(name):
        raise DeclarationError(f"verb.name {name!r} must match [a-z0-9_]+")
    if name in RESERVED_TOOL_NAMES:
        raise DeclarationError(
            f"verb.name {name!r} is reserved; the reserved names are {sorted(RESERVED_TOOL_NAMES)}"
        )
    description = _str(d, "description", "verb")
    dockerfile = _str(d, "dockerfile", "verb")
    entrypoint = _str(d, "entrypoint", "verb")
    assert description is not None and dockerfile is not None and entrypoint is not None
    params = d.get("params")
    if not isinstance(params, dict) or params.get("type") != "object":
        raise DeclarationError("verb.params must be a JSON schema of type object")
    properties = params.get("properties", {})
    if not isinstance(properties, dict):
        raise DeclarationError("verb.params.properties must be an object")
    for placeholder in PLACEHOLDER_RE.findall(entrypoint):
        if placeholder not in properties:
            raise DeclarationError(
                f"verb.entrypoint names {placeholder!r}, which is not a param; "
                f"the params are {sorted(properties)}"
            )
    effect = d.get("effect")
    if effect not in EFFECTS:
        raise DeclarationError(f"verb.effect must be one of {sorted(EFFECTS)}")
    state = d.get("state")
    if state == "event":
        raise DeclarationError(
            "verb.state event is specified but not in the embryo (spec §4); "
            f"must be one of {sorted(STATE_KINDS)}"
        )
    if state not in STATE_KINDS:
        raise DeclarationError(f"verb.state must be one of {sorted(STATE_KINDS)}")
    chain = d.get("chain", f"{CHAIN_PREFIX}{name}")
    if (
        not isinstance(chain, str)
        or not chain.startswith(CHAIN_PREFIX)
        or len(chain) <= len(CHAIN_PREFIX)
    ):
        raise DeclarationError(f"verb.chain must start with {CHAIN_PREFIX!r}")
    if chain.startswith(TRIAL_CHAIN_PREFIX):
        raise DeclarationError(
            f"verb.chain may not start with {TRIAL_CHAIN_PREFIX!r}, which trials use for "
            f"their own scratch chains; name a chain under {CHAIN_PREFIX!r} instead"
        )
    asserts = _str(d, "asserts", "verb", required=False)
    if asserts is not None:
        if asserts in RESERVED_NAMESPACES:
            raise DeclarationError(
                f"verb.asserts {asserts!r} is a reserved namespace; "
                f"the reserved namespaces are {sorted(RESERVED_NAMESPACES)}"
            )
        if not NAMESPACE_RE.match(asserts):
            raise DeclarationError("verb.asserts must be a lower-case identifier")
        # `asserts` has no meaning except to a pre-turn hook, and hooks.py invokes a
        # hook with the decoded payload as the value of its single parameter. Two of
        # the first three post-cut runs wrote a hook that read stdin and declared no
        # parameters (#123); refusing at propose time puts the correction in the same
        # turn as the mistake, instead of at approval, in an inbox, a turn later.
        if len(properties) != 1:
            raise DeclarationError(
                f"verb.asserts makes {name!r} a pre-turn hook, and a hook takes exactly one "
                f"parameter, which receives the decoded payload; it declares "
                f"{sorted(properties)}"
            )
    needs = d.get("needs", dict(DEFAULT_NEEDS))
    if not isinstance(needs, dict):
        raise DeclarationError("verb.needs must be an object")
    timeout = d.get("timeout_seconds", TIMEOUT_DEFAULT)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= TIMEOUT_MAX:
        raise DeclarationError(f"verb.timeout_seconds must be an integer from 1 to {TIMEOUT_MAX}")
    return Verb(
        name=name,
        description=description,
        params=params,
        dockerfile=dockerfile,
        entrypoint=entrypoint,
        effect=effect,
        state=state,
        chain=chain,
        asserts=asserts,
        needs=needs,
        timeout_seconds=timeout,
        allow=_namespaced_principals(d.get("allow"), "verb.allow"),
        requires=_requirements(d.get("requires")),
    )


def parse_runs(params: object, runs: object) -> list[dict[str, Any]]:
    """The invocations of one trial (#118). `params` is one invocation; `runs` is
    several, in order. A `chain` verb's runs share the trial's scratch chain, so a
    second entry is how the model sees whether its state persisted."""
    if params is not None and runs is not None:
        raise DeclarationError(
            "give params for one invocation or runs for several, not both; "
            "runs is a list of parameter objects, one per invocation, in order"
        )
    if runs is None:
        return [dict(_obj(params, "params"))] if params is not None else [{}]
    if not isinstance(runs, list):
        raise DeclarationError("runs must be a list of parameter objects, one per invocation")
    if not runs:
        raise DeclarationError("runs must name at least one invocation")
    return [dict(_obj(entry, f"runs[{i}]")) for i, entry in enumerate(runs)]


def render_command(verb: Verb, params: dict[str, Any]) -> str:
    """The entrypoint with every {{param}} shell-quoted, under the guest's timeout."""

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise DeclarationError(f"param {key!r} is required by the entrypoint")
        value = params[key]
        text = value if isinstance(value, str) else json.dumps(value)
        return shlex.quote(text)

    rendered = PLACEHOLDER_RE.sub(substitute, verb.entrypoint)
    return (
        f"timeout --preserve-status -s KILL {verb.timeout_seconds} bash -c {shlex.quote(rendered)}"
    )


@dataclass(frozen=True)
class Grant:
    invoke: Literal["*"] | tuple[str, ...] = ()
    propose: bool = False

    def allows(self, verb: str) -> bool:
        return self.invoke == "*" or verb in self.invoke

    def to_doc(self) -> dict[str, Any]:
        return {"invoke": "*" if self.invoke == "*" else list(self.invoke), "propose": self.propose}


NOTHING = Grant()


@dataclass(frozen=True)
class Policy:
    principals: dict[str, Grant]
    hooks: tuple[str, ...]
    door: Literal["open", "closed"]

    def grant(self, principal: str) -> Grant:
        return self.principals.get(principal, NOTHING)

    def to_doc(self) -> dict[str, Any]:
        return {
            "principals": {p: g.to_doc() for p, g in self.principals.items()},
            "hooks": list(self.hooks),
            "door": self.door,
        }


def parse_policy(doc: object) -> Policy:
    d = _obj(doc, "policy")
    unknown = sorted(set(d) - POLICY_FIELDS)
    if unknown:
        raise DeclarationError(
            f"policy has unknown fields {unknown}; policy is data, not code: "
            f"the fields are {sorted(POLICY_FIELDS)}"
        )
    principals_raw = _obj(d.get("principals", {}), "policy.principals")
    principals: dict[str, Grant] = {}
    for principal, grant_raw in principals_raw.items():
        # §10.1: root may always invoke everything and propose, and
        # may_invoke/may_propose short-circuit on it, so a policy naming root
        # would be silently inert. The seed used to assert this; the refusal
        # does now (#123).
        if principal == "root":
            raise DeclarationError(
                "policy.principals: root is fixed and is not policy's to grant or refuse (§10.1)"
            )
        if not PRINCIPAL_RE.match(principal):
            raise DeclarationError(
                f"policy.principals: {principal!r} is not a principal; "
                "a principal is 'root', 'anonymous' or '<namespace>:<name>'"
            )
        grant = _obj(grant_raw, f"policy.principals[{principal}]")
        invoke_raw = grant.get("invoke", [])
        invoke: Literal["*"] | tuple[str, ...]
        if invoke_raw == "*":
            invoke = "*"
        elif isinstance(invoke_raw, list) and all(
            isinstance(v, str) and NAME_RE.match(v) for v in invoke_raw
        ):
            invoke = tuple(invoke_raw)
        else:
            raise DeclarationError(
                f"policy.principals[{principal}].invoke must be '*' or a list of verb names"
            )
        propose = grant.get("propose", False)
        if not isinstance(propose, bool):
            raise DeclarationError(f"policy.principals[{principal}].propose must be a boolean")
        principals[principal] = Grant(invoke=invoke, propose=propose)
    hooks_raw = d.get("hooks", [])
    if not isinstance(hooks_raw, list) or not all(
        isinstance(h, str) and NAME_RE.match(h) for h in hooks_raw
    ):
        raise DeclarationError("policy.hooks must be a list of verb names")
    door = d.get("door", "closed")
    if door not in ("open", "closed"):
        raise DeclarationError("policy.door must be open or closed")
    return Policy(principals=principals, hooks=tuple(hooks_raw), door=door)


@dataclass
class Proposal:
    id: str
    kind: ProposalKind
    title: str
    rationale: str
    supersedes: str | None = None
    verb: Verb | None = None
    policy: Policy | None = None
    prompt: str | None = None
    status: str = "pending"
    recipe_id: str | None = None
    log: str | None = None

    def to_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "rationale": self.rationale,
            "supersedes": self.supersedes,
            "status": self.status,
            "recipe_id": self.recipe_id,
            "log": self.log,
        }
        if self.verb is not None:
            doc["verb"] = self.verb.to_doc()
        if self.policy is not None:
            doc["policy"] = self.policy.to_doc()
        if self.prompt is not None:
            doc["prompt"] = self.prompt
        return doc


def parse_proposal(doc: object, *, id: str) -> Proposal:  # noqa: A002 — the field is called id
    d = _obj(doc, "proposal")
    _require(d, "proposal", PROPOSAL_REQUIRED)
    kind = d.get("kind")
    if kind not in ("verb", "policy", "prompt"):
        raise DeclarationError("proposal.kind must be verb, policy or prompt")
    title = _str(d, "title", "proposal")
    rationale = _str(d, "rationale", "proposal")
    assert title is not None and rationale is not None
    supersedes = _str(d, "supersedes", "proposal", required=False)
    status = d.get("status", "pending")
    if status not in PROPOSAL_STATUSES:
        raise DeclarationError(f"proposal.status {status!r} is not a status")
    proposal = Proposal(
        id=id,
        kind=kind,
        title=title,
        rationale=rationale,
        supersedes=supersedes,
        status=status,
        recipe_id=_str(d, "recipe_id", "proposal", required=False),
        log=d.get("log") if isinstance(d.get("log"), str) else None,
    )
    if kind == "verb":
        if "verb" not in d:
            raise DeclarationError("proposal.verb is required for kind verb")
        proposal.verb = parse_verb(d["verb"])
    elif kind == "policy":
        if "policy" not in d:
            raise DeclarationError("proposal.policy is required for kind policy")
        proposal.policy = parse_policy(d["policy"])
    else:
        proposal.prompt = _str(d, "prompt", "proposal")
    return proposal
