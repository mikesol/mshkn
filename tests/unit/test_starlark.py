"""The Starlark sandbox: literal conversion, validation, and transform execution."""

from __future__ import annotations

from typing import Any

import pytest

from mshkn.services.starlark import (
    StarlarkError,
    _to_starlark_literal,
    execute_transform,
    validate_starlark,
)


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (None, "None"),
        (True, "True"),
        (False, "False"),
        (3, "3"),
        (2.5, "2.5"),
        ("it's", '"it\'s"'),
        ([1, "a"], "[1, 'a']"),
        ((1, 2), "[1, 2]"),
        ({"k": [None, {"n": 1.0}]}, "{'k': [None, {'n': 1.0}]}"),
        (b"raw", "b'raw'"),
    ],
)
def test_to_starlark_literal(value: object, literal: str) -> None:
    assert _to_starlark_literal(value) == literal


def test_validate_starlark_valid() -> None:
    source = 'def transform(req):\n  return {"action": "fork", "checkpoint_id": "cp_1"}'
    errors = validate_starlark(source)
    assert errors == []


def test_validate_starlark_no_transform() -> None:
    source = "def other(req):\n  return None"
    errors = validate_starlark(source)
    assert len(errors) == 1
    assert "transform" in errors[0]


def test_validate_starlark_syntax_error() -> None:
    source = "def transform(req):\n  return {{{{"
    errors = validate_starlark(source)
    assert len(errors) >= 1


def test_execute_transform_fork() -> None:
    source = (
        'def transform(req):\n  return {"action": "fork", "checkpoint_id": req["body_json"]["cp"]}'
    )
    req = {
        "method": "POST",
        "path": "/webhook",
        "headers": {},
        "query_params": {},
        "body_json": {"cp": "cp_abc"},
        "body_form": None,
        "body_raw": '{"cp": "cp_abc"}',
        "content_type": "application/json",
    }
    result = execute_transform(source, req)
    assert result == {"action": "fork", "checkpoint_id": "cp_abc"}


def test_execute_transform_returns_none() -> None:
    source = "def transform(req):\n  return None"
    req: dict[str, Any] = {
        "method": "GET",
        "path": "/",
        "headers": {},
        "query_params": {},
        "body_json": None,
        "body_form": None,
        "body_raw": "",
        "content_type": "",
    }
    result = execute_transform(source, req)
    assert result is None


def test_execute_transform_runtime_error() -> None:
    source = 'def transform(req):\n  return req["nonexistent"]["key"]'
    req: dict[str, Any] = {
        "method": "GET",
        "path": "/",
        "headers": {},
        "query_params": {},
        "body_json": None,
        "body_form": None,
        "body_raw": "",
        "content_type": "",
    }
    with pytest.raises(StarlarkError):
        execute_transform(source, req)


def test_transform_sees_nested_request_values() -> None:
    source = (
        "def transform(req):\n"
        '  return {"n": req["body_json"]["items"][1]["v"], "q": req["query_params"]["x"]}'
    )
    req = {"body_json": {"items": [{"v": 0}, {"v": 7}]}, "query_params": {"x": "y"}, "headers": {}}
    assert execute_transform(source, req) == {"n": 7, "q": "y"}
