"""Ingress rule CRUD and the unauthenticated trigger endpoint.

The router owns only HTTP concerns: turning a live request into the plain dict
the Starlark transform sees, and turning a TriggerOutcome back into a response.
Everything else is IngressService.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from mshkn.api.deps import get_runtime, require_account
from mshkn.api.schemas import (
    IngressLogResponse,
    IngressRuleCreateRequest,
    IngressRuleDetail,
    IngressRuleResponse,
    IngressRuleUpdateRequest,
    IngressTestRequest,
    IngressTestResponse,
)
from mshkn.db import get_ingress_rule_by_id
from mshkn.errors import InvalidInput, NotFound, PayloadTooLarge

if TYPE_CHECKING:
    from mshkn.models import Account, IngressRule

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingress"])

_require_account = Depends(require_account)


def _rule_to_response(rule: IngressRule, domain: str) -> IngressRuleResponse:
    return IngressRuleResponse(
        id=rule.id,
        name=rule.name,
        ingress_url=f"https://{domain}/ingress/{rule.id}",
        response_mode=rule.response_mode,
        max_body_bytes=rule.max_body_bytes,
        rate_limit_rpm=rule.rate_limit_rpm,
        enabled=rule.enabled,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


# --- CRUD endpoints (authenticated) ---


@router.post("/ingress_rules", response_model=IngressRuleResponse)
async def create_rule(
    request: Request,
    body: IngressRuleCreateRequest,
    account: Account = _require_account,
) -> IngressRuleResponse:
    rt = get_runtime(request)
    rule = await rt.ingress.create_rule(
        account,
        name=body.name,
        starlark_source=body.starlark_source,
        response_mode=body.response_mode,
        max_body_bytes=body.max_body_bytes,
        rate_limit_rpm=body.rate_limit_rpm,
    )
    return _rule_to_response(rule, rt.config.domain)


@router.get("/ingress_rules", response_model=list[IngressRuleResponse])
async def list_rules(
    request: Request,
    account: Account = _require_account,
) -> list[IngressRuleResponse]:
    rt = get_runtime(request)
    return [_rule_to_response(r, rt.config.domain) for r in await rt.ingress.list_rules(account)]


@router.get("/ingress_rules/{rule_id}", response_model=IngressRuleDetail)
async def get_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> IngressRuleDetail:
    rt = get_runtime(request)
    rule = await rt.ingress.get_rule(account, rule_id)
    return IngressRuleDetail(
        **_rule_to_response(rule, rt.config.domain).model_dump(),
        starlark_source=rule.starlark_source,
    )


@router.put("/ingress_rules/{rule_id}", response_model=IngressRuleResponse)
async def update_rule(
    rule_id: str,
    request: Request,
    body: IngressRuleUpdateRequest,
    account: Account = _require_account,
) -> IngressRuleResponse:
    rt = get_runtime(request)
    rule = await rt.ingress.update_rule(
        account,
        rule_id,
        name=body.name,
        starlark_source=body.starlark_source,
        response_mode=body.response_mode,
        max_body_bytes=body.max_body_bytes,
        rate_limit_rpm=body.rate_limit_rpm,
        enabled=body.enabled,
    )
    return _rule_to_response(rule, rt.config.domain)


@router.delete("/ingress_rules/{rule_id}", status_code=204)
async def delete_rule_endpoint(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> Response:
    rt = get_runtime(request)
    await rt.ingress.delete_rule(account, rule_id)
    return Response(status_code=204)


@router.post("/ingress_rules/{rule_id}/rotate", response_model=IngressRuleResponse)
async def rotate_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> IngressRuleResponse:
    rt = get_runtime(request)
    rule = await rt.ingress.rotate_rule(account, rule_id)
    return _rule_to_response(rule, rt.config.domain)


@router.post("/ingress_rules/{rule_id}/test", response_model=IngressTestResponse)
async def test_rule(
    rule_id: str,
    request: Request,
    body: IngressTestRequest,
    account: Account = _require_account,
) -> IngressTestResponse:
    rt = get_runtime(request)
    rule = await rt.ingress.get_rule(account, rule_id)

    request_dict: dict[str, object] = {
        "method": body.method,
        "path": body.path,
        "headers": body.headers,
        "query_params": body.query_params,
        "body_json": None,
        "body_form": None,
        "body_raw": body.body or "",
        "content_type": body.headers.get("content-type", ""),
    }
    if body.body:
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            request_dict["body_json"] = json.loads(body.body)

    result, errors, elapsed_ms = rt.ingress.test_rule(rule, request_dict)
    return IngressTestResponse(
        starlark_result=result,
        validation_errors=errors,
        execution_time_ms=elapsed_ms,
    )


@router.get("/ingress_rules/{rule_id}/logs", response_model=list[IngressLogResponse])
async def get_rule_logs(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> list[IngressLogResponse]:
    rt = get_runtime(request)
    return [
        IngressLogResponse(
            id=log.id,
            status=log.status,
            starlark_result=(json.loads(log.starlark_result) if log.starlark_result else None),
            error_message=log.error_message,
            created_at=log.created_at,
        )
        for log in await rt.ingress.logs(account, rule_id)
    ]


# --- Body parsing for the trigger endpoint ---


async def _parse_ingress_body(request: Request, max_bytes: int) -> dict[str, object]:
    """Parse an incoming ingress request into the dict Starlark sees."""
    method = request.method
    headers = dict(request.headers)
    content_type = request.headers.get("content-type", "")

    body_json: object = None
    body_form: object = None
    body_raw = ""

    if method != "GET":
        declared = request.headers.get("content-length")
        if declared:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > max_bytes:
                raise PayloadTooLarge("Request body too large")

        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise PayloadTooLarge("Request body too large")
            chunks.append(chunk)
        body_bytes = b"".join(chunks)
        body_raw = body_bytes.decode("utf-8", errors="replace")

        if "application/json" in content_type:
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                body_json = json.loads(body_bytes)
        elif "application/x-www-form-urlencoded" in content_type:
            parsed = parse_qs(body_raw)
            body_form = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

    return {
        "method": method,
        "path": request.url.path,
        "headers": headers,
        "query_params": dict(request.query_params),
        "body_json": body_json,
        "body_form": body_form,
        "body_raw": body_raw,
        "content_type": content_type,
    }


# --- Trigger endpoint (unauthenticated) ---


@router.api_route("/ingress/{rule_id}", methods=["GET", "POST", "PUT", "PATCH"])
async def handle_ingress(rule_id: str, request: Request) -> Response:
    """Unauthenticated ingress trigger endpoint."""
    rt = get_runtime(request)
    # The body limit is per rule, so the rule has to be read before the body is.
    # IngressService.trigger reads it again and owns the authoritative check.
    rule = await get_ingress_rule_by_id(rt.db, rule_id)
    if rule is None or not rule.enabled:
        raise NotFound("Ingress rule not found")

    try:
        request_dict = await _parse_ingress_body(request, rule.max_body_bytes)
    except PayloadTooLarge:
        raise
    except Exception as exc:
        logger.warning("Failed to parse ingress body for %s: %s", rule_id, exc)
        raise InvalidInput("Failed to parse request body") from None

    outcome = await rt.ingress.trigger(rule_id, request_dict)
    if outcome.body is None:
        return Response(status_code=outcome.status_code)
    return JSONResponse(status_code=outcome.status_code, content=outcome.body)
