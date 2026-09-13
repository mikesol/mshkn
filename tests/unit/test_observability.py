from __future__ import annotations

import json
import logging
import sys

import pytest
from prometheus_client import generate_latest

from mshkn.errors import HostError, NotFound
from mshkn.host.shell import ShellError
from mshkn.observability.logging import (
    ECS_VERSION,
    ECSFormatter,
    account_id_var,
    request_id_var,
    to_ecs,
)
from mshkn.observability.metrics import (
    operation_duration_seconds,
    operation_errors_total,
    timed,
)


def _record(
    msg: str = "x", args: tuple[object, ...] | None = None, **attrs: object
) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, msg, args, None)
    for key, value in attrs.items():
        setattr(record, key, value)
    return record


def test_the_ecs_envelope_is_present_on_every_record() -> None:
    entry = to_ecs(_record("hello %s", ("w",)))
    assert entry["message"] == "hello w"
    assert entry["log.level"] == "info"
    assert entry["log.logger"] == "t"
    assert entry["ecs.version"] == ECS_VERSION
    assert str(entry["@timestamp"]).endswith("+00:00"), "ECS timestamps are UTC and offset-aware"


def test_trace_id_comes_from_the_context_and_is_absent_outside_a_request() -> None:
    assert "trace.id" not in to_ecs(_record()), "a reaper cycle has no trace"
    token = request_id_var.set("req-123")
    try:
        assert to_ecs(_record())["trace.id"] == "req-123"
    finally:
        request_id_var.reset(token)


def test_an_explicit_account_beats_the_context() -> None:
    """destroy logs the computer's account even when the reaper ran it."""
    token = account_id_var.set("acct-ctx")
    try:
        assert to_ecs(_record())["mshkn.account_id"] == "acct-ctx"
        assert to_ecs(_record(account_id="acct-row"))["mshkn.account_id"] == "acct-row"
    finally:
        account_id_var.reset(token)
    assert "mshkn.account_id" not in to_ecs(_record())


def test_extras_are_namespaced_not_lifted_to_the_top_level() -> None:
    entry = to_ecs(_record(computer_id="comp-1", op="create"))
    assert entry["mshkn.computer_id"] == "comp-1"
    assert entry["mshkn.op"] == "create"
    assert "computer_id" not in entry, "ECS reserves the top level for itself"


def test_an_exception_becomes_three_error_fields() -> None:
    try:
        raise NotFound("no such computer")
    except NotFound:
        record = logging.LogRecord("t", logging.ERROR, "f.py", 1, "boom", None, sys.exc_info())
    entry = to_ecs(record)
    assert entry["error.type"] == "NotFound"
    assert entry["error.message"] == "no such computer"
    assert "Traceback" in str(entry["error.stack_trace"])
    assert "exception" not in entry, "the single blob field is gone"


def test_an_oversized_field_is_truncated_and_marked() -> None:
    value = str(to_ecs(_record(blob="a" * 9000))["mshkn.blob"])
    assert len(value) < 9000 and value.endswith("[truncated]")


def test_scalars_keep_their_type() -> None:
    entry = to_ecs(_record(slot=7, ok=True, nothing=None))
    assert entry["mshkn.slot"] == 7, "an int stays an int, not a string"
    assert entry["mshkn.ok"] is True
    assert entry["mshkn.nothing"] is None


def test_the_formatter_emits_one_json_object_per_record() -> None:
    line = ECSFormatter().format(_record("hi"))
    assert "\n" not in line
    assert json.loads(line)["message"] == "hi"


def _sample(metric_text: str, name: str, labels: str) -> float:
    for line in metric_text.splitlines():
        if line.startswith(f"{name}{{{labels}}}"):
            return float(line.split()[-1])
    return 0.0


async def test_timed_observes_duration_and_counts_domain_errors() -> None:
    before = _sample(
        generate_latest().decode(), "mshkn_operation_duration_seconds_count", 'op="unit_test"'
    )
    async with timed("unit_test"):
        pass
    with pytest.raises(NotFound):
        async with timed("unit_test"):
            raise NotFound("x")
    with pytest.raises(HostError):
        async with timed("unit_test"):
            raise HostError("y")
    with pytest.raises(RuntimeError):
        async with timed("unit_test"):
            raise RuntimeError("z")
    text = generate_latest().decode()
    assert _sample(text, "mshkn_operation_duration_seconds_count", 'op="unit_test"') == before + 4
    assert _sample(text, "mshkn_operation_errors_total", 'kind="domain",op="unit_test"') == 1
    assert _sample(text, "mshkn_operation_errors_total", 'kind="host",op="unit_test"') == 1
    assert _sample(text, "mshkn_operation_errors_total", 'kind="unexpected",op="unit_test"') == 1
    assert operation_duration_seconds is not None and operation_errors_total is not None


async def test_timed_meters_a_shell_failure_as_a_host_error() -> None:
    """A dm-thin/tap/rclone failure arrives as ShellError; it must count as host.

    The fake host raises HostError for the same failures, so if ShellError were
    metered as "unexpected" the fake and the real host would disagree.
    """
    with pytest.raises(ShellError):
        async with timed("unit_shell"):
            raise ShellError("dmsetup message ... create_snap", 1, "pool is full")
    text = generate_latest().decode()
    assert _sample(text, "mshkn_operation_errors_total", 'kind="host",op="unit_shell"') == 1
    assert _sample(text, "mshkn_operation_errors_total", 'kind="unexpected",op="unit_shell"') == 0
