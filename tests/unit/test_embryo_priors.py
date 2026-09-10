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

from mshkn.models import RelayDelivery, parse_scopes
from mshkn.services.recipes import BASE_IMAGE, dockerfile_base_image, image_name
from mshkn.services.starlark import execute_transform, validate_starlark
from tests.support_embryo import LITURGY

EMBRYO = Path(__file__).resolve().parents[2] / "embryo"


def test_initial_policy_is_closed_and_grants_anonymous_nothing() -> None:
    policy = parse_policy(json.loads((EMBRYO / "policy.json").read_text()))
    assert not door_is_open(policy) and policy.hooks == ()
    assert policy.grant("anonymous").invoke == () and not policy.grant("anonymous").propose
    assert "root" not in policy.principals  # root is fixed, not policy


def test_the_seed_is_bootstrap_and_invisible_mechanism_and_nothing_else() -> None:
    """The contract of docs/superpowers/specs/2026-09-10-seed-reduction-design.md
    §3, and the standing rule in CLAUDE.md. Present: what cannot be learned
    because learning it requires it, and what no experiment reveals because the
    failure is silent. Absent: everything a constructive refusal, a tool
    description or the liturgy teaches instead (#123)."""
    seed = (EMBRYO / "seed.md").read_text()
    for phrase in (
        # bootstrap
        "remember",
        "try",
        "propose",
        "verb",
        "proposal",
        "dockerfile",
        "entrypoint",
        "requires",
        "no verbs",
        "public door is closed",
        "root",
        # invisible mechanism: no experiment reveals these, because the
        # failure is silent
        "shell-quoted",
        "Anonymous input is never remembered",
        "verb/<name>",
        "inbox",
        # 2026-09-10-postcut-run-1: the model refused to propose the policy that opens
        # the door because a blind full replacement might drop root's own access, and it
        # cannot read its policy. No refusal can teach this — a refusal fires on an act the
        # model correctly declines to take — and the only experiment that would risks
        # permanent loss of contact with the one party who could repair it.
        "whatever your policy says or omits",
    ):
        assert phrase in seed, phrase
    for phrase in (
        # the liturgy asks instead (turn 2)
        "ssh-keygen",
        '"sig"',
        "ssh:mike",
        # a constructive refusal teaches these
        "communicate",
        "transact",
        "administer",
        "200",
        "[a-z0-9_]",
        "`principals`",
        "`hooks`",
        "`door`",
        "`local`",
        "`read`",
        "supersedes",
        # PROPOSE_TOOL's description carries this
        "not a diff",
    ):
        assert phrase not in seed, phrase


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
    match = re.search(r'jq -cn --arg t "\$ANTHROPIC_BASE_URL/" \'(.+)\'', script)
    assert match is not None, "hatch.sh must build SCOPES from ANTHROPIC_BASE_URL with jq"
    result = subprocess.run(
        ["jq", "-cn", "--arg", "t", "https://example.com/", match.group(1)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"the SCOPES filter is not valid jq: {result.stderr}"
    scopes = parse_scopes(json.loads(result.stdout))
    assert scopes.recipes_create is True and scopes.recipes_read is True
    assert scopes.create_from == "*"
    assert scopes.labels == ("verb/",)
    assert scopes.has_relay is True
    assert scopes.relay_targets == ("https://example.com/",)
    assert scopes.relay_deliver == RelayDelivery(label="brain", exec="membrane resume")
    assert "set -euo pipefail" in script
    assert (EMBRYO / "liturgy.md").read_text().count("| ") > 20


def test_turn_two_states_the_facts_only_the_sender_can_state() -> None:
    """The seed no longer carries the signing protocol (#123). ssh-keygen -Y
    verify needs the signer's exact namespace, and root builds the envelope,
    so both are root's to state; the identity is stated because the measure
    scores the literal ssh:mike and inventing a name flexes no muscle. What
    to build, which namespace to assert and what the policy says stay the
    embryo's."""
    turn2 = LITURGY[2]
    for phrase in ("ssh-keygen -Y sign -n mshkn", "mike", "beside my message", "{key}"):
        assert phrase in turn2, phrase
    for phrase in ("allowed_signers", "asserts", "hook", "policy"):
        assert phrase not in turn2, phrase


def test_the_liturgy_the_tiers_send_is_the_liturgy_the_repository_publishes() -> None:
    """`LITURGY` is the script the flow and E2E tiers speak; `embryo/liturgy.md`
    is the table a reader sees (spec §9). Nothing else in the suite would notice
    the two drifting apart, so every turn's words are pinned here. Turn 2 is
    split around its `{key}` placeholder, which the document writes as an
    ellipsis."""
    published = (EMBRYO / "liturgy.md").read_text()
    for turn, words in LITURGY.items():
        for part in words.split("{key}"):
            assert part in published, (turn, part)


def _expand(expression: str, wheel_name: str) -> str:
    """What the shell makes of a path expression from hatch.sh, with WHEEL_NAME set."""
    result = subprocess.run(
        ["bash", "-c", f'WHEEL_NAME={shlex.quote(wheel_name)}; printf "%s" "{expression}"'],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout


def test_the_wheel_is_uploaded_and_installed_under_its_own_pep427_name() -> None:
    """pip reads a wheel's distribution, version and compatibility tags off its
    filename (PEP 427) and refuses one that carries none, so uploading the build
    as a bare `membrane.whl` aborts every hatch at `pip install` with
    "membrane.whl is not a valid wheel filename" (spec §8). The name must be the
    one `uv build` produced, and the upload and the install must name one file."""
    script = (EMBRYO / "hatch.sh").read_text()
    assert "membrane.whl" not in script, "the wheel must keep its own filename"
    assert re.search(r'WHEEL_NAME="\$\(basename "\$WHEEL"\)"', script) is not None, script

    upload = re.search(r'^upload "\$CID" "([^"]+)" "\$WHEEL"$', script, re.M)
    assert upload is not None, "hatch.sh must upload $WHEEL to a quoted path"
    installed = re.search(r"pip install --no-deps -q (\S+)", script)
    assert installed is not None, "hatch.sh must pip install the uploaded wheel"

    built = "membrane-0.1.0-py3-none-any.whl"  # what `uv build --package membrane` writes
    uploaded_path = _expand(upload.group(1), built)
    installed_path = _expand(installed.group(1), built)
    assert uploaded_path == installed_path, (uploaded_path, installed_path)
    assert Path(uploaded_path).name == built


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
