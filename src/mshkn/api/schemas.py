"""Every request and response model the HTTP layer speaks.

The routers translate between these and the service layer's domain objects;
nothing here knows how a computer is booted or a checkpoint is frozen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from mshkn.models import Computer, EphemeralResult, RelayJob

# --- computers ---------------------------------------------------------------


class CreateRequest(BaseModel):
    recipe_id: str | None = None
    needs: dict[str, object] | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    label: str | None = None
    meta_exec: str | None = None


class CreateResponse(BaseModel):
    computer_id: str
    url: str
    recipe_id: str | None = None
    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None


class ExecRequest(BaseModel):
    command: str
    timeout_seconds: int = Field(default=60, ge=1, le=600)


class ExecBgResponse(BaseModel):
    pid: int


class ExecKillResponse(BaseModel):
    status: str
    stderr: str | None = None


class UploadResponse(BaseModel):
    status: str
    path: str


class ExecLogResponse(BaseModel):
    """What an ephemeral run did, readable after its computer is gone (#58)."""

    computer_id: str
    source_checkpoint_id: str | None = None
    created_checkpoint_id: str | None = None
    label: str | None = None
    command: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    created_at: str


class ComputerStatusResponse(BaseModel):
    computer_id: str
    status: str
    url: str
    vm_ip: str
    recipe_id: str | None = None
    created_at: str
    last_exec_at: str | None = None
    cpu_pct: float | None = None
    ram_usage_mb: int | None = None
    ram_total_mb: int | None = None
    disk_usage_mb: int | None = None
    disk_total_mb: int | None = None
    processes: list[dict[str, object]] | None = None


class CheckpointRequest(BaseModel):
    label: str | None = None
    pin: bool = False


class CheckpointResponse(BaseModel):
    checkpoint_id: str
    recipe_id: str | None = None


class DestroyResponse(BaseModel):
    status: str


# --- checkpoints -------------------------------------------------------------


class ForkRequest(BaseModel):
    recipe_id: str | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    exclusive: Literal["error_on_conflict", "defer_on_conflict"] | None = None
    meta_exec: str | None = None


class ForkByLabelRequest(ForkRequest):
    label: str


class ForkResponse(BaseModel):
    computer_id: str
    checkpoint_id: str
    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None


class DeferredResponse(BaseModel):
    deferred_id: str
    status: str


class AcceptedResponse(BaseModel):
    status: str


class MergeRequest(BaseModel):
    checkpoint_a: str
    checkpoint_b: str


class MergeConflict(BaseModel):
    path: str
    resolution: str


class MergeResponse(BaseModel):
    checkpoint_id: str
    conflicts: list[MergeConflict]
    auto_merged: int
    unchanged: int


class CheckpointSummary(BaseModel):
    id: str
    checkpoint_id: str
    parent_id: str | None = None
    computer_id: str | None = None
    recipe_id: str | None = None
    r2_prefix: str
    disk_delta_size_bytes: int | None = None
    memory_size_bytes: int | None = None
    label: str | None = None
    pinned: bool
    created_at: str


class DeleteResponse(BaseModel):
    status: str


# --- recipes -----------------------------------------------------------------


class CreateRecipeRequest(BaseModel):
    dockerfile: str


class RecipeResponse(BaseModel):
    recipe_id: str
    status: str
    content_hash: str
    build_log: str | None = None
    base_volume_id: int | None = None
    created_at: str | None = None
    built_at: str | None = None


# --- ingress -----------------------------------------------------------------


class IngressRuleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    starlark_source: str = Field(..., min_length=1)
    response_mode: Literal["async", "sync"] = "async"
    max_body_bytes: int = Field(default=10485760, ge=1024, le=104857600)
    rate_limit_rpm: int = Field(default=60, ge=1, le=10000)


class IngressRuleUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    starlark_source: str | None = Field(default=None, min_length=1)
    response_mode: Literal["async", "sync"] | None = None
    max_body_bytes: int | None = Field(default=None, ge=1024, le=104857600)
    rate_limit_rpm: int | None = Field(default=None, ge=1, le=10000)
    enabled: bool | None = None


class IngressRuleResponse(BaseModel):
    id: str
    name: str
    ingress_url: str
    response_mode: str
    max_body_bytes: int
    rate_limit_rpm: int
    enabled: bool
    created_at: str
    updated_at: str


class IngressRuleDetail(IngressRuleResponse):
    starlark_source: str


class IngressTestRequest(BaseModel):
    method: str = "POST"
    path: str = "/"
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class IngressTestResponse(BaseModel):
    starlark_result: dict[str, Any] | None
    validation_errors: list[str]
    execution_time_ms: float


class IngressLogResponse(BaseModel):
    id: str
    status: str
    starlark_result: dict[str, Any] | None
    error_message: str | None
    created_at: str
    computer_id: str | None = None


# --- keys --------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    scopes: dict[str, Any]
    label: str | None = None


class KeyResponse(BaseModel):
    id: str
    label: str | None = None
    scopes: dict[str, Any]
    created_at: str


class KeyCreatedResponse(KeyResponse):
    """The one response that carries the secret."""

    secret: str


# --- system ------------------------------------------------------------------


class AlertResponse(BaseModel):
    level: str
    source: str
    message: str
    value: float
    threshold: float
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    subsystems: dict[str, str] = Field(default_factory=dict)


# --- relay -------------------------------------------------------------------


class RelayRetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: int = Field(default=3, ge=1)
    backoff: Literal["exponential"] = "exponential"
    initial_delay_ms: int = Field(default=1000, ge=0)
    max_delay_ms: int = Field(default=30000, ge=1)


class RelayDeliverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    exec: str = Field(min_length=1)


class RelayRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "POST"
    forward_headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    retry: RelayRetryBody = Field(default_factory=RelayRetryBody)
    timeout_seconds: int | None = Field(default=None, ge=1)
    deliver: RelayDeliverBody | None = None


class RelayAcceptedResponse(BaseModel):
    job_id: str
    status: str


class RelayResponseBody(BaseModel):
    status: int
    headers: dict[str, str]
    body: Any


class RelayDeliveryResponse(BaseModel):
    label: str
    exec: str
    status: str
    attempts: int
    computer_id: str | None = None
    deferred_id: str | None = None
    error: str | None = None


class RelayJobResponse(BaseModel):
    job_id: str
    status: str
    target: str
    method: str
    created_at: str
    updated_at: str
    attempts: int
    error: str | None = None
    response: RelayResponseBody | None = None
    delivery: RelayDeliveryResponse | None = None


def relay_job_response(job: RelayJob) -> RelayJobResponse:
    response = None
    if job.response_status is not None:
        response = RelayResponseBody(
            status=job.response_status, headers=job.response_headers or {}, body=job.response_body
        )
    delivery = None
    if job.deliver is not None:
        delivery = RelayDeliveryResponse(
            label=job.deliver.label,
            exec=job.deliver.exec,
            status=str(job.delivery_status),
            attempts=job.delivery_attempts,
            computer_id=job.delivery_computer_id,
            deferred_id=job.delivery_deferred_id,
            error=job.delivery_error,
        )
    return RelayJobResponse(
        job_id=job.id,
        status=str(job.status),
        target=job.target,
        method=job.method,
        created_at=job.created_at,
        updated_at=job.updated_at,
        attempts=job.attempts,
        error=job.error,
        response=response,
        delivery=delivery,
    )


# --- shared constructors -----------------------------------------------------
# One body per concept: the REST routers and the ingress router build these
# through the same functions, so the shapes cannot drift apart.


def create_response(computer: Computer, result: EphemeralResult, *, domain: str) -> CreateResponse:
    """The body of a create that ran. REST create and ingress create share it."""
    return CreateResponse(
        computer_id=computer.id,
        url=f"https://{computer.id}.{domain}",
        recipe_id=computer.recipe_id,
        exec_exit_code=result.exec_exit_code,
        exec_stdout=result.exec_stdout,
        exec_stderr=result.exec_stderr,
        created_checkpoint_id=result.created_checkpoint_id,
    )


def fork_response(computer: Computer, checkpoint_id: str, result: EphemeralResult) -> ForkResponse:
    """The body of a fork that ran. REST fork and ingress fork share it."""
    return ForkResponse(
        computer_id=computer.id,
        checkpoint_id=checkpoint_id,
        exec_exit_code=result.exec_exit_code,
        exec_stdout=result.exec_stdout,
        exec_stderr=result.exec_stderr,
        created_checkpoint_id=result.created_checkpoint_id,
    )
