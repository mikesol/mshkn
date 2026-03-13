"""Ingress rules CRUD API and unauthenticated trigger endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from mshkn.api.auth import require_account
from mshkn.api.ratelimit import RateLimiter
from mshkn.ingress.db import (
    delete_ingress_rule,
    get_ingress_rule_by_id,
    insert_ingress_log,
    insert_ingress_rule,
    list_ingress_logs,
    list_ingress_rules_by_account,
    rotate_ingress_rule_id,
    update_ingress_rule,
)
from mshkn.ingress.models import (
    IngressLog,
    IngressLogResponse,
    IngressRule,
    IngressRuleCreateRequest,
    IngressRuleResponse,
    IngressRuleUpdateRequest,
    IngressTestRequest,
    IngressTestResponse,
)
from mshkn.ingress.starlark import StarlarkError, execute_transform, validate_starlark

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Account
    from mshkn.vm.manager import VMManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingress"])

_require_account = Depends(require_account)

# Hold references to background tasks to prevent GC
_background_tasks: set[asyncio.Task[None]] = set()

# --- Per-rule rate limiting ---

_rule_rate_limiters: dict[str, RateLimiter] = {}


def _get_rule_rate_limiter(rule_id: str, rate_limit_rpm: int) -> RateLimiter:
    limiter = _rule_rate_limiters.get(rule_id)
    if limiter is None or limiter.max_requests != rate_limit_rpm:
        limiter = RateLimiter(max_requests=rate_limit_rpm, window_seconds=60.0)
        _rule_rate_limiters[rule_id] = limiter
    return limiter


# --- Validation helpers ---

VALID_FORK_FIELDS = {
    "action", "checkpoint_id", "label", "exec", "self_destruct", "exclusive",
    "callback_url", "meta_exec",
}
VALID_CREATE_FIELDS = {
    "action", "capabilities", "uses", "exec", "self_destruct",
    "callback_url", "label", "meta_exec",
}
VALID_EXCLUSIVE_VALUES = {"error_on_conflict", "defer_on_conflict"}


def _validate_transform_result(result: dict[str, Any] | None) -> list[str]:
    """Validate a Starlark transform result dict. Returns list of errors."""
    if result is None:
        return []

    errors: list[str] = []

    if not isinstance(result, dict):
        return ["transform must return a dict or None"]

    action = result.get("action")
    if action not in ("fork", "create"):
        errors.append(f"action must be 'fork' or 'create', got {action!r}")
        return errors

    if action == "fork":
        if "checkpoint_id" not in result and "label" not in result:
            errors.append("fork action requires 'checkpoint_id' or 'label'")
        unknown = set(result.keys()) - VALID_FORK_FIELDS
        if unknown:
            errors.append(f"unknown fields for fork action: {unknown}")
    elif action == "create":
        unknown = set(result.keys()) - VALID_CREATE_FIELDS
        if unknown:
            errors.append(f"unknown fields for create action: {unknown}")

    exclusive = result.get("exclusive")
    if exclusive is not None and exclusive not in VALID_EXCLUSIVE_VALUES:
        errors.append(
            f"exclusive must be one of {VALID_EXCLUSIVE_VALUES}, got {exclusive!r}"
        )

    return errors


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


# --- CRUD Endpoints (authenticated) ---


@router.post("/ingress_rules", response_model=IngressRuleResponse)
async def create_rule(
    request: Request,
    body: IngressRuleCreateRequest,
    account: Account = _require_account,
) -> IngressRuleResponse:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config

    # Validate starlark
    errors = validate_starlark(body.starlark_source)
    if errors:
        raise HTTPException(status_code=422, detail={"starlark_errors": errors})

    now = datetime.now(UTC).isoformat()
    rule = IngressRule(
        internal_id=str(uuid.uuid4()),
        id=f"ir_{secrets.token_urlsafe(20)}",
        account_id=account.id,
        name=body.name,
        starlark_source=body.starlark_source,
        response_mode=body.response_mode,
        max_body_bytes=body.max_body_bytes,
        rate_limit_rpm=body.rate_limit_rpm,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    await insert_ingress_rule(db, rule)

    return _rule_to_response(rule, config.domain)


@router.get("/ingress_rules", response_model=list[IngressRuleResponse])
async def list_rules(
    request: Request,
    account: Account = _require_account,
) -> list[IngressRuleResponse]:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    rules = await list_ingress_rules_by_account(db, account.id)
    return [_rule_to_response(r, config.domain) for r in rules]


@router.get("/ingress_rules/{rule_id}")
async def get_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> dict[str, object]:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")
    resp = _rule_to_response(rule, config.domain).model_dump()
    resp["starlark_source"] = rule.starlark_source
    return resp


@router.put("/ingress_rules/{rule_id}", response_model=IngressRuleResponse)
async def update_rule(
    rule_id: str,
    request: Request,
    body: IngressRuleUpdateRequest,
    account: Account = _require_account,
) -> IngressRuleResponse:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")

    if body.starlark_source is not None:
        errors = validate_starlark(body.starlark_source)
        if errors:
            raise HTTPException(status_code=422, detail={"starlark_errors": errors})
        rule.starlark_source = body.starlark_source

    if body.name is not None:
        rule.name = body.name
    if body.response_mode is not None:
        rule.response_mode = body.response_mode
    if body.max_body_bytes is not None:
        rule.max_body_bytes = body.max_body_bytes
    if body.rate_limit_rpm is not None:
        rule.rate_limit_rpm = body.rate_limit_rpm
    if body.enabled is not None:
        rule.enabled = body.enabled

    rule.updated_at = datetime.now(UTC).isoformat()
    await update_ingress_rule(db, rule)

    return _rule_to_response(rule, config.domain)


@router.delete("/ingress_rules/{rule_id}", status_code=204)
async def delete_rule_endpoint(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> Response:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")
    await delete_ingress_rule(db, rule_id)
    # Clean up cached rate limiter
    _rule_rate_limiters.pop(rule_id, None)
    return Response(status_code=204)


@router.post("/ingress_rules/{rule_id}/rotate", response_model=IngressRuleResponse)
async def rotate_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> IngressRuleResponse:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")

    new_id = f"ir_{secrets.token_urlsafe(20)}"
    await rotate_ingress_rule_id(db, rule.internal_id, new_id)

    # Move rate limiter to new key
    old_limiter = _rule_rate_limiters.pop(rule_id, None)
    if old_limiter is not None:
        _rule_rate_limiters[new_id] = old_limiter

    rule.id = new_id
    rule.updated_at = datetime.now(UTC).isoformat()
    return _rule_to_response(rule, config.domain)


@router.post("/ingress_rules/{rule_id}/test", response_model=IngressTestResponse)
async def test_rule(
    rule_id: str,
    request: Request,
    body: IngressTestRequest,
    account: Account = _require_account,
) -> IngressTestResponse:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")

    request_dict = {
        "method": body.method,
        "path": body.path,
        "headers": body.headers,
        "query_params": body.query_params,
        "body_json": None,
        "body_form": None,
        "body_raw": body.body or "",
        "content_type": body.headers.get("content-type", ""),
    }

    # Try parsing body as JSON for the test
    if body.body:
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            request_dict["body_json"] = json.loads(body.body)

    t0 = time.monotonic()
    try:
        result = execute_transform(rule.starlark_source, request_dict)
    except StarlarkError as exc:
        return IngressTestResponse(
            starlark_result=None,
            validation_errors=[str(exc)],
            execution_time_ms=(time.monotonic() - t0) * 1000,
        )
    elapsed_ms = (time.monotonic() - t0) * 1000

    validation_errors = _validate_transform_result(result)
    return IngressTestResponse(
        starlark_result=result,
        validation_errors=validation_errors,
        execution_time_ms=elapsed_ms,
    )


@router.get("/ingress_rules/{rule_id}/logs", response_model=list[IngressLogResponse])
async def get_rule_logs(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> list[IngressLogResponse]:
    db: aiosqlite.Connection = request.app.state.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")

    logs = await list_ingress_logs(db, rule.internal_id)
    return [
        IngressLogResponse(
            id=log.id,
            status=log.status,
            starlark_result=(
                json.loads(log.starlark_result) if log.starlark_result else None
            ),
            error_message=log.error_message,
            created_at=log.created_at,
        )
        for log in logs
    ]


# --- Body parsing for trigger endpoint ---


async def _parse_ingress_body(request: Request, max_bytes: int) -> dict[str, object]:
    """Parse an incoming ingress request into a dict for Starlark."""
    method = request.method
    path = request.url.path
    headers = dict(request.headers)
    query_params = dict(request.query_params)
    content_type = request.headers.get("content-type", "")

    body_json = None
    body_form = None
    body_raw = ""

    if method == "GET":
        return {
            "method": method,
            "path": path,
            "headers": headers,
            "query_params": query_params,
            "body_json": body_json,
            "body_form": body_form,
            "body_raw": body_raw,
            "content_type": content_type,
        }

    # Check Content-Length header first
    cl = request.headers.get("content-length")
    if cl:
        try:
            cl_int = int(cl)
        except ValueError:
            cl_int = 0
        if cl_int > max_bytes:
            raise HTTPException(status_code=413, detail="Request body too large")

    # Stream body with limit
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Request body too large")
        chunks.append(chunk)
    body_bytes = b"".join(chunks)
    body_raw = body_bytes.decode("utf-8", errors="replace")

    # Parse based on content type
    if "application/json" in content_type:
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            body_json = json.loads(body_bytes)
    elif "application/x-www-form-urlencoded" in content_type:
        from urllib.parse import parse_qs

        parsed = parse_qs(body_raw)
        body_form = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

    return {
        "method": method,
        "path": path,
        "headers": headers,
        "query_params": query_params,
        "body_json": body_json,
        "body_form": body_form,
        "body_raw": body_raw,
        "content_type": content_type,
    }


# --- Internal action executors ---


async def _do_create(
    db: aiosqlite.Connection,
    vm_manager: VMManager,
    config: Config,
    account_id: str,
    exec_cmd: str | None = None,
    self_destruct: bool = False,
    callback_url: str | None = None,
    label: str | None = None,
    meta_exec: str | None = None,  # noqa: ARG001
) -> dict[str, object]:
    """Core create-computer logic, shared by API endpoint and ingress trigger."""
    from mshkn.api.computers import _self_destruct
    from mshkn.db import count_active_computers_by_account, get_account_by_id
    from mshkn.vm.ssh import ssh_exec

    account = await get_account_by_id(db, account_id)
    if account is None:
        raise HTTPException(status_code=500, detail="Account not found")

    active_count = await count_active_computers_by_account(db, account_id)
    if active_count >= account.vm_limit:
        raise HTTPException(status_code=429, detail="VM limit reached")

    computer = await vm_manager.create(account_id)

    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None

    if exec_cmd is not None:
        result = await ssh_exec(computer.vm_ip, exec_cmd, config.ssh_key_path, pool=None)
        exec_exit_code = result.exit_code
        exec_stdout = result.stdout
        exec_stderr = result.stderr

        if self_destruct:
            created_checkpoint_id = await _self_destruct(
                computer=computer,
                account=account,
                label=label,
                source_checkpoint_id=None,
                exec_exit_code=exec_exit_code,
                exec_stdout=exec_stdout,
                exec_stderr=exec_stderr,
                callback_url=callback_url,
                db=db,
                config=config,
                vm_mgr=vm_manager,
                pool=None,
            )

    return {
        "computer_id": computer.id,
        "url": f"https://{computer.id}.{config.domain}",
        "recipe_id": computer.recipe_id,
        "exec_exit_code": exec_exit_code,
        "exec_stdout": exec_stdout,
        "exec_stderr": exec_stderr,
        "created_checkpoint_id": created_checkpoint_id,
    }


async def _do_fork(
    db: aiosqlite.Connection,
    vm_manager: VMManager,
    config: Config,
    account_id: str,
    checkpoint_id: str,
    exec_cmd: str | None = None,
    self_destruct: bool = False,
    callback_url: str | None = None,
    exclusive: str | None = None,
    meta_exec: str | None = None,
) -> dict[str, object]:
    """Core fork-from-checkpoint logic, shared by API endpoint and ingress trigger."""
    from mshkn.api.computers import _self_destruct
    from mshkn.db import get_active_computer_for_label, get_checkpoint, insert_deferred
    from mshkn.vm.ssh import ssh_exec

    ckpt = await get_checkpoint(db, checkpoint_id)
    if ckpt is None or ckpt.account_id != account_id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    # Exclusive restore
    if exclusive and ckpt.label:
        active = await get_active_computer_for_label(db, account_id, ckpt.label)
        if active is not None:
            if exclusive == "error_on_conflict":
                raise HTTPException(
                    status_code=409,
                    detail="Checkpoint chain has active computer",
                )
            if exclusive == "defer_on_conflict":
                import uuid as _uuid

                deferred_id = f"def-{_uuid.uuid4().hex[:12]}"
                payload = {
                    "checkpoint_id": checkpoint_id,
                    "exec": exec_cmd,
                    "self_destruct": self_destruct,
                    "callback_url": callback_url,
                    "meta_exec": meta_exec,
                }
                now = datetime.now(UTC).isoformat()
                await insert_deferred(
                    db, deferred_id, ckpt.label, account_id,
                    json.dumps(payload), now,
                )
                return {"deferred_id": deferred_id, "status": "queued"}

    account_obj = await _get_account(db, account_id)
    computer = await vm_manager.fork_from_checkpoint(account_id, ckpt, recipe_id=ckpt.recipe_id)

    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None

    if exec_cmd is not None:
        result = await ssh_exec(computer.vm_ip, exec_cmd, config.ssh_key_path, pool=None)
        exec_exit_code = result.exit_code
        exec_stdout = result.stdout
        exec_stderr = result.stderr

        if self_destruct:
            ckpt_label = ckpt.label
            created_checkpoint_id = await _self_destruct(
                computer=computer,
                account=account_obj,
                label=ckpt_label,
                source_checkpoint_id=checkpoint_id,
                exec_exit_code=exec_exit_code,
                exec_stdout=exec_stdout,
                exec_stderr=exec_stderr,
                callback_url=callback_url,
                db=db,
                config=config,
                vm_mgr=vm_manager,
                pool=None,
            )

    return {
        "computer_id": computer.id,
        "checkpoint_id": checkpoint_id,
        "exec_exit_code": exec_exit_code,
        "exec_stdout": exec_stdout,
        "exec_stderr": exec_stderr,
        "created_checkpoint_id": created_checkpoint_id,
    }


async def _get_account(db: aiosqlite.Connection, account_id: str) -> Account:
    """Fetch account by ID."""
    from mshkn.db import get_account_by_id

    account = await get_account_by_id(db, account_id)
    if account is None:
        raise HTTPException(status_code=500, detail="Account not found")
    return account


# --- Trigger endpoint (unauthenticated) ---


@router.api_route("/ingress/{rule_id}", methods=["GET", "POST", "PUT", "PATCH"])
async def handle_ingress(rule_id: str, request: Request) -> Response:
    """Unauthenticated ingress trigger endpoint."""
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config

    # 1. Look up rule
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or not rule.enabled:
        raise HTTPException(status_code=404, detail="Ingress rule not found")

    # 2. Per-rule rate limit
    limiter = _get_rule_rate_limiter(rule_id, rule.rate_limit_rpm)
    if not limiter.check(rule_id):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # 3. Parse body
    try:
        request_dict = await _parse_ingress_body(request, rule.max_body_bytes)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Failed to parse ingress body for %s: %s", rule_id, exc)
        raise HTTPException(
            status_code=400, detail="Failed to parse request body",
        ) from None

    # 4. Execute Starlark transform
    try:
        result = execute_transform(rule.starlark_source, request_dict)
    except StarlarkError as exc:
        # Log the failure
        await _log_invocation(db, rule.internal_id, "failed", None, str(exc))
        raise HTTPException(
            status_code=502, detail=f"Starlark execution error: {exc}",
        ) from None

    # 5. Validate result
    if result is None:
        await _log_invocation(db, rule.internal_id, "completed", None, None)
        return Response(status_code=204)

    validation_errors = _validate_transform_result(result)
    if validation_errors:
        await _log_invocation(
            db, rule.internal_id, "failed",
            json.dumps(result), "; ".join(validation_errors),
        )
        raise HTTPException(
            status_code=502,
            detail={"errors": validation_errors, "starlark_result": result},
        )

    # 6. Execute action
    vm_mgr: VMManager = request.app.state.vm_manager
    action = result["action"]

    if rule.response_mode == "async":
        # Fire-and-forget
        task = asyncio.create_task(
            _execute_action_and_log(
                db=db, vm_mgr=vm_mgr, config=config,
                rule=rule, action=action, result=result,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        await _log_invocation(
            db, rule.internal_id, "accepted", json.dumps(result), None,
        )
        return JSONResponse(status_code=202, content={"status": "accepted"})

    # Sync: wait and return
    try:
        action_result = await _execute_action(
            db=db, vm_mgr=vm_mgr, config=config,
            account_id=rule.account_id, action=action, result=result,
        )
        await _log_invocation(
            db, rule.internal_id, "completed", json.dumps(result), None,
        )
        return JSONResponse(status_code=200, content=action_result)
    except HTTPException as exc:
        await _log_invocation(
            db, rule.internal_id, "failed",
            json.dumps(result), str(exc.detail),
        )
        raise
    except Exception as exc:
        await _log_invocation(
            db, rule.internal_id, "failed",
            json.dumps(result), str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from None


async def _log_invocation(
    db: aiosqlite.Connection,
    rule_internal_id: str,
    status: str,
    starlark_result: str | None,
    error_message: str | None,
) -> None:
    """Record an ingress invocation log entry."""
    log = IngressLog(
        id=f"ilog-{uuid.uuid4().hex[:12]}",
        rule_internal_id=rule_internal_id,
        status=status,
        starlark_result=starlark_result,
        error_message=error_message,
        created_at=datetime.now(UTC).isoformat(),
    )
    try:
        await insert_ingress_log(db, log)
    except Exception:
        logger.warning("Failed to write ingress log for %s", rule_internal_id)


async def _execute_action(
    db: aiosqlite.Connection,
    vm_mgr: VMManager,
    config: Config,
    account_id: str,
    action: str,
    result: dict[str, Any],
) -> dict[str, object]:
    """Execute a fork or create action from a Starlark transform result."""
    if action == "fork":
        # Resolve label to checkpoint_id if needed
        checkpoint_id = result.get("checkpoint_id")
        if checkpoint_id is None:
            label = result.get("label")
            if label is None:
                raise HTTPException(
                    status_code=502, detail="fork needs checkpoint_id or label",
                )
            from mshkn.db import list_checkpoints_by_account

            ckpts = await list_checkpoints_by_account(db, account_id, label=label)
            if not ckpts:
                raise HTTPException(
                    status_code=404,
                    detail=f"No checkpoint with label '{label}'",
                )
            checkpoint_id = ckpts[0].id  # Most recent with this label

        return await _do_fork(
            db=db,
            vm_manager=vm_mgr,
            config=config,
            account_id=account_id,
            checkpoint_id=checkpoint_id,
            exec_cmd=result.get("exec"),
            self_destruct=result.get("self_destruct", False),
            callback_url=result.get("callback_url"),
            exclusive=result.get("exclusive"),
            meta_exec=result.get("meta_exec"),
        )
    if action == "create":
        return await _do_create(
            db=db,
            vm_manager=vm_mgr,
            config=config,
            account_id=account_id,
            exec_cmd=result.get("exec"),
            self_destruct=result.get("self_destruct", False),
            callback_url=result.get("callback_url"),
            label=result.get("label"),
            meta_exec=result.get("meta_exec"),
        )
    raise HTTPException(status_code=502, detail=f"Unknown action: {action}")


async def _execute_action_and_log(
    db: aiosqlite.Connection,
    vm_mgr: VMManager,
    config: Config,
    rule: IngressRule,
    action: str,
    result: dict[str, Any],
) -> None:
    """Execute action in background and log the outcome."""
    try:
        await _execute_action(
            db=db, vm_mgr=vm_mgr, config=config,
            account_id=rule.account_id, action=action, result=result,
        )
    except Exception as exc:
        logger.warning(
            "Async ingress action failed for rule %s: %s", rule.id, exc,
        )
