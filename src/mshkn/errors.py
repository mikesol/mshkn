"""Domain errors. The API layer maps these to HTTP responses (see api/errors.py)."""

from __future__ import annotations


class MshknError(Exception):
    """Base class for errors that carry a user-facing message.

    ``detail`` is an optional structured payload the API returns verbatim
    under ``{"detail": ...}`` instead of the message (ingress validation
    errors carry a list, for example).
    """

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFound(MshknError):  # noqa: N818 -- name is part of the public API contract
    """A referenced resource does not exist (or is not visible to the caller)."""


class Conflict(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The operation is valid but the resource is in the wrong state for it."""


class BadRequest(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The request cannot be carried out as stated (legacy 400 contract: merge
    validation, operations on a computer that is not running)."""


class InvalidInput(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The request is well-formed but its values are not acceptable."""


class PayloadTooLarge(MshknError):  # noqa: N818 -- name is part of the public API contract
    """The request body exceeds the configured limit."""


class LimitExceeded(MshknError):  # noqa: N818 -- name is part of the public API contract
    """A per-account or per-key limit was hit."""


class TransformError(MshknError):
    """An ingress rule's Starlark failed or returned an invalid action (502)."""


class HostError(MshknError):
    """A host-side operation failed.

    Raised by the dm-thin, tap and rclone paths (as ``ShellError``), by the
    Firecracker and SSH wrappers, by ``CaddyProxy``, and by the fake host.
    """


class ConfigError(MshknError):
    """Startup configuration is invalid."""
