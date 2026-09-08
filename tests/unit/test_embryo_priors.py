"""The state before turn 1 (spec §8): the seed, the initial policy, the brain
recipe and the public door's transform are real declarations mshkn accepts."""

from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
from pathlib import Path

from membrane.declarations import parse_policy
from membrane.invariants import door_is_open
from sse_starlette.event import ensure_bytes

from mshkn.services.recipes import BASE_IMAGE, dockerfile_base_image, image_name
from mshkn.services.starlark import execute_transform, validate_starlark

EMBRYO = Path(__file__).resolve().parents[2] / "embryo"


def test_initial_policy_is_closed_and_grants_anonymous_nothing() -> None:
    policy = parse_policy(json.loads((EMBRYO / "policy.json").read_text()))
    assert not door_is_open(policy) and policy.hooks == ()
    assert policy.grant("anonymous").invoke == () and not policy.grant("anonymous").propose
    assert "root" not in policy.principals  # root is fixed, not policy


def test_seed_says_what_the_spec_requires() -> None:
    seed = (EMBRYO / "seed.md").read_text()
    for phrase in (
        "remember",
        "try",
        "propose",
        "verb",
        "proposal",
        "local",
        "read",
        "requires",
        "no verbs",
        "public door is closed",
        "root",
    ):
        assert phrase in seed, phrase


def test_brain_dockerfile_is_from_the_base_and_pins_the_sdks() -> None:
    text = (EMBRYO / "Dockerfile.brain").read_text()
    base = dockerfile_base_image(text)
    assert base is not None and image_name(base) == BASE_IMAGE
    for pin in ("anthropic==1.4.0", "mem0ai==2.0.20", "openai==3.8.0"):
        assert pin in text
    assert "COPY" not in text and "<<" not in text  # no build context, no heredocs


def test_transform_forks_brain_with_a_public_say_and_nothing_else() -> None:
    source = (EMBRYO / "ingress.star").read_text()
    assert validate_starlark(source) == []
    b64 = base64.b64encode(b"hello").decode()
    action = execute_transform(source, {"body_json": {"b64": b64}, "method": "POST"})
    assert action == {
        "action": "fork",
        "label": "brain",
        "self_destruct": True,
        "exclusive": "error_on_conflict",
        "exec": f"membrane say {b64}",
    }
    assert execute_transform(source, {"body_json": {"msg": "x"}, "method": "POST"}) is None
    assert execute_transform(source, {"body_json": None, "method": "POST"}) is None
    rejected = {"body_json": {"b64": "not b64; rm -rf /"}, "method": "POST"}
    assert execute_transform(source, rejected) is None


def test_hatch_script_makes_the_calls_the_spec_lists() -> None:
    script = (EMBRYO / "hatch.sh").read_text()
    calls = (
        "/keys",
        "/recipes",
        "/computers",
        "/upload?path=",
        "/checkpoint",
        "/ingress_rules",
        "uv build",
    )
    for call in calls:
        assert call in script, call
    scopes = re.search(r"SCOPES='(\{.*\})'", script)
    assert scopes is not None
    assert json.loads(scopes.group(1)) == {
        "recipes": {"create": True, "read": True},
        "computers": {"create_from": "*"},
        "labels": ["verb/"],
    }
    assert "set -euo pipefail" in script
    assert (EMBRYO / "liturgy.md").read_text().count("| ") > 20


def test_run_parses_the_exit_code_from_a_real_crlf_sse_stream() -> None:
    """mshkn's exec endpoint answers over server-sent events separated by CRLF
    (sse_starlette's default), not bare LF; the exit-code parse in hatch.sh's
    `run()` must survive that or every hatch aborts right after it uploads
    `/brain/.env` (spec §8)."""
    script = (EMBRYO / "hatch.sh").read_text()
    match = re.search(r'code="\$\((.*)\)"', script)
    assert match is not None, "hatch.sh's run() must assign code from a command substitution"
    pipeline = match.group(1)

    stream = (
        ensure_bytes({"event": "stdout", "data": "hi"}, sep="\r\n")
        + ensure_bytes({"event": "exit", "data": "0"}, sep="\r\n")
    ).decode()

    # Bytes, not text=True: Python's text-mode pipes do universal-newline
    # translation and would silently turn \r\n back into \n, hiding the very
    # bug this test pins.
    result = subprocess.run(
        ["bash", "-c", f"out={shlex.quote(stream)}\n{pipeline}"],
        capture_output=True,
        check=True,
    )
    assert result.stdout.rstrip(b"\n") == b"0"
