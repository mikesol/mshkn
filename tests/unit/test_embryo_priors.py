"""The state before turn 1 (spec §8): the seed, the initial policy, the brain
recipe and the public door's transform are real declarations mshkn accepts."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from membrane.declarations import parse_policy
from membrane.invariants import door_is_open
from sse_starlette.event import ensure_bytes

from mshkn.models import RelayDelivery, parse_scopes
from mshkn.services.recipes import BASE_IMAGE, dockerfile_base_image, image_name
from mshkn.services.starlark import execute_transform, validate_starlark
from tests.support_embryo import WORDS

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
    description or the capability teaches instead (#123)."""
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
        # #118: no experiment reveals that a trial's chain is scratch and discarded,
        # and without this a model must assume a trial leaves state it answers for.
        # "discarded" pins that the chain goes away, not merely that it exists, so this
        # tuple cannot pass against a future seed asserting the opposite.
        "scratch chain",
        "discarded",
        # spec §7.2: a delivered refusal teaches the block on invocation, but
        # nothing teaches that the placement is the agent's to design (invisible
        # mechanism)
        "cannot be invoked until root has provided every name",
        "where root puts it is yours to say",
        # the envelope's second field name. #123 took it out on the theory that turn 2
        # asks for it; turn 2 says only "attach the signature beside my message", which
        # names nothing. See test_the_seed_names_both_envelope_fields below.
        "`sig`",
    ):
        assert phrase in seed, phrase
    for phrase in (
        # hatch.md asks instead (row 2)
        "ssh-keygen",
        "ssh:mike",
        # the approval-time block (#91) is gone; a false line in the genome is
        # worse than a missing one
        "blocks approval",
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
        # TRY_TOOL's description carries this; the result shows what it did
        "`runs`",
    ):
        assert phrase not in seed, phrase


def test_the_seed_names_both_envelope_fields_and_neither_encoding() -> None:
    """Half an envelope is the worst of the three states. #123 deleted the signing
    sentence because it had gone wrong — it disclosed a base64 layer over the armor
    that `sign()` no longer sends — and `sig` went out with it while `msg` stayed.
    Nothing reveals the missing name: a hook's `try` payloads are the agent's own, so
    the trial agrees with whatever the agent guessed, and the live door fails silently
    to `anonymous`. Two runs wrote `.msg` correctly, guessed the second field and lost
    `authentication` for it — 2026-09-10-postcut-run-4 (`.sig`, defeated by the base64
    layer) and 2026-09-15-run-3 (`.signature`, defeated by the name). Opus's
    2026-09-15-run-1 did not discover `sig`; it guessed the same word the driver was
    written with and announced it outward as a contract.

    The second assertion is the negative control against over-restoring: the field
    name is invisible mechanism and belongs here, the signing protocol is turn 2's to
    state, and the encoding was paid for by deleting the quirk rather than disclosing
    it (postcut-run-4's FINDING). Restoring any of the latter two here fails."""
    seed = (EMBRYO / "seed.md").read_text()
    assert "`msg`" in seed and "`sig`" in seed
    assert "base64" not in seed and "ssh-keygen" not in seed


def test_the_seed_does_not_say_a_trial_has_no_chain() -> None:
    """#118: `try` runs a chain verb's invocations on a scratch chain, so the old
    clause is false. A false line in the genome is worse than a missing one."""
    seed = (EMBRYO / "seed.md").read_text()
    assert "no chain" not in seed
    assert "runs it once" not in seed


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
    # the words the script hatches the brain to hear: one headed section per row
    assert (EMBRYO / "capabilities" / "hatch.md").read_text().count("\n### ") == 11


def test_turn_two_states_the_facts_only_the_sender_can_state() -> None:
    """The seed no longer carries the signing protocol (#123). ssh-keygen -Y
    verify needs the signer's exact namespace, and root builds the envelope,
    so both are root's to state; the identity is stated because the measure
    scores the literal ssh:mike and inventing a name flexes no muscle. What
    to build, which namespace to assert and what the policy says stay the
    embryo's."""
    turn2 = WORDS["2"]
    for phrase in ("ssh-keygen -Y sign -n mshkn", "mike", "beside my message", "{key}"):
        assert phrase in turn2, phrase
    for phrase in ("allowed_signers", "asserts", "hook", "policy"):
        assert phrase not in turn2, phrase


def test_turn_two_states_roots_intent_to_keep_changing_the_agent_from_outside() -> None:
    """#128: turns 4 to 9 all arrive at ingress, where the hatcher is `ssh:mike`
    and nothing but the policy turn 2 writes can let that principal propose. So
    turn 2's choice is one-shot — an agent that withholds the grant cannot
    propose its way out of it — and until now nothing in turn 2 said root would
    ever ask for anything from out there. It says so now, as an outcome: no
    field, no tool and no document is named, and root is not named either,
    because the policy language cannot express root's rights and asking for them
    invites a correct refusal. What the intent raises — that a signature proves
    authorship and not freshness — is the model's problem to meet."""
    turn2 = WORDS["2"]
    assert "I'll speak to you from outside rather than from here" in turn2
    assert "asking you to become something different" in turn2
    for phrase in ("grant", "`propose`", "authority", "root", "replay", "nonce"):
        assert phrase not in turn2, phrase


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


def test_hatch_writes_only_the_brains_own_names_into_env() -> None:
    """Spec §7.1: `/brain/.env` names the brain's own keys and nothing else; the
    security check `no_foreign_credential_on_brain` judges a brain against the
    names this script writes, so the two must agree."""
    from membrane.capabilities import CAPABILITIES, load, load_module

    security = load_module(load(CAPABILITIES / "security.md"))
    assert security is not None
    script = (EMBRYO / "hatch.sh").read_text()
    block = script.split('} > "$TMP/env"', 1)[0].rsplit("{\n", 1)[1]
    written = set(re.findall(r'echo "([A-Z_]+)=', block))
    assert written == security.HATCH_ENV


def _hatch_function(name: str) -> str:
    """The real text of a top-level function in hatch.sh, to run as itself."""
    script = (EMBRYO / "hatch.sh").read_text()
    match = re.search(rf"^{name}\(\) \{{.*?^\}}$", script, re.M | re.S)
    assert match is not None, f"hatch.sh has no {name}()"
    return match.group(0)


def _run_unwind(tmp_path: Path, hatched: str) -> tuple[int, list[str], bool]:
    """hatch.sh's own trap, over a stub `api`. Returns the exit status, the
    deletes it asked for, and whether the scratch directory survived."""
    scratch = tmp_path / f"tmp{hatched or 'unfinished'}"
    scratch.mkdir()
    log = tmp_path / f"calls{hatched or 'unfinished'}"
    program = "\n".join(
        (
            "set -euo pipefail",
            f'api() {{ printf \'%s %s\\n\' "$1" "$2" >> {shlex.quote(str(log))}; }}',
            f"TMP={shlex.quote(str(scratch))}",
            "CID=c-1",
            "KEY_ID=k-1",
            f"HATCHED={hatched}",
            _hatch_function("unwind"),
            "trap unwind EXIT",
            "exit 3",
        )
    )
    result = subprocess.run(["bash", "-c", program], capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return result.returncode, calls, scratch.exists()


def test_a_hatch_that_dies_midway_takes_back_the_computer_and_the_scoped_key(
    tmp_path: Path,
) -> None:
    """The first live run of Phase 14 died after uploading `/brain/.env` and left
    its scoped key on the account and its computer running until the reaper: the
    only trap removed the scratch directory (#99). The E2E's teardown cannot
    cover this — when `hatch.sh` fails the `hatched` fixture never yields — so the
    script unwinds itself, and still exits on the status it was given."""
    status, calls, scratch_survived = _run_unwind(tmp_path, "")
    assert status == 3
    assert calls == ["DELETE /computers/c-1", "DELETE /keys/k-1"]
    assert not scratch_survived


def test_a_hatch_that_reached_its_final_json_line_unwinds_nothing(tmp_path: Path) -> None:
    """Everything a finished hatch created belongs to the caller, who is told
    about it on stdout. Only the scratch directory goes."""
    status, calls, scratch_survived = _run_unwind(tmp_path, "1")
    assert status == 3
    assert calls == []
    assert not scratch_survived


def test_no_curl_in_hatch_carries_the_account_key_in_its_arguments() -> None:
    """argv is readable by every user on the operator's machine for the life of
    a call, and a hatch makes dozens (#99). Every curl takes its credentials
    from stdin instead."""
    script = (EMBRYO / "hatch.sh").read_text()
    assert "Authorization" not in script.replace(_hatch_function("auth_config"), "")
    invocations = [
        line
        for line in script.splitlines()
        if "curl " in line and not line.lstrip().startswith("#")
    ]
    assert len(invocations) == 4, invocations
    for line in invocations:
        assert "auth_config |" in line and "--config -" in line, line


def test_the_authorization_header_curl_reads_from_stdin_is_the_one_it_sends() -> None:
    """A curl config value that carries a colon must be quoted — curl drops the
    unquoted form silently, which would send no credentials at all — and a
    quoted one takes backslash escapes, so the key is escaped on the way in."""
    received: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    key = 'mk-A_b-c9"x\\y'
    auth_config = _hatch_function("auth_config")
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        # A daemon thread over a server that gives up: a curl that never arrives
        # must fail this test, not hang the session waiting to be served.
        server.timeout = 30
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        program = (
            auth_config
            + "\nauth_config | curl -fsS --config - -o /dev/null "
            + f"http://127.0.0.1:{server.server_port}/"
        )
        result = subprocess.run(
            ["bash", "-c", program],
            env={**os.environ, "MSHKN_API_KEY": key},
            capture_output=True,
            text=True,
        )
        thread.join(timeout=30)
    assert result.returncode == 0, result.stderr
    assert received == [f"Bearer {key}"]
