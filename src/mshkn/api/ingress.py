"""Ingress rules CRUD API and unauthenticated trigger endpoint."""

from __future__ import annotations

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

from mshkn.api.deps import get_runtime, require_account
from mshkn.db.ingress import (
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
    IngressLogStatus,
    IngressRule,
    IngressRuleCreateRequest,
    IngressRuleResponse,
    IngressRuleUpdateRequest,
    IngressTestRequest,
    IngressTestResponse,
)
from mshkn.services.ingress import validate_transform_result
from mshkn.services.starlark import StarlarkError, execute_transform, validate_starlark

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Account
    from mshkn.runtime import BackgroundTasks
    from mshkn.vm.manager import VMManager

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


# --- CRUD Endpoints (authenticated) ---


@router.post("/ingress_rules", response_model=IngressRuleResponse)
async def create_rule(
    request: Request,
    body: IngressRuleCreateRequest,
    account: Account = _require_account,
) -> IngressRuleResponse:
    rt = get_runtime(request)
    db = rt.db
    config = rt.config

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
    rt = get_runtime(request)
    db = rt.db
    config = rt.config
    rules = await list_ingress_rules_by_account(db, account.id)
    return [_rule_to_response(r, config.domain) for r in rules]


@router.get("/ingress_rules/{rule_id}")
async def get_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> dict[str, object]:
    rt = get_runtime(request)
    db = rt.db
    config = rt.config
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
    rt = get_runtime(request)
    db = rt.db
    config = rt.config
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
    rt = get_runtime(request)
    db = rt.db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")
    await delete_ingress_rule(db, rule_id)
    # Clean up cached rate limiter
    rt.rule_limiters.pop(rule_id, None)
    return Response(status_code=204)


@router.post("/ingress_rules/{rule_id}/rotate", response_model=IngressRuleResponse)
async def rotate_rule(
    rule_id: str,
    request: Request,
    account: Account = _require_account,
) -> IngressRuleResponse:
    rt = get_runtime(request)
    db = rt.db
    config = rt.config
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")

    new_id = f"ir_{secrets.token_urlsafe(20)}"
    await rotate_ingress_rule_id(db, rule.internal_id, new_id)

    # Move rate limiter to new key
    old_limiter = rt.rule_limiters.pop(rule_id, None)
    if old_limiter is not None:
        rt.rule_limiters[new_id] = old_limiter

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
    db = get_runtime(request).db
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

    validation_errors = validate_transform_result(result)
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
    db = get_runtime(request).db
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or rule.account_id != account.id:
        raise HTTPException(status_code=404, detail="Ingress rule not found")

    logs = await list_ingress_logs(db, rule.internal_id)
    return [
        IngressLogResponse(
            id=log.id,
            status=log.status,
            starlark_result=(json.loads(log.starlark_result) if log.starlark_result else None),
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
    tasks: BackgroundTasks,
    exec_cmd: str | None = None,
    self_destruct: bool = False,
    callback_url: str | None = None,
    label: str | None = None,
    meta_exec: str | None = None,  # noqa: ARG001
) -> dict[str, object]:
    """Core create-computer logic, shared by API endpoint and ingress trigger."""
    from mshkn.api.computers import _self_destruct
    from mshkn.db import count_active_computers_by_account, get_account_by_id

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
        result = await vm_manager.host.guest.exec(computer.vm_ip, exec_cmd)
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
                host=vm_manager.host,
                tasks=tasks,
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
    tasks: BackgroundTasks,
    exec_cmd: str | None = None,
    self_destruct: bool = False,
    callback_url: str | None = None,
    exclusive: str | None = None,
    meta_exec: str | None = None,
) -> dict[str, object]:
    """Core fork-from-checkpoint logic, shared by API endpoint and ingress trigger."""
    from mshkn.api.computers import _self_destruct
    from mshkn.db import get_active_computer_for_label, get_checkpoint, insert_deferred

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
                    db,
                    deferred_id,
                    ckpt.label,
                    account_id,
                    json.dumps(payload),
                    now,
                )
                return {"deferred_id": deferred_id, "status": "queued"}

    account_obj = await _get_account(db, account_id)
    computer = await vm_manager.fork_from_checkpoint(account_id, ckpt, recipe_id=ckpt.recipe_id)

    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None

    if exec_cmd is not None:
        result = await vm_manager.host.guest.exec(computer.vm_ip, exec_cmd)
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
                host=vm_manager.host,
                tasks=tasks,
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
    rt = get_runtime(request)
    db = rt.db
    config = rt.config

    # 1. Look up rule
    rule = await get_ingress_rule_by_id(db, rule_id)
    if rule is None or not rule.enabled:
        raise HTTPException(status_code=404, detail="Ingress rule not found")

    # 2. Per-rule rate limit
    limiter = rt.rule_limiter(rule_id, rule.rate_limit_rpm)
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
            status_code=400,
            detail="Failed to parse request body",
        ) from None

    # 4. Execute Starlark transform
    try:
        result = execute_transform(rule.starlark_source, request_dict)
    except StarlarkError as exc:
        # Log the failure
        await _log_invocation(db, rule.internal_id, IngressLogStatus.FAILED, None, str(exc))
        raise HTTPException(
            status_code=502,
            detail=f"Starlark execution error: {exc}",
        ) from None

    # 5. Validate result
    if result is None:
        await _log_invocation(db, rule.internal_id, IngressLogStatus.COMPLETED, None, None)
        return Response(status_code=204)

    validation_errors = validate_transform_result(result)
    if validation_errors:
        await _log_invocation(
            db,
            rule.internal_id,
            IngressLogStatus.FAILED,
            json.dumps(result),
            "; ".join(validation_errors),
        )
        raise HTTPException(
            status_code=502,
            detail={"errors": validation_errors, "starlark_result": result},
        )

    # 6. Execute action
    vm_mgr = rt.vm_manager
    action = result["action"]

    if rule.response_mode == "async":
        # Fire-and-forget
        rt.tasks.spawn(
            _execute_action_and_log(
                db=db,
                vm_mgr=vm_mgr,
                config=config,
                rule=rule,
                action=action,
                result=result,
                tasks=rt.tasks,
            ),
            name=f"ingress:{rule.id}",
        )

        await _log_invocation(
            db,
            rule.internal_id,
            IngressLogStatus.ACCEPTED,
            json.dumps(result),
            None,
        )
        return JSONResponse(status_code=202, content={"status": IngressLogStatus.ACCEPTED})

    # Sync: wait and return
    try:
        action_result = await _execute_action(
            db=db,
            vm_mgr=vm_mgr,
            config=config,
            account_id=rule.account_id,
            action=action,
            result=result,
            tasks=rt.tasks,
        )
        await _log_invocation(
            db,
            rule.internal_id,
            IngressLogStatus.COMPLETED,
            json.dumps(result),
            None,
        )
        return JSONResponse(status_code=200, content=action_result)
    except HTTPException as exc:
        await _log_invocation(
            db,
            rule.internal_id,
            IngressLogStatus.FAILED,
            json.dumps(result),
            str(exc.detail),
        )
        raise
    except Exception as exc:
        await _log_invocation(
            db,
            rule.internal_id,
            IngressLogStatus.FAILED,
            json.dumps(result),
            str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from None


async def _log_invocation(
    db: aiosqlite.Connection,
    rule_internal_id: str,
    status: IngressLogStatus,
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
    tasks: BackgroundTasks,
) -> dict[str, object]:
    """Execute a fork or create action from a Starlark transform result."""
    if action == "fork":
        # Resolve label to checkpoint_id if needed
        checkpoint_id = result.get("checkpoint_id")
        if checkpoint_id is None:
            label = result.get("label")
            if label is None:
                raise HTTPException(
                    status_code=502,
                    detail="fork needs checkpoint_id or label",
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
            tasks=tasks,
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
            tasks=tasks,
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
    tasks: BackgroundTasks,
) -> None:
    """Execute action in background and log the outcome."""
    try:
        await _execute_action(
            db=db,
            vm_mgr=vm_mgr,
            config=config,
            account_id=rule.account_id,
            action=action,
            result=result,
            tasks=tasks,
        )
    except Exception as exc:
        logger.warning(
            "Async ingress action failed for rule %s: %s",
            rule.id,
            exc,
        )
