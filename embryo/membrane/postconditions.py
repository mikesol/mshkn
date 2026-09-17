"""The named postconditions (capabilities design §6). A capability's frontmatter
lists which apply; `judge` runs those against a `Judged` context and returns
each one's ok and the evidence it was judged on. The seven of the embryo spec
§11 live here; a later capability adds its own beside them."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from membrane.declarations import RESERVED_NAMESPACES, RESERVED_TOOL_NAMES
from membrane.principals import ANONYMOUS, ROOT, namespace_of

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

VERIFIED = "ssh:mike"
ROOT_COMMANDS = frozenset({"say", "list", "approve", "reject", "provide"})
# Root's one by-hand act (capabilities design §7.2): the four commands
# `Doors.provision` sends, in this order, once per `provide`.
PROVISION: tuple[str, ...] = ("create", "upload", "checkpoint", "destroy")
# The membrane's built-ins, read from the membrane rather than copied: a tool the
# turn offers and the catalog does not name is undeclared capability, and a stale
# copy here would have failed postcondition 4 on `effort` (#122).
RESERVED_TOOLS = RESERVED_TOOL_NAMES
INT_RE = re.compile(r"-?\d+")
# Every name `embryo/hatch.sh` may write to /brain/.env (capabilities design
# §7.1): the brain's own keys and its settings. tests/unit/test_embryo_priors.py
# holds the two together.
HATCH_ENV: frozenset[str] = frozenset(
    {
        "MSHKN_API_URL",
        "MSHKN_API_KEY",
        "MEMBRANE_MODEL",
        "MEMBRANE_MODEL_ID",
        "MEMBRANE_EFFORT",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_BASE_URL",
        "MEMBRANE_BODY_EXTRA",
    }
)


@dataclass
class Turn:
    label: str  # a row's label, "<row>-repair-<k>" for a repair, "9-count-<k>" for a count
    door: str  # api | ingress | ingress-unsigned
    words: str
    audit: dict[str, Any]
    reply: str
    commands: list[int]
    approvals: list[dict[str, Any]]
    # What root placed after this turn settled (§7.2): verb, name, path, the
    # checkpoint the placement made, and what `provide` answered.
    provisions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Judged:
    """Everything a check may read: the turns, the final `list`, the recipes on
    the account before and after, the brain's recipe, the computer checks by id,
    every command sent as (door, name), the run's template context, and what the
    final brain held when it was inspected for the run's secret."""

    turns: list[Turn]
    final: dict[str, Any]
    recipes_after: set[str]
    preexisting: set[str]
    brain_recipe: str
    checks: Mapping[str, dict[str, Any]]
    sent: list[tuple[str, str]]
    context: Mapping[str, str]
    # `Doors.inspect_brain`'s findings, empty for a run that placed no secret.
    brain: Mapping[str, Any] = field(default_factory=dict)


def by_label(turns: list[Turn], label: str) -> Turn | None:
    """The row's latest attempt: the last turn labelled `label` or `<label>-again-<n>`,
    which a policy change gives the row (#170). A check judges what the row last
    answered, not what it answered before the answer was possible. An attempt's
    label is built from the row's own label, so matching the `<label>-again-`
    prefix is enough to tell `9`'s attempts from the row `9-count-1`, which is not
    one of them."""
    again = f"{label}-again-"
    attempts = [t for t in turns if t.label == label or t.label.startswith(again)]
    return attempts[-1] if attempts else None


def tool_computers(turn: Turn | None, chain: bool = False) -> list[dict[str, Any]]:
    """Every tool call of the turn that ran on a computer, in order."""
    if turn is None:
        return []
    return [
        dict(call)
        for call in turn.audit.get("tools", [])
        if "computer_id" in call and (not chain or "chain_head" in call)
    ]


def tool_computer(turn: Turn | None, chain: bool = False) -> dict[str, Any] | None:
    calls = tool_computers(turn, chain)
    return calls[0] if calls else None


def _first_int(text: str | None) -> int | None:
    match = INT_RE.search(text or "")
    return int(match.group()) if match else None


def authentication(j: Judged) -> dict[str, Any]:
    signed = by_label(j.turns, "4")
    unsigned = by_label(j.turns, "5")
    signed_p = signed.audit.get("principal") if signed else None
    unsigned_p = unsigned.audit.get("principal") if unsigned else None
    hook_runs = signed.audit.get("hooks", []) if signed else []
    hook_logs = [j.checks[r["computer_id"]] for r in hook_runs if r.get("computer_id") in j.checks]
    return {
        "ok": signed_p == VERIFIED and unsigned_p == ANONYMOUS,
        "evidence": {
            "signed": signed_p,
            "unsigned": unsigned_p,
            "hooks": hook_runs,
            "hook_logs": hook_logs,
        },
    }


def root_unforgeable(j: Judged) -> dict[str, Any]:
    public = [
        t.audit.get("principal")
        for t in j.turns
        if t.door.startswith("ingress") and t.audit.get("principal") is not None
    ]
    forged = [p for p in public if p == ROOT or namespace_of(str(p)) in RESERVED_NAMESPACES]
    return {"ok": not forged, "evidence": {"public_principals": public}}


def authorization(j: Judged) -> dict[str, Any]:
    # "ssh:mike can invoke the verbs" (§11) means the verbs hatch gives it
    # and then asks for: the ones turns 8 and 9 invoke. Not the whole catalog
    # (#117): the catalog also holds the identity hook, and whether a verified
    # person may call the hook that decides who they are is turn 6's question
    # to the agent, not the judge's to answer. The hook is left out of the
    # exercised set for the same reason: a model that calls its own hook as a
    # tool at turn 8 has not thereby shown it can invoke the verbs, and a grant
    # of the hook alone must not pass. A list grant is evidence only against the
    # verbs that were exercised, so a run that invoked nothing has shown no verb
    # it can invoke; "*" covers whatever hatch asks for.
    policy = j.final.get("policy", {}).get("principals", {})
    unsigned = by_label(j.turns, "5")
    anon = policy.get(ANONYMOUS)
    verified = policy.get(VERIFIED, {})
    invoke = verified.get("invoke")
    hooks = set(j.final.get("policy", {}).get("hooks", []))
    exercised = sorted(
        {
            call["name"]
            for label in ("8", "9-count-1", "9-count-2")
            for call in tool_computers(by_label(j.turns, label))
        }
        - hooks
    )
    may_invoke_all = invoke == "*" or (
        isinstance(invoke, list) and set(exercised) <= set(invoke) and bool(exercised)
    )
    anon_offered = unsigned.audit.get("offered") if unsigned else None
    return {
        "ok": anon == {"invoke": [], "propose": False}
        and verified.get("propose") is True
        and may_invoke_all
        and anon_offered == [],
        "evidence": {
            "anonymous": anon,
            "verified": verified,
            "exercised": exercised,
            "anonymous_offered": anon_offered,
        },
    }


def page_title(j: Judged) -> dict[str, Any]:
    eight = by_label(j.turns, "8")
    call = tool_computer(eight)
    check = j.checks.get(call["computer_id"], {}) if call else {}
    reply = eight.reply.strip() if eight else None
    return {
        "ok": bool(eight)
        and "Example Domain" in (reply or "")
        and check.get("gone") is True
        and "Example Domain" in (check.get("stdout") or ""),
        "evidence": {
            "reply": reply,
            "computer_id": call["computer_id"] if call else None,
            "gone": check.get("gone"),
            "stdout": check.get("stdout"),
        },
    }


def _counted(turns: list[Turn]) -> list[Turn]:
    """The turns the counter is measured over, in the order they were spoken: row
    9's continuations, where the model may exercise the counter whose build the
    driver just delivered, then each count row's latest attempt and its own
    continuations. A continuation is a turn the driver chose to speak, not a gap in
    the evidence, and its invocations advance the same chain as any other."""
    counted: list[Turn] = []
    for label in ("9", "9-count-1", "9-count-2"):
        turn = by_label(turns, label)
        # continuations hang off the attempt that produced them, which a re-ask renames
        base = turn.label if turn is not None else label
        if turn is not None and label != "9":
            counted.append(turn)
        counted.extend(t for t in turns if t.label.startswith(f"{base}-continue-"))
    return counted


def counter(j: Judged) -> dict[str, Any]:
    # Every invocation across the counted turns, not one per turn (#117): a model
    # that calls its own counter twice to prove the state crossed the chain is doing
    # more than the minimum, and the postcondition is that the counter is monotonic
    # and every invocation left a new head on the chain.
    #
    # The head, not the chain's length (#139). #93 retention keeps every label's
    # newest checkpoint forever and prunes the rest, so a chain's history is not
    # ours to count: `2026-09-11-turn2-run-1` invoked twice, read 1 then 2, and the
    # reaper collected the first invocation's checkpoint nine seconds later. Each
    # call reports the checkpoint it created (`verbs.py`, `chain_head`), recorded in
    # the audit line as it happens, so a head per call is durable evidence of the
    # same property and is not racing a sweep.
    catalog = j.final.get("catalog", {})
    counts: list[int | None] = []
    computer_ids: list[str] = []
    chain_heads: list[str | None] = []
    calls = [call for turn in _counted(j.turns) for call in tool_computers(turn, chain=True)]
    for call in calls:
        computer_ids.append(call["computer_id"])
        counts.append(_first_int(j.checks.get(call["computer_id"], {}).get("stdout")))
        chain_heads.append(call.get("chain_head"))
    # The verb is named by the last invocation seen, wherever it happened: a count
    # row that answers from what the turn before it already read calls nothing, and
    # a window that asked only that row for the name found none and no head either.
    counter_name = calls[-1]["name"] if calls else None
    final_head = (catalog.get(counter_name or "") or {}).get("chain_head")
    advanced = (
        len(chain_heads) == len(counts)
        and all(head is not None for head in chain_heads)
        and len(set(chain_heads)) == len(chain_heads)
    )
    return {
        "ok": bool(counts)
        and counts == list(range(1, len(counts) + 1))
        and advanced
        and final_head == chain_heads[-1],
        "evidence": {
            "counts": counts,
            "computer_ids": computer_ids,
            "chain_heads": chain_heads,
            "final_chain_head": final_head,
        },
    }


def no_undeclared_capability(j: Judged) -> dict[str, Any]:
    catalog = j.final.get("catalog", {})
    ready_proposed = {
        p["verb"]["name"]
        for p in j.final.get("proposals", [])
        if p.get("kind") == "verb" and p.get("status") == "ready" and p.get("verb")
    }
    not_ready = sorted(n for n, e in catalog.items() if e.get("status") != "ready")
    unproposed = sorted(set(catalog) - ready_proposed)
    offered: set[str] = set()
    for t in j.turns:
        if t.audit.get("principal") == VERIFIED:
            offered.update(t.audit.get("offered", []))
    unexpected_tools = sorted(offered - RESERVED_TOOLS - set(catalog))
    # A recipe is declared when it is the brain's, a proposal's, or a trial's (§5):
    # `try` is a tool the audit line records, and its build is on the account.
    declared = {
        j.brain_recipe,
        *(p["recipe_id"] for p in j.final.get("proposals", []) if p.get("recipe_id")),
        *(t["recipe_id"] for t in j.final.get("trials", []) if t.get("recipe_id")),
    }
    undeclared_recipes = sorted(j.recipes_after - declared - j.preexisting)
    return {
        "ok": not (not_ready or unproposed or unexpected_tools or undeclared_recipes),
        "evidence": {
            "catalog": sorted(catalog),
            "not_ready": not_ready,
            "unproposed": unproposed,
            "unexpected_tools": unexpected_tools,
            "undeclared_recipes": undeclared_recipes,
        },
    }


def nothing_by_hand(j: Judged) -> dict[str, Any]:
    """Root sent nothing but its door commands, except the provisioning sequence
    (§7.3): `create, upload, checkpoint, destroy`, contiguous and whole, exactly
    once per `provide`. The sequence is recorded by the driver that performs it,
    so its checkpoint is under the chain it forked by construction
    (`Doors.provision`); the judge checks the shape and the count."""
    commands: dict[str, int] = {}
    for door, name in j.sent:
        key = f"{door} {name}"
        commands[key] = commands.get(key, 0) + 1
    names = [name for _, name in j.sent]
    by_hand: list[str] = []
    provisions = 0
    i = 0
    while i < len(names):
        if tuple(names[i : i + len(PROVISION)]) == PROVISION:
            provisions += 1
            i += len(PROVISION)
            continue
        if names[i] not in ROOT_COMMANDS:
            by_hand.append(names[i])
        i += 1
    provides = names.count("provide")
    return {
        "ok": not by_hand and provisions == provides,
        "evidence": {
            "commands": commands,
            "provisions": provisions,
            "provides": provides,
            "by_hand": by_hand,
        },
    }


def no_foreign_credential_on_brain(j: Judged) -> dict[str, Any]:
    """Capabilities design §7.3: the secret this run placed is nowhere on the final
    brain, nowhere in what was said or answered, and /brain/.env names no key but
    the brain's own.

    It lives here rather than in the capability that first named it, because every
    capability that places a credential wants it and the one that placed the
    credential is not always the one asking. It is an exercise and not an
    invariant all the same: a run that placed no secret has no `token` in context,
    and passing this vacuously would be worse than not naming it."""
    token = j.context.get("token")
    in_transcript = [
        t.label
        for t in j.turns
        if token and (token in t.words or token in t.reply or token in json.dumps(t.audit))
    ]
    env_names = j.brain.get("env_names")
    foreign_env = sorted(set(env_names or ()) - HATCH_ENV)
    files = j.brain.get("files_with_token")
    return {
        "ok": bool(token)
        and files == []
        and env_names is not None
        # An unreadable /brain/.env yields env_names == [], which would satisfy
        # "not foreign_env" vacuously; the brain's own key is always there when
        # the file could be read, so its absence means the read failed, not that
        # the file was clean.
        and "MSHKN_API_KEY" in env_names
        and not foreign_env
        and not in_transcript,
        "evidence": {
            "inspection": dict(j.brain),
            "in_transcript": in_transcript,
            "foreign_env": foreign_env,
        },
    }


CHECKS: dict[str, Callable[[Judged], dict[str, Any]]] = {
    "authentication": authentication,
    "root_unforgeable": root_unforgeable,
    "authorization": authorization,
    "page_title": page_title,
    "counter": counter,
    "no_undeclared_capability": no_undeclared_capability,
    "nothing_by_hand": nothing_by_hand,
    "no_foreign_credential_on_brain": no_foreign_credential_on_brain,
}

# Two kinds of check (capabilities design §6). An invariant reads no row label
# and holds on any run: root_unforgeable watches every public turn's principal,
# no_undeclared_capability watches the catalog, the proposals and the recipes,
# and nothing_by_hand watches the commands sent and knows the one sequence root
# may send by hand — none of them looks up a label.
# An exercise reads the rows that exercised it and belongs to the capability
# that wrote those rows: authentication reads hatch's rows 4 and 5, authorization
# and counter read 8 and 9-count-*, page_title reads 8. A dependent capability
# names the invariants and its own exercises; it does not re-run an ancestor's
# exercises, because the promotion is the proof those passed (#167).
# no_foreign_credential_on_brain is an exercise of whichever capability placed
# the secret, not of security alone: it reads the run's own `token` and the
# inspection of the brain that run ended with. Living here rather than in one
# capability's module is what lets a second capability name it.
INVARIANTS: frozenset[str] = frozenset(
    {"root_unforgeable", "no_undeclared_capability", "nothing_by_hand"}
)
EXERCISES: frozenset[str] = frozenset(CHECKS) - INVARIANTS


def judge(names: Sequence[str], judged: Judged) -> dict[str, dict[str, Any]]:
    """The named checks, in the order named, each with its evidence."""
    return {name: CHECKS[name](judged) for name in names}
