"""Structured JSON logging formatter for mshkn."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    # Fields from LogRecord to exclude from extra output
    _BUILTIN_ATTRS = frozenset(
        logging.LogRecord("", 0, "", 0, None, None, None).__dict__.keys()
        | {"message", "asctime"}
    )

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Include any extra fields passed via logger.info("...", extra={...})
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS:
                entry[key] = value

        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)
