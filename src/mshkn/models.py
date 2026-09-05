from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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


@dataclass
class Computer:
    id: str
    account_id: str
    thin_volume_id: int
    tap_device: str
    vm_ip: str
    socket_path: str
    firecracker_pid: int | None
    manifest_hash: str
    manifest_json: str
    status: ComputerStatus
    created_at: str
    last_exec_at: str | None
    source_checkpoint_id: str | None = None
    recipe_id: str | None = None


@dataclass
class Checkpoint:
    id: str
    account_id: str
    parent_id: str | None
    computer_id: str | None
    thin_volume_id: int | None
    manifest_hash: str
    manifest_json: str
    r2_prefix: str
    disk_delta_size_bytes: int | None
    memory_size_bytes: int | None
    label: str | None
    pinned: bool
    created_at: str
    recipe_id: str | None = None


@dataclass(frozen=True)
class DeferredRequest:
    id: str
    label: str
    account_id: str
    request_payload: str
    created_at: str
