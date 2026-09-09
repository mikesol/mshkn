"""Who is speaking (spec §6). Root is minted only by the authenticated door;
a hook asserts a name inside the namespace it declared; everything else is
anonymous (§10.1)."""

from __future__ import annotations

import re

from membrane.declarations import RESERVED_NAMESPACES

ROOT = "root"
ANONYMOUS = "anonymous"
NAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]+$")


def is_authenticated(principal: str) -> bool:
    return principal != ANONYMOUS


def namespace_of(principal: str) -> str | None:
    head, sep, _ = principal.partition(":")
    return head if sep else None


def principal_from_hook(namespace: str, stdout: str, exit_code: int) -> str:
    """A hook that fails, prints nothing, or prints a reserved name yields anonymous."""
    if namespace in RESERVED_NAMESPACES or exit_code != 0:
        return ANONYMOUS
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return ANONYMOUS
    name = lines[0]
    if name in RESERVED_NAMESPACES or name in (ROOT, ANONYMOUS) or not NAME_RE.match(name):
        return ANONYMOUS
    return f"{namespace}:{name}"
