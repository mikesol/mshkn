"""What a scoped key may do, route by route (#88).

Every function is a check the router runs before any service does work. A
principal carrying the account key passes every check; a scoped key passes
when its scope document says so, and otherwise gets a `Forbidden` (403)
naming the scope that stopped it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.errors import Forbidden, InvalidInput, NotFound

if TYPE_CHECKING:
    from mshkn.models import Checkpoint, Computer, Principal, RelayJob


def require_account_key(principal: Principal) -> None:
    """Routes a scoped key may never call: merge, recipe delete."""
    if principal.key is not None:
        raise Forbidden("This route requires the account key")


def require_recipes_create(principal: Principal) -> None:
    if principal.scopes is not None and not principal.scopes.recipes_create:
        raise Forbidden("Scope recipes.create is not granted to this key")


def require_recipes_read(principal: Principal) -> None:
    if principal.scopes is not None and not principal.scopes.recipes_read:
        raise Forbidden("Scope recipes.read is not granted to this key")


def require_create_from(principal: Principal, recipe_id: str | None) -> None:
    if principal.scopes is not None and not principal.scopes.may_create_from(recipe_id):
        source = "bare" if recipe_id is None else recipe_id
        raise Forbidden(f"Scope computers.create_from does not allow {source}")


def require_label(principal: Principal, label: str | None) -> None:
    """A label given on create or checkpoint must fall under the key's prefixes."""
    if label is None or principal.scopes is None:
        return
    if not principal.scopes.covers_label(label):
        raise Forbidden(f"Scope labels does not cover {label!r}")


def require_checkpoint_label(principal: Principal, checkpoint: Checkpoint) -> None:
    """Fork and delete need the checkpoint's label under the key's prefixes;
    an unlabelled checkpoint is never reachable by a scoped key."""
    if principal.scopes is None:
        return
    if not principal.scopes.covers_label(checkpoint.label):
        raise Forbidden(
            f"Scope labels does not cover checkpoint {checkpoint.id} (label {checkpoint.label!r})"
        )


def require_computer_owner(principal: Principal, computer: Computer) -> None:
    """Every /computers/{id}/… route: the computer must have been created by this key."""
    if principal.key is not None and computer.api_key_id != principal.key.id:
        raise Forbidden(f"Scope computers: {computer.id} was not created by this key")


def visible_checkpoints(principal: Principal, checkpoints: list[Checkpoint]) -> list[Checkpoint]:
    """The listing a key may see: everything for the account key, only labels
    under the prefixes for a scoped one."""
    scopes = principal.scopes
    if scopes is None:
        return checkpoints
    return [c for c in checkpoints if scopes.covers_label(c.label)]


def key_id(principal: Principal) -> str | None:
    """What to record on a computer this principal creates."""
    return None if principal.key is None else principal.key.id


def require_relay(principal: Principal, target: str, *, deliver_in_body: bool) -> None:
    """`POST /relay`: a scoped key needs the relay section, a target under its
    prefixes, and no `deliver` of its own (the scope pins it, #110)."""
    scopes = principal.scopes
    if scopes is None:
        return
    if not scopes.has_relay:
        raise Forbidden("Scope relay is not granted to this key")
    if not scopes.may_relay_to(target):
        raise Forbidden(f"Scope relay.targets does not allow {target}")
    if deliver_in_body:
        raise InvalidInput("deliver is pinned by this key's scope and may not be given")


def require_relay_job_owner(principal: Principal, job: RelayJob) -> None:
    """`GET /relay/{job_id}`: a scoped key sees only the jobs it created."""
    if principal.key is not None and job.api_key_id != principal.key.id:
        raise NotFound("Relay job not found")
