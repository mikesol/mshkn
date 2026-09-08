"""The brain disk (spec §3, §8): state.json, policy.json, self.md, seed.md, memory/."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from membrane.declarations import Policy, Proposal, Verb, parse_policy, parse_proposal, parse_verb

if TYPE_CHECKING:
    from pathlib import Path

WINDOW = 10

CatalogStatus = Literal["building", "ready", "failed", "disabled"]
TrialStatus = Literal["building", "done", "failed"]


@dataclass
class CatalogEntry:
    verb: Verb
    status: CatalogStatus
    recipe_id: str | None
    proposal_id: str

    def to_doc(self) -> dict[str, Any]:
        return {
            "verb": self.verb.to_doc(),
            "status": self.status,
            "recipe_id": self.recipe_id,
            "proposal_id": self.proposal_id,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> CatalogEntry:
        return cls(
            verb=parse_verb(doc["verb"]),
            status=doc["status"],
            recipe_id=doc.get("recipe_id"),
            proposal_id=doc["proposal_id"],
        )


@dataclass
class Trial:
    id: str
    verb: Verb
    params: dict[str, Any]
    recipe_id: str | None
    status: TrialStatus
    result: dict[str, Any] | None

    def to_doc(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "verb": self.verb.to_doc(),
            "params": self.params,
            "recipe_id": self.recipe_id,
            "status": self.status,
            "result": self.result,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Trial:
        return cls(
            id=doc["id"],
            verb=parse_verb(doc["verb"]),
            params=doc["params"],
            recipe_id=doc.get("recipe_id"),
            status=doc["status"],
            result=doc.get("result"),
        )


@dataclass(frozen=True)
class InboxItem:
    kind: str
    text: str


@dataclass(frozen=True)
class Exchange:
    turn: int
    principal: str
    door: str
    input: str
    reply: str


@dataclass
class State:
    turn: int = 0
    catalog: dict[str, CatalogEntry] = field(default_factory=dict)
    proposals: dict[str, Proposal] = field(default_factory=dict)
    trials: dict[str, Trial] = field(default_factory=dict)
    inbox: list[InboxItem] = field(default_factory=list)
    window: list[Exchange] = field(default_factory=list)
    principals: set[str] = field(default_factory=set)
    previous_policy: dict[str, Any] | None = None
    previous_prompt: str | None = None
    applied_policy: str | None = None
    applied_prompt: str | None = None
    next_proposal: int = 1
    next_trial: int = 1

    def new_proposal_id(self) -> str:
        pid = f"p-{self.next_proposal}"
        self.next_proposal += 1
        return pid

    def new_trial_id(self) -> str:
        tid = f"t-{self.next_trial}"
        self.next_trial += 1
        return tid

    def ready_verbs(self) -> dict[str, Verb]:
        return {name: e.verb for name, e in self.catalog.items() if e.status == "ready"}

    def to_doc(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "catalog": {k: v.to_doc() for k, v in self.catalog.items()},
            "proposals": {k: v.to_doc() for k, v in self.proposals.items()},
            "trials": {k: v.to_doc() for k, v in self.trials.items()},
            "inbox": [{"kind": i.kind, "text": i.text} for i in self.inbox],
            "window": [
                {
                    "turn": e.turn,
                    "principal": e.principal,
                    "door": e.door,
                    "input": e.input,
                    "reply": e.reply,
                }
                for e in self.window
            ],
            "principals": sorted(self.principals),
            "previous_policy": self.previous_policy,
            "previous_prompt": self.previous_prompt,
            "applied_policy": self.applied_policy,
            "applied_prompt": self.applied_prompt,
            "next_proposal": self.next_proposal,
            "next_trial": self.next_trial,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> State:
        return cls(
            turn=doc["turn"],
            catalog={k: CatalogEntry.from_doc(v) for k, v in doc["catalog"].items()},
            proposals={k: parse_proposal(v, id=k) for k, v in doc["proposals"].items()},
            trials={k: Trial.from_doc(v) for k, v in doc["trials"].items()},
            inbox=[InboxItem(**i) for i in doc["inbox"]],
            window=[Exchange(**e) for e in doc["window"]],
            principals=set(doc["principals"]),
            previous_policy=doc.get("previous_policy"),
            previous_prompt=doc.get("previous_prompt"),
            applied_policy=doc.get("applied_policy"),
            applied_prompt=doc.get("applied_prompt"),
            next_proposal=doc["next_proposal"],
            next_trial=doc["next_trial"],
        )


class Brain:
    """The files under /brain, read and written whole."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def memory_dir(self) -> Path:
        path = self.root / "memory"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def state(self) -> State:
        path = self.root / "state.json"
        if not path.exists():
            return State()
        return State.from_doc(json.loads(path.read_text()))

    def save(self, state: State) -> None:
        (self.root / "state.json").write_text(json.dumps(state.to_doc(), indent=1, sort_keys=True))

    def policy(self) -> Policy:
        return parse_policy(json.loads((self.root / "policy.json").read_text()))

    def write_policy(self, policy: Policy) -> None:
        (self.root / "policy.json").write_text(json.dumps(policy.to_doc(), indent=1))

    def seed(self) -> str:
        return (self.root / "seed.md").read_text()

    def self_description(self) -> str:
        path = self.root / "self.md"
        return path.read_text() if path.exists() else ""

    def write_self(self, text: str) -> None:
        (self.root / "self.md").write_text(text)
