"""Application factory."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request

from mshkn.api.checkpoints import router as checkpoints_router
from mshkn.api.computers import router as computers_router
from mshkn.api.errors import install_error_handlers
from mshkn.api.ingress import router as ingress_router
from mshkn.api.recipes import router as recipes_router
from mshkn.api.system import router as system_router
from mshkn.observability.logging import configure_logging, request_id_var
from mshkn.runtime import Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Build the FastAPI app.

    With a Runtime given (tests), it is attached immediately so requests work
    without running the lifespan. Without one (production), the lifespan
    builds it from the environment. Either way the lifespan starts and closes it.
    """
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime if runtime is not None else await Runtime.from_env()
        app.state.runtime = rt
        try:
            await rt.start()
            yield
        finally:
            await rt.close()

    app = FastAPI(title="mshkn", version="0.1.0", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime
    install_error_handlers(app)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach a request id to the response and to every log line during the request."""
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response

    app.include_router(computers_router)
    app.include_router(checkpoints_router)
    app.include_router(ingress_router)
    app.include_router(recipes_router)
    app.include_router(system_router)
    return app
