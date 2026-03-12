"""Starlark sandbox for ingress request transformation."""

from __future__ import annotations

from starlark_go import Starlark


class StarlarkError(Exception):
    """Raised when Starlark execution fails."""


def _to_starlark_literal(obj: object) -> str:
    """Convert a Python object to a Starlark literal string."""
    if obj is None:
        return "None"
    if isinstance(obj, bool):
        return "True" if obj else "False"
    if isinstance(obj, int):
        return repr(obj)
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, str):
        return repr(obj)
    if isinstance(obj, dict):
        items = ", ".join(
            f"{_to_starlark_literal(k)}: {_to_starlark_literal(v)}"
            for k, v in obj.items()
        )
        return "{" + items + "}"
    if isinstance(obj, (list, tuple)):
        items = ", ".join(_to_starlark_literal(v) for v in obj)
        return "[" + items + "]"
    return repr(obj)


def validate_starlark(source: str) -> list[str]:
    """Validate that source parses and defines a ``transform`` function.

    Returns a list of error strings (empty means valid).
    """
    errors: list[str] = []
    try:
        s = Starlark()
        s.exec(source)
        if "transform" not in s.globals():
            errors.append("source must define a 'transform' function")
    except Exception as exc:
        errors.append(str(exc))
    return errors


def execute_transform(
    source: str, request_dict: dict, timeout_ms: int = 1000
) -> dict | None:
    """Execute a Starlark transform function against a request dict.

    Returns the transform result (dict or None).
    Raises StarlarkError on any failure.
    """
    try:
        s = Starlark()
        s.exec(source)
        literal = _to_starlark_literal(request_dict)
        result = s.eval("transform(" + literal + ")")
        return result  # type: ignore[return-value]
    except Exception as exc:
        raise StarlarkError(str(exc)) from exc
