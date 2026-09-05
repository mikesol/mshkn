"""JSON log formatting with a per-request id carried in a contextvar."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("mshkn_request_id", default="-")

_CONFIGURED_MARKER = "_mshkn_configured"


class RequestIdFilter(logging.Filter):
    """Stamp every record with the current request id (or "-")."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    _BUILTIN_ATTRS = frozenset(
        logging.LogRecord("", 0, "", 0, None, None, None).__dict__.keys() | {"message", "asctime"}
    )

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS and key != "request_id":
                entry[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Route the root and uvicorn loggers through the JSON formatter. Idempotent."""
    root = logging.root
    if getattr(root, _CONFIGURED_MARKER, False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    handler.addFilter(RequestIdFilter())
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False
    setattr(root, _CONFIGURED_MARKER, True)
