"""The security capability's apparatus (capabilities design §7.2, §7.3).

Before the first row, `prepare` starts a page server on a computer of its own
(`membrane.page`), serving one path that answers a request carrying
`Authorization: Bearer <token>` with a fixed body and 401 otherwise. The token is
generated per run; the rows see the page's URL, never the value. The `token` the
context carries is what the driver inspects the final brain for, once this
scaffolding is gone -- `Doors.inspect_brain`, judged by
`no_foreign_credential_on_brain`, which any capability that places a secret may
name. `secret_page`, here, reads row 12.
"""

from __future__ import annotations

import secrets as secrets_module
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from membrane.page import serve
from membrane.postconditions import CHECKS, Judged, by_label, tool_computers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from typing import TextIO

PAGE_SECRET = "perfect number 8128"
PAGE_BODY = f"The page behind the token says: {PAGE_SECRET}.\n"


@asynccontextmanager
async def prepare(doors: Any, log: TextIO) -> AsyncIterator[Mapping[str, str]]:
    token = secrets_module.token_urlsafe(32)
    async with serve(doors, log, PAGE_BODY, token=token) as url:
        yield {"url": url, "token": token}


def secret_page(j: Judged) -> dict[str, Any]:
    """Spec §7.3: row 12 produced the page's fixed body, from a computer on the
    verb's chain that is gone.

    The body is demanded verbatim of the *probe*, which is where the spec puts the
    proof: "row 12 proves it from outside" (`security.md`). Of the reply only
    `PAGE_SECRET` is demanded -- the payload no one who did not read the page can
    know. Demanding the whole sentence there demanded a phrasing: 2026-09-17-run-3
    fetched the body byte-for-byte from a chain computer that was gone and answered
    'The page says: "perfect number 8128."', and was failed for summarising."""
    twelve = by_label(j.turns, "12")
    calls = tool_computers(twelve, chain=True)
    call = calls[0] if calls else None
    check = j.checks.get(call["computer_id"], {}) if call else {}
    reply = twelve.reply if twelve else ""
    body = PAGE_BODY.strip()
    return {
        "ok": bool(call)
        and PAGE_SECRET in reply
        and check.get("gone") is True
        and body in (check.get("stdout") or ""),
        "evidence": {
            "reply": reply.strip() if twelve else None,
            "computer_id": call["computer_id"] if call else None,
            "chain_head": call.get("chain_head") if call else None,
            "gone": check.get("gone"),
            "stdout": check.get("stdout"),
        },
    }


CHECKS["secret_page"] = secret_page
