"""Ingress rules and the trigger path (spec §6.5).

The Starlark result is validated with Pydantic models instead of hand-rolled
set arithmetic; `create` actions take `recipe_id` and `needs` exactly like
REST, and the retired `capabilities`/`uses` fields are rejected as unknown.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from mshkn.db import (
    delete_ingress_rule,
    get_account_by_id,
    get_ingress_rule_by_id,
    insert_ingress_log,
    insert_ingress_rule,
    list_ingress_logs,
    list_ingress_rules_by_account,
    rotate_ingress_rule_id,
    update_ingress_rule,
)
from mshkn.errors import InvalidInput, LimitExceeded, NotFound, TransformError
from mshkn.models import (
    Checkpoint,
    Computer,
    EphemeralResult,
    ExecSpec,
    IngressLog,
    IngressLogStatus,
    IngressRule,
)
from mshkn.ratelimit import RateLimiter
from mshkn.resources import Resources
from mshkn.services.checkpoints import Deferred
from mshkn.services.starlark import StarlarkError, execute_transform, validate_starlark

if TYPE_CHECKING:
    import aiosqlite
    from pydantic_core import ErrorDetails

    from mshkn.models import Account
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.computers import ComputerService
    from mshkn.services.lifecycle import Lifecycle

logger = logging.getLogger(__name__)


class ForkAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["fork"]
    checkpoint_id: str | None = None
    label: str | None = None
    exec: str | None = None
    self_destruct: bool = False
    exclusive: Literal["error_on_conflict", "defer_on_conflict"] | None = None
    callback_url: str | None = None
    meta_exec: str | None = None

    @model_validator(mode="after")
    def _target(self) -> ForkAction:
        if self.checkpoint_id is None and self.label is None:
            raise ValueError("fork action requires 'checkpoint_id' or 'label'")
        return self


class CreateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create"]
    recipe_id: str | None = None
    needs: dict[str, object] | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    label: str | None = None
    meta_exec: str | None = None

    @field_validator("needs")
    @classmethod
    def _resources(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        """Pre-flight `needs` through the same parser `execute` uses.

        Without this a bad `needs` validates clean here and only fails later
        inside `execute`, so the dry-run endpoint would report no errors for a
        transform that is guaranteed to fail at trigger time. `execute` still
        parses it for real: `Resources.from_needs` stays the source of truth.
        """
        try:
            Resources.from_needs(value)
        except InvalidInput as exc:
            raise ValueError(exc.message) from None
        return value


def validate_transform_result(result: object) -> list[str]:
    """Errors for a Starlark transform result; empty means valid. None is valid."""
    if result is None:
        return []
    if not isinstance(result, dict):
        return ["transform must return a dict or None"]
    action = result.get("action")
    model: type[BaseModel]
    if action == "fork":
        model = ForkAction
    elif action == "create":
        model = CreateAction
    else:
        return [f"action must be 'fork' or 'create', got {action!r}"]
    try:
        model.model_validate(result)
    except ValidationError as exc:
        return [_describe(e) for e in exc.errors()]
    return []


def _describe(error: ErrorDetails) -> str:
    loc = ".".join(str(p) for p in error.get("loc", ()))
    if error.get("type") == "extra_forbidden":
        return f"unknown field for this action: {loc}"
    return f"{loc}: {error.get('msg', 'invalid')}" if loc else str(error.get("msg", "invalid"))


@dataclass(frozen=True)
class ForkOutcome:
    """A sync fork action that ran: the computer, what it forked from, what it did."""

    computer: Computer
    checkpoint: Checkpoint
    result: EphemeralResult


@dataclass(frozen=True)
class CreateOutcome:
    """A sync create action that ran."""

    computer: Computer
    result: EphemeralResult


IngressResult = Deferred | ForkOutcome | CreateOutcome


@dataclass(frozen=True)
class TriggerOutcome:
    """What the trigger produced. `result` is None for 204 (no action) and 202
    (accepted, still running); the router owns every JSON shape."""

    status_code: int
    result: IngressResult | None


class IngressService:
    def __init__(
        self,
        db: aiosqlite.Connection,
        computers: ComputerService,
        checkpoints: CheckpointService,
        lifecycle: Lifecycle,
        tasks: BackgroundTasks,
    ) -> None:
        self.db = db
        self.computers = computers
        self.checkpoints = checkpoints
        self.lifecycle = lifecycle
        self.tasks = tasks
        self._limiters: dict[str, RateLimiter] = {}

    # -- rules ---------------------------------------------------------------

    async def create_rule(
        self,
        account: Account,
        *,
        name: str,
        starlark_source: str,
        response_mode: str,
        max_body_bytes: int,
        rate_limit_rpm: int,
    ) -> IngressRule:
        self._check_source(starlark_source)
        now = datetime.now(UTC).isoformat()
        rule = IngressRule(
            internal_id=str(uuid.uuid4()),
            id=f"ir_{secrets.token_urlsafe(20)}",
            account_id=account.id,
            name=name,
            starlark_source=starlark_source,
            response_mode=response_mode,
            max_body_bytes=max_body_bytes,
            rate_limit_rpm=rate_limit_rpm,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        await insert_ingress_rule(self.db, rule)
        return rule

    async def list_rules(self, account: Account) -> list[IngressRule]:
        return await list_ingress_rules_by_account(self.db, account.id)

    async def get_rule(self, account: Account, rule_id: str) -> IngressRule:
        rule = await get_ingress_rule_by_id(self.db, rule_id)
        if rule is None or rule.account_id != account.id:
            raise NotFound("Ingress rule not found")
        return rule

    async def update_rule(
        self,
        account: Account,
        rule_id: str,
        *,
        name: str | None = None,
        starlark_source: str | None = None,
        response_mode: str | None = None,
        max_body_bytes: int | None = None,
        rate_limit_rpm: int | None = None,
        enabled: bool | None = None,
    ) -> IngressRule:
        rule = await self.get_rule(account, rule_id)
        if starlark_source is not None:
            self._check_source(starlark_source)
            rule.starlark_source = starlark_source
        if name is not None:
            rule.name = name
        if response_mode is not None:
            rule.response_mode = response_mode
        if max_body_bytes is not None:
            rule.max_body_bytes = max_body_bytes
        if rate_limit_rpm is not None:
            rule.rate_limit_rpm = rate_limit_rpm
        if enabled is not None:
            rule.enabled = enabled
        rule.updated_at = datetime.now(UTC).isoformat()
        await update_ingress_rule(self.db, rule)
        return rule

    async def delete_rule(self, account: Account, rule_id: str) -> None:
        rule = await self.get_rule(account, rule_id)
        await delete_ingress_rule(self.db, rule_id)
        self._limiters.pop(rule.internal_id, None)

    async def rotate_rule(self, account: Account, rule_id: str) -> IngressRule:
        rule = await self.get_rule(account, rule_id)
        new_id = f"ir_{secrets.token_urlsafe(20)}"
        await rotate_ingress_rule_id(self.db, rule.internal_id, new_id)
        rule.id = new_id
        rule.updated_at = datetime.now(UTC).isoformat()
        return rule

    async def logs(self, account: Account, rule_id: str) -> list[IngressLog]:
        rule = await self.get_rule(account, rule_id)
        return await list_ingress_logs(self.db, rule.internal_id)

    def test_rule(
        self, rule: IngressRule, request_dict: dict[str, object]
    ) -> tuple[dict[str, Any] | None, list[str], float]:
        t0 = time.monotonic()
        try:
            result = execute_transform(rule.starlark_source, request_dict)
        except StarlarkError as exc:
            return None, [str(exc)], (time.monotonic() - t0) * 1000
        elapsed_ms = (time.monotonic() - t0) * 1000
        return result, validate_transform_result(result), elapsed_ms

    def limiter_for(self, rule: IngressRule) -> RateLimiter:
        """The rule's limiter, keyed by its stable internal id.

        The public id rotates; the window must not, or rotating a leaked URL
        would hand the caller a fresh minute's worth of requests.
        """
        limiter = self._limiters.get(rule.internal_id)
        if limiter is None or limiter.max_requests != rule.rate_limit_rpm:
            limiter = RateLimiter(max_requests=rule.rate_limit_rpm, window_seconds=60.0)
            self._limiters[rule.internal_id] = limiter
        return limiter

    @staticmethod
    def _check_source(source: str) -> None:
        errors = validate_starlark(source)
        if errors:
            raise InvalidInput("invalid starlark", detail={"starlark_errors": errors})

    # -- trigger -------------------------------------------------------------

    async def enabled_rule(self, rule_id: str) -> IngressRule:
        """The rule a trigger URL points at. A disabled rule is as good as absent.

        The router needs the rule before the body (the size limit is per rule),
        so it resolves it here and hands it back to trigger().
        """
        rule = await get_ingress_rule_by_id(self.db, rule_id)
        if rule is None or not rule.enabled:
            raise NotFound("Ingress rule not found")
        return rule

    async def trigger(self, rule: IngressRule, request_dict: dict[str, object]) -> TriggerOutcome:
        if not self.limiter_for(rule).check(rule.internal_id):
            raise LimitExceeded("Rate limit exceeded")
        try:
            result = execute_transform(rule.starlark_source, request_dict)
        except StarlarkError as exc:
            await self._log(rule, IngressLogStatus.FAILED, None, str(exc))
            raise TransformError(
                "starlark failed", detail=f"Starlark execution error: {exc}"
            ) from None
        if result is None:
            await self._log(rule, IngressLogStatus.COMPLETED, None, None)
            return TriggerOutcome(204, None)
        errors = validate_transform_result(result)
        if errors:
            await self._log(rule, IngressLogStatus.FAILED, json.dumps(result), "; ".join(errors))
            raise TransformError(
                "invalid transform result", detail={"errors": errors, "starlark_result": result}
            )
        account = await get_account_by_id(self.db, rule.account_id)
        if account is None:
            raise NotFound("Ingress rule's account not found")
        if rule.response_mode == "async":
            self.tasks.spawn(
                self._execute_and_log(account, rule, result), name=f"ingress:{rule.id}"
            )
            await self._log(rule, IngressLogStatus.ACCEPTED, json.dumps(result), None)
            return TriggerOutcome(202, None)
        try:
            executed = await self.execute(account, result)
        except Exception as exc:
            await self._log(rule, IngressLogStatus.FAILED, json.dumps(result), _error_text(exc))
            raise
        await self._log(rule, IngressLogStatus.COMPLETED, json.dumps(result), None)
        return TriggerOutcome(200, executed)

    async def execute(self, account: Account, action: dict[str, Any]) -> IngressResult:
        if action["action"] == "fork":
            fork = ForkAction.model_validate(action)
            if fork.checkpoint_id is not None:
                checkpoint = await self.checkpoints.get_owned(account, fork.checkpoint_id)
            else:
                assert fork.label is not None
                latest = await self.checkpoints.latest_for_label(account, fork.label)
                if latest is None:
                    raise NotFound(f"No checkpoint with label '{fork.label}'")
                checkpoint = latest
            spec = ExecSpec(
                command=fork.exec,
                self_destruct=fork.self_destruct,
                callback_url=fork.callback_url,
                label=None,
                meta_exec=fork.meta_exec,
            )
            forked = await self.checkpoints.fork_or_defer(
                account, checkpoint, spec, recipe_id=checkpoint.recipe_id, exclusive=fork.exclusive
            )
            if isinstance(forked, Deferred):
                return forked
            outcome = await self.lifecycle.run_ephemeral(
                account, forked, spec, source_checkpoint=checkpoint
            )
            return ForkOutcome(computer=forked, checkpoint=checkpoint, result=outcome)
        create = CreateAction.model_validate(action)
        resources = Resources.from_needs(create.needs)
        computer = await self.computers.create(
            account, recipe_id=create.recipe_id, resources=resources
        )
        spec = ExecSpec(
            command=create.exec,
            self_destruct=create.self_destruct,
            callback_url=create.callback_url,
            label=create.label,
            meta_exec=create.meta_exec,
        )
        outcome = await self.lifecycle.run_ephemeral(
            account, computer, spec, source_checkpoint=None
        )
        return CreateOutcome(computer=computer, result=outcome)

    async def _execute_and_log(
        self, account: Account, rule: IngressRule, action: dict[str, Any]
    ) -> None:
        try:
            await self.execute(account, action)
        except Exception as exc:
            logger.warning("Async ingress action failed for rule %s: %s", rule.id, exc)
            await self._log(rule, IngressLogStatus.FAILED, json.dumps(action), _error_text(exc))

    async def _log(
        self, rule: IngressRule, status: IngressLogStatus, result: str | None, error: str | None
    ) -> None:
        log = IngressLog(
            id=f"ilog-{uuid.uuid4().hex[:12]}",
            rule_internal_id=rule.internal_id,
            status=status,
            starlark_result=result,
            error_message=error,
            created_at=datetime.now(UTC).isoformat(),
        )
        try:
            await insert_ingress_log(self.db, log)
        except Exception:
            logger.warning("Failed to write ingress log for %s", rule.internal_id)


def _error_text(exc: Exception) -> str:
    message = getattr(exc, "message", None)
    return str(message) if message else str(exc)
