from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class ComputerStatus(StrEnum):
    CREATING = "creating"
    RUNNING = "running"
    DESTROYED = "destroyed"


class RecipeStatus(StrEnum):
    PENDING = "pending"
    BUILDING = "building"
    EXPORTING = "exporting"
    INJECTING = "injecting"
    READY = "ready"
    FAILED = "failed"


class CheckpointTrigger(StrEnum):
    API = "api"
    SELF_DESTRUCT = "self_destruct"
    IDLE = "idle"


class IngressLogStatus(StrEnum):
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"


ExclusiveMode = Literal["error_on_conflict", "defer_on_conflict"]


# dm-thin device names. scripts/e2e.sh cleans orphans by these prefixes, and the
# callers below need them before the row they belong to exists, so they are
# functions of the id and the dataclass properties delegate to them.
def computer_volume_name(computer_id: str) -> str:
    return f"mshkn-{computer_id}"


def checkpoint_volume_name(checkpoint_id: str) -> str:
    return f"mshkn-ckpt-{checkpoint_id}"


def recipe_volume_name(recipe_id: str) -> str:
    return f"mshkn-recipe-{recipe_id}"


@dataclass
class Account:
    id: str
    api_key: str
    vm_limit: int
    created_at: str


@dataclass
class Recipe:
    id: str
    account_id: str
    dockerfile: str
    content_hash: str
    status: RecipeStatus
    build_log: str | None
    base_volume_id: int | None
    template_vmstate: str | None
    template_memory: str | None
    created_at: str
    built_at: str | None

    @property
    def volume_name(self) -> str:
        return recipe_volume_name(self.id)


@dataclass
class Computer:
    id: str
    account_id: str
    thin_volume_id: int
    tap_device: str
    vm_ip: str
    socket_path: str
    firecracker_pid: int | None
    status: ComputerStatus
    created_at: str
    last_exec_at: str | None
    source_checkpoint_id: str | None = None
    recipe_id: str | None = None

    @property
    def slot(self) -> int:
        return int(self.tap_device.removeprefix("tap"))

    @property
    def volume_name(self) -> str:
        return computer_volume_name(self.id)


@dataclass
class Checkpoint:
    id: str
    account_id: str
    parent_id: str | None
    computer_id: str | None
    thin_volume_id: int | None
    r2_prefix: str
    disk_delta_size_bytes: int | None
    memory_size_bytes: int | None
    label: str | None
    pinned: bool
    created_at: str
    recipe_id: str | None = None

    @property
    def volume_name(self) -> str:
        return checkpoint_volume_name(self.id)


@dataclass(frozen=True)
class DeferredRequest:
    id: str
    label: str
    account_id: str
    request_payload: str
    created_at: str


@dataclass(frozen=True)
class ExecSpec:
    """What to do with a freshly created or forked computer (spec §6.4)."""

    command: str | None
    self_destruct: bool
    callback_url: str | None
    label: str | None
    meta_exec: str | None


@dataclass(frozen=True)
class EphemeralResult:
    computer_id: str
    exec_exit_code: int | None
    exec_stdout: str | None
    exec_stderr: str | None
    created_checkpoint_id: str | None


@dataclass
class Alert:
    level: str  # "warning" or "critical"
    source: str  # "nvme", "ram", "thin_pool_data", "thin_pool_metadata"
    message: str
    value: float
    threshold: float
    timestamp: str  # ISO 8601


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
    status: IngressLogStatus
    starlark_result: str | None
    error_message: str | None
    created_at: str
