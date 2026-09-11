"""What a reply says about ids the membrane issued (#121). `p-N` and `t-N` are
the membrane's own grammar: the model never mints one, so a reply that names
an id that does not exist is a lookup, not a judgement. The membrane never
edits the reply. It appends a fact to the inbox for the next turn, in the
voice of a build result, and the next turn reconciles as it would with a
failed build. A fact degrades gracefully when the match is a false positive
(a hypothetical, a past tense): "p-3 does not exist" is then simply true.

Verb names and claims about the inbox or the substrate are not checked: they
have to be parsed out of prose, which is judgement."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from membrane.state import State

ID = re.compile(r"\b([pt])-(\d+)\b", re.IGNORECASE)
KIND = {"p": "proposal", "t": "trial"}


def _key(ident: str) -> tuple[str, int]:
    kind, number = ident.split("-", 1)
    return kind, int(number)


def unknown_references(text: str, state: State) -> list[str]:
    """The ids `text` names that neither `state.proposals` nor `state.trials`
    holds, each once, proposals before trials and in numeric order."""
    found = {m.group(0).lower() for m in ID.finditer(text)}
    return sorted(
        (i for i in found if i not in state.proposals and i not in state.trials), key=_key
    )


def _range(ids: list[str], kind: str) -> str:
    plural = f"{kind}s"
    if not ids:
        return f"there are no {plural}"
    ordered = sorted(ids, key=_key)
    if len(ordered) == 1:
        return f"the {plural} are {ordered[0]}"
    return f"the {plural} are {ordered[0]} to {ordered[-1]}"


def describe(*, turn: int, unknown: list[str], state: State) -> str:
    """The inbox item for a turn whose reply named ids that do not exist."""
    kinds = sorted({KIND[i[0]] for i in unknown}, key=list(KIND.values()).index)
    exists = " or ".join(kinds)
    parts = [f"your reply on turn {turn} named {', '.join(unknown)}; no such {exists} exists"]
    if "proposal" in kinds:
        parts.append(_range(list(state.proposals), "proposal"))
    if "trial" in kinds:
        parts.append(_range(list(state.trials), "trial"))
    return "; ".join(parts)
