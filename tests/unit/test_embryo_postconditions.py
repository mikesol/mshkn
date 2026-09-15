"""The named checks (capabilities design §6): each reads a `Judged` context and
returns ok plus the evidence it was judged on; a capability lists the ones that
apply by name."""

from __future__ import annotations

import json
from typing import Any

import pytest
from membrane.postconditions import CHECKS, EXERCISES, INVARIANTS, Judged, Turn, judge

from tests.support_embryo import audit_line

pytestmark = pytest.mark.unit


def _judged(turns: list[Turn], final: dict[str, Any], **over: Any) -> Judged:
    base: dict[str, Any] = {
        "turns": turns,
        "final": final,
        "recipes_after": set(),
        "preexisting": set(),
        "brain_recipe": "rcp-brain",
        "checks": {},
        "sent": [],
        "context": {},
    }
    base.update(over)
    return Judged(**base)


def test_the_registry_holds_the_seven_and_judge_runs_the_named_ones() -> None:
    assert list(CHECKS) == [
        "authentication",
        "root_unforgeable",
        "authorization",
        "page_title",
        "counter",
        "no_undeclared_capability",
        "nothing_by_hand",
    ]
    judged = _judged([], {"policy": {"principals": {}}, "catalog": {}, "proposals": []})
    result = judge(["root_unforgeable", "nothing_by_hand"], judged)
    assert list(result) == ["root_unforgeable", "nothing_by_hand"]
    assert result["root_unforgeable"] == {"ok": True, "evidence": {"public_principals": []}}
    with pytest.raises(KeyError, match="no_such_check"):
        judge(["no_such_check"], judged)


def test_invariants_are_a_subset_of_the_checks_and_exercises_are_the_rest() -> None:
    assert set(CHECKS) >= INVARIANTS
    assert set(CHECKS) - INVARIANTS == EXERCISES
    assert {"root_unforgeable", "no_undeclared_capability", "nothing_by_hand"} == INVARIANTS


# ---------------------------------------------------------------- the verdict on fixtures


def _turn(label: str, door: str, audit: dict[str, Any], reply: str = "") -> Turn:
    return Turn(
        label=label, door=door, words="w", audit=audit, reply=reply, commands=[], approvals=[]
    )


def _good_final() -> dict[str, Any]:
    return {
        "door": {"status": "open", "hooks": ["verify_ssh"], "hooks_ready": ["verify_ssh"]},
        "policy": {
            "principals": {
                "ssh:mike": {"invoke": "*", "propose": True},
                "anonymous": {"invoke": [], "propose": False},
            },
            "hooks": ["verify_ssh"],
            "door": "open",
        },
        "catalog": {
            "verify_ssh": {
                "status": "ready",
                "state": "ephemeral",
                "chain_length": 0,
                "recipe_id": "r1",
            },
            "page_title": {
                "status": "ready",
                "state": "ephemeral",
                "chain_length": 0,
                "recipe_id": "r2",
            },
            "counter": {
                "status": "ready",
                "state": "chain",
                "chain_length": 2,
                "chain_head": "k2",
                "recipe_id": "r3",
            },
        },
        "proposals": [
            {
                "id": "p-1",
                "kind": "verb",
                "status": "ready",
                "recipe_id": "r1",
                "verb": {"name": "verify_ssh"},
            },
            {"id": "p-2", "kind": "policy", "status": "applied", "recipe_id": None},
            {"id": "p-3", "kind": "policy", "status": "applied", "recipe_id": None},
            {
                "id": "p-4",
                "kind": "verb",
                "status": "ready",
                "recipe_id": "r2",
                "verb": {"name": "page_title"},
            },
            {
                "id": "p-5",
                "kind": "verb",
                "status": "ready",
                "recipe_id": "r3",
                "verb": {"name": "counter"},
            },
        ],
    }


def _good_turns() -> list[Turn]:
    return [
        _turn("1", "api", audit_line()),
        _turn("4", "ingress", audit_line(door="ingress", principal="ssh:mike")),
        _turn(
            "5", "ingress-unsigned", audit_line(door="ingress", principal="anonymous", offered=[])
        ),
        _turn(
            "8",
            "ingress",
            audit_line(
                door="ingress",
                principal="ssh:mike",
                tools=[{"name": "page_title", "computer_id": "c8"}],
                offered=["effort", "page_title", "propose", "remember", "try", "verify_ssh"],
            ),
            "Example Domain",
        ),
        _turn(
            "9-count-1",
            "ingress",
            audit_line(
                door="ingress",
                principal="ssh:mike",
                tools=[{"name": "counter", "computer_id": "c9a", "chain_head": "k1"}],
            ),
            "1",
        ),
        _turn(
            "9-count-2",
            "ingress",
            audit_line(
                door="ingress",
                principal="ssh:mike",
                tools=[{"name": "counter", "computer_id": "c9b", "chain_head": "k2"}],
            ),
            "2",
        ),
    ]


def _good_checks() -> dict[str, dict[str, Any]]:
    return {
        "c8": {"computer_id": "c8", "gone": True, "stdout": "Example Domain\n", "exit_code": 0},
        "c9a": {"computer_id": "c9a", "gone": True, "stdout": "1\n", "exit_code": 0},
        "c9b": {"computer_id": "c9b", "gone": True, "stdout": "called 2 times\n", "exit_code": 0},
    }


def _judge(**overrides: Any) -> dict[str, dict[str, Any]]:
    args: dict[str, Any] = {
        "recipes_after": {"pre", "brain", "r1", "r2", "r3"},
        "preexisting": {"pre"},
        "brain_recipe": "brain",
        "checks": _good_checks(),
        "sent": [("api", "say"), ("api", "list"), ("api", "approve"), ("ingress", "say")],
    }
    args.update(overrides)
    turns = args.pop("turns", _good_turns())
    final = args.pop("final", _good_final())
    return judge(list(CHECKS), _judged(turns, final, **args))


def test_the_fixtures_pass_every_postcondition() -> None:
    result = _judge()
    assert all(v["ok"] for v in result.values()), result


def test_by_label_reads_the_latest_attempt_of_a_row_and_not_a_label_sharing_its_prefix() -> None:
    """A row re-asked after a policy change is `<label>-again-<n>` (#170), and a
    check reads what the row last answered. `9-count-1` is not an attempt at `9`."""
    from membrane.postconditions import by_label

    turns = [
        _turn("9", "ingress", audit_line()),
        _turn("9-count-1", "ingress", audit_line()),
        _turn("9-count-1-again-1", "ingress", audit_line()),
        _turn("9-again-1", "ingress", audit_line()),
        _turn("9-again-2", "ingress", audit_line()),
    ]
    assert by_label(turns, "9") is turns[4]
    assert by_label(turns, "9-count-1") is turns[2]
    assert by_label(turns, "9-count-2") is None
    assert by_label(turns, "8") is None
    assert by_label([], "9") is None


def test_authentication_evidence_carries_the_hook_runs_and_their_logs() -> None:
    turns = _good_turns()
    hook = {
        "name": "verify_ssh",
        "status": "ok",
        "computer_id": "c-hook",
        "exit_code": 1,
        "principal": "anonymous",
    }
    turns[1] = _turn(
        "4", "ingress", audit_line(door="ingress", principal="anonymous", hooks=[hook])
    )
    checks = _good_checks()
    checks["c-hook"] = {"computer_id": "c-hook", "gone": True, "stdout": "", "exit_code": 1}
    result = _judge(turns=turns, checks=checks)["authentication"]
    assert result["ok"] is False
    assert result["evidence"]["hooks"] == [hook] and result["evidence"]["hook_logs"] == [
        checks["c-hook"]
    ]


def test_invariants_do_not_care_about_labels_but_exercises_do() -> None:
    """#167: a dependent capability's rows are not labelled 4, 5, 8 and
    9-count-* the way hatch's are — a security run speaks rows 11 to 14. An
    invariant reads no label, so relabelling the turns does not change its
    verdict. An exercise reads hatch's labels by name, so it cannot find its
    evidence on the relabelled rows and fails regardless of the brain's
    state — the defect the issue describes, pinned here as a fact."""
    original = _judge()
    relabel = {
        "1": "11",
        "4": "12",
        "5": "13",
        "8": "14",
        "9-count-1": "14-count-1",
        "9-count-2": "14-count-2",
    }
    relabelled_turns = [
        _turn(relabel[t.label], t.door, dict(t.audit), t.reply) for t in _good_turns()
    ]
    relabelled = _judge(turns=relabelled_turns)
    for name in INVARIANTS:
        assert relabelled[name]["ok"] == original[name]["ok"], name
    assert relabelled["page_title"]["ok"] is False
    assert relabelled["counter"]["ok"] is False


def test_root_is_unforgeable_fails_when_a_public_turn_is_root() -> None:
    turns = _good_turns()
    turns.append(_turn("x", "ingress", audit_line(door="ingress", principal="root")))
    assert _judge(turns=turns)["root_unforgeable"] == {
        "ok": False,
        "evidence": {
            "public_principals": [
                "ssh:mike",
                "anonymous",
                "ssh:mike",
                "ssh:mike",
                "ssh:mike",
                "root",
            ]
        },
    }
    turns = _good_turns()
    turns.append(_turn("x", "ingress", audit_line(door="ingress", principal="system:me")))
    assert _judge(turns=turns)["root_unforgeable"]["ok"] is False


def test_authorization_needs_the_policy_and_the_empty_anonymous_offer() -> None:
    final = _good_final()
    final["policy"]["principals"]["anonymous"] = {"invoke": ["page_title"], "propose": False}
    assert _judge(final=final)["authorization"]["ok"] is False
    final = _good_final()
    final["policy"]["principals"]["ssh:mike"] = {"invoke": ["page_title"], "propose": True}
    assert _judge(final=final)["authorization"]["ok"] is False  # counter not invokable
    final["policy"]["principals"]["ssh:mike"] = {
        "invoke": ["page_title", "counter", "verify_ssh"],
        "propose": True,
    }
    assert _judge(final=final)["authorization"]["ok"] is True
    final["policy"]["principals"]["ssh:mike"] = {"invoke": "*", "propose": False}
    assert _judge(final=final)["authorization"]["ok"] is False  # cannot propose
    turns = _good_turns()
    turns[2] = _turn(
        "5",
        "ingress-unsigned",
        audit_line(door="ingress", principal="anonymous", offered=["page_title"]),
    )
    assert _judge(turns=turns)["authorization"]["ok"] is False


def test_authorization_is_judged_on_the_verbs_hatch_exercises() -> None:
    """#117: "ssh:mike can invoke the verbs" means the verbs turns 8 and 9 invoke.
    A grant that withholds the agent's own identity hook from the public
    principal is a decision turn 6 asked for, not a miss."""
    final = _good_final()
    final["policy"]["principals"]["ssh:mike"] = {
        "invoke": ["page_title", "counter"],
        "propose": True,
    }
    judged = _judge(final=final)["authorization"]
    assert judged["ok"] is True
    assert judged["evidence"]["exercised"] == ["counter", "page_title"]
    # A list grant is only evidence against the verbs that were exercised: a
    # run that invoked nothing at turns 8 and 9 has shown no verb it can invoke.
    turns = _good_turns()
    turns[3] = _turn("8", "ingress", audit_line(door="ingress", principal="ssh:mike"), reply="?")
    turns[4] = _turn("9-count-1", "ingress", audit_line(door="ingress", principal="ssh:mike"))
    turns[5] = _turn("9-count-2", "ingress", audit_line(door="ingress", principal="ssh:mike"))
    judged = _judge(final=final, turns=turns)["authorization"]
    assert judged["ok"] is False
    assert judged["evidence"]["exercised"] == []
    # "*" covers whatever hatch asks for, exercised or not.
    assert _judge(turns=turns)["authorization"]["ok"] is True
    # The hook is not one of the verbs: a run that invoked only its own hook as a
    # tool, under a grant of the hook alone, has not shown it can invoke anything
    # hatch gave it.
    hook_call = [{"name": "verify_ssh", "computer_id": "cx"}]
    for turn in turns[3:6]:
        turn.audit["tools"] = hook_call
    final["policy"]["principals"]["ssh:mike"] = {"invoke": ["verify_ssh"], "propose": True}
    judged = _judge(final=final, turns=turns)["authorization"]
    assert judged["ok"] is False
    assert judged["evidence"]["exercised"] == []


def test_page_title_needs_the_words_a_gone_computer_and_its_log() -> None:
    checks = _good_checks()
    checks["c8"]["gone"] = False
    assert _judge(checks=checks)["page_title"]["ok"] is False
    checks = _good_checks()
    checks["c8"]["stdout"] = "Something else"
    assert _judge(checks=checks)["page_title"]["ok"] is False
    turns = _good_turns()
    turns[3] = _turn("8", "ingress", audit_line(door="ingress", principal="ssh:mike"), "I cannot.")
    assert _judge(turns=turns)["page_title"] == {
        "ok": False,
        "evidence": {"reply": "I cannot.", "computer_id": None, "gone": None, "stdout": None},
    }


def _repeated_head_turns() -> list[Turn]:
    """The counted turns with both invocations reporting one head: the second
    invocation left no new checkpoint, so the chain did not advance."""
    turns = []
    for t in _good_turns():
        if t.label.startswith("9-count"):
            audit = json.loads(json.dumps(t.audit))
            for call in audit.get("tools", []):
                if call.get("name") == "counter":
                    call["chain_head"] = "k1"
            t = _turn(t.label, t.door, audit, t.reply)
        turns.append(t)
    return turns


def test_counter_needs_one_then_two_and_a_head_that_advanced_once_per_call() -> None:
    """The counter is monotonic and every invocation leaves a new head (#139).
    Counting the chain's rows instead asserted retained history, which #93
    retention is entitled to collect: `list_prunable_checkpoints` keeps every
    label's newest row and prunes the rest."""
    checks = _good_checks()
    checks["c9b"]["stdout"] = "3\n"
    assert _judge(checks=checks)["counter"]["ok"] is False  # 1 then 3, not monotonic

    turns = _repeated_head_turns()
    result = _judge(turns=turns)["counter"]
    assert result["ok"] is False  # two calls, one head: the chain never advanced
    assert result["evidence"]["chain_heads"] == ["k1", "k1"]

    final = _good_final()
    final["catalog"]["counter"]["chain_head"] = "k-other"
    assert _judge(final=final)["counter"]["ok"] is False  # the head is not the last call's

    turns = [t for t in _good_turns() if not t.label.startswith("9-count")]
    result = _judge(turns=turns)["counter"]
    assert result["ok"] is False and result["evidence"]["counts"] == []


def test_the_counter_survives_retention_pruning_its_history() -> None:
    """#139: `2026-09-11-turn2-run-1` invoked its counter twice, got 1 then 2, and
    the reaper pruned the first invocation's checkpoint nine seconds later, so the
    catalog reported one row for two invocations. Every durable fact still holds,
    and the postcondition must pass on them."""
    final = _good_final()
    final["catalog"]["counter"]["chain_length"] = 1  # the older row is gone
    result = _judge(final=final)["counter"]
    assert result["ok"] is True
    assert result["evidence"]["counts"] == [1, 2]
    assert result["evidence"]["chain_heads"] == ["k1", "k2"]
    assert result["evidence"]["final_chain_head"] == "k2"


def test_a_trials_recipe_is_declared() -> None:
    final = _good_final()
    final["trials"] = [{"id": "t-1", "verb": "probe", "status": "done", "recipe_id": "r-trial"}]
    result = _judge(final=final, recipes_after={"pre", "brain", "r1", "r2", "r3", "r-trial"})
    assert result["no_undeclared_capability"]["ok"] is True


def test_no_undeclared_capability_watches_the_catalog_the_offer_and_the_recipes() -> None:
    assert _judge(recipes_after={"pre", "brain", "r1", "r2", "r3", "stray"})[
        "no_undeclared_capability"
    ] == {
        "ok": False,
        "evidence": {
            "catalog": ["counter", "page_title", "verify_ssh"],
            "not_ready": [],
            "unproposed": [],
            "unexpected_tools": [],
            "undeclared_recipes": ["stray"],
        },
    }
    final = _good_final()
    final["catalog"]["extra"] = {
        "status": "ready",
        "state": "ephemeral",
        "chain_length": 0,
        "recipe_id": "r9",
    }
    assert _judge(final=final)["no_undeclared_capability"]["evidence"]["unproposed"] == ["extra"]
    final = _good_final()
    final["catalog"]["counter"]["status"] = "building"
    assert _judge(final=final)["no_undeclared_capability"]["evidence"]["not_ready"] == ["counter"]
    turns = _good_turns()
    turns[3].audit["offered"] = ["propose", "remember", "shell", "try"]
    assert _judge(turns=turns)["no_undeclared_capability"]["evidence"]["unexpected_tools"] == [
        "shell"
    ]


def test_nothing_by_hand_is_the_command_list() -> None:
    assert _judge()["nothing_by_hand"] == {
        "ok": True,
        "evidence": {
            "commands": {"api say": 1, "api list": 1, "api approve": 1, "ingress say": 1},
            "provisions": 0,
            "provides": 0,
            "by_hand": [],
        },
    }
    assert _judge(sent=[("api", "upload")])["nothing_by_hand"]["ok"] is False


def test_a_count_turn_needs_a_chain_head_to_count() -> None:
    turns = _good_turns()
    turns[4] = _turn(
        "9-count-1",
        "ingress",
        audit_line(
            door="ingress",
            principal="ssh:mike",
            tools=[{"name": "page_title", "computer_id": "c8"}],
        ),
    )
    result = _judge(turns=turns)["counter"]
    assert result["ok"] is False and result["evidence"]["computer_ids"] == ["c9b"]


def _counted(label: str, tools: list[dict[str, Any]], reply: str) -> Turn:
    return _turn(
        label, "ingress", audit_line(door="ingress", principal="ssh:mike", tools=tools), reply
    )


def test_the_counter_passes_when_the_model_verifies_its_verb_within_one_turn() -> None:
    """#117, from live run 2026-09-10-run-4: the model called its counter twice in
    `9-count-1` to prove that state crossed the chain, so the invocations returned
    1, 2, 3 and the chain grew to three. What the postcondition tests is that the
    counter is monotonic and the chain grows once per invocation, not the numerals."""
    turns = _good_turns()
    turns[4] = _counted(
        "9-count-1",
        [
            {"name": "counter", "computer_id": "c9a", "chain_head": "k1"},
            {"name": "counter", "computer_id": "c9a2", "chain_head": "k2"},
        ],
        "1, then 2 on a different computer",
    )
    turns[5] = _counted(
        "9-count-2", [{"name": "counter", "computer_id": "c9b", "chain_head": "k3"}], "3"
    )
    checks = _good_checks()
    checks["c9a2"] = {"computer_id": "c9a2", "gone": True, "stdout": "2\n", "exit_code": 0}
    checks["c9b"]["stdout"] = "called 3 times\n"
    final = _good_final()
    final["catalog"]["counter"]["chain_length"] = 3
    final["catalog"]["counter"]["chain_head"] = "k3"  # the third invocation's head

    result = _judge(turns=turns, checks=checks, final=final)["counter"]

    assert result["ok"] is True
    assert result["evidence"]["counts"] == [1, 2, 3]
    assert result["evidence"]["chain_heads"] == ["k1", "k2", "k3"]


def test_the_counter_fails_when_an_invocation_is_lost() -> None:
    """The stricter half of #117: counting every invocation catches a chain that
    skipped one, which the old `counts == [1, 2]` would have passed on two calls."""
    turns = _good_turns()
    turns[4] = _counted(
        "9-count-1",
        [
            {"name": "counter", "computer_id": "c9a", "chain_head": "k1"},
            {"name": "counter", "computer_id": "c9a2", "chain_head": "k2"},
        ],
        "1, then 3",
    )
    checks = _good_checks()
    checks["c9a2"] = {"computer_id": "c9a2", "gone": True, "stdout": "3\n", "exit_code": 0}
    checks["c9b"]["stdout"] = "called 4 times\n"
    final = _good_final()
    final["catalog"]["counter"]["chain_length"] = 3

    result = _judge(turns=turns, checks=checks, final=final)["counter"]

    assert result["ok"] is False
    assert result["evidence"]["counts"] == [1, 3, 4]


def test_nothing_by_hand_allows_one_provisioning_sequence_per_provide() -> None:
    """Spec §7.3: root's by-hand acts are allowed when they are exactly the
    provisioning sequence and there is one such sequence per `provide`."""
    final = {"policy": {"principals": {}}, "catalog": {}, "proposals": []}
    ok = _judged(
        [],
        final,
        sent=[
            ("api", "say"),
            ("api", "list"),
            ("api", "approve"),
            ("api", "create"),
            ("api", "upload"),
            ("api", "checkpoint"),
            ("api", "destroy"),
            ("api", "provide"),
            ("ingress", "say"),
        ],
    )
    verdict = CHECKS["nothing_by_hand"](ok)
    assert verdict["ok"] is True
    assert verdict["evidence"]["provisions"] == 1 and verdict["evidence"]["provides"] == 1
    assert verdict["evidence"]["by_hand"] == []
    # a provide with no sequence behind it, or a sequence with no provide, is by hand
    for sent in (
        [("api", "provide")],
        [("api", "create"), ("api", "upload"), ("api", "checkpoint"), ("api", "destroy")],
        # the sequence must be contiguous and whole
        [
            ("api", "create"),
            ("api", "list"),
            ("api", "upload"),
            ("api", "checkpoint"),
            ("api", "destroy"),
            ("api", "provide"),
        ],
        [("api", "create"), ("api", "upload"), ("api", "destroy"), ("api", "provide")],
        # any other command is by hand, as today
        [("api", "exec")],
    ):
        assert CHECKS["nothing_by_hand"](_judged([], final, sent=sent))["ok"] is False, sent
    broken = CHECKS["nothing_by_hand"](
        _judged([], final, sent=[("api", "create"), ("api", "list")])
    )
    assert broken["evidence"]["by_hand"] == ["create"]


def test_a_turn_carries_what_root_provided_for_it() -> None:
    turn = Turn("11", "ingress", "w", {}, "", [], [])
    assert turn.provisions == []
    turn.provisions.append(
        {
            "verb": "secret_page",
            "name": "page_token",
            "path": "/verb/token",
            "checkpoint": "ck-1",
            "result": "secret_page: page_token provided (1/1)",
        }
    )
    assert turn.provisions[0]["path"] == "/verb/token"


def test_the_counter_counts_the_invocations_of_row_nines_continuations() -> None:
    """Live `2026-09-15-run-1`: after row 9's proposal settled, the driver's
    `9-continue-1` delivered the build and the model invoked its new counter twice
    to check it, so `9-count-1` read 3 and the window that looked only at the two
    count rows saw `[3]` and called a working counter a miss. A continuation is a
    turn the driver spoke, not a gap in the evidence: its invocations are counted
    in the order they happened, and the last one seen names the verb whose head
    the catalog must agree with."""
    turns = _good_turns()
    turns.insert(
        4,
        _counted(
            "9-continue-1",
            [
                {"name": "counter", "computer_id": "c9x", "chain_head": "k1"},
                {"name": "counter", "computer_id": "c9y", "chain_head": "k2"},
            ],
            "1, then 2 — it holds across calls",
        ),
    )
    turns[5] = _counted(
        "9-count-1", [{"name": "counter", "computer_id": "c9a", "chain_head": "k3"}], "3"
    )
    turns[6] = _counted("9-count-2", [], "4")  # answered from what it already knew
    checks = _good_checks()
    checks["c9x"] = {"computer_id": "c9x", "gone": True, "stdout": "1\n", "exit_code": 0}
    checks["c9y"] = {"computer_id": "c9y", "gone": True, "stdout": "2\n", "exit_code": 0}
    checks["c9a"]["stdout"] = "3\n"
    final = _good_final()
    final["catalog"]["counter"]["chain_length"] = 3
    final["catalog"]["counter"]["chain_head"] = "k3"

    result = _judge(turns=turns, checks=checks, final=final)["counter"]

    assert result["ok"] is True
    assert result["evidence"]["counts"] == [1, 2, 3]
    assert result["evidence"]["chain_heads"] == ["k1", "k2", "k3"]
    assert result["evidence"]["final_chain_head"] == "k3"
