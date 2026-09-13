"""ECS log records, and the two places they go.

`to_ecs` maps a LogRecord to the Elastic Common Schema. `ECSFormatter`
serialises the result for stdout; `RingBufferHandler` (Task 3) keeps the dict in
a bounded deque the API reads. One mapping, two consumers, identical shape —
which is the whole point: what a collector scrapes and what `GET /logs` returns
are the same document.
"""

from __future__ import annotations

import json
import logging
import math
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("mshkn_request_id", default="-")
account_id_var: ContextVar[str | None] = ContextVar("mshkn_account_id", default=None)

ECS_VERSION = "8.11.0"

_CONFIGURED_MARKER = "_mshkn_configured"
_MAX_FIELD_CHARS = 4096
_TRUNCATED = "[truncated]"
# The range a JSON consumer and an Elasticsearch `long` both accept.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63

_UNSET = object()
_EXC_FORMATTER = logging.Formatter()

_BUILTIN_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, None, None, None).__dict__.keys() | {"message", "asctime"}
)
# Read off the record into a field of their own; never namespaced as well.
_LIFTED = frozenset({"request_id", "account_id"})


def _cap(value: object) -> object:
    """Bound one field, and leave it something a strict JSON parser accepts.

    The guarantee is per field: no value exceeds `_MAX_FIELD_CHARS` characters
    and none of them is a token (`NaN`, `Infinity`, a bignum) that only Python
    calls JSON. It says nothing about how many fields a record carries.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        text = repr(value)
    elif isinstance(value, int):
        if _INT64_MIN <= value < _INT64_MAX:
            return value
        # Wider than a JSON consumer will hold — and past CPython's 4300-digit
        # limit `str()` on it raises, which hex conversion is not subject to.
        text = hex(value)
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[: _MAX_FIELD_CHARS - len(_TRUNCATED)] + _TRUNCATED


def to_ecs(record: logging.LogRecord) -> dict[str, object]:
    """One LogRecord as a flat, dotted ECS document.

    Dotted keys rather than nested objects: that is what Filebeat emits, it is
    what a flat dict produces for free, and it is what a test can assert on
    without walking a tree.
    """
    try:
        message = record.getMessage()
    except (TypeError, ValueError, KeyError, IndexError):
        # %-formatting the args failed. to_ecs is the only mapping there is, so
        # raising here would lose the record from stdout and from the ring both;
        # keep the pieces, which are the evidence of what the caller logged.
        message = f"{record.msg!r} % {record.args!r}"
    entry: dict[str, object] = {
        "@timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        "ecs.version": ECS_VERSION,
        "log.level": record.levelname.lower(),
        "log.logger": record.name,
        "message": _cap(message),
    }
    # The contextvars are read here, at format time, rather than stamped onto the
    # record when it is made: a value therefore does not survive a hand-off to
    # another thread. Nothing threaded emits today, but a QueueHandler paired
    # with a QueueListener would format on the listener thread with both vars at
    # their defaults, and those records would be invisible to their own tenant.
    #
    # Presence, not truthiness, decides: an explicit record attribute beats the
    # contextvar even when it is empty or None.
    request_id = record.__dict__.get("request_id", _UNSET)
    if request_id is _UNSET:
        request_id = request_id_var.get()
    if request_id != "-":
        # Client-supplied, straight off X-Request-Id and never validated.
        entry["trace.id"] = _cap(str(request_id))
    account_id = record.__dict__.get("account_id", _UNSET)
    if account_id is _UNSET:
        account_id = account_id_var.get()
    if account_id is not None:
        # destroy logs the computer's own account, which is right even when the
        # reaper is what ran it; an explicit None means no account at all.
        entry["mshkn.account_id"] = _cap(str(account_id))
    for key, value in record.__dict__.items():
        if key not in _BUILTIN_ATTRS and key not in _LIFTED:
            entry[f"mshkn.{key}"] = _cap(value)
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        entry["error.type"] = type(exc).__name__
        entry["error.message"] = _cap(str(exc))
        entry["error.stack_trace"] = _cap(_EXC_FORMATTER.formatException(record.exc_info))
    return entry


class ECSFormatter(logging.Formatter):
    """Serialise `to_ecs` as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(to_ecs(record))


def configure_logging(level: int = logging.INFO) -> None:
    """Route the root and uvicorn loggers through `ECSFormatter`. Idempotent."""
    root = logging.root
    if getattr(root, _CONFIGURED_MARKER, False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(ECSFormatter())
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False
    setattr(root, _CONFIGURED_MARKER, True)
