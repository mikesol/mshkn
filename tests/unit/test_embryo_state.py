"""Everything the membrane owns lives under /brain (spec §3) and survives the
checkpoint as files."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from membrane.declarations import Policy, parse_policy, parse_proposal, parse_verb
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
    assert state == State(policy=Policy(principals={}, hooks=(), door="closed"))
    assert state.self_description == ""
    assert brain.seed() == "I am."
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
    state.policy = Policy(principals={}, hooks=("page_title",), door="open")
    state.self_description = "I title pages."
    state.previous_policy = Policy(principals={}, hooks=(), door="closed")
    state.previous_prompt = ""
    state.applied_policy = "p-2"
    brain.save(state)
    assert (tmp_path / "state.json").exists()
    loaded = Brain(tmp_path).state()
    assert loaded == state
    assert loaded.ready_verbs() == {"page_title": verb}


def test_policy_and_self_live_in_state_json(tmp_path: Path) -> None:
    """#100: the policy and the self-description are fields of the one state
    document, so an approval commits them and its bookkeeping together."""
    brain = Brain(tmp_path)
    policy = Policy(principals={}, hooks=("verify_ssh",), door="open")
    state = State(policy=policy, self_description="I verify signatures.")
    brain.save(state)
    doc = json.loads((tmp_path / "state.json").read_text())
    assert doc["policy"] == policy.to_doc() and doc["self_description"] == "I verify signatures."
    assert not (tmp_path / "policy.json").exists() and not (tmp_path / "self.md").exists()
    loaded = Brain(tmp_path).state()
    assert loaded.policy == policy and loaded.self_description == "I verify signatures."


def test_empty_brain_takes_its_policy_from_the_prior(tmp_path: Path) -> None:
    """Before the first save, the policy is the hatched `policy.json` (spec §8)
    and the self-description is empty; after it, `policy.json` is history."""
    prior = {
        "principals": {"anonymous": {"invoke": [], "propose": False}},
        "hooks": [],
        "door": "closed",
    }
    (tmp_path / "policy.json").write_text(json.dumps(prior))
    state = Brain(tmp_path).state()
    assert state.policy == parse_policy(prior) and state.self_description == ""
    state.policy = Policy(principals={}, hooks=("h",), door="open")
    Brain(tmp_path).save(state)
    (tmp_path / "policy.json").write_text("not read again")
    assert Brain(tmp_path).state().policy == state.policy


def test_save_replaces_state_json_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100: the checkpoint takes whatever is on disk when the process dies, so
    state.json is never half-written: a save that fails leaves the previous
    document byte for byte, and no temp file beside it."""
    brain = Brain(tmp_path)
    brain.save(State(turn=3))
    before = (tmp_path / "state.json").read_bytes()

    def refuse(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        brain.save(State(turn=4))
    assert (tmp_path / "state.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
    assert Brain(tmp_path).state().turn == 3


def test_ready_verbs_excludes_every_other_status(tmp_path: Path) -> None:
    verb = parse_verb(VERB)
    state = State()
    statuses: tuple[CatalogStatus, ...] = ("building", "failed", "disabled")
    for status in statuses:
        state.catalog[status] = CatalogEntry(
            verb=verb, status=status, recipe_id=None, proposal_id="p-1"
        )
    assert state.ready_verbs() == {}
