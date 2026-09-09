"""Everything the membrane owns lives under /brain (spec §3) and survives the
checkpoint as files."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from membrane.declarations import Policy, parse_proposal, parse_verb
from membrane.state import Brain, CatalogEntry, CatalogStatus, Exchange, InboxItem, State, Trial

from tests.unit.test_embryo_declarations import VERB

if TYPE_CHECKING:
    from pathlib import Path


def test_empty_brain_has_empty_state_and_no_self(tmp_path: Path) -> None:
    (tmp_path / "policy.json").write_text(
        json.dumps({"principals": {}, "hooks": [], "door": "closed"})
    )
    (tmp_path / "seed.md").write_text("I am.")
    brain = Brain(tmp_path)
    state = brain.state()
    assert state == State()
    assert brain.self_description() == ""
    assert brain.seed() == "I am."
    assert brain.policy() == Policy(principals={}, hooks=(), door="closed")
    assert brain.memory_dir == tmp_path / "memory" and brain.memory_dir.is_dir()
    assert brain.env_path == tmp_path / ".env"


def test_state_round_trips_through_json(tmp_path: Path) -> None:
    brain = Brain(tmp_path)
    verb = parse_verb(VERB)
    state = State()
    state.turn = 4
    pid = state.new_proposal_id()
    assert pid == "p-1" and state.new_proposal_id() == "p-2"
    tid = state.new_trial_id()
    assert tid == "t-1"
    state.proposals[pid] = parse_proposal(
        {"kind": "verb", "title": "t", "rationale": "r", "verb": VERB}, id=pid
    )
    state.catalog[verb.name] = CatalogEntry(
        verb=verb, status="ready", recipe_id="rcp-1", proposal_id=pid
    )
    state.trials[tid] = Trial(
        id=tid, verb=verb, params={"url": "u"}, recipe_id="rcp-2", status="building", result=None
    )
    state.inbox.append(InboxItem(kind="build", text="verb page_title is ready"))
    state.window.append(Exchange(turn=4, principal="root", door="api", input="hi", reply="hello"))
    state.principals.add("ssh:mike")
    state.previous_policy = {"principals": {}, "hooks": [], "door": "closed"}
    state.applied_policy = "p-2"
    brain.save(state)
    assert (tmp_path / "state.json").exists()
    loaded = Brain(tmp_path).state()
    assert loaded == state
    assert loaded.ready_verbs() == {"page_title": verb}


def test_policy_and_self_are_separate_files(tmp_path: Path) -> None:
    brain = Brain(tmp_path)
    policy = Policy(principals={}, hooks=("verify_ssh",), door="open")
    brain.write_policy(policy)
    assert json.loads((tmp_path / "policy.json").read_text()) == policy.to_doc()
    brain.write_self("I verify signatures.")
    assert (tmp_path / "self.md").read_text() == "I verify signatures."
    assert Brain(tmp_path).policy() == policy


def test_ready_verbs_excludes_every_other_status(tmp_path: Path) -> None:
    verb = parse_verb(VERB)
    state = State()
    statuses: tuple[CatalogStatus, ...] = ("building", "failed", "disabled")
    for status in statuses:
        state.catalog[status] = CatalogEntry(
            verb=verb, status=status, recipe_id=None, proposal_id="p-1"
        )
    assert state.ready_verbs() == {}
