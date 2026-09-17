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

import contextlib
import json
import re
from typing import TYPE_CHECKING, Any

import httpx
from membrane.capability import FENCED_RE, sse_stdout, upload
from membrane.postconditions import by_label

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import TextIO

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


def chain_for(command: str, final: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """The chain the command runs on, and every chain it could have been: the one
    whose verb name the command names, or the only one there is. Two candidates and
    nothing to choose between them is not guessed at.

    Root's own path is blanked out of the command before any name is looked for in
    it. Row 20's words hand the agent `/tmp/amounts`, so every command it can name
    carries that path, and a verb named `amounts` would be named by all of them --
    longer than `total`, so it would win the rule below and send `verify` to fork a
    chain the agent's program is not on, failing a correct agent with a record that
    names a chain and reads as authoritative. `command_in` keeps off the same path
    for the same reason (line 79). Matching the first token instead would be wrong
    the other way: the program token of `python3 /work/total.py /tmp/amounts` is
    `python3`, which names no verb at all.

    When several names still appear the longest wins, and only a tie in length is
    ambiguity. Verbs `total` and `subtotal` are both named by
    `/verb/subtotal.sh /tmp/amounts`, since one name is a substring of the other;
    calling that ambiguous would cost both exercises for a command that named its
    chain exactly. `sum`/`checksum` and `total`/`totals` are the same shape.

    Only a verb declared `state: chain` is a candidate. Every catalog entry
    carries a `chain` name -- `embryo/membrane/declarations.py:215` defaults it
    from the verb's name -- but an ephemeral verb never checkpoints onto it, so
    reading `chain` alone would offer a label with no head as a candidate and
    turn a one-chain run into an ambiguous one.

    The candidates are the distinct chains and not the verbs holding them: two
    verbs may declare one `chain`, and counting names would report the single
    chain there is as impossible to choose and then offer it twice in the record.

    They come back beside the choice because the caller must tell two absences
    apart in the record: a catalog holding no chain verb at all is the agent
    having shipped nothing that persists (design §7), which is a different finding
    from an apparatus that could not choose among several."""
    chains: dict[str, str] = {
        name: entry["chain"]
        for name, entry in (final.get("catalog") or {}).items()
        if entry.get("chain") and entry.get("state") == "chain"
    }
    candidates = sorted(set(chains.values()))
    without_path = command.replace(PROBE_PATH, " ")
    named = sorted((name for name in chains if name in without_path), key=len, reverse=True)
    if named and (len(named) == 1 or len(named[0]) > len(named[1])):
        return chains[named[0]], candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


async def probe_fork(doors: Any, checkpoint: str, command: str, results: dict[str, Any]) -> None:
    """Fork `checkpoint`, write each of the three files of amounts to `PROBE_PATH`
    on the fork and run `command` over it, then destroy the fork. No checkpoint is
    taken, so the chain is left exactly as the run left it.

    `results` is filled in place rather than returned: a host that breaks on the
    second probe has still told root what the first one did, and `verify` records
    that beside the failure."""
    forked = await doors.api.post(f"/checkpoints/{checkpoint}/fork", json={})
    forked.raise_for_status()
    computer_id = str(forked.json()["computer_id"])
    try:
        for name, body in AMOUNTS:
            await upload(doors, computer_id, PROBE_PATH, body.encode())
            ran = await doors.api.post(
                f"/computers/{computer_id}/exec",
                json={"command": command, "timeout_seconds": 120},
                timeout=180.0,
            )
            ran.raise_for_status()
            out, code = sse_stdout(ran.text)
            results[name] = {"exit_code": code, "stdout": out, "totals": totals_in(out)}
    finally:
        with contextlib.suppress(httpx.HTTPError):
            await doors.api.delete(f"/computers/{computer_id}")


async def verify(doors: Any, turns: list[Any], final: Mapping[str, Any], log: TextIO) -> None:
    """Root uses the program himself: the command row 20's reply named, run on a
    fork of the head of the verb chain it runs on, over amounts the script never
    spoke.

    `probes` is one flat dict and a check reads it key by key. `command` is there
    once row 20 named one, `chain` once a chain was chosen, `checkpoint` once that
    chain had a head, `error` whenever anything went wrong, and `candidates` --
    the distinct chains the catalog offered, empty when it offered none -- only on
    the finding that chose no chain.

    `results` is there on every finding but three: row 20 naming no command, no
    single chain to run it on, and a chain with no head. Those three stopped
    before a fork could be attempted, and say so by leaving the key out. Every
    other finding has `results`, and it may be empty: a `GET /checkpoints` that
    answered 500, or a fork that failed, leaves `{}` beside the `error` having run
    nothing. So `"results" in probes` is not "the probes ran", and a check reading
    it that way takes a host hiccup for a program that printed nothing --
    `runs_again` and `fixed` read `probes.get("results") or {}` and judge on what
    is in it. Each entry is `{"exit_code": int | None, "stdout": str, "totals":
    list[float]}` under its name from `AMOUNTS`, in the order `AMOUNTS` lists
    them, stopping wherever the host broke.

    `exit_code` is `None` when the stream carried no `event: exit` --
    `sse_stdout` (`embryo/membrane/capability.py:93`) only assigns one if it
    arrives. A check must test it with `isinstance(code, int)` before comparing:
    `fixed` asks that the word file exited nonzero, and `None != 0` is true, so
    a truncated stream would otherwise read as the program correctly refusing a
    line it never saw.

    A probe that dies is a finding and not an absence: `run_verify` in the driver
    (`embryo/membrane/capability.py:1775`) swallows what escapes here into its
    log, and that log is not the committed record, so an error that left `probes`
    empty would reach `runs_again` as a program that does not run. Every way out
    of here writes into `probes` before it writes a line to `log`, in that order:
    `log.write` raises too when the disk is full, and a handler that logged first
    left behind the very empty `probes` this net exists to prevent."""
    probes.clear()
    found: dict[str, Any] = {}
    results: dict[str, Any] = {}
    # Wide on purpose, the way `security.keep_alive` is: a probe's own failure has
    # to become a legible finding, and anything narrower leaves `probes` empty for
    # a check to misread as "the agent's program does not run". `doors.head`
    # raises `httpx` errors of its own, and a fork answering 200 with a body of
    # another shape raises `KeyError`, which is neither `HTTPError` nor
    # `ValueError`. Cancellation is a `BaseException` and still passes through.
    try:
        twenty = by_label(turns, "20")
        command = command_in(twenty.reply if twenty else "")
        if not command:
            probes["error"] = "row 20 named no command"
            log.write("row 20 named no command; nothing to probe\n")
            return
        found["command"] = command
        chain, candidates = chain_for(command, final)
        if chain is None:
            probes.update(
                {
                    **found,
                    "error": (
                        f"the catalog holds no chain verb to run {command!r} on"
                        if not candidates
                        else f"several chain verbs, and {command!r} singles out none of them"
                    ),
                    "candidates": candidates,
                }
            )
            log.write(f"no single chain to run {command!r} on: {candidates}\n")
            return
        found["chain"] = chain
        head = await doors.head(chain)
        if head is None:
            probes.update({**found, "error": f"{chain} has no head"})
            log.write(f"{chain} has no head to fork\n")
            return
        found["checkpoint"] = str(head["id"])
        await probe_fork(doors, found["checkpoint"], command, results)
        line = f"probed {chain} at {found['checkpoint']}: {json.dumps(results)}\n"
    except Exception as exc:
        found["error"] = f"{type(exc).__name__}: {exc}"
        line = f"the probes broke: {type(exc).__name__}: {exc}\n"
    probes.update({**found, "results": results})
    log.write(line)
