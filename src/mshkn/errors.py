"""Domain errors. The API layer maps these to HTTP responses (see api/errors.py)."""

from __future__ import annotations


class MshknError(Exception):
    """Base class for errors that carry a user-facing message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(MshknError):  # noqa: N818 -- name is part of the public API contract
    """A referenced resource does not exist (or is not visible to the caller)."""


class Conflict(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The operation is valid but the resource is in the wrong state for it."""


class InvalidInput(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The request is well-formed but its values are not acceptable."""


class LimitExceeded(MshknError):  # noqa: N818 -- name is part of the public API contract
    """A per-account or per-key limit was hit."""


class HostError(MshknError):
    """A host-side operation (dm-thin, tap, Firecracker, SSH, rclone) failed."""


class ConfigError(MshknError):
    """Startup configuration is invalid."""
