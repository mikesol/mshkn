from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mshkn.api.errors import install_error_handlers
from mshkn.errors import (
    BadRequest,
    ConfigError,
    Conflict,
    HostError,
    InvalidInput,
    LimitExceeded,
    MshknError,
    NotFound,
    PayloadTooLarge,
    TransformError,
)


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/raise/{kind}")
    async def _raise(kind: str) -> dict[str, str]:
        errors: dict[str, MshknError] = {
            "not_found": NotFound("recipe rcp-1 not found"),
            "conflict": Conflict("recipe rcp-1 is not ready"),
            "invalid": InvalidInput("ram must end with MB or GB"),
            "limit": LimitExceeded("VM limit reached"),
            "host": HostError("dmsetup create failed: device busy"),
            "config": ConfigError("MSHKN_PORT: invalid literal for int()"),
        }
        raise errors[kind]

    return app


async def _get(kind: str) -> tuple[int, dict[str, str]]:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.get(f"/raise/{kind}")
    return resp.status_code, resp.json()


async def _status(exc: MshknError) -> tuple[int, dict[str, object]]:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/raise")
    async def _raise() -> dict[str, str]:
        raise exc

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/raise")
    return resp.status_code, resp.json()


async def test_not_found_maps_to_404_with_message() -> None:
    assert await _get("not_found") == (404, {"detail": "recipe rcp-1 not found"})


async def test_conflict_maps_to_409() -> None:
    assert await _get("conflict") == (409, {"detail": "recipe rcp-1 is not ready"})


async def test_invalid_input_maps_to_422() -> None:
    assert await _get("invalid") == (422, {"detail": "ram must end with MB or GB"})


async def test_limit_exceeded_maps_to_429() -> None:
    assert await _get("limit") == (429, {"detail": "VM limit reached"})


async def test_host_error_maps_to_502_without_leaking_detail() -> None:
    status, body = await _get("host")
    assert status == 502
    assert body == {"detail": "host operation failed"}


async def test_unmapped_domain_error_is_500() -> None:
    status, body = await _get("config")
    assert status == 500
    assert body == {"detail": "internal error"}


def test_message_attribute_and_str() -> None:
    err = NotFound("x")
    assert err.message == "x"
    assert str(err) == "x"
    assert isinstance(err, MshknError)


async def test_bad_request_maps_to_400() -> None:
    assert await _status(BadRequest("nope")) == (400, {"detail": "nope"})


async def test_payload_too_large_maps_to_413() -> None:
    assert await _status(PayloadTooLarge("too big")) == (413, {"detail": "too big"})


async def test_transform_error_keeps_its_structured_detail() -> None:
    exc = TransformError("bad transform", detail={"errors": ["x"], "starlark_result": {"a": 1}})
    assert await _status(exc) == (502, {"detail": {"errors": ["x"], "starlark_result": {"a": 1}}})


def test_detail_defaults_to_none() -> None:
    assert NotFound("x").detail is None
