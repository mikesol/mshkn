"""The brain disk (spec §3, §8): state.json, seed.md, policy.json, memory/.

Everything the membrane changes — the policy, the self-description, the
catalog, the proposals, the trials, the inbox, the turn window — is one
document, `state.json`, replaced atomically on save (#100). `seed.md` and
`policy.json` are the priors: fixed after hatching, the latter read once to
seed the first state."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from membrane.declarations import Policy, Proposal, Verb, parse_policy, parse_proposal, parse_verb
from membrane.model import zero_usage

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
    runs: list[dict[str, Any]]
    recipe_id: str | None
    status: TrialStatus
    results: list[dict[str, Any]]
    build_log: str | None = None
    # The scratch chain a `chain` verb's invocations share, `verb/trial/<id>`, and
    # whether its checkpoints have been deleted (#118). `None` until run 1 is attempted.
    chain: str | None = None
    swept: bool = False

    def to_doc(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "verb": self.verb.to_doc(),
            "runs": self.runs,
            "recipe_id": self.recipe_id,
            "status": self.status,
            "results": self.results,
            "build_log": self.build_log,
            "chain": self.chain,
            "swept": self.swept,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Trial:
        return cls(
            id=doc["id"],
            verb=parse_verb(doc["verb"]),
            runs=doc["runs"],
            recipe_id=doc.get("recipe_id"),
            status=doc["status"],
            results=doc.get("results") or [],
            build_log=doc.get("build_log"),
            chain=doc.get("chain"),
            swept=bool(doc.get("swept")),
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
    # What the door printed after the audit line (the reply with the proposals
    # appended) and the turn's closing audit fields, so `list` can show a turn
    # that closed in a fork nobody was watching (relay design §6).
    output: str = ""
    audit: dict[str, Any] = field(default_factory=dict)


@dataclass
class Pending:
    """The in-flight turn (relay design §6): what `say` began, the messages sent
    so far, the relay job in flight, and the turn's bookkeeping."""

    turn: int
    principal: str
    door: str
    message: str
    payload: str
    messages: list[dict[str, Any]]
    offered: list[str]
    job: str
    hooks: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    made: list[str] = field(default_factory=list)
    model_calls: int = 0
    usage: dict[str, int] = field(default_factory=zero_usage)
    forks: int = 1
    started_at: str = ""
    write_memory: bool = False
    # What this turn drained from the inbox, as plain documents. A turn that
    # ends in error gives them back (#123): the drain at start_turn is
    # unconditional, so a build log, a trial result or a refusal consumed by a
    # turn the model service never answered would otherwise be lost for good.
    drained: list[dict[str, Any]] = field(default_factory=list)

    def to_doc(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Pending:
        return cls(**doc)


@dataclass(frozen=True)
class Queued:
    """A message that arrived while a turn was pending. Its hooks ran at queue
    time to name the principal, so the runs ride with it: the closing audit line
    of the turn it becomes is the record authorization is read from (§10.5), and
    it has to stand alone."""

    principal: str
    door: str
    message: str
    payload: str
    hooks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class State:
    turn: int = 0
    catalog: dict[str, CatalogEntry] = field(default_factory=dict)
    proposals: dict[str, Proposal] = field(default_factory=dict)
    trials: dict[str, Trial] = field(default_factory=dict)
    inbox: list[InboxItem] = field(default_factory=list)
    window: list[Exchange] = field(default_factory=list)
    pending: Pending | None = None
    queue: list[Queued] = field(default_factory=list)
    principals: set[str] = field(default_factory=set)
    policy: Policy = field(default_factory=lambda: Policy(principals={}, hooks=(), door="closed"))
    self_description: str = ""
    previous_policy: Policy | None = None
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
                    "output": e.output,
                    "audit": e.audit,
                }
                for e in self.window
            ],
            "pending": None if self.pending is None else self.pending.to_doc(),
            "queue": [asdict(q) for q in self.queue],
            "principals": sorted(self.principals),
            "policy": self.policy.to_doc(),
            "self_description": self.self_description,
            "previous_policy": None
            if self.previous_policy is None
            else self.previous_policy.to_doc(),
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
            pending=None if doc.get("pending") is None else Pending.from_doc(doc["pending"]),
            queue=[Queued(**q) for q in doc.get("queue", [])],
            principals=set(doc["principals"]),
            policy=parse_policy(doc["policy"]),
            self_description=doc["self_description"],
            previous_policy=(
                None if doc.get("previous_policy") is None else parse_policy(doc["previous_policy"])
            ),
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
        """The saved state, or before the first save the state the priors
        describe (spec §8): the hatched `policy.json`, an empty
        self-description, nothing else."""
        path = self.root / "state.json"
        if not path.exists():
            return State(policy=parse_policy(json.loads((self.root / "policy.json").read_text())))
        return State.from_doc(json.loads(path.read_text()))

    def save(self, state: State) -> None:
        """Replace `state.json` atomically: the whole document is written and
        fsynced beside it, then renamed over it. The fork that runs a command
        is checkpointed however the process ends (#100), so the disk holds
        either the previous state or the new one, never a torn file and
        never a document that disagrees with its own bookkeeping."""
        path = self.root / "state.json"
        tmp = path.with_name("state.json.tmp")
        try:
            with tmp.open("w") as f:
                f.write(json.dumps(state.to_doc(), indent=1, sort_keys=True))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def seed(self) -> str:
        return (self.root / "seed.md").read_text()
