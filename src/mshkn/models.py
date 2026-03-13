from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Manifest:
    uses: list[str]

    def content_hash(self) -> str:
        normalized = sorted(self.uses)
        raw = json.dumps(normalized, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps({"uses": self.uses}, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        data = json.loads(raw)
        return cls(uses=data["uses"])


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
    status: str  # pending | building | ready | failed
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
    status: str
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


@dataclass
class CapabilityCacheEntry:
    manifest_hash: str
    volume_id: int
    nix_closure_size_bytes: int | None
    last_used_at: str
    created_at: str
