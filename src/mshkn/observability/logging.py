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
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("mshkn_request_id", default="-")
account_id_var: ContextVar[str | None] = ContextVar("mshkn_account_id", default=None)

ECS_VERSION = "8.11.0"

_CONFIGURED_MARKER = "_mshkn_configured"
_MAX_FIELD_CHARS = 4096
_TRUNCATED = "[truncated]"

_BUILTIN_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, None, None, None).__dict__.keys() | {"message", "asctime"}
)
# Read off the record into a field of their own; never namespaced as well.
_LIFTED = frozenset({"request_id", "account_id"})


def _cap(value: object) -> object:
    """Bound one field. JSON scalars pass through; anything else is stringified
    and truncated, so one oversized `extra=` cannot pin megabytes in the ring."""
    if value is None or isinstance(value, bool | int | float):
        return value
    text = value if isinstance(value, str) else str(value)
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[:_MAX_FIELD_CHARS] + _TRUNCATED


def to_ecs(record: logging.LogRecord) -> dict[str, object]:
    """One LogRecord as a flat, dotted ECS document.

    Dotted keys rather than nested objects: that is what Filebeat emits, it is
    what a flat dict produces for free, and it is what a test can assert on
    without walking a tree.
    """
    entry: dict[str, object] = {
        "@timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        "ecs.version": ECS_VERSION,
        "log.level": record.levelname.lower(),
        "log.logger": record.name,
        "message": _cap(record.getMessage()),
    }
    # An explicit attribute wins over the contextvar: destroy logs the computer's
    # own account, which is right even when the reaper is what ran it.
    request_id = getattr(record, "request_id", None) or request_id_var.get()
    if request_id != "-":
        entry["trace.id"] = request_id
    account_id = getattr(record, "account_id", None) or account_id_var.get()
    if account_id is not None:
        entry["mshkn.account_id"] = account_id
    for key, value in record.__dict__.items():
        if key not in _BUILTIN_ATTRS and key not in _LIFTED:
            entry[f"mshkn.{key}"] = _cap(value)
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        entry["error.type"] = type(exc).__name__
        entry["error.message"] = _cap(str(exc))
        entry["error.stack_trace"] = _cap(logging.Formatter().formatException(record.exc_info))
    return entry


class ECSFormatter(logging.Formatter):
    """Serialise `to_ecs` as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(to_ecs(record), default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Route the root and uvicorn loggers through the JSON formatter. Idempotent."""
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
