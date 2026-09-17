"""The web-search capability's apparatus (design §5, §6).

The credential is the operator's, not the run's. `prepare` reads the provider's
endpoint and key the way `load_run_settings` reads the driver's own -- the `.env`
beside the process, under the environment -- and refuses to start without them: a
run that would place nothing must not start. The key reaches the rows as the
context's `token`, which is what the driver provisions onto the verb's chain and
what it later inspects the brain for.

The page row 14 reads is this capability's own (`membrane.page`), served openly
on a computer of its own, so that reading a page is judged against a body this
repository controls. `searched` is the only check that touches the live web, and
it asserts the shape of an answer and the provenance of the call, never what the
web said: a postcondition that asserts the content of a search result is one that
fails when the web changes.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from membrane.config import parse_env
from membrane.page import serve
from membrane.postconditions import CHECKS, Judged, by_label, tool_computers, trial_runs

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from typing import TextIO

# The operator's, read from where the driver reads its own keys (`--env`'s
# default). Which provider is a choice that appears in exactly two places: this
# variable's value and the `{url}` the rows speak.
ENV_FILE = Path(".env")
ENDPOINT_ENV = "SEARCH_API_URL"
KEY_ENV = "SEARCH_API_KEY"
QUERY_ENV = "SEARCH_QUERY"
DEFAULT_QUERY = "what is a Firecracker microVM"
PAGE_BODY = "The page anyone may read says: perfect number 33550336.\n"
# Any http(s) URL. A result set is recognised by carrying one, never by a field
# name: a check that knew the provider's field names is a check that has to
# change with the provider.
URL_RE = re.compile(r"https?://\S+")
# Enough of the verb's output to see why a judgement went the way it did, without
# a search response's whole JSON body in the run's summary.
STDOUT_EVIDENCE = 2000


@asynccontextmanager
async def prepare(doors: Any, log: TextIO) -> AsyncIterator[Mapping[str, str]]:
    values = parse_env(ENV_FILE.read_text()) if ENV_FILE.exists() else {}
    values.update({k: v for k, v in os.environ.items() if k in (ENDPOINT_ENV, KEY_ENV, QUERY_ENV)})
    missing = [name for name in (ENDPOINT_ENV, KEY_ENV) if not values.get(name)]
    if missing:
        raise RuntimeError(
            f"missing {', '.join(missing)}: put them in {ENV_FILE} or the environment"
        )
    async with serve(doors, log, PAGE_BODY, token=None) as page:
        yield {
            "url": values[ENDPOINT_ENV],
            "token": values[KEY_ENV],
            "query": values.get(QUERY_ENV) or DEFAULT_QUERY,
            "page": page,
        }


def _first_json(text: str) -> Any:
    """The first JSON document in `text`, or None. The verb is the agent's own
    build and may print anything around its answer, so the check finds the
    document rather than demanding the output be one."""
    decoder = json.JSONDecoder()
    for i, character in enumerate(text):
        if character in "{[":
            try:
                value, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            return value
    return None


def _results(stdout: str) -> list[dict[str, Any]]:
    """Every mapping in the verb's output that carries a URL: a result shape,
    named by no provider."""
    found: list[dict[str, Any]] = []
    stack: list[Any] = [_first_json(stdout)]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if any(isinstance(v, str) and URL_RE.fullmatch(v) for v in node.values()):
                found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _needed_the_key(j: Judged, turn_label: str) -> dict[str, Any]:
    """What a row shows of an attempt made before the key was there.

    Two things count, and the row asks for neither by name:

    `refusals` -- the provider's 401 or 403, from the exec log of every computer
    the row ran on, invocations and trials alike, with the row's reply as the
    fallback for a model that reported the refusal without leaving a log behind.

    `blocked` -- a trial of a verb on its own chain that exited non-zero. This is
    what run-1 produced and what the first draft of this check could not accept.
    The agent's verb read its secret path, found nothing there, and exited 3 before
    it ever reached the provider; there is no HTTP status to find because the verb
    is a good one. Demanding the 401 demands a verb that sends an unauthenticated
    request, which is the opposite of what this capability is about."""
    turn = by_label(j.turns, turn_label)
    trials = trial_runs(turn)
    logs = [
        j.checks.get(call["computer_id"], {}).get("stdout") or ""
        for call in [*tool_computers(turn), *trials]
    ]
    logs.append(turn.reply if turn else "")
    return {
        "refusals": [code for code in ("401", "403") if any(code in text for text in logs)],
        "blocked": [
            run["computer_id"]
            for run in trial_runs(turn, chain=True)
            if run.get("exit_code") not in (0, None)
        ],
    }


def searched(j: Judged) -> dict[str, Any]:
    """Design §6: row 12's reply carries results; the call ran on the search verb's
    chain from a computer that is now gone; its output holds a result shape with
    at least one entry carrying a URL; and the key was needed -- row 11's record
    holds an attempt made before the key was there that could not succeed.

    That last clause is what separates "the key was used" from "the verb happened
    to work". It asks the agent for something row 11 invites and does not command:
    that it try the verb before asking for the key. What the attempt *looks* like
    is the agent's business -- the provider's 401, or the verb's own refusal to run
    without its secret -- and `_needed_the_key` takes either."""
    twelve = by_label(j.turns, "12")
    calls = tool_computers(twelve, chain=True)
    call = calls[0] if calls else None
    check = j.checks.get(call["computer_id"], {}) if call else {}
    stdout = check.get("stdout") or ""
    results = _results(stdout)
    tried = _needed_the_key(j, "11")
    reply = twelve.reply if twelve else ""
    return {
        "ok": bool(call)
        and check.get("gone") is True
        and bool(results)
        and bool(URL_RE.search(reply))
        and bool(tried["refusals"] or tried["blocked"]),
        "evidence": {
            "reply": reply.strip() if twelve else None,
            "computer_id": call["computer_id"] if call else None,
            "chain_head": call.get("chain_head") if call else None,
            "gone": check.get("gone"),
            "results": len(results),
            "first_result": results[0] if results else None,
            "stdout": stdout[:STDOUT_EVIDENCE],
            "tried_before_the_key": tried,
        },
    }


def read_page(j: Judged) -> dict[str, Any]:
    """Row 14 produced this capability's own fixed body, from a computer that is
    gone: `page_title`'s structure against a page this repository serves.

    The design (§6) said `secret_page`'s structure -- the same, and a chain call.
    That clause is dropped here, because it would judge an implementation choice
    row 13 never asks for. A chain is how a *secret* survives an invocation, and
    row 13's verb holds none; the natural answer to "read the page at a URL I give
    it" is an ephemeral verb, as hatch's `page_title` is, and a postcondition that
    fails such a run is testing the model's taste rather than the capability."""
    fourteen = by_label(j.turns, "14")
    calls = tool_computers(fourteen)
    call = calls[0] if calls else None
    check = j.checks.get(call["computer_id"], {}) if call else {}
    reply = fourteen.reply if fourteen else ""
    body = PAGE_BODY.strip()
    return {
        "ok": bool(call)
        and body in reply
        and check.get("gone") is True
        and body in (check.get("stdout") or ""),
        "evidence": {
            "reply": reply.strip() if fourteen else None,
            "computer_id": call["computer_id"] if call else None,
            "chain_head": call.get("chain_head") if call else None,
            "gone": check.get("gone"),
            "stdout": check.get("stdout"),
        },
    }


CHECKS["searched"] = searched
CHECKS["read_page"] = read_page
