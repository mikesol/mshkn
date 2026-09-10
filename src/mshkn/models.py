from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from mshkn.errors import InvalidInput

# The relay speaks these two schemes only, with their default ports.
_RELAY_SCHEMES = {"http": 80, "https": 443}


def _target_parts(url: str) -> tuple[tuple[str, str, int], str] | None:
    """A relay target as ((scheme, host, effective port), path), or None when it is
    not an absolute http or https URL. Origins are compared as this triple and never
    as text: `https://api.anthropic.com.evil.example` starts with
    `https://api.anthropic.com` and must not match it (#110)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _RELAY_SCHEMES or not parts.hostname:
        return None
    return (parts.scheme, parts.hostname, port or _RELAY_SCHEMES[parts.scheme]), parts.path


class ComputerStatus(StrEnum):
    CREATING = "creating"
    RUNNING = "running"
    DESTROYING = "destroying"
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


class RelayStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
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


# The scope document of a scoped key (#88). Three optional fields; an absent
# field means "none". `create_from` is "*" (any recipe on the account) or the
# set of recipe ids the key may create from, where "bare" stands for no recipe.
BARE = "bare"


@dataclass(frozen=True)
class RelayDelivery:
    """The wake-up a relay job causes: fork `label` with `exec <job_id>`."""

    label: str
    exec: str


@dataclass(frozen=True)
class Scopes:
    recipes_create: bool = False
    recipes_read: bool = False
    create_from: Literal["*"] | tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    # The relay (#110): URL prefixes the key may have called, and the one wake-up
    # it may cause. Both or neither; a key without them may relay nowhere.
    relay_targets: tuple[str, ...] = ()
    relay_deliver: RelayDelivery | None = None

    def may_create_from(self, recipe_id: str | None) -> bool:
        if self.create_from == "*":
            return recipe_id is not None
        return (BARE if recipe_id is None else recipe_id) in self.create_from

    def covers_label(self, label: str | None) -> bool:
        """True when the label starts with one of the prefixes; no label is never covered."""
        if not label:
            return False
        return any(label.startswith(prefix) for prefix in self.labels)

    @property
    def has_relay(self) -> bool:
        return bool(self.relay_targets) and self.relay_deliver is not None

    def may_relay_to(self, target: str) -> bool:
        """True when the target shares a prefix's scheme, host and effective port and
        its path lies under the prefix's path. `parse_scopes` has already checked that
        every prefix is an absolute http or https URL."""
        parts = _target_parts(target)
        if parts is None:
            return False
        origin, path = parts
        for prefix in self.relay_targets:
            allowed = _target_parts(prefix)
            if allowed is not None and allowed[0] == origin and path.startswith(allowed[1]):
                return True
        return False

    def to_document(self) -> dict[str, object]:
        doc: dict[str, object] = {}
        recipes: dict[str, bool] = {}
        if self.recipes_create:
            recipes["create"] = True
        if self.recipes_read:
            recipes["read"] = True
        if recipes:
            doc["recipes"] = recipes
        if self.create_from == "*":
            doc["computers"] = {"create_from": "*"}
        elif self.create_from:
            doc["computers"] = {"create_from": list(self.create_from)}
        if self.labels:
            doc["labels"] = list(self.labels)
        if self.has_relay:
            assert self.relay_deliver is not None
            doc["relay"] = {
                "targets": list(self.relay_targets),
                "deliver": {"label": self.relay_deliver.label, "exec": self.relay_deliver.exec},
            }
        return doc


@dataclass(frozen=True)
class ApiKey:
    """A scoped key: a second credential on an account that can only do what
    its scopes say. The secret is returned once, on creation."""

    id: str
    account_id: str
    secret: str
    scopes: Scopes
    label: str | None
    created_at: str


@dataclass(frozen=True)
class Principal:
    """Who is calling: the account, and the scoped key when the bearer was one.

    ``key`` is None for the account key, which is unrestricted.
    """

    account: Account
    key: ApiKey | None = None

    @property
    def scopes(self) -> Scopes | None:
        return None if self.key is None else self.key.scopes


def _reject(message: str) -> InvalidInput:
    return InvalidInput(f"Invalid scopes: {message}")


def _section(document: dict[str, object], name: str, allowed: set[str]) -> dict[str, object]:
    section = document.get(name, {})
    if not isinstance(section, dict):
        raise _reject(f"{name} must be an object")
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise _reject(f"unknown {name} fields {unknown}")
    return section


def _flag(section: dict[str, object], name: str, where: str) -> bool:
    value = section.get(name, False)
    if not isinstance(value, bool):
        raise _reject(f"{where}.{name} must be a boolean")
    return value


def _strings(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _reject(f"{where} must be a list of strings")
    return value


def parse_scopes(document: object) -> Scopes:
    """The scope document as a Scopes, or InvalidInput naming what is wrong."""
    if not isinstance(document, dict):
        raise _reject("must be an object")
    unknown = sorted(set(document) - {"recipes", "computers", "labels", "relay"})
    if unknown:
        raise _reject(f"unknown fields {unknown}")
    recipes = _section(document, "recipes", {"create", "read"})
    computers = _section(document, "computers", {"create_from"})
    raw_from = computers.get("create_from", [])
    create_from: Literal["*"] | tuple[str, ...] = (
        "*" if raw_from == "*" else tuple(_strings(raw_from, "computers.create_from"))
    )
    labels = tuple(_strings(document.get("labels", []), "labels"))
    if any(not label for label in labels):
        raise _reject("labels must not contain an empty prefix")
    relay_targets: tuple[str, ...] = ()
    relay_deliver: RelayDelivery | None = None
    if "relay" in document:
        relay = _section(document, "relay", {"targets", "deliver"})
        relay_targets = tuple(_strings(relay.get("targets"), "relay.targets"))
        if not relay_targets or any(not t for t in relay_targets):
            raise _reject("relay.targets must be a non-empty list of non-empty prefixes")
        if any(_target_parts(t) is None for t in relay_targets):
            raise _reject("relay.targets must be absolute http or https URLs with a host")
        deliver = relay.get("deliver")
        if not isinstance(deliver, dict) or set(deliver) != {"label", "exec"}:
            raise _reject("relay.deliver must be an object with label and exec")
        label, command = deliver["label"], deliver["exec"]
        if not isinstance(label, str) or not label or not isinstance(command, str) or not command:
            raise _reject("relay.deliver.label and relay.deliver.exec must be non-empty strings")
        relay_deliver = RelayDelivery(label=label, exec=command)
    return Scopes(
        recipes_create=_flag(recipes, "create", "recipes"),
        recipes_read=_flag(recipes, "read", "recipes"),
        create_from=create_from,
        labels=labels,
        relay_targets=relay_targets,
        relay_deliver=relay_deliver,
    )


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
    api_key_id: str | None = None  # the scoped key that created it; None for the account key

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


@dataclass(frozen=True)
class ExecLog:
    """What an ephemeral run did, kept after its computer is gone (#58)."""

    computer_id: str
    account_id: str
    source_checkpoint_id: str | None
    created_checkpoint_id: str | None
    label: str | None
    command: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    created_at: str


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
    computer_id: str | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """Lampas's policy: exponential backoff, min(initial * 2^attempt, max)."""

    attempts: int = 3
    initial_delay_ms: int = 1000
    max_delay_ms: int = 30000

    def delay(self, attempt: int) -> float:
        """Seconds to wait after the zero-based `attempt` failed."""
        exponent: int = 2**attempt
        delay_ms: int = min(self.initial_delay_ms * exponent, self.max_delay_ms)
        return delay_ms / 1000

    def to_document(self) -> dict[str, int]:
        return {
            "attempts": self.attempts,
            "initial_delay_ms": self.initial_delay_ms,
            "max_delay_ms": self.max_delay_ms,
        }


@dataclass
class RelayJob:
    """One upstream HTTP call plus at most one delivery (#110)."""

    id: str
    account_id: str
    api_key_id: str | None
    status: RelayStatus
    target: str
    method: str
    forward_headers: dict[str, str] | None
    body: object
    retry: RetryPolicy
    timeout_seconds: int
    deliver: RelayDelivery | None
    attempts: int
    error: str | None
    response_status: int | None
    response_headers: dict[str, str] | None
    response_body: object
    delivery_status: DeliveryStatus | None
    delivery_attempts: int
    delivery_computer_id: str | None
    delivery_deferred_id: str | None
    delivery_error: str | None
    created_at: str
    updated_at: str
