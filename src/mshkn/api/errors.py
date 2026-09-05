"""Map domain errors to HTTP responses, keeping FastAPI's {"detail": ...} shape."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

from mshkn.errors import (
    Conflict,
    HostError,
    InvalidInput,
    LimitExceeded,
    MshknError,
    NotFound,
)

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

_STATUS_BY_TYPE: tuple[tuple[type[MshknError], int], ...] = (
    (NotFound, 404),
    (Conflict, 409),
    (InvalidInput, 422),
    (LimitExceeded, 429),
    (HostError, 502),
)


def _status_for(exc: MshknError) -> int:
    for cls, status in _STATUS_BY_TYPE:
        if isinstance(exc, cls):
            return status
    return 500


async def _handle_domain_error(request: Request, exc: MshknError) -> JSONResponse:
    status = _status_for(exc)
    if isinstance(exc, HostError):
        logger.error("host operation failed: %s", exc.message, extra={"path": request.url.path})
        return JSONResponse(status_code=status, content={"detail": "host operation failed"})
    if status == 500:
        logger.error("unmapped domain error: %s", exc.message, extra={"path": request.url.path})
        return JSONResponse(status_code=500, content={"detail": "internal error"})
    return JSONResponse(status_code=status, content={"detail": exc.message})


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(MshknError, _handle_domain_error)  # type: ignore[arg-type]
