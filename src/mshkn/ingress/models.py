from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- DB dataclasses ---


@dataclass
class IngressRule:
    internal_id: str
    id: str
    account_id: str
    name: str
    starlark_source: str
    response_mode: str  # "async" | "sync"
    max_body_bytes: int
    rate_limit_rpm: int
    enabled: bool
    created_at: str
    updated_at: str


@dataclass
class IngressLog:
    id: str
    rule_internal_id: str
    status: str  # "accepted" | "completed" | "failed" | "rejected"
    starlark_result: str | None
    error_message: str | None
    created_at: str


# --- Pydantic request/response models ---


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
