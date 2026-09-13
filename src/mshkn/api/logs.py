"""GET /logs: the calling account's recent records, as ECS NDJSON.

Its own module rather than a route in api/system.py, because that file's
endpoints are unauthenticated by design — they carry host facts. A log record
carries a tenant's identifiers, so this one takes the account key and returns
nothing the account did not cause.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from mshkn.api.deps import get_runtime, require_account_key
from mshkn.errors import InvalidInput

if TYPE_CHECKING:
    from mshkn.models import Account

router = APIRouter(tags=["logs"])

NDJSON = "application/x-ndjson"

# The module-level Depends that api/keys.py uses. Same reason: a call in a
# default argument is evaluated once at import, and ruff's B008 flags it inline.
_require_account_key = Depends(require_account_key)


def _timestamp(value: str, *, field: str) -> datetime:
    """Parse an ISO 8601 timestamp, treating a naive one as UTC.

    Records are written offset-aware; comparing one to a naive datetime raises,
    so a caller who drops the offset gets UTC rather than a 500.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        # A `+` in a query string decodes to a space, and every `@timestamp` in
        # the response carries one. Paging by pasting the last record's
        # timestamp into `since` is the obvious next call, so it must not 422.
        # Only reached once the plain parse has failed, which leaves the ISO
        # space-separated form ("2026-09-13 20:13:33") unaffected.
        try:
            parsed = datetime.fromisoformat(value.replace(" ", "+"))
        except ValueError:
            raise InvalidInput(f"{field} is not an ISO 8601 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@router.get("/logs")
async def read_logs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100_000)] = 100,
    since: Annotated[str | None, Query()] = None,
    account: Account = _require_account_key,
) -> Response:
    """The newest `limit` records this account caused, oldest first.

    `limit` is applied last and keeps the newest end: a truncated response drops
    the oldest records, never the most recent thing that happened.
    """
    # A snapshot. Every logging call appends to this deque, and iterating it
    # live raises "deque mutated during iteration".
    records = [
        r for r in list(get_runtime(request).logs) if r.get("mshkn.account_id") == account.id
    ]
    if since is not None:
        cutoff = _timestamp(since, field="since")
        records = [
            r for r in records if _timestamp(str(r["@timestamp"]), field="@timestamp") > cutoff
        ]
    body = "".join(json.dumps(r) + "\n" for r in records[-limit:])
    return Response(content=body, media_type=NDJSON)
