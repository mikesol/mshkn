"""The measure (spec §11, #101): settings from .env, the doors over HTTP, the
liturgy spoken turn by turn with approvals and repairs, the seven
postconditions, the evidence written under docs/embryo/, and the cost."""

from __future__ import annotations

import base64
import io
import json
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from membrane.liturgy import COUNT, LITURGY, REFUSED
from membrane.measure import (
    POSTCONDITIONS,
    TURN_WAIT,
    AskApprover,
    AutoApprover,
    Doors,
    Hatched,
    MeasureSettings,
    Record,
    Turn,
    cost_usd,
    hatch,
    load_measure_settings,
    main,
    membrane_version,
    new_key,
    run_once,
    sign,
    speak_liturgy,
    split_output,
    transport_for,
    verdict,
)
from membrane.model import zero_usage

from tests.support_embryo import b64

pytestmark = pytest.mark.unit

USAGE = {
    "input_tokens": 1000,
    "output_tokens": 100,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def _audit(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "door": "api",
        "principal": "root",
        "offered": ["effort", "propose", "remember", "try"],
        "tools": [],
        "proposals": [],
        "memory_written": True,
        "stopped": "done",
        "model_calls": 1,
        "effort": ["medium"],
        "usage": dict(USAGE),
    }
    base.update(fields)
    return base


def _out(audit: dict[str, Any], reply: str) -> str:
    return "audit " + json.dumps(audit) + "\n" + reply + "\n"


# ---------------------------------------------------------------- settings and cost


def test_settings_come_from_the_env_file_and_the_environment_wins(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=http://file:8000\nMSHKN_API_KEY=file-key\n"
        "ANTHROPIC_API_KEY='sk-file'\nOPENAI_API_KEY=oa-file\n"
    )
    settings = load_measure_settings(env, {"MSHKN_API_KEY": "env-key", "HOME": "/x"})
    assert settings == MeasureSettings(
        api_url="http://file:8000",
        api_key="env-key",
        brain_api_url="https://api.mshkn.dev",
        anthropic_api_key="sk-file",
        openai_api_key="oa-file",
        model_id="claude-opus-5",
    )
    with_brain = load_measure_settings(
        env, {"BRAIN_API_URL": "http://10.0.0.1:8000"}, model_id="claude-sonnet-5", effort="medium"
    )
    assert with_brain.brain_api_url == "http://10.0.0.1:8000"
    # `--effort` is the run's default now, not the effort (#122): the turn raises it.
    assert with_brain.model_id == "claude-sonnet-5" and with_brain.default_effort == "medium"


def test_a_missing_key_is_named(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("MSHKN_API_URL=http://file:8000\nOPENAI_API_KEY=\n")
    with pytest.raises(ValueError, match="MSHKN_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY"):
        load_measure_settings(env, {})
    with pytest.raises(ValueError, match=str(tmp_path / "absent")):
        load_measure_settings(tmp_path / "absent", {})


def test_cost_uses_the_price_table_and_the_cache_multipliers() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cache_creation_input_tokens": 200_000,
        "cache_read_input_tokens": 1_000_000,
    }
    # 5.00 + 2.50 + 0.25 * 5 + 0.1 * 5 = 5 + 2.5 + 1.25 + 0.5
    assert cost_usd(usage, "claude-opus-5") == pytest.approx(9.25)
    assert cost_usd(zero_usage(), "claude-opus-5") == 0.0
    with pytest.raises(KeyError):
        cost_usd(usage, "claude-unknown")


# ---------------------------------------------------------------- the record


def test_record_writes_every_command_the_transcript_and_the_summary(tmp_path: Path) -> None:
    record = Record(tmp_path / "run")
    n = record.command("api", "say", LITURGY[1], "audit {}\nhi\n", 200, 1.5)
    m = record.command(
        "ingress", "say", {"msg": "Who am I?", "sig": "x"}, "audit {}\nyou\n", 200, 2.0
    )
    assert (n, m) == (1, 2)
    files = sorted(p.name for p in (tmp_path / "run" / "commands").iterdir())
    assert files == ["001-api-say.json", "002-ingress-say.json"]
    doc = json.loads((tmp_path / "run" / "commands" / "002-ingress-say.json").read_text())
    assert doc["detail"] == {"msg": "Who am I?", "sig": "x"} and doc["stdout"].endswith("you\n")
    assert doc["status"] == 200 and doc["seconds"] == 2.0
    turn = Turn(
        label="2",
        door="api",
        words="open the door",
        audit=_audit(proposals=[{"id": "p-1", "sha256": "abc"}]),
        reply="I propose.\nproposal p-1\n{}",
        commands=[1],
        approvals=[{"id": "p-1", "decision": "approve", "result": "p-1 building: verb v"}],
    )
    transcript = record.transcript("claude-opus-5", [turn])
    text = transcript.read_text()
    assert "## Turn 2" in text and "open the door" in text and "p-1 building: verb v" in text
    assert '"model_calls": 1' in text and "I propose." in text
    listing = {"turn": 12, "catalog": {}}
    assert json.loads(record.final_list(listing).read_text()) == listing
    summary = {"ok": False, "postconditions": {}}
    assert json.loads(record.summary(summary).read_text()) == summary


# ---------------------------------------------------------------- the doors over HTTP


@dataclass
class FakeApi:
    """The routes the measure uses, with canned membrane output per command."""

    outputs: dict[str, list[str]] = field(default_factory=dict)
    conflicts: int = 0
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    recipes: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    exec_logs: dict[str, dict[str, Any]] = field(default_factory=dict)
    gone: set[str] = field(default_factory=set)
    fail_deletes: bool = False
    turns: dict[int, tuple[dict[str, Any], str]] = field(default_factory=dict)
    next_turn: int = 1

    def _listing(self) -> dict[str, Any]:
        return {
            "catalog": {},
            "proposals": [],
            "policy": {"principals": {}},
            "pending": None,
            "queue": [],
            "window": [
                {
                    "turn": n,
                    "principal": audit.get("principal"),
                    "door": audit.get("door"),
                    "input": "",
                    "reply": reply,
                    "output": reply,
                    "audit": audit,
                }
                for n, (audit, reply) in self.turns.items()
            ],
        }

    def _stdout(self, command: str) -> str:
        is_say = command.startswith("membrane root say ") or command.startswith("membrane say ")
        outs = self.outputs.get(command)
        if not outs and command == "membrane root list":
            return json.dumps(self._listing())
        out = (
            _out(_audit(), f"nothing for {command}")
            if not outs
            else (outs.pop(0) if len(outs) > 1 else outs[0])
        )
        if is_say:
            audit, reply = split_output(out)
            if "stopped" in audit:
                n = self.next_turn
                self.next_turn += 1
                self.turns[n] = (audit, reply)
                return _out(
                    _audit(started=True, turn=n, job=f"rj-{n}"),
                    json.dumps({"turn": n, "job": f"rj-{n}"}),
                )
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body or None))
        path = request.url.path
        if request.method == "POST" and path == "/checkpoints/fork":
            if self.conflicts:
                self.conflicts -= 1
                return httpx.Response(409, json={"detail": "busy"})
            assert body["label"] == "brain" and body["exclusive"] == "error_on_conflict"
            out = self._stdout(body["exec"])
            return httpx.Response(
                200, json={"computer_id": "comp-root", "exec_exit_code": 0, "exec_stdout": out}
            )
        if request.method == "POST" and path.startswith("/ingress/"):
            assert "authorization" not in request.headers
            out = self._stdout("membrane say " + body["b64"])
            return httpx.Response(
                200, json={"computer_id": "comp-pub", "exec_exit_code": 0, "exec_stdout": out}
            )
        if request.method == "GET" and path == "/recipes":
            return httpx.Response(200, json=self.recipes)
        if request.method == "GET" and path == "/checkpoints":
            label = request.url.params.get("label")
            rows = [c for c in self.checkpoints if label is None or c["label"] == label]
            return httpx.Response(200, json=rows)
        if request.method == "GET" and path.endswith("/status"):
            cid = path.split("/")[2]
            return httpx.Response(404 if cid in self.gone else 200, json={"id": cid})
        if request.method == "GET" and path.endswith("/exec_log"):
            cid = path.split("/")[2]
            if cid in self.exec_logs:
                return httpx.Response(200, json=self.exec_logs[cid])
            return httpx.Response(404, json={"detail": "no log"})
        if request.method == "DELETE":
            return httpx.Response(500 if self.fail_deletes else 200, json={})
        return httpx.Response(404, json={"detail": f"unrouted {request.method} {path}"})


def _doors(api: FakeApi, tmp_path: Path) -> Doors:
    transport = httpx.MockTransport(api.handler)
    authed = httpx.AsyncClient(
        base_url="http://api", headers={"Authorization": "Bearer k"}, transport=transport
    )
    public = httpx.AsyncClient(base_url="http://api", transport=transport)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    doors = Doors(authed, public, "rule-1", Record(tmp_path / "run"), sleep=sleep)
    doors.slept = slept  # type: ignore[attr-defined]
    return doors


async def test_root_retries_a_409_and_records_the_command(tmp_path: Path) -> None:
    api = FakeApi(outputs={f"membrane root say {b64('hi')}": [_out(_audit(), "hello")]})
    api.conflicts = 2
    doors = _doors(api, tmp_path)
    audit, reply = await doors.root_say("hi")
    assert audit["principal"] == "root" and reply == "hello\n"
    assert doors.slept == [3.0, 3.0]  # type: ignore[attr-defined]
    assert [s.name for s in doors.sent] == ["say", "list"] and doors.sent[0].detail == "hi"
    assert doors.sent[0].door == "api" and doors.sent[0].stdout.startswith("audit ")
    assert (tmp_path / "run" / "commands" / "001-api-say.json").exists()


async def test_root_gives_up_on_409_after_the_turn_timeout(tmp_path: Path) -> None:
    api = FakeApi()
    api.conflicts = 10**6
    doors = _doors(api, tmp_path)
    clock = iter(range(0, 10**6, 100))
    doors.now = lambda: float(next(clock))
    with pytest.raises(RuntimeError, match="409"):
        await doors.root("list")


async def test_a_failed_exec_is_an_error_with_the_output(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "computer_id": "c",
                "exec_exit_code": 1,
                "exec_stdout": "Traceback",
                "exec_stderr": "openai.RateLimitError: insufficient_quota",
            },
        )

    transport = httpx.MockTransport(handler)
    doors = Doors(
        httpx.AsyncClient(base_url="http://api", transport=transport),
        httpx.AsyncClient(base_url="http://api", transport=transport),
        "rule-1",
        Record(tmp_path / "run"),
    )
    with pytest.raises(RuntimeError, match=r"on c: exit 1\nstdout: Traceback\nstderr: openai"):
        await doors.root("list")
    assert doors.sent[0].status == 200 and doors.sent[0].stdout == "Traceback"
    assert doors.sent[0].computer_id == "c" and "insufficient_quota" in doors.sent[0].stderr
    doc = json.loads((tmp_path / "run" / "commands" / "001-api-list.json").read_text())
    assert doc["computer_id"] == "c" and doc["stderr"].startswith("openai.")


async def test_public_say_carries_no_credential_and_records_the_principal(
    tmp_path: Path,
) -> None:
    payload = {"msg": "Who am I?", "sig": "s"}
    api = FakeApi(
        outputs={
            "membrane say " + b64(payload): [
                _out(_audit(door="ingress", principal="ssh:mike"), "You are ssh:mike.")
            ]
        }
    )
    doors = _doors(api, tmp_path)
    audit, reply = await doors.public_say(payload)
    assert audit["principal"] == "ssh:mike" and reply == "You are ssh:mike.\n"
    assert doors.sent[0].door == "ingress" and doors.sent[0].detail == payload
    assert api.requests[0][1] == "/ingress/rule-1"


async def test_public_say_of_a_closed_door_has_a_null_principal(tmp_path: Path) -> None:
    out = (
        'audit {"door": "ingress", "principal": null, "closed": true}\nThe public door is closed.\n'
    )
    api = FakeApi(outputs={"membrane say " + b64("Who am I?"): [out]})
    audit, reply = await _doors(api, tmp_path).public_say("Who am I?")
    assert audit["principal"] is None and audit["closed"] is True
    assert reply == "The public door is closed.\n"


async def test_root_say_waits_for_the_turn_in_the_window(tmp_path: Path) -> None:
    api = FakeApi(
        outputs={f"membrane root say {b64('hi')}": [_out(_audit(stopped="done"), "hello")]}
    )
    doors = _doors(api, tmp_path)
    audit, reply = await doors.root_say("hi")
    assert audit["stopped"] == "done" and reply == "hello\n"
    assert [p for m, p, _ in api.requests] == ["/checkpoints/fork", "/checkpoints/fork"], (
        "the say, then one list"
    )


async def test_a_closed_door_answers_at_once_and_a_queued_say_is_an_error(tmp_path: Path) -> None:
    closed = _out(
        {"door": "ingress", "principal": None, "closed": True}, "The public door is closed."
    )
    api = FakeApi(outputs={f"membrane say {b64('hi')}": [closed]})
    doors = _doors(api, tmp_path)
    audit, reply = await doors.public_say("hi")
    assert audit["principal"] is None and reply.startswith("The public door is closed.")
    api.outputs[f"membrane say {b64('again')}"] = [
        _out({"door": "ingress", "principal": "root", "queued": 1}, '{"queued": 1}')
    ]
    with pytest.raises(RuntimeError, match="pending"):
        await doors.public_say("again")


async def test_await_turn_gives_up_after_turn_wait(tmp_path: Path) -> None:
    api = FakeApi()
    doors = _doors(api, tmp_path)

    async def empty_listing() -> dict[str, Any]:
        return {"window": []}

    doors.listing = empty_listing  # type: ignore[method-assign]
    clock = iter([0.0, TURN_WAIT + 1.0])
    doors.now = lambda: next(clock)
    with pytest.raises(RuntimeError, match=f"turn 1 did not close within {TURN_WAIT:.0f} s"):
        await doors.await_turn(1)


async def test_listing_and_wait_builds_poll_until_nothing_builds(tmp_path: Path) -> None:
    building = json.dumps({"catalog": {"v": {"status": "building"}}, "proposals": []})
    ready = json.dumps({"catalog": {"v": {"status": "ready"}}, "proposals": []})
    api = FakeApi(outputs={"membrane root list": [building, building, ready]})
    doors = _doors(api, tmp_path)
    listing = await doors.wait_builds()
    assert listing["catalog"]["v"]["status"] == "ready"
    assert doors.slept == [5.0, 5.0]  # type: ignore[attr-defined]
    assert [s.name for s in doors.sent] == ["list", "list", "list"]


async def test_wait_builds_gives_up_at_the_build_timeout(tmp_path: Path) -> None:
    building = json.dumps({"catalog": {"v": {"status": "building"}}, "proposals": []})
    api = FakeApi(outputs={"membrane root list": [building]})
    doors = _doors(api, tmp_path)
    clock = iter(range(0, 10**6, 400))
    doors.now = lambda: float(next(clock))
    listing = await doors.wait_builds()
    assert listing["catalog"]["v"]["status"] == "building"


async def test_computer_checks_read_status_and_exec_log(tmp_path: Path) -> None:
    api = FakeApi(
        exec_logs={"comp-1": {"stdout": "Example Domain\n", "exit_code": 0}},
        gone={"comp-1"},
    )
    doors = _doors(api, tmp_path)
    assert await doors.check_computer("comp-1") == {
        "computer_id": "comp-1",
        "gone": True,
        "stdout": "Example Domain\n",
        "exit_code": 0,
    }
    assert await doors.check_computer("comp-2") == {
        "computer_id": "comp-2",
        "gone": False,
        "stdout": None,
        "exit_code": None,
    }


async def test_recipes_and_checkpoints(tmp_path: Path) -> None:
    api = FakeApi(
        recipes=[{"recipe_id": "rcp-a"}, {"recipe_id": "rcp-b"}],
        checkpoints=[{"id": "ck-1", "label": "brain", "recipe_id": "rcp-a"}],
    )
    doors = _doors(api, tmp_path)
    assert await doors.recipes() == {"rcp-a", "rcp-b"}
    assert await doors.checkpoints("brain") == [
        {"id": "ck-1", "label": "brain", "recipe_id": "rcp-a"}
    ]
    assert await doors.checkpoints("verb/counter") == []


async def test_teardown_deletes_the_door_the_key_the_chains_and_the_recipes(
    tmp_path: Path,
) -> None:
    api = FakeApi(
        checkpoints=[
            {"id": "ck-brain", "label": "brain", "recipe_id": "rcp-brain"},
            {"id": "ck-count", "label": "verb/counter", "recipe_id": "rcp-count"},
            {"id": "ck-verb", "label": None, "recipe_id": "rcp-page"},
            {"id": "ck-other", "label": "someone-else", "recipe_id": "rcp-x"},
        ]
    )
    doors = _doors(api, tmp_path)
    hatched = Hatched("http://api/ingress/rule-1", "rule-1", "key-1", "rcp-brain", "ck-brain")
    listing = {
        "proposals": [
            {"id": "p-1", "recipe_id": "rcp-page"},
            {"id": "p-2", "recipe_id": None},
            {"id": "p-3", "recipe_id": "rcp-count"},
        ]
    }
    await doors.teardown(hatched, listing)
    deletes = [p for m, p, _ in api.requests if m == "DELETE"]
    assert deletes[:2] == ["/ingress_rules/rule-1", "/keys/key-1"]
    assert set(deletes[2:5]) == {
        "/checkpoints/ck-brain",
        "/checkpoints/ck-count",
        "/checkpoints/ck-verb",
    }
    assert set(deletes[5:]) == {"/recipes/rcp-brain", "/recipes/rcp-page", "/recipes/rcp-count"}
    assert "/checkpoints/ck-other" not in deletes


async def test_teardown_drops_the_scripted_server_first(tmp_path: Path) -> None:
    api = FakeApi()
    doors = _doors(api, tmp_path)
    hatched = Hatched("u", "rule-1", "key-1", "rcp-brain", "ck-brain", server_id="srv-1")
    await doors.teardown(hatched, None)
    deletes = [p for m, p, _ in api.requests if m == "DELETE"]
    assert deletes[0] == "/computers/srv-1"
    assert deletes[1:3] == ["/ingress_rules/rule-1", "/keys/key-1"]


async def test_teardown_survives_failing_deletes(tmp_path: Path) -> None:
    api = FakeApi(fail_deletes=True)
    doors = _doors(api, tmp_path)
    hatched = Hatched("u", "rule-1", "key-1", "rcp-brain", "ck-brain")
    await doors.teardown(hatched, None)
    assert [p for m, p, _ in api.requests if m == "DELETE"] == [
        "/ingress_rules/rule-1",
        "/keys/key-1",
        "/recipes/rcp-brain",
    ]


# ---------------------------------------------------------------- keys and signatures


def test_new_key_names_its_owner_and_sign_verifies(tmp_path: Path) -> None:
    pubkey = new_key(tmp_path)
    kind, key, comment = pubkey.split()
    assert kind == "ssh-ed25519" and comment == "mike" and len(key) > 40
    payload = sign(tmp_path, "Who am I?")
    assert payload["msg"] == "Who am I?"
    # The envelope carries the armor `ssh-keygen -Y sign` printed, with no second
    # encoding over it (#123): a hook that writes `sig` to a file and verifies it,
    # which is the obvious thing to write, must succeed. 2026-09-10-postcut-run-4
    # lost authentication to a base64 layer nothing disclosed and nothing reported.
    assert payload["sig"].startswith("-----BEGIN SSH SIGNATURE-----")
    (tmp_path / "allowed_signers").write_text(f"mike {pubkey}\n")
    (tmp_path / "sig.txt").write_text(payload["sig"])
    (tmp_path / "msg.txt").write_text(payload["msg"])
    verified = subprocess.run(
        [
            *("ssh-keygen", "-Y", "verify"),
            *("-f", str(tmp_path / "allowed_signers")),
            *("-I", "mike"),
            *("-n", "mshkn"),
            *("-s", str(tmp_path / "sig.txt")),
        ],
        stdin=(tmp_path / "msg.txt").open("rb"),
        capture_output=True,
    )
    assert verified.returncode == 0, verified.stderr.decode()


# ---------------------------------------------------------------- the liturgy over a fake door


class FakeDoors:
    """A door whose membrane is a small state machine: proposals are made on the
    turns the liturgy expects them, approvals move them to building, and each
    verb becomes ready after `builds_to_ready` polls (a failed build first when
    the verb is in `fail_first`)."""

    def __init__(
        self,
        *,
        fail_first: set[str] | None = None,
        never_ready: set[str] | None = None,
        open_door: bool = True,
        title_reply: str = 'The page\'s title is "Example Domain".',
        counts: tuple[str, str] = ("1\n", "2\n"),
        refuse: set[str] | None = None,
        polls_to_ready: int = 1,
        policy_first: bool = False,
        deadline_first: bool = False,
    ) -> None:
        self.policy_first = policy_first
        self.deadline_first = deadline_first
        self.fail_first = fail_first or set()
        self.never_ready = never_ready or set()
        self.open_door = open_door
        self.title_reply = title_reply
        self.counts = list(counts)
        self.refuse = refuse or set()
        self.polls_to_ready = polls_to_ready
        self.proposals: list[dict[str, Any]] = []
        self.catalog: dict[str, dict[str, Any]] = {}
        self.polls: dict[str, int] = {}
        self.policy: dict[str, Any] = {
            "principals": {"anonymous": {"invoke": [], "propose": False}},
            "hooks": [],
            "door": "closed",
        }
        self.sent: list[Any] = []
        self.public_principals: list[str] = []
        self.commands = 0
        self.turn = 0
        self.repairs = 0

    def _propose(self, kind: str, name: str, **extra: Any) -> dict[str, Any]:
        pid = f"p-{len(self.proposals) + 1}"
        doc = {
            "id": pid,
            "kind": kind,
            "status": "pending",
            "recipe_id": None,
            "log": None,
            **extra,
        }
        if kind == "verb":
            doc["verb"] = {"name": name, "state": extra.get("state", "ephemeral"), "effect": "read"}
        self.proposals.append(doc)
        return doc

    def _reply(self, audit: dict[str, Any], reply: str) -> tuple[dict[str, Any], str]:
        self.commands += 1
        self.turn += 1
        audit.setdefault("turn", self.turn)
        return audit, reply + "\n"

    async def root_say(self, text: str) -> tuple[dict[str, Any], str]:
        self.sent.append(("api", "say", text))
        made: list[dict[str, Any]] = []
        if text == LITURGY[1]:
            return self._reply(_audit(), "I have remember, try and propose. My door is closed.")
        if text.startswith(LITURGY[2][:30]) and self.deadline_first:
            # the trial's build outlived the turn (live run 2026-09-09-run-5)
            self.deadline_first = False
            self.pending_door = True
            audit = _audit(tools=[{"name": "try", "status": "building", "trial": "t-1"}])
            audit["stopped"] = "deadline"
            return self._reply(audit, "I ran out of time before finishing this turn.")
        if text == LITURGY[3] and getattr(self, "pending_door", False):
            self.pending_door = False
            text = LITURGY[2]  # the check of the trial ends in the two proposals
        if text.startswith(LITURGY[2][:30]):
            door_policy = {
                "principals": {
                    "ssh:mike": {"invoke": [], "propose": True},
                    "anonymous": {"invoke": [], "propose": False},
                },
                "hooks": ["verify_ssh"],
                "door": "open",
            }
            if self.policy_first:
                made.append(self._propose("policy", "door", policy=door_policy))
                made.append(self._propose("verb", "verify_ssh", asserts="ssh"))
            else:
                made.append(self._propose("verb", "verify_ssh", asserts="ssh"))
                made.append(self._propose("policy", "door", policy=door_policy))
        elif text == LITURGY[3]:
            self.repairs += 1
            failed = [n for n, e in self.catalog.items() if e["status"] == "failed"]
            for name in failed:
                self.fail_first.discard(name)
                made.append(
                    self._propose("verb", name, supersedes=self.catalog[name]["proposal_id"])
                )
        audit = _audit(
            tools=[{"name": "propose", "status": "ok", "id": p["id"]} for p in made],
            proposals=[{"id": p["id"], "sha256": "x"} for p in made],
        )
        return self._reply(audit, "Proposed.")

    async def public_say(self, payload: Any) -> tuple[dict[str, Any], str]:
        self.sent.append(("ingress", "say", payload))
        if self.policy["door"] != "open" or not self.open_door:
            self.commands += 1
            return {
                "door": "ingress",
                "principal": None,
                "closed": True,
            }, "The public door is closed.\n"
        signed = isinstance(payload, dict) and "sig" in payload
        principal = (
            "ssh:mike"
            if signed and self.catalog.get("verify_ssh", {}).get("status") == "ready"
            else "anonymous"
        )
        self.public_principals.append(principal)
        msg = payload["msg"] if isinstance(payload, dict) else payload
        if principal == "anonymous":
            return self._reply(
                _audit(door="ingress", principal="anonymous", offered=[], memory_written=False),
                "I do not know you; I will not act or remember.",
            )
        offered = ["propose", "remember", "try", *sorted(self.catalog)]
        made: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        reply = "You are ssh:mike."
        if msg == LITURGY[6]:
            made.append(
                self._propose(
                    "policy",
                    "authz",
                    policy={
                        "principals": {
                            "ssh:mike": {"invoke": "*", "propose": True},
                            "anonymous": {"invoke": [], "propose": False},
                        },
                        "hooks": ["verify_ssh"],
                        "door": "open",
                    },
                )
            )
            reply = "Recorded."
        elif msg == LITURGY[7]:
            tools.append({"name": "try", "status": "done", "trial": "t-1"})
            made.append(self._propose("verb", "page_title"))
            reply = "Proposed page_title."
        elif msg == LITURGY[8]:
            if "page_title" in self.catalog:
                tools.append(
                    {
                        "name": "page_title",
                        "status": "ok",
                        "exit_code": 0,
                        "computer_id": "comp-title",
                    }
                )
                reply = self.title_reply
            else:
                reply = "I have no such verb."
        elif msg == LITURGY[9]:
            made.append(self._propose("verb", "counter", state="chain"))
            reply = "Proposed counter."
        elif msg == COUNT:
            n = len(
                [
                    s
                    for s in self.sent
                    if s[2] and isinstance(s[2], dict) and s[2].get("msg") == COUNT
                ]
            )
            tools.append(
                {
                    "name": "counter",
                    "status": "ok",
                    "exit_code": 0,
                    "computer_id": f"comp-count-{n}",
                    "chain_head": f"ck-{n}",
                }
            )
            reply = f"The count is {self.counts[n - 1].strip()}."
        audit = _audit(
            door="ingress",
            principal="ssh:mike",
            offered=offered,
            tools=[*tools, *({"name": "propose", "status": "ok", "id": p["id"]} for p in made)],
            proposals=[{"id": p["id"], "sha256": "x"} for p in made],
        )
        return self._reply(audit, reply)

    async def root(self, *argv: str) -> str:
        self.sent.append(("api", argv[0], argv[1:]))
        self.commands += 1
        if argv[0] == "list":
            return json.dumps(self._listing())
        pid = argv[1]
        proposal = next(p for p in self.proposals if p["id"] == pid)
        if argv[0] == "reject":
            proposal["status"] = "rejected"
            return f"{pid} rejected\n"
        if pid in self.refuse:
            # The membrane records a refusal on the proposal and puts it in the
            # inbox (#123), which is how the driver knows a repair turn is owed.
            proposal["log"] = "refused: an effect the embryo does not approve"
            return f"{pid} refused: an effect the embryo does not approve\n"
        if proposal["kind"] == "policy":
            missing = [h for h in proposal["policy"]["hooks"] if h not in self.catalog]
            if missing:  # the membrane's invariant (§10.6): a door needs its hook
                proposal["log"] = f"refused: hook {missing[0]} is not a verb in the catalog"
                return f"{pid} refused: hook {missing[0]} is not a verb in the catalog\n"
            proposal["status"] = "applied"
            self.policy = proposal["policy"]
            return f"{pid} applied: policy replaced; effective from the next turn\n"
        name = proposal["verb"]["name"]
        proposal["status"] = "building"
        proposal["recipe_id"] = f"rcp-{name}-{pid}"
        self.catalog[name] = {
            "status": "building",
            "proposal_id": pid,
            "state": proposal["verb"]["state"],
            "effect": "read",
            "recipe_id": proposal["recipe_id"],
            "chain_head": None,
            "chain_length": 0,
        }
        self.polls[name] = 0
        return f"{pid} building: verb {name} recipe {proposal['recipe_id']}\n"

    def _listing(self) -> dict[str, Any]:
        for name, entry in self.catalog.items():
            if entry["status"] != "building":
                continue
            self.polls[name] += 1
            if name in self.never_ready or self.polls[name] < self.polls_to_ready:
                continue
            proposal = next(p for p in self.proposals if p["id"] == entry["proposal_id"])
            if name in self.fail_first:
                entry["status"] = proposal["status"] = "failed"
                proposal["log"] = "apt: package nope not found"
            else:
                entry["status"] = proposal["status"] = "ready"
        counts = len([s for s in self.sent if isinstance(s[2], dict) and s[2].get("msg") == COUNT])
        if "counter" in self.catalog:
            # What `list_state` reports: the newest checkpoint on the label, which is
            # the head the last invocation created (`verbs.py`, `chain_head`).
            self.catalog["counter"]["chain_length"] = counts
            self.catalog["counter"]["chain_head"] = f"ck-{counts}" if counts else None
        return {
            "turn": self.turn,
            "door": {
                "status": self.policy["door"],
                "hooks": self.policy["hooks"],
                "hooks_ready": [
                    h
                    for h in self.policy["hooks"]
                    if self.catalog.get(h, {}).get("status") == "ready"
                ],
            },
            "policy": self.policy,
            "principals": sorted(set(self.public_principals) - {"anonymous"}),
            "catalog": {
                n: {k: v for k, v in e.items() if k != "proposal_id"}
                for n, e in self.catalog.items()
            },
            "proposals": [dict(p) for p in self.proposals],
            "trials": [],
            "inbox": 0,
        }

    async def listing(self) -> dict[str, Any]:
        return dict(json.loads(await self.root("list")))

    async def wait_builds(self) -> dict[str, Any]:
        for _ in range(5):
            listing = await self.listing()
            if not any(e["status"] == "building" for e in listing["catalog"].values()):
                return listing
        return listing

    async def check_computer(self, computer_id: str) -> dict[str, Any]:
        if computer_id == "comp-title":
            return {
                "computer_id": computer_id,
                "gone": True,
                "stdout": "Example Domain\n",
                "exit_code": 0,
            }
        n = int(computer_id.rsplit("-", 1)[1])
        return {
            "computer_id": computer_id,
            "gone": True,
            "stdout": self.counts[n - 1],
            "exit_code": 0,
        }

    async def recipes(self) -> set[str]:
        return {"rcp-pre", "rcp-brain", *(p["recipe_id"] for p in self.proposals if p["recipe_id"])}


def _keys(tmp_path: Path) -> tuple[Path, str]:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    return key_dir, new_key(key_dir)


async def test_the_happy_path_reaches_every_postcondition(tmp_path: Path) -> None:
    doors = FakeDoors()
    key_dir, pubkey = _keys(tmp_path)
    log = io.StringIO()
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=log)
    labels = [t.label for t in turns]
    assert labels == ["1", "2", "4", "5", "6", "7", "8", "9", "9-count-1", "9-count-2"]
    assert turns[1].approvals == [
        {
            "id": "p-1",
            "decision": "approve",
            "result": "p-1 building: verb verify_ssh recipe rcp-verify_ssh-p-1",
        },
        {
            "id": "p-2",
            "decision": "approve",
            "result": "p-2 applied: policy replaced; effective from the next turn",
        },
    ]
    assert turns[1].words.endswith(pubkey)
    assert turns[2].audit["principal"] == "ssh:mike" and turns[3].audit["principal"] == "anonymous"
    # the unsigned turn is exactly {"msg": ...}, the signed ones carry a signature
    public = [s for s in doors.sent if s[0] == "ingress"]
    assert set(public[0][2]) == {"msg", "sig"} and public[1][2] == {"msg": LITURGY[4]}
    final = await doors.listing()
    checks = {
        c["computer_id"]: c
        for c in [
            await doors.check_computer(cid)
            for cid in ("comp-title", "comp-count-1", "comp-count-2")
        ]
    }
    result = verdict(
        turns,
        final,
        recipes_after=await doors.recipes(),
        preexisting={"rcp-pre"},
        brain_recipe="rcp-brain",
        checks=checks,
        sent=[(s[0], s[1]) for s in doors.sent],
    )
    assert list(result) == list(POSTCONDITIONS)
    assert all(v["ok"] for v in result.values()), {k: v for k, v in result.items() if not v["ok"]}
    assert result["page_title"]["evidence"]["computer_id"] == "comp-title"
    assert result["counter"]["evidence"]["counts"] == [1, 2]
    assert "Turn 8" in log.getvalue()


async def test_verbs_are_approved_before_the_policies_that_name_them(tmp_path: Path) -> None:
    """Live run 2026-09-09-run-5: the model proposed the door policy as p-1 and the
    hook as p-2; approving in id order had the policy refused ("hook ... is not a
    verb in the catalog") and the door stayed closed until the next pass."""
    doors = FakeDoors(policy_first=True)
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    assert [a["id"] for a in turns[1].approvals] == ["p-2", "p-1"]
    assert all("refused" not in a["result"] for a in turns[1].approvals), turns[1].approvals
    assert turns[2].audit["principal"] == "ssh:mike"


async def test_a_proposal_refused_before_its_build_is_approved_again_after_it(
    tmp_path: Path,
) -> None:
    doors = FakeDoors(policy_first=True)
    original = doors.root

    async def refuse_once(*argv: str) -> str:
        # a first approval of the policy is refused whatever the order
        if argv[0] == "approve" and argv[1] == "p-1" and not getattr(doors, "seen", False):
            doors.seen = True  # type: ignore[attr-defined]
            doors.sent.append(("api", "approve", argv[1:]))
            return "p-1 refused: not yet\n"
        return await original(*argv)

    doors.root = refuse_once  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    results = [(a["id"], a["result"][:14]) for a in turns[1].approvals]
    assert results == [
        ("p-2", "p-2 building: "),
        ("p-1", "p-1 refused: n"),
        ("p-1", "p-1 applied: p"),
    ]
    assert turns[2].audit["principal"] == "ssh:mike"


async def test_a_turn_that_ran_out_before_proposing_gets_turn_3(tmp_path: Path) -> None:
    doors = FakeDoors(deadline_first=True)
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    assert [t.label for t in turns][:4] == ["1", "2", "3-repair-1", "4"]
    assert turns[1].audit["stopped"] == "deadline" and turns[1].approvals == []
    assert [a["id"] for a in turns[2].approvals] == ["p-1", "p-2"]
    assert turns[3].audit["principal"] == "ssh:mike"


async def test_a_refused_approval_gets_turn_3_and_root_says_check_your_inbox(
    tmp_path: Path,
) -> None:
    """2026-09-10-postcut-run-2: the membrane refused a hook that declared no
    parameters and put the reason in the inbox, but the repair loop watched only
    the catalog for a failed build, so no turn ever existed in which to read it.
    Turns 4 onward all arrive through the public door, so turn 3 is the only
    window there is."""
    doors = FakeDoors(refuse={"p-1"})
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    labels = [t.label for t in turns]
    assert "3-repair-1" in labels, labels
    repair = turns[labels.index("3-repair-1")]
    assert repair.words == REFUSED == "check your inbox"


async def test_a_refusal_earns_one_repair_round_not_one_per_settle(tmp_path: Path) -> None:
    """A proposal the model never repairs keeps its reason forever, and settle()
    runs after turns 6, 7 and 9 as well as turn 2. Without a memo each of those
    would buy three more turns of the model's time on a refusal it has already
    been shown and declined to fix (2026-09-10-postcut-run-3, killed by hand
    while it did exactly that)."""
    doors = FakeDoors(refuse={"p-1"})
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    repairs = [t.label for t in turns if t.label.startswith("3-repair-")]
    assert repairs == ["3-repair-1"], repairs


async def test_a_failed_build_is_repaired_with_turn_3(tmp_path: Path) -> None:
    doors = FakeDoors(fail_first={"verify_ssh"})
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    labels = [t.label for t in turns]
    assert labels[:4] == ["1", "2", "3-repair-1", "4"]
    repair = turns[2]
    assert repair.words == LITURGY[3] and repair.door == "api"
    assert (
        repair.approvals[0]["id"] == "p-3"
        and "building: verb verify_ssh" in repair.approvals[0]["result"]
    )
    assert turns[3].audit["principal"] == "ssh:mike"


async def test_repairs_stop_after_three_rounds_and_the_run_goes_on(tmp_path: Path) -> None:
    doors = FakeDoors(fail_first={"verify_ssh"})
    doors.fail_first = {"verify_ssh"}
    original = doors.root_say

    async def stubborn(text: str) -> tuple[dict[str, Any], str]:
        out = await original(text)
        doors.fail_first.add("verify_ssh")  # every fix fails too
        return out

    doors.root_say = stubborn  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    labels = [t.label for t in turns]
    assert labels[:6] == ["1", "2", "3-repair-1", "3-repair-2", "3-repair-3", "4"]
    # the hook never became ready, so the signed knock is anonymous
    assert turns[5].audit["principal"] == "anonymous"
    final = await doors.listing()
    result = verdict(
        turns,
        final,
        recipes_after=set(),
        preexisting=set(),
        brain_recipe="rcp-brain",
        checks={},
        sent=[],
    )
    assert result["authentication"]["ok"] is False


async def test_a_closed_door_makes_every_public_turn_a_refusal(tmp_path: Path) -> None:
    doors = FakeDoors(open_door=False)
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    public = [t for t in turns if t.door.startswith("ingress")]
    assert public and all(t.audit["principal"] is None for t in public)
    final = await doors.listing()
    result = verdict(
        turns,
        final,
        recipes_after=set(),
        preexisting=set(),
        brain_recipe="rcp-brain",
        checks={},
        sent=[],
    )
    failed = {k for k, v in result.items() if not v["ok"]}
    assert failed == {"authentication", "authorization", "page_title", "counter"}
    assert result["root_unforgeable"]["evidence"] == {"public_principals": []}


async def test_a_refused_approval_is_recorded_and_the_verdict_sees_it(tmp_path: Path) -> None:
    doors = FakeDoors(refuse={"p-2"})
    key_dir, pubkey = _keys(tmp_path)
    turns = await speak_liturgy(doors, key_dir, pubkey, AutoApprover(), log=io.StringIO())
    assert turns[1].approvals[1]["result"].startswith("p-2 refused")
    assert doors.policy["door"] == "closed"


async def test_the_asking_approver_reads_the_pilot(tmp_path: Path) -> None:
    doors = FakeDoors()
    key_dir, pubkey = _keys(tmp_path)
    stdin = io.StringIO("approve\nreject not like this\napprove\napprove\napprove\n")
    stdout = io.StringIO()
    turns = await speak_liturgy(
        doors, key_dir, pubkey, AskApprover(stdin, stdout), log=io.StringIO()
    )
    assert turns[1].approvals[1] == {
        "id": "p-2",
        "decision": "reject: not like this",
        "result": "p-2 rejected",
    }
    rejected = next(s for s in doors.sent if s[1] == "reject")
    assert rejected[2][0] == "p-2" and base64.b64decode(rejected[2][1]) == b"not like this"
    assert "p-2" in stdout.getvalue() and "approve | reject <reason>" in stdout.getvalue()


def test_the_asking_approver_at_end_of_input_rejects() -> None:
    approver = AskApprover(io.StringIO(""), io.StringIO())
    assert approver.decide({"id": "p-9"}) == "no pilot"


# ---------------------------------------------------------------- the verdict on fixtures


def _turn(label: str, door: str, audit: dict[str, Any], reply: str = "") -> Turn:
    return Turn(
        label=label, door=door, words="w", audit=audit, reply=reply, commands=[], approvals=[]
    )


def _good_final() -> dict[str, Any]:
    return {
        "door": {"status": "open", "hooks": ["verify_ssh"], "hooks_ready": ["verify_ssh"]},
        "policy": {
            "principals": {
                "ssh:mike": {"invoke": "*", "propose": True},
                "anonymous": {"invoke": [], "propose": False},
            },
            "hooks": ["verify_ssh"],
            "door": "open",
        },
        "catalog": {
            "verify_ssh": {
                "status": "ready",
                "state": "ephemeral",
                "chain_length": 0,
                "recipe_id": "r1",
            },
            "page_title": {
                "status": "ready",
                "state": "ephemeral",
                "chain_length": 0,
                "recipe_id": "r2",
            },
            "counter": {
                "status": "ready",
                "state": "chain",
                "chain_length": 2,
                "chain_head": "k2",
                "recipe_id": "r3",
            },
        },
        "proposals": [
            {
                "id": "p-1",
                "kind": "verb",
                "status": "ready",
                "recipe_id": "r1",
                "verb": {"name": "verify_ssh"},
            },
            {"id": "p-2", "kind": "policy", "status": "applied", "recipe_id": None},
            {"id": "p-3", "kind": "policy", "status": "applied", "recipe_id": None},
            {
                "id": "p-4",
                "kind": "verb",
                "status": "ready",
                "recipe_id": "r2",
                "verb": {"name": "page_title"},
            },
            {
                "id": "p-5",
                "kind": "verb",
                "status": "ready",
                "recipe_id": "r3",
                "verb": {"name": "counter"},
            },
        ],
    }


def _good_turns() -> list[Turn]:
    return [
        _turn("1", "api", _audit()),
        _turn("4", "ingress", _audit(door="ingress", principal="ssh:mike")),
        _turn("5", "ingress-unsigned", _audit(door="ingress", principal="anonymous", offered=[])),
        _turn(
            "8",
            "ingress",
            _audit(
                door="ingress",
                principal="ssh:mike",
                tools=[{"name": "page_title", "computer_id": "c8"}],
                offered=["effort", "page_title", "propose", "remember", "try", "verify_ssh"],
            ),
            "Example Domain",
        ),
        _turn(
            "9-count-1",
            "ingress",
            _audit(
                door="ingress",
                principal="ssh:mike",
                tools=[{"name": "counter", "computer_id": "c9a", "chain_head": "k1"}],
            ),
            "1",
        ),
        _turn(
            "9-count-2",
            "ingress",
            _audit(
                door="ingress",
                principal="ssh:mike",
                tools=[{"name": "counter", "computer_id": "c9b", "chain_head": "k2"}],
            ),
            "2",
        ),
    ]


def _good_checks() -> dict[str, dict[str, Any]]:
    return {
        "c8": {"computer_id": "c8", "gone": True, "stdout": "Example Domain\n", "exit_code": 0},
        "c9a": {"computer_id": "c9a", "gone": True, "stdout": "1\n", "exit_code": 0},
        "c9b": {"computer_id": "c9b", "gone": True, "stdout": "called 2 times\n", "exit_code": 0},
    }


def _judge(**overrides: Any) -> dict[str, dict[str, Any]]:
    args: dict[str, Any] = {
        "recipes_after": {"pre", "brain", "r1", "r2", "r3"},
        "preexisting": {"pre"},
        "brain_recipe": "brain",
        "checks": _good_checks(),
        "sent": [("api", "say"), ("api", "list"), ("api", "approve"), ("ingress", "say")],
    }
    args.update(overrides)
    turns = args.pop("turns", _good_turns())
    final = args.pop("final", _good_final())
    return verdict(turns, final, **args)


def test_the_fixtures_pass_every_postcondition() -> None:
    result = _judge()
    assert all(v["ok"] for v in result.values()), result


def test_authentication_evidence_carries_the_hook_runs_and_their_logs() -> None:
    turns = _good_turns()
    hook = {
        "name": "verify_ssh",
        "status": "ok",
        "computer_id": "c-hook",
        "exit_code": 1,
        "principal": "anonymous",
    }
    turns[1] = _turn("4", "ingress", _audit(door="ingress", principal="anonymous", hooks=[hook]))
    checks = _good_checks()
    checks["c-hook"] = {"computer_id": "c-hook", "gone": True, "stdout": "", "exit_code": 1}
    result = _judge(turns=turns, checks=checks)["authentication"]
    assert result["ok"] is False
    assert result["evidence"]["hooks"] == [hook] and result["evidence"]["hook_logs"] == [
        checks["c-hook"]
    ]


def test_root_is_unforgeable_fails_when_a_public_turn_is_root() -> None:
    turns = _good_turns()
    turns.append(_turn("x", "ingress", _audit(door="ingress", principal="root")))
    assert _judge(turns=turns)["root_unforgeable"] == {
        "ok": False,
        "evidence": {
            "public_principals": [
                "ssh:mike",
                "anonymous",
                "ssh:mike",
                "ssh:mike",
                "ssh:mike",
                "root",
            ]
        },
    }
    turns = _good_turns()
    turns.append(_turn("x", "ingress", _audit(door="ingress", principal="system:me")))
    assert _judge(turns=turns)["root_unforgeable"]["ok"] is False


def test_authorization_needs_the_policy_and_the_empty_anonymous_offer() -> None:
    final = _good_final()
    final["policy"]["principals"]["anonymous"] = {"invoke": ["page_title"], "propose": False}
    assert _judge(final=final)["authorization"]["ok"] is False
    final = _good_final()
    final["policy"]["principals"]["ssh:mike"] = {"invoke": ["page_title"], "propose": True}
    assert _judge(final=final)["authorization"]["ok"] is False  # counter not invokable
    final["policy"]["principals"]["ssh:mike"] = {
        "invoke": ["page_title", "counter", "verify_ssh"],
        "propose": True,
    }
    assert _judge(final=final)["authorization"]["ok"] is True
    turns = _good_turns()
    turns[2] = _turn(
        "5",
        "ingress-unsigned",
        _audit(door="ingress", principal="anonymous", offered=["page_title"]),
    )
    assert _judge(turns=turns)["authorization"]["ok"] is False


def test_page_title_needs_the_words_a_gone_computer_and_its_log() -> None:
    checks = _good_checks()
    checks["c8"]["gone"] = False
    assert _judge(checks=checks)["page_title"]["ok"] is False
    checks = _good_checks()
    checks["c8"]["stdout"] = "Something else"
    assert _judge(checks=checks)["page_title"]["ok"] is False
    turns = _good_turns()
    turns[3] = _turn("8", "ingress", _audit(door="ingress", principal="ssh:mike"), "I cannot.")
    assert _judge(turns=turns)["page_title"] == {
        "ok": False,
        "evidence": {"reply": "I cannot.", "computer_id": None, "gone": None, "stdout": None},
    }


def _repeated_head_turns() -> list[Turn]:
    """The counted turns with both invocations reporting one head: the second
    invocation left no new checkpoint, so the chain did not advance."""
    turns = []
    for t in _good_turns():
        if t.label.startswith("9-count"):
            audit = json.loads(json.dumps(t.audit))
            for call in audit.get("tools", []):
                if call.get("name") == "counter":
                    call["chain_head"] = "k1"
            t = _turn(t.label, t.door, audit, t.reply)
        turns.append(t)
    return turns


def test_counter_needs_one_then_two_and_a_head_that_advanced_once_per_call() -> None:
    """The counter is monotonic and every invocation leaves a new head (#139).
    Counting the chain's rows instead asserted retained history, which #93
    retention is entitled to collect: `list_prunable_checkpoints` keeps every
    label's newest row and prunes the rest."""
    checks = _good_checks()
    checks["c9b"]["stdout"] = "3\n"
    assert _judge(checks=checks)["counter"]["ok"] is False  # 1 then 3, not monotonic

    turns = _repeated_head_turns()
    result = _judge(turns=turns)["counter"]
    assert result["ok"] is False  # two calls, one head: the chain never advanced
    assert result["evidence"]["chain_heads"] == ["k1", "k1"]

    final = _good_final()
    final["catalog"]["counter"]["chain_head"] = "k-other"
    assert _judge(final=final)["counter"]["ok"] is False  # the head is not the last call's

    turns = [t for t in _good_turns() if not t.label.startswith("9-count")]
    result = _judge(turns=turns)["counter"]
    assert result["ok"] is False and result["evidence"]["counts"] == []


def test_the_counter_survives_retention_pruning_its_history() -> None:
    """#139: `2026-09-11-turn2-run-1` invoked its counter twice, got 1 then 2, and
    the reaper pruned the first invocation's checkpoint nine seconds later, so the
    catalog reported one row for two invocations. Every durable fact still holds,
    and the postcondition must pass on them."""
    final = _good_final()
    final["catalog"]["counter"]["chain_length"] = 1  # the older row is gone
    result = _judge(final=final)["counter"]
    assert result["ok"] is True
    assert result["evidence"]["counts"] == [1, 2]
    assert result["evidence"]["chain_heads"] == ["k1", "k2"]
    assert result["evidence"]["final_chain_head"] == "k2"


def test_a_trials_recipe_is_declared() -> None:
    final = _good_final()
    final["trials"] = [{"id": "t-1", "verb": "probe", "status": "done", "recipe_id": "r-trial"}]
    result = _judge(final=final, recipes_after={"pre", "brain", "r1", "r2", "r3", "r-trial"})
    assert result["no_undeclared_capability"]["ok"] is True


def test_no_undeclared_capability_watches_the_catalog_the_offer_and_the_recipes() -> None:
    assert _judge(recipes_after={"pre", "brain", "r1", "r2", "r3", "stray"})[
        "no_undeclared_capability"
    ] == {
        "ok": False,
        "evidence": {
            "catalog": ["counter", "page_title", "verify_ssh"],
            "not_ready": [],
            "unproposed": [],
            "unexpected_tools": [],
            "undeclared_recipes": ["stray"],
        },
    }
    final = _good_final()
    final["catalog"]["extra"] = {
        "status": "ready",
        "state": "ephemeral",
        "chain_length": 0,
        "recipe_id": "r9",
    }
    assert _judge(final=final)["no_undeclared_capability"]["evidence"]["unproposed"] == ["extra"]
    final = _good_final()
    final["catalog"]["counter"]["status"] = "building"
    assert _judge(final=final)["no_undeclared_capability"]["evidence"]["not_ready"] == ["counter"]
    turns = _good_turns()
    turns[3].audit["offered"] = ["propose", "remember", "shell", "try"]
    assert _judge(turns=turns)["no_undeclared_capability"]["evidence"]["unexpected_tools"] == [
        "shell"
    ]


def test_nothing_by_hand_is_the_command_list() -> None:
    assert _judge()["nothing_by_hand"] == {
        "ok": True,
        "evidence": {"commands": {"api say": 1, "api list": 1, "api approve": 1, "ingress say": 1}},
    }
    assert _judge(sent=[("api", "upload")])["nothing_by_hand"]["ok"] is False


# ---------------------------------------------------------------- hatching and a whole run


def _stub_hatch(tmp_path: Path, *, fail: bool = False) -> Path:
    script = tmp_path / "hatch.sh"
    body = "#!/bin/sh\necho building >&2\n"
    if fail:
        body += "echo boom >&2\nexit 1\n"
    else:
        body += (
            "env | grep -E '^(MSHKN_API_URL|MSHKN_API_KEY|BRAIN_API_URL|MEMBRANE_MODEL"
            "|MEMBRANE_MODEL_ID|MEMBRANE_EFFORT|ANTHROPIC_API_KEY|OPENAI_API_KEY)='"
            ' | sort > "$HATCH_ENV_OUT"\n'
            "echo '"
            + json.dumps(
                {
                    "ingress_url": "http://api/ingress/rule-1",
                    "rule_id": "rule-1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-brain",
                }
            )
            + "'\n"
        )
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _settings() -> MeasureSettings:
    return MeasureSettings(
        api_url="http://api",
        api_key="k",
        brain_api_url="https://api.mshkn.dev",
        anthropic_api_key="sk-a",
        openai_api_key="oa",
        model_id="claude-opus-5",
    )


def test_hatch_runs_the_script_with_the_keys_and_the_real_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "env.txt"
    monkeypatch.setenv("HATCH_ENV_OUT", str(out))
    hatched = hatch(_settings(), _stub_hatch(tmp_path), log=io.StringIO())
    assert hatched == Hatched(
        "http://api/ingress/rule-1", "rule-1", "key-1", "rcp-brain", "ck-brain"
    )
    assert out.read_text() == (
        "ANTHROPIC_API_KEY=sk-a\nBRAIN_API_URL=https://api.mshkn.dev\nMEMBRANE_EFFORT=\n"
        "MEMBRANE_MODEL=anthropic\nMEMBRANE_MODEL_ID=claude-opus-5\nMSHKN_API_KEY=k\n"
        "MSHKN_API_URL=http://api\nOPENAI_API_KEY=oa\n"
    )


def test_a_failed_hatch_raises_with_its_stderr(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        hatch(_settings(), _stub_hatch(tmp_path, fail=True), log=io.StringIO())


async def test_run_once_hatches_speaks_judges_records_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    api = FakeApi(recipes=[{"recipe_id": "rcp-pre"}])
    transport = httpx.MockTransport(api.handler)
    monkeypatch.setattr("membrane.measure.transport_for", lambda _url: transport)
    out_dir = tmp_path / "docs" / "2026-09-09-run-1"
    summary = await run_once(
        _settings(),
        out_dir,
        AutoApprover(),
        hatch_script=_stub_hatch(tmp_path),
        key_dir=tmp_path / "keys",
        keep=False,
        log=io.StringIO(),
    )
    # every membrane command was answered by the fake's default (a root reply), so the
    # door was never opened and the postconditions that need it fail honestly
    assert summary["ok"] is False and summary["model"] == "claude-opus-5"
    assert summary["hatched"]["rule_id"] == "rule-1" and summary["passed"] < len(POSTCONDITIONS)
    assert summary["usage"]["input_tokens"] > 0 and summary["cost_usd"] > 0
    assert set(summary["postconditions"]) == set(POSTCONDITIONS)
    assert (out_dir / "run.json").exists() and (out_dir / "transcript.md").exists()
    assert (out_dir / "final-list.json").exists() and any((out_dir / "commands").iterdir())
    run_doc = json.loads((out_dir / "run.json").read_text())
    assert run_doc["turns"][0]["label"] == "1"
    # what the run was given, and what each turn's calls actually spent (#122)
    assert run_doc["default_effort"] is None
    assert run_doc["turns"][0]["effort"] == ["medium"]
    deletes = [p for m, p, _ in api.requests if m == "DELETE"]
    assert (
        deletes[:2] == ["/ingress_rules/rule-1", "/keys/key-1"] and "/recipes/rcp-brain" in deletes
    )


async def test_run_once_refuses_an_account_that_already_has_a_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi(checkpoints=[{"id": "ck", "label": "brain", "recipe_id": "r"}])
    monkeypatch.setattr(
        "membrane.measure.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    with pytest.raises(RuntimeError, match="already has a brain"):
        await run_once(
            _settings(),
            tmp_path / "run",
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
        )


async def test_an_aborted_run_writes_what_it_had_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    failing = 'audit {"principal": "root"}\n'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/checkpoints/fork":
            return httpx.Response(
                200,
                json={
                    "computer_id": "c1",
                    "exec_exit_code": 1,
                    "exec_stdout": failing,
                    "exec_stderr": "boom",
                },
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("membrane.measure.transport_for", lambda _url: httpx.MockTransport(handler))
    out_dir = tmp_path / "run"
    with pytest.raises(RuntimeError, match="boom"):
        await run_once(
            _settings(),
            out_dir,
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
        )
    summary = json.loads((out_dir / "run.json").read_text())
    assert summary["ok"] is False and "boom" in summary["error"] and summary["commands"] == 1
    assert summary["hatched"]["rule_id"] == "rule-1"
    assert "commit" in summary["membrane"]  # an aborted run names its code too


async def test_keep_skips_the_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    api = FakeApi()
    monkeypatch.setattr(
        "membrane.measure.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    await run_once(
        _settings(),
        tmp_path / "run",
        AutoApprover(),
        hatch_script=_stub_hatch(tmp_path),
        key_dir=tmp_path / "keys",
        keep=True,
        log=io.StringIO(),
    )
    assert not [p for m, p, _ in api.requests if m == "DELETE"]


def test_main_parses_and_runs_n_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=http://api\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n"
    )
    calls: list[tuple[Path, str, bool]] = []

    async def fake_run_once(
        settings: MeasureSettings, out_dir: Path, approver: Any, **kwargs: Any
    ) -> dict[str, Any]:
        calls.append((out_dir, type(approver).__name__, kwargs["keep"]))
        out_dir.mkdir(parents=True)
        return {
            "ok": len(calls) == 1,
            "passed": 7 if len(calls) == 1 else 5,
            "cost_usd": 1.25,
            "model": settings.model_id,
        }

    monkeypatch.setattr("membrane.measure.run_once", fake_run_once)
    code = main(
        [
            "--runs",
            "2",
            "--env",
            str(env),
            "--out",
            str(tmp_path / "docs"),
            "--date",
            "2026-09-09",
            "--keep",
        ],
        log=io.StringIO(),
    )
    assert code == 1  # not every run reached every postcondition
    assert [c[0].name for c in calls] == ["2026-09-09-run-1", "2026-09-09-run-2"]
    assert calls[0][1] == "AutoApprover" and calls[0][2] is True
    # a third invocation numbers itself after the directories that exist
    monkeypatch.setattr("membrane.measure.run_once", fake_run_once)
    calls.clear()
    code = main(
        [
            "--runs",
            "1",
            "--env",
            str(env),
            "--out",
            str(tmp_path / "docs"),
            "--date",
            "2026-09-09",
            "--approve",
            "ask",
        ],
        log=io.StringIO(),
    )
    assert code == 0 and calls[0][0].name == "2026-09-09-run-3" and calls[0][1] == "AskApprover"


def test_main_reports_missing_settings(tmp_path: Path) -> None:
    log = io.StringIO()
    assert main(["--env", str(tmp_path / "none")], log=log) == 2
    assert "MSHKN_API_URL" in log.getvalue()


def test_env_of_this_repository_is_ignored_by_git() -> None:
    root = Path(__file__).resolve().parents[2]
    assert ".env" in (root / ".gitignore").read_text().split()


# ---------------------------------------------------------------- edges


def test_split_output_requires_an_audit_line() -> None:
    with pytest.raises(RuntimeError, match="audit line"):
        split_output("Traceback (most recent call last)\n")
    assert transport_for("http://api") is None


async def test_teardown_goes_on_when_the_checkpoints_cannot_be_listed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    doors = Doors(
        httpx.AsyncClient(base_url="http://api", transport=transport),
        httpx.AsyncClient(base_url="http://api", transport=transport),
        "rule-1",
        Record(tmp_path / "run"),
    )
    await doors.teardown(Hatched("u", "rule-1", "key-1", "rcp-brain", "ck-brain"), None)


def test_the_asking_approver_asks_again_after_an_unknown_answer() -> None:
    stdout = io.StringIO()
    approver = AskApprover(io.StringIO("what\napprove\n"), stdout)
    assert approver.decide({"id": "p-1"}) is None
    assert stdout.getvalue().count("approve | reject") == 2
    assert AskApprover(io.StringIO("reject\n"), io.StringIO()).decide({"id": "p-1"}) == "rejected"


def test_a_count_turn_needs_a_chain_head_to_count() -> None:
    turns = _good_turns()
    turns[4] = _turn(
        "9-count-1",
        "ingress",
        _audit(
            door="ingress",
            principal="ssh:mike",
            tools=[{"name": "page_title", "computer_id": "c8"}],
        ),
    )
    result = _judge(turns=turns)["counter"]
    assert result["ok"] is False and result["evidence"]["computer_ids"] == ["c9b"]


def test_membrane_version_names_the_commit_and_whether_the_tree_was_dirty(
    tmp_path: Path,
) -> None:
    version = membrane_version()
    assert len(version["commit"]) == 40 and isinstance(version["dirty"], bool)
    # outside a repository there is no commit to name
    assert membrane_version(tmp_path) == {"commit": None, "dirty": None}


def _counted(label: str, tools: list[dict[str, Any]], reply: str) -> Turn:
    return _turn(label, "ingress", _audit(door="ingress", principal="ssh:mike", tools=tools), reply)


def test_the_counter_passes_when_the_model_verifies_its_verb_within_one_turn() -> None:
    """#117, from live run 2026-09-10-run-4: the model called its counter twice in
    `9-count-1` to prove that state crossed the chain, so the invocations returned
    1, 2, 3 and the chain grew to three. What the postcondition tests is that the
    counter is monotonic and the chain grows once per invocation, not the numerals."""
    turns = _good_turns()
    turns[4] = _counted(
        "9-count-1",
        [
            {"name": "counter", "computer_id": "c9a", "chain_head": "k1"},
            {"name": "counter", "computer_id": "c9a2", "chain_head": "k2"},
        ],
        "1, then 2 on a different computer",
    )
    turns[5] = _counted(
        "9-count-2", [{"name": "counter", "computer_id": "c9b", "chain_head": "k3"}], "3"
    )
    checks = _good_checks()
    checks["c9a2"] = {"computer_id": "c9a2", "gone": True, "stdout": "2\n", "exit_code": 0}
    checks["c9b"]["stdout"] = "called 3 times\n"
    final = _good_final()
    final["catalog"]["counter"]["chain_length"] = 3
    final["catalog"]["counter"]["chain_head"] = "k3"  # the third invocation's head

    result = _judge(turns=turns, checks=checks, final=final)["counter"]

    assert result["ok"] is True
    assert result["evidence"]["counts"] == [1, 2, 3]
    assert result["evidence"]["chain_heads"] == ["k1", "k2", "k3"]


def test_the_counter_fails_when_an_invocation_is_lost() -> None:
    """The stricter half of #117: counting every invocation catches a chain that
    skipped one, which the old `counts == [1, 2]` would have passed on two calls."""
    turns = _good_turns()
    turns[4] = _counted(
        "9-count-1",
        [
            {"name": "counter", "computer_id": "c9a", "chain_head": "k1"},
            {"name": "counter", "computer_id": "c9a2", "chain_head": "k2"},
        ],
        "1, then 3",
    )
    checks = _good_checks()
    checks["c9a2"] = {"computer_id": "c9a2", "gone": True, "stdout": "3\n", "exit_code": 0}
    checks["c9b"]["stdout"] = "called 4 times\n"
    final = _good_final()
    final["catalog"]["counter"]["chain_length"] = 3

    result = _judge(turns=turns, checks=checks, final=final)["counter"]

    assert result["ok"] is False
    assert result["evidence"]["counts"] == [1, 3, 4]
