from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from prometheus_client import generate_latest

from mshkn.errors import HostError, NotFound
from mshkn.host.shell import ShellError
from mshkn.observability.logging import (
    _MAX_FIELD_CHARS,
    _TRUNCATED,
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
    msg: object = "x", args: tuple[object, ...] | None = None, **attrs: object
) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, msg, args, None)
    for key, value in attrs.items():
        setattr(record, key, value)
    return record


class _Detached:
    """A detached or half-constructed ORM row, which is the common real case:
    `__str__` raises AttributeError, which no tuple of %-formatting errors
    covers, so enumerating types re-raises it out of `to_ecs`."""

    def __str__(self) -> str:
        raise AttributeError("'_Detached' object has no attribute 'name'")


class _Hostile(_Detached):
    """A value with nothing left to say: its repr raises as well, so even the
    fallback that keeps the pieces has to be built out of something else."""

    def __repr__(self) -> str:
        raise RuntimeError("nothing to say")


def test_the_ecs_envelope_is_present_on_every_record() -> None:
    entry = to_ecs(_record("hello %s", ("w",)))
    assert entry["message"] == "hello w"
    assert entry["log.level"] == "info"
    assert entry["log.logger"] == "t"
    # The literal, not the constant: the field names above are the ones 8.11.0
    # defines, so bumping the version without revisiting them is a defect.
    assert entry["ecs.version"] == "8.11.0" == ECS_VERSION
    assert str(entry["@timestamp"]).endswith("+00:00"), "ECS timestamps are UTC and offset-aware"


def test_trace_id_comes_from_the_context_and_is_absent_outside_a_request() -> None:
    assert "trace.id" not in to_ecs(_record()), "a reaper cycle has no trace"
    token = request_id_var.set("req-123")
    try:
        assert to_ecs(_record())["trace.id"] == "req-123"
    finally:
        request_id_var.reset(token)


def test_an_explicit_request_id_beats_the_context() -> None:
    """The other half of the precedence rule: the record wins, both fields."""
    token = request_id_var.set("req-ctx")
    try:
        assert to_ecs(_record(request_id="req-row"))["trace.id"] == "req-row"
    finally:
        request_id_var.reset(token)


def test_a_falsy_request_id_on_the_record_still_beats_the_context() -> None:
    """The presence check, pinned with a value `or` would swallow: falling back
    to the contextvar here stamps the record with somebody else's trace.

    Doubles as the coercion: the field is a string in every other record, and a
    consumer that groups on it should not have to handle two types.
    """
    token = request_id_var.set("req-ctx")
    try:
        assert to_ecs(_record(request_id=0))["trace.id"] == "0"
    finally:
        request_id_var.reset(token)


def test_an_empty_request_id_is_omitted_rather_than_emitted_empty() -> None:
    """The same rule as the account: absent means absent. `trace.id: ""` is a
    trace that matches nothing and reads like a bug in the tracer."""
    token = request_id_var.set("req-ctx")
    try:
        assert "trace.id" not in to_ecs(_record(request_id=None))
        assert "trace.id" not in to_ecs(_record(request_id=""))
    finally:
        request_id_var.reset(token)


def test_a_client_supplied_request_id_is_capped() -> None:
    """X-Request-Id is unvalidated client input and lands on every record."""
    token = request_id_var.set("r" * 16000)
    try:
        trace = str(to_ecs(_record())["trace.id"])
    finally:
        request_id_var.reset(token)
    assert len(trace) == _MAX_FIELD_CHARS


def test_an_explicit_account_beats_the_context() -> None:
    """destroy logs the computer's account even when the reaper ran it."""
    token = account_id_var.set("acct-ctx")
    try:
        assert to_ecs(_record())["mshkn.account_id"] == "acct-ctx"
        assert to_ecs(_record(account_id="acct-row"))["mshkn.account_id"] == "acct-row"
    finally:
        account_id_var.reset(token)
    assert "mshkn.account_id" not in to_ecs(_record())


def test_a_non_string_account_id_becomes_a_string() -> None:
    """GET /logs selects a tenant's records by comparing this field with `==`
    against the account id on the principal, which is a `str`. An int account id
    stored as an int matches none of them, and the tenant sees an empty log
    rather than an error — the quietest half of the mis-attribution bug.
    """
    assert to_ecs(_record(account_id=123))["mshkn.account_id"] == "123"


def test_an_oversized_account_id_is_capped_like_any_other_field() -> None:
    """It reaches here off a token the client supplied, so it is as unbounded as
    the trace id and gets the same budget."""
    value = str(to_ecs(_record(account_id="a" * 9000))["mshkn.account_id"])
    assert len(value) == _MAX_FIELD_CHARS
    assert value.endswith(_TRUNCATED)


def test_an_explicit_account_of_none_means_no_account() -> None:
    """Presence, not truthiness: GET /logs filters tenants on this field, so
    inheriting the caller's account here hands one account another's record."""
    token = account_id_var.set("acct-ctx")
    try:
        assert "mshkn.account_id" not in to_ecs(_record(account_id=None))
    finally:
        account_id_var.reset(token)


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
    assert value.endswith(_TRUNCATED)
    assert len(value) == _MAX_FIELD_CHARS, (
        "the marker fits inside the budget, it does not extend it"
    )


def test_a_field_at_the_budget_is_left_alone() -> None:
    exact = "a" * _MAX_FIELD_CHARS
    assert to_ecs(_record(blob=exact))["mshkn.blob"] == exact
    assert str(to_ecs(_record(blob=exact + "b"))["mshkn.blob"]).endswith(_TRUNCATED)


def test_the_field_budget_is_four_kilobytes() -> None:
    """The number is a decision, not an implementation detail: it is what one
    record can pin in the ring buffer and what a collector has to swallow."""
    assert _MAX_FIELD_CHARS == 4096


def test_the_message_is_capped_like_any_other_field() -> None:
    """An oversized message is the likely one: it carries a subprocess's stderr."""
    assert len(str(to_ecs(_record("m" * 9000))["message"])) == _MAX_FIELD_CHARS


def test_a_non_scalar_extra_is_stringified() -> None:
    class Slot:
        def __str__(self) -> str:
            return "slot-3"

    assert to_ecs(_record(slot=Slot()))["mshkn.slot"] == "slot-3"


def test_scalars_keep_their_type() -> None:
    entry = to_ecs(_record(slot=7, ratio=0.5, ok=True, nothing=None))
    assert entry["mshkn.slot"] == 7, "an int stays an int, not a string"
    assert entry["mshkn.ratio"] == 0.5
    assert entry["mshkn.ok"] is True
    assert entry["mshkn.nothing"] is None


def test_numbers_json_cannot_represent_become_strings() -> None:
    """NaN, Infinity and a bignum are valid Python and invalid JSON; a strict
    parser rejects the line, which defeats ECS-for-a-stock-collector."""
    entry = json.loads(
        ECSFormatter().format(
            _record(nan=float("nan"), inf=float("inf"), wide=2**70, huge=10**5000)
        )
    )
    assert entry["mshkn.nan"] == "nan"
    assert entry["mshkn.inf"] == "inf"
    assert entry["mshkn.wide"] == hex(2**70)
    assert str(entry["mshkn.huge"]).endswith(_TRUNCATED)


def test_the_formatter_emits_one_json_object_per_record() -> None:
    line = ECSFormatter().format(_record("first\nsecond"))
    assert len(line.splitlines()) == 1, "one record is one line, whatever the message holds"
    assert json.loads(line)["message"] == "first\nsecond", "and the newline survives the round trip"


def test_a_message_that_cannot_be_formatted_does_not_lose_the_record() -> None:
    """to_ecs is the only mapping, so raising loses the record from stdout and
    the ring buffer both. The pieces are the evidence of what was logged."""
    record = _record("100% done", ("extra",))
    with pytest.raises(TypeError):
        record.getMessage()
    message = str(to_ecs(record)["message"])
    assert "100% done" in message
    assert "extra" in message


def test_an_argument_whose_str_raises_does_not_lose_the_record() -> None:
    """`logger.info("v=%s", row)` on a detached row raises AttributeError out of
    getMessage, which is not a %-formatting error at all."""
    entry = to_ecs(_record("v=%s", (_Detached(),)))
    message = str(entry["message"])
    assert "v=%s" in message, "the format string is still the evidence of what was logged"
    assert "_Detached" in message, "and so is the type of the argument that would not render"
    assert entry["log.level"] == "info", "the rest of the document is untouched"


def test_a_value_whose_repr_also_raises_still_produces_a_document() -> None:
    """The fallback cannot lean on repr either: it is built out of
    `object.__repr__`, which runs none of the value's own code."""
    message = str(to_ecs(_record(_Hostile()))["message"])
    assert "unrenderable" in message
    assert "_Hostile" in message, "the placeholder names the value"
    assert "RuntimeError" in message, "and what it raised"


def test_an_extra_that_will_not_render_becomes_a_placeholder() -> None:
    """An `extra=` is arbitrary caller data; one bad row must not take the
    record's other fields down with it."""
    entry = to_ecs(_record(row=_Detached(), op="create"))
    assert "unrenderable" in str(entry["mshkn.row"])
    assert "AttributeError" in str(entry["mshkn.row"])
    assert entry["mshkn.op"] == "create", "the fields around it survive"


def test_an_exception_whose_str_raises_still_produces_three_error_fields() -> None:
    """The error branch renders the exception too, and an exception carrying a
    detached row is exactly the one you most need logged."""

    class UnprintableError(Exception):
        def __str__(self) -> str:
            raise AttributeError("no attribute 'detail'")

    try:
        raise UnprintableError("boom")
    except UnprintableError:
        record = logging.LogRecord("t", logging.ERROR, "f.py", 1, "boom", None, sys.exc_info())
    entry = to_ecs(record)
    assert entry["error.type"] == "UnprintableError"
    assert "unrenderable" in str(entry["error.message"])
    assert "Traceback" in str(entry["error.stack_trace"])


def test_a_live_logger_emits_a_line_for_a_record_that_will_not_render(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The end of the argument: a partial `to_ecs` does not surface as an
    exception the caller can see, it surfaces as `--- Logging error ---` on
    stderr and an empty line from the handler, and the record is gone from both
    consumers at once.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ECSFormatter())
    logger = logging.getLogger("mshkn.test.unrenderable")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info("v=%s", _Detached())
    finally:
        logger.handlers = []
    assert "Logging error" not in capsys.readouterr().err, "the handler did not fall over"
    entry = json.loads(stream.getvalue())
    assert "v=%s" in entry["message"]


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
