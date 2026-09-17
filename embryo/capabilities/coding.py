"""The coding capability's apparatus (2026-09-17-coding-design.md §6, §7).

Nothing is prepared: coding serves nothing and holds no secret. After the final
listing, `verify` reads the command row 20's reply named, forks the head of the
verb chain that command runs on, and — with the account key and through no door —
writes three files of amounts on the fork and runs the command over each. That is
root using the program himself, on a disk he reached without the agent. The
findings are `probes`, which `runs_again` and `fixed` read.

The amounts the probes use are not row 16's: a program that answered row 16 with a
constant fails here, which is the point of asking again with numbers the script
never spoke. The word is not row 17's `N/A` for the same reason: a program that
rejects only the token it was shown passes without doing what row 18 asked.
"""

from __future__ import annotations

import re
from typing import Any

from membrane.capability import FENCED_RE

INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

PROBE_PATH = "/tmp/amounts"
CLEAN = "101.25\n3\n0.75\n"
WITH_BLANKS = "101.25\n\n3\n\n0.75\n"
WITH_WORD = "101.25\npending\n0.75\n"
PROBE_TOTAL = 105.0
TOLERANCE = 0.005
AMOUNTS: tuple[tuple[str, str], ...] = (
    ("clean", CLEAN),
    ("blanks", WITH_BLANKS),
    ("word", WITH_WORD),
)

# What `verify` found, filled after the final listing and read by the checks. The
# same shape as security's `inspection`: module state, because a check is a pure
# function of `Judged` and cannot reach the account itself.
probes: dict[str, Any] = {}


def command_in(reply: str) -> str | None:
    """The command row 20 named: the first fenced block that is a single line,
    failing that the first inline code span that is not root's own path. A block
    of several lines is not a command a probe can run, and is read as none at
    all.

    A leading `$ ` is dropped from either. It is a rendering convention for shell
    blocks, not part of the command, and a probe that pasted it whole would get
    `$: not found` and exit 127 from a command that was right.

    Row 20's words carry `/tmp/amounts`, so a reply that quotes the path back in
    inline code would otherwise offer it as the command to run; `paths_in` keeps
    to paths by construction and this keeps off the one path it was handed. Only
    the model's words are read either way: the proposal documents the membrane
    appends after the reply hold an entrypoint, which is not a command root was
    given."""
    text = reply.split("\nproposal p-", 1)[0]
    # `Pattern.findall` is typed `list[Any]`, since a pattern with groups yields
    # tuples; both of these have exactly one group, so both yield strings.
    blocks: list[str] = FENCED_RE.findall(text)
    for block in blocks:
        line = block.strip()
        if line and "\n" not in line:
            return line.removeprefix("$ ")
    inline: list[str] = INLINE_CODE_RE.findall(FENCED_RE.sub("", text))
    for span in inline:
        command = span.strip().removeprefix("$ ")
        if command != PROBE_PATH:
            return command
    return None


def totals_in(stdout: str) -> list[float]:
    """Every number the program printed, in order, and not the one that looks
    like the total. Picking one loses: the last number is a trailing count in
    `Total: 105.00 (3 amounts)`, the first is a label's in `3 amounts, 105.00`.
    A caller asks instead whether any of these is the total, which is safe here
    and nowhere else: none of the probe amounts is near their total, and a total
    under 1000 carries no thousands separator, so a number that matches is the
    total and not a coincidence."""
    return [float(found) for found in NUMBER_RE.findall(stdout)]
