from __future__ import annotations

import json
import logging

import pytest
from prometheus_client import generate_latest

from mshkn.errors import HostError, NotFound
from mshkn.observability.logging import JSONFormatter, RequestIdFilter, request_id_var
from mshkn.observability.metrics import (
    operation_duration_seconds,
    operation_errors_total,
    timed,
)


def _format(record: logging.LogRecord) -> dict[str, object]:
    RequestIdFilter().filter(record)
    result: dict[str, object] = json.loads(JSONFormatter().format(record))
    return result


def test_json_formatter_includes_request_id_from_context() -> None:
    token = request_id_var.set("req-123")
    try:
        record = logging.LogRecord("t", logging.INFO, "f.py", 1, "hello %s", ("w",), None)
        entry = _format(record)
    finally:
        request_id_var.reset(token)
    assert entry["msg"] == "hello w"
    assert entry["request_id"] == "req-123"
    assert entry["level"] == "info"


def test_request_id_defaults_to_dash_outside_a_request() -> None:
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "x", None, None)
    assert _format(record)["request_id"] == "-"


def test_extra_fields_are_emitted() -> None:
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "x", None, None)
    record.computer_id = "comp-1"
    assert _format(record)["computer_id"] == "comp-1"


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
