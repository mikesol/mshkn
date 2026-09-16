"""The measure (spec §11, #101): settings from .env, the doors over HTTP, a
capability spoken row by row with approvals and repairs, the seven
postconditions, the evidence written under docs/embryo/, and the cost."""

from __future__ import annotations

import base64
import io
import json
import stat
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from membrane.capabilities import CAPABILITIES, load
from membrane.capability import (
    CONFLICT_INTERVAL,
    MAX_REASKS,
    TRANSPORT_RETRIES,
    TURN_WAIT,
    AskApprover,
    AutoApprover,
    Doors,
    Hatched,
    Record,
    RunSettings,
    bare_model_id,
    cost_usd,
    hatch,
    load_key,
    load_run_settings,
    main,
    membrane_version,
    new_key,
    run_once,
    sign,
    speak,
    split_output,
    transport_for,
)
from membrane.config import EFFORT_OFF
from membrane.model import zero_usage
from membrane.postconditions import CHECKS, Judged, Turn, by_label, judge, tool_computers

from tests.support_embryo import HATCH, WORDS, audit_line, b64

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.unit


def _out(audit: dict[str, Any], reply: str) -> str:
    return "audit " + json.dumps(audit) + "\n" + reply + "\n"


# ---------------------------------------------------------------- settings and cost


def test_settings_come_from_the_env_file_and_the_environment_wins(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=http://file:8000\nMSHKN_API_KEY=file-key\n"
        "ANTHROPIC_API_KEY='sk-file'\nOPENAI_API_KEY=oa-file\n"
    )
    settings = load_run_settings(env, {"MSHKN_API_KEY": "env-key", "HOME": "/x"})
    assert settings == RunSettings(
        api_url="http://file:8000",
        api_key="env-key",
        brain_api_url="https://api.mshkn.dev",
        anthropic_api_key="sk-file",
        openai_api_key="oa-file",
        model_id="claude-opus-5",
    )
    with_brain = load_run_settings(
        env, {"BRAIN_API_URL": "http://10.0.0.1:8000"}, model_id="claude-sonnet-5", effort="medium"
    )
    assert with_brain.brain_api_url == "http://10.0.0.1:8000"
    # `--effort` is the run's default now, not the effort (#122): the turn raises it.
    assert with_brain.model_id == "claude-sonnet-5" and with_brain.default_effort == "medium"


def test_a_missing_key_is_named(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("MSHKN_API_URL=http://file:8000\nOPENAI_API_KEY=\n")
    with pytest.raises(ValueError, match="MSHKN_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY"):
        load_run_settings(env, {})
    with pytest.raises(ValueError, match=str(tmp_path / "absent")):
        load_run_settings(tmp_path / "absent", {})


def test_the_base_url_defaults_to_anthropic_and_the_anthropic_key_travels(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    env.write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=sk-a\nOPENAI_API_KEY=oa\n")
    settings = load_run_settings(env, {})
    assert settings.base_url == "https://api.anthropic.com"
    assert settings.gateway_api_key is None
    assert settings.model_api_key == "sk-a"


def test_a_gateway_base_url_sends_the_gateway_key_instead(tmp_path: Path) -> None:
    """The operator holds both slots so `--base-url` alone flips a run; the brain is
    handed one key and never learns which kind it is."""
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=sk-a\nOPENAI_API_KEY=oa\n"
        "AI_GATEWAY_API_KEY=vck-1\n"
    )
    settings = load_run_settings(env, {}, base_url="https://ai-gateway.vercel.sh/")
    # The trailing slash goes, as it does in the brain's own config (config.py:84).
    assert settings.base_url == "https://ai-gateway.vercel.sh"
    assert settings.model_api_key == "vck-1"
    assert settings.anthropic_api_key == "sk-a"


def test_a_gateway_base_url_without_a_gateway_key_is_refused_before_it_hatches(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    env.write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=sk-a\nOPENAI_API_KEY=oa\n")
    with pytest.raises(ValueError, match="AI_GATEWAY_API_KEY"):
        load_run_settings(env, {}, base_url="https://ai-gateway.vercel.sh")


def test_the_base_url_comes_from_the_env_file_too(tmp_path: Path) -> None:
    """Today it reaches hatch.sh only by leaking through `**os.environ`."""
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=sk-a\nOPENAI_API_KEY=oa\n"
        "AI_GATEWAY_API_KEY=vck-1\nANTHROPIC_BASE_URL=https://ai-gateway.vercel.sh\n"
    )
    assert load_run_settings(env, {}).model_api_key == "vck-1"


def test_model_api_key_raises_for_a_directly_built_settings_without_a_gateway_key() -> None:
    """`load_run_settings` refuses this combination, but `RunSettings` is a public frozen
    dataclass and nothing stops a caller building one directly (as `_stub_hatch`'s callers do
    with `dataclasses.replace`): the property must not silently hand an Anthropic key to a
    gateway."""
    settings = replace(_settings(), base_url="https://ai-gateway.vercel.sh")
    with pytest.raises(ValueError, match="AI_GATEWAY_API_KEY"):
        _ = settings.model_api_key


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


def test_a_gateway_id_prices_as_the_model_it_names() -> None:
    """A hosted gateway namespaces every id by its provider. The six runs spoken
    before the gateway existed must stay comparable to the ones spoken through it,
    so the prefix is stripped rather than given a second price row."""
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert cost_usd(usage, "anthropic/claude-opus-5") == cost_usd(usage, "claude-opus-5")
    assert bare_model_id("anthropic/claude-opus-5") == "claude-opus-5"
    assert bare_model_id("claude-opus-5") == "claude-opus-5"


def test_the_cheap_backend_is_priced_so_a_run_can_show_what_it_saved() -> None:
    """`zai/glm-4.7` is the first non-Anthropic model the gateway was proved to
    speak the liturgy to (2026-09-15 probe, spec §12). Without a row it records
    `cost_usd: null`, and a run whose whole point is that it is cheaper cannot say
    by how much. Prices from the gateway's own catalogue that day, per Mtok."""
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert cost_usd(usage, "zai/glm-4.7") == pytest.approx(2.80)
    assert cost_usd(usage, "claude-opus-5") == pytest.approx(30.0)


def test_deepseek_is_priced_at_the_rate_it_is_actually_served_at() -> None:
    """The catalogue lists `deepseek-v4-pro` at $0.66/$1.98 and then serves it from
    `us` only, where its own `regional` block doubles both. Taking the headline
    would halve every cost this model records; the second assertion is the negative
    control against exactly that. Its 2x weekday peak multiplier has no home in this
    table and is a known under-report rather than an omission to fix here — `Price`
    has no time axis, and giving it one to track a provider's tariff calendar is the
    organism learning what a provider is."""
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert cost_usd(usage, "deepseek/deepseek-v4-pro") == pytest.approx(5.28)
    assert cost_usd(usage, "deepseek/deepseek-v4-pro") != pytest.approx(2.64)


def test_the_cheapest_backend_on_the_gateway_is_priced_before_it_is_run() -> None:
    """`poolside/laguna-s-2.1` at $0.10/$0.20 is an order of magnitude under the two
    cheap models already here, so an unpriced run would record `cost_usd: null` for
    the one number the whole cheap-model exercise exists to produce. Catalogue read
    2026-09-16: flat pricing, no `regional` block and no peak multiplier, so this row
    carries none of the caveats the DeepSeek row above it does. The second assertion
    is the negative control against transposing the two rates, which is why the
    usage is lopsided: at a symmetric 1M/1M a transposed row sums to the same $0.30
    and the control would prove nothing. A hatch's usage is lopsided the same way —
    2026-09-16-run-1 spent 84k in against 168k out."""
    usage = {"input_tokens": 2_000_000, "output_tokens": 1_000_000}
    assert cost_usd(usage, "poolside/laguna-s-2.1") == pytest.approx(0.40)
    assert cost_usd(usage, "poolside/laguna-s-2.1") != pytest.approx(0.50)


def test_an_unpriced_model_costs_nothing_known_rather_than_losing_the_run() -> None:
    """`cost_usd` is called while the summary is assembled, after every turn has
    been spoken and paid for. A raise there throws away the evidence of a run that
    has already cost money, which is the worst moment this code could choose."""
    assert cost_usd(zero_usage(), "moonshot/kimi-k2") is None
    assert cost_usd({"input_tokens": 10}, "claude-unknown") is None


# ---------------------------------------------------------------- the record


def test_record_writes_every_command_the_transcript_and_the_summary(tmp_path: Path) -> None:
    record = Record(tmp_path / "run")
    n = record.command("api", "say", WORDS["1"], "audit {}\nhi\n", 200, 1.5)
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
        audit=audit_line(proposals=[{"id": "p-1", "sha256": "abc"}]),
        reply="I propose.\nproposal p-1\n{}",
        commands=[1],
        approvals=[{"id": "p-1", "decision": "approve", "result": "p-1 building: verb v"}],
        provisions=[
            {
                "verb": "secret_page",
                "name": "page_token",
                "path": "/verb/token",
                "checkpoint": "ck-1",
                "result": "ok",
            }
        ],
    )
    transcript = record.transcript("claude-opus-5", [turn])
    text = transcript.read_text()
    assert "## Turn 2" in text and "open the door" in text and "p-1 building: verb v" in text
    assert '"model_calls": 1' in text and "I propose." in text
    assert "**Provided:**" in text
    assert "- secret_page page_token at /verb/token -> ck-1: ok" in text
    listing = {"turn": 12, "catalog": {}}
    assert json.loads(record.final_list(listing).read_text()) == listing
    summary = {"ok": False, "postconditions": {}}
    assert json.loads(record.summary(summary).read_text()) == summary


# ---------------------------------------------------------------- the promotion record


def test_a_promotion_record_round_trips_and_is_readable_markdown(tmp_path: Path) -> None:
    from membrane.capability import Promotion, promotion_path, read_promotion, write_promotion

    p = Promotion(
        capability="hatch",
        run="hatch/2026-09-12-run-1",
        membrane={"commit": "abc", "dirty": False},
        promoted_at="2026-09-12T12:00:00+00:00",
        labels={"brain": "ck-b", "verb/counter": "ck-c"},
        rule_id="ir_1",
        key_id="key-1",
        brain_recipe="rcp-brain",
        recipe_ids=("rcp-brain", "rcp-counter"),
        key_dir="/home/mike/.mshkn/keys/hatch/2026-09-12-run-1",
        pubkey="ssh-ed25519 AAAA mike",
        model="claude-opus-5",
        default_effort=None,
        reasks=0,
        started_from=None,
        rotated_from=None,
    )
    path = write_promotion(tmp_path, p)
    assert path == promotion_path(tmp_path, "hatch") == tmp_path / "hatch" / "PROMOTED.md"
    text = path.read_text()
    assert text.startswith("# Promoted: hatch\n") and "```json" in text
    assert "capability/hatch/brain" in text and "ck-b" in text
    assert read_promotion(tmp_path, "hatch") == p
    assert read_promotion(tmp_path, "security") is None
    # The keys a run rotated after it was promoted (spec §7.1 step 2), written by
    # hand until `capability rotate` exists: the record carries them either way.
    rotated = replace(p, rotated_from="hatch/2026-09-13-run-5 ckpt-3d9e33337cff")
    write_promotion(tmp_path, rotated)
    assert '"rotated_from": "hatch/2026-09-13-run-5 ckpt-3d9e33337cff"' in path.read_text()
    read = read_promotion(tmp_path, "hatch")
    assert read is not None and read.rotated_from == "hatch/2026-09-13-run-5 ckpt-3d9e33337cff"


def test_ancestry_walks_started_from(tmp_path: Path) -> None:
    from membrane.capability import Promotion, ancestry, write_promotion

    base: dict[str, Any] = {
        "membrane": {},
        "promoted_at": "t",
        "labels": {},
        "rule_id": "r",
        "key_id": "k",
        "brain_recipe": "rcp",
        "recipe_ids": (),
        "key_dir": "/keys",
        "pubkey": "ssh-ed25519 AAAA mike",
        "model": "claude-opus-5",
        "default_effort": None,
        "reasks": 0,
    }
    write_promotion(
        tmp_path, Promotion(capability="hatch", run="hatch/r1", started_from=None, **base)
    )
    write_promotion(
        tmp_path,
        Promotion(capability="security", run="security/r1", started_from="hatch/r1", **base),
    )
    write_promotion(
        tmp_path,
        Promotion(capability="coding", run="coding/r1", started_from="security/r1", **base),
    )
    assert ancestry(tmp_path, "coding") == ["coding", "security", "hatch"]
    assert ancestry(tmp_path, "hatch") == ["hatch"]
    assert ancestry(tmp_path, "nope") == []


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
    fail_checkpoints: bool = False
    turns: dict[int, tuple[dict[str, Any], str]] = field(default_factory=dict)
    next_turn: int = 1
    computers: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[tuple[str, str, bytes]] = field(default_factory=list)
    created_checkpoints: list[dict[str, Any]] = field(default_factory=list)

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
            _out(audit_line(), f"nothing for {command}")
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
                    audit_line(started=True, turn=n, job=f"rj-{n}"),
                    json.dumps({"turn": n, "job": f"rj-{n}"}),
                )
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        try:
            body: dict[str, Any] = json.loads(request.content) if request.content else {}
        except json.JSONDecodeError:
            # an upload's body is the file itself, not JSON (spec §7.2's placement)
            body = {}
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
            if self.fail_checkpoints:
                return httpx.Response(500, json={"detail": "no"})
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
        if request.method == "POST" and path == "/computers":
            cid = f"comp-{len(self.computers) + 1}"
            self.computers.append({"id": cid, "recipe_id": body.get("recipe_id")})
            return httpx.Response(
                200,
                json={
                    "computer_id": cid,
                    "url": f"https://{cid}.test.dev",
                    "recipe_id": body.get("recipe_id"),
                },
            )
        if (
            request.method == "POST"
            and path.startswith("/checkpoints/")
            and path.endswith("/fork")
            and not body
        ):
            cid = f"comp-{len(self.computers) + 1}"
            self.computers.append({"id": cid, "from": path.split("/")[2]})
            return httpx.Response(200, json={"computer_id": cid, "url": f"https://{cid}.test.dev"})
        if request.method == "POST" and path.endswith("/upload"):
            self.uploads.append((path.split("/")[2], request.url.params["path"], request.content))
            return httpx.Response(
                200, json={"status": "uploaded", "path": request.url.params["path"]}
            )
        if request.method == "POST" and path.endswith("/checkpoint"):
            ck: dict[str, Any] = {
                "id": f"ck-{len(self.created_checkpoints) + 1}",
                "label": body["label"],
                "created_at": f"2026-09-13T00:00:{len(self.created_checkpoints):02d}",
                "recipe_id": None,
            }
            self.created_checkpoints.append(ck)
            self.checkpoints.append(ck)
            return httpx.Response(200, json={"checkpoint_id": ck["id"]})
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
    api = FakeApi(outputs={f"membrane root say {b64('hi')}": [_out(audit_line(), "hello")]})
    api.conflicts = 2
    doors = _doors(api, tmp_path)
    audit, reply = await doors.root_say("hi")
    assert audit["principal"] == "root" and reply == "hello\n"
    assert doors.slept == [3.0, 3.0]  # type: ignore[attr-defined]
    assert [s.name for s in doors.sent] == ["say", "list"] and doors.sent[0].detail == "hi"
    assert doors.sent[0].door == "api" and doors.sent[0].stdout.startswith("audit ")
    assert (tmp_path / "run" / "commands" / "001-api-say.json").exists()


async def test_a_doors_with_no_record_does_not_write_but_still_works() -> None:
    """`promote`'s `Doors` carries no `Record` (there is no run directory to write
    commands under); a turn still succeeds, and nothing is recorded."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"computer_id": "c", "exec_exit_code": 0, "exec_stdout": "ok\n"}
        )

    transport = httpx.MockTransport(handler)
    doors = Doors(
        httpx.AsyncClient(base_url="http://api", transport=transport),
        httpx.AsyncClient(base_url="http://api", transport=transport),
        "rule-1",
        None,
    )
    assert await doors.root("list") == "ok\n"
    assert doors.sent == []


async def test_root_gives_up_on_409_after_the_turn_timeout(tmp_path: Path) -> None:
    api = FakeApi()
    api.conflicts = 10**6
    doors = _doors(api, tmp_path)
    clock = iter(range(0, 10**6, 100))
    doors.now = lambda: float(next(clock))
    with pytest.raises(RuntimeError, match="409"):
        await doors.root("list")


async def test_a_transport_error_on_a_poll_is_retried_and_then_succeeds(tmp_path: Path) -> None:
    """A poll's connection can drop transiently (2026-09-13-run-1): the third
    `list` poll raised `httpx.ConnectError` with an empty message while the API
    was healthy before and after. A transport error never got a response, so it
    is retried, each failed attempt recorded with status 0."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(
            200, json={"computer_id": "c", "exec_exit_code": 0, "exec_stdout": "ok\n"}
        )

    doors = _bare_doors(tmp_path, handler)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    doors.sleep = sleep
    assert await doors.root("list") == "ok\n"
    assert calls["n"] == 3
    assert [s.status for s in doors.sent] == [0, 0, 200]
    assert all(s.stdout.startswith("ConnectError: boom") for s in doors.sent[:2])
    assert slept == [CONFLICT_INTERVAL, CONFLICT_INTERVAL]


async def test_a_transport_error_that_never_stops_propagates_after_the_retries(
    tmp_path: Path,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    doors = _bare_doors(tmp_path, handler)

    async def sleep(_seconds: float) -> None:
        return None

    doors.sleep = sleep
    with pytest.raises(httpx.ConnectError):
        await doors.root("list")
    assert calls["n"] == TRANSPORT_RETRIES + 1
    assert len(doors.sent) == TRANSPORT_RETRIES + 1
    assert all(s.status == 0 for s in doors.sent)


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
                _out(audit_line(door="ingress", principal="ssh:mike"), "You are ssh:mike.")
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
        outputs={f"membrane root say {b64('hi')}": [_out(audit_line(stopped="done"), "hello")]}
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


async def test_teardown_never_touches_a_promoted_checkpoint_or_the_recipe_it_names(
    tmp_path: Path,
) -> None:
    """The E2E run of 2026-09-13 lost the real `capability/hatch/brain`: the service
    dedupes recipes by content, so the scripted hatch was built from the very recipe
    the promoted brain was built from, `hatched.recipe_id` matched that checkpoint
    through the recipe clause, and the recipe went after it. A promotion is another
    run's evidence; the working head beside it still goes."""
    api = FakeApi(
        checkpoints=[
            {"id": "ck-promoted", "label": "capability/hatch/brain", "recipe_id": "rcp-brain"},
            {"id": "ck-work", "label": "brain", "recipe_id": "rcp-brain"},
        ]
    )
    doors = _doors(api, tmp_path)
    hatched = Hatched("http://api/ingress/rule-1", "rule-1", "key-1", "rcp-brain", "ck-work")
    await doors.teardown(hatched, {"proposals": []})
    deletes = [p for m, p, _ in api.requests if m == "DELETE"]
    assert "/checkpoints/ck-work" in deletes
    assert "/checkpoints/ck-promoted" not in deletes
    assert "/recipes/rcp-brain" not in deletes


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


async def test_teardown_deletes_no_recipe_when_the_checkpoints_cannot_be_listed(
    tmp_path: Path,
) -> None:
    """The recipe set's promoted-checkpoint subtraction reads the very listing
    that just failed to load, so falling through to the recipe loop below would
    delete every recipe named by `hatched` or a proposal, promoted or not -- the
    exact mechanism that destroyed `capability/hatch/brain` on 2026-09-13, since
    the service dedupes recipes by content and a scripted hatch builds the very
    recipe a promoted brain was built from. A failed listing must stop before
    that loop instead."""
    api = FakeApi(fail_checkpoints=True)
    doors = _doors(api, tmp_path)
    hatched = Hatched("u", "rule-1", "key-1", "rcp-brain", "ck-brain")
    listing = {"proposals": [{"id": "p-1", "recipe_id": "rcp-page"}]}
    log = io.StringIO()
    await doors.teardown(hatched, listing, log=log)
    assert [p for m, p, _ in api.requests if m == "DELETE"] == [
        "/ingress_rules/rule-1",
        "/keys/key-1",
    ]
    assert not any(p.startswith("/recipes/") for m, p, _ in api.requests if m == "DELETE")
    assert not any(p.startswith("/checkpoints/") for m, p, _ in api.requests if m == "DELETE")
    assert "could not list checkpoints" in log.getvalue()


# ---------------------------------------------------------------- the door helpers a promotion uses


def _bare_doors(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> Doors:
    transport = httpx.MockTransport(handler)
    api = httpx.AsyncClient(base_url="http://api", transport=transport)
    public = httpx.AsyncClient(base_url="http://api", transport=transport)
    return Doors(api, public, "rule", Record(tmp_path / "run"))


async def test_head_is_the_newest_checkpoint_on_the_label(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/checkpoints" and request.url.params["label"] == "brain"
        return httpx.Response(
            200,
            json=[
                {"id": "ck-old", "label": "brain", "created_at": "2026-09-12T10:00:00Z"},
                {"id": "ck-new", "label": "brain", "created_at": "2026-09-12T11:00:00Z"},
            ],
        )

    doors = _bare_doors(tmp_path, handler)
    head = await doors.head("brain")
    assert head is not None and head["id"] == "ck-new"


async def test_head_of_an_empty_label_is_none(tmp_path: Path) -> None:
    doors = _bare_doors(tmp_path, lambda _request: httpx.Response(200, json=[]))
    assert await doors.head("brain") is None


async def test_copy_label_forks_the_head_checkpoints_under_the_new_label_and_destroys(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path == "/checkpoints":
            return httpx.Response(200, json=[{"id": "ck-1", "label": "brain", "created_at": "t"}])
        if request.url.path == "/checkpoints/ck-1/fork":
            return httpx.Response(200, json={"computer_id": "comp-9", "checkpoint_id": "ck-1"})
        if request.url.path == "/computers/comp-9/checkpoint":
            return httpx.Response(200, json={"checkpoint_id": "ck-2"})
        if request.url.path == "/computers/comp-9":
            return httpx.Response(200, json={"status": "destroyed"})
        raise AssertionError(request.url.path)

    doors = _bare_doors(tmp_path, handler)
    assert await doors.copy_label("brain", "capability/hatch/brain") == "ck-2"
    assert seen[1:] == [
        ("POST", "/checkpoints/ck-1/fork", {}),
        ("POST", "/computers/comp-9/checkpoint", {"label": "capability/hatch/brain"}),
        ("DELETE", "/computers/comp-9", None),
    ]


async def test_copy_label_of_an_empty_label_is_an_error(tmp_path: Path) -> None:
    doors = _bare_doors(tmp_path, lambda _request: httpx.Response(200, json=[]))
    with pytest.raises(RuntimeError, match="no checkpoint on brain"):
        await doors.copy_label("brain", "x")


async def test_working_labels_are_brain_and_the_verb_chains_without_trials(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "a", "label": "brain", "created_at": "t"},
                {"id": "b", "label": "verb/counter", "created_at": "t"},
                {"id": "c", "label": "verb/counter", "created_at": "t"},
                {"id": "d", "label": "verb/trial/t-1", "created_at": "t"},
                {"id": "e", "label": "capability/hatch/brain", "created_at": "t"},
                {"id": "f", "label": None, "recipe_id": "rcp-x", "created_at": "t"},
            ],
        )

    doors = _bare_doors(tmp_path, handler)
    assert await doors.working_labels() == ["brain", "verb/counter"]


# ---------------------------------------------------------------- promote, start_from, lineage


async def test_promote_copies_the_heads_writes_the_record_and_drops_the_working_labels(
    tmp_path: Path,
) -> None:
    from membrane.capability import promote, promotion_path, read_promotion

    run_dir = tmp_path / "hatch" / "2026-09-12-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {"commit": "abc", "dirty": False},
                "hatched": {
                    "rule_id": "ir_1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-0",
                    "ingress_url": "u",
                    "server_id": None,
                },
                "key_dir": "/keys/hatch",
                "pubkey": "ssh-ed25519 AAAA mike",
                "model": "claude-opus-5",
                "default_effort": None,
                "reasks": ["4", "5"],
                "started_from": "hatch",
            }
        )
    )
    (run_dir / "final-list.json").write_text(
        json.dumps({"proposals": [{"recipe_id": "rcp-counter"}, {"recipe_id": None}]})
    )
    checkpoints = [
        {"id": "ck-b", "label": "brain", "created_at": "t", "recipe_id": "rcp-brain"},
        {"id": "ck-c", "label": "verb/counter", "created_at": "t"},
        # a checkpoint from an earlier promotion: not a working label, so the
        # cleanup after copying leaves it alone
        {"id": "ck-old", "label": "capability/other/brain", "created_at": "t"},
    ]
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/checkpoints":
            label = request.url.params.get("label")
            rows = [c for c in checkpoints if not label or c["label"] == label]
            return httpx.Response(200, json=rows)
        if request.url.path.endswith("/fork"):
            return httpx.Response(200, json={"computer_id": "comp-1", "checkpoint_id": "x"})
        if request.url.path == "/computers/comp-1/checkpoint":
            label = json.loads(request.content)["label"]
            new_id = f"promoted-{label.rsplit('/', 1)[1]}"
            return httpx.Response(200, json={"checkpoint_id": new_id})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "deleted"})
        raise AssertionError(request.url.path)

    doors = _bare_doors(tmp_path, handler)
    p = await promote(doors, tmp_path, "hatch", run_dir, log=io.StringIO())
    assert p.labels == {"brain": "promoted-brain", "verb/counter": "promoted-counter"}
    assert p.recipe_ids == ("rcp-brain", "rcp-counter")
    assert p.rule_id == "ir_1" and p.key_id == "key-1"
    # the lineage a dependent needs: the hatcher's key, and the model its brain runs
    assert p.key_dir == "/keys/hatch" and p.pubkey == "ssh-ed25519 AAAA mike"
    assert p.model == "claude-opus-5" and p.default_effort is None
    # the road, kept beside the score: how many rows the run had to be asked twice
    assert p.reasks == 2
    assert "promoted from a run with 2 re-asks" in promotion_path(tmp_path, "hatch").read_text()
    assert p.run == "hatch/2026-09-12-run-1" and p.started_from is None
    assert read_promotion(tmp_path, "hatch") == p
    # the working checkpoints went; the key, the rule and the recipes stayed
    assert ("DELETE", "/checkpoints/ck-b") in seen and ("DELETE", "/checkpoints/ck-c") in seen
    assert not any(path.startswith(("/keys", "/ingress_rules", "/recipes")) for _, path in seen)
    assert ("DELETE", "/checkpoints/ck-old") not in seen  # not a working label: left alone


async def test_promote_logs_a_delete_that_fails_but_still_returns_the_record(
    tmp_path: Path,
) -> None:
    """A working checkpoint that will not delete (a 500) does not stop the
    promotion or the other deletes; it is named in the log so the next run's
    'already has a brain' is not a mystery."""
    from membrane.capability import promote, read_promotion

    run_dir = tmp_path / "hatch" / "2026-09-12-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {"commit": "abc", "dirty": False},
                "hatched": {
                    "rule_id": "ir_1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-0",
                    "ingress_url": "u",
                    "server_id": None,
                },
                "key_dir": "/keys/hatch",
                "pubkey": "ssh-ed25519 AAAA mike",
                "model": "claude-opus-5",
                "default_effort": None,
                "reasks": [],
                "started_from": "hatch",
            }
        )
    )
    (run_dir / "final-list.json").write_text(json.dumps({"proposals": []}))
    checkpoints = [
        {"id": "ck-b", "label": "brain", "created_at": "t", "recipe_id": "rcp-brain"},
        {"id": "ck-c", "label": "verb/counter", "created_at": "t"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            label = request.url.params.get("label")
            rows = [c for c in checkpoints if not label or c["label"] == label]
            return httpx.Response(200, json=rows)
        if request.url.path.endswith("/fork"):
            return httpx.Response(200, json={"computer_id": "comp-1", "checkpoint_id": "x"})
        if request.url.path == "/computers/comp-1/checkpoint":
            label = json.loads(request.content)["label"]
            new_id = f"promoted-{label.rsplit('/', 1)[1]}"
            return httpx.Response(200, json={"checkpoint_id": new_id})
        if request.url.path == "/checkpoints/ck-c":
            return httpx.Response(500, json={"detail": "boom"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "deleted"})
        raise AssertionError(request.url.path)

    doors = _bare_doors(tmp_path, handler)
    log = io.StringIO()
    p = await promote(doors, tmp_path, "hatch", run_dir, log=log)
    assert read_promotion(tmp_path, "hatch") == p
    assert "could not drop verb/counter" in log.getvalue()
    assert "dropped brain" in log.getvalue()


async def test_promote_reports_which_labels_were_already_copied_when_a_later_one_fails(
    tmp_path: Path,
) -> None:
    """`copy_label` for `verb/counter` (the second working label, alphabetically
    after `brain`) fails: the labels already copied under `capability/<name>/`
    are named in the log, the error propagates, and no record is written."""
    from membrane.capability import promote, promotion_path

    run_dir = tmp_path / "hatch" / "2026-09-12-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {"commit": "abc", "dirty": False},
                "hatched": {
                    "rule_id": "ir_1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-0",
                    "ingress_url": "u",
                    "server_id": None,
                },
                "key_dir": "/keys/hatch",
                "pubkey": "ssh-ed25519 AAAA mike",
                "model": "claude-opus-5",
                "default_effort": None,
                "reasks": [],
                "started_from": "hatch",
            }
        )
    )
    (run_dir / "final-list.json").write_text(json.dumps({"proposals": []}))
    checkpoints = [
        {"id": "ck-b", "label": "brain", "created_at": "t", "recipe_id": "rcp-brain"},
        {"id": "ck-c", "label": "verb/counter", "created_at": "t"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            label = request.url.params.get("label")
            rows = [c for c in checkpoints if not label or c["label"] == label]
            return httpx.Response(200, json=rows)
        if request.url.path == "/checkpoints/ck-c/fork":
            return httpx.Response(500, json={"detail": "boom"})
        if request.url.path.endswith("/fork"):
            return httpx.Response(200, json={"computer_id": "comp-1", "checkpoint_id": "x"})
        if request.url.path == "/computers/comp-1/checkpoint":
            label = json.loads(request.content)["label"]
            new_id = f"promoted-{label.rsplit('/', 1)[1]}"
            return httpx.Response(200, json={"checkpoint_id": new_id})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "deleted"})
        raise AssertionError(request.url.path)

    doors = _bare_doors(tmp_path, handler)
    log = io.StringIO()
    with pytest.raises(httpx.HTTPStatusError):
        await promote(doors, tmp_path, "hatch", run_dir, log=log)
    assert "promotion of hatch failed after copying brain" in log.getvalue()
    assert not promotion_path(tmp_path, "hatch").exists()


async def test_promote_refuses_a_run_that_is_not_ok(tmp_path: Path) -> None:
    from membrane.capability import promote

    run_dir = tmp_path / "hatch" / "2026-09-12-run-2"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"ok": False, "passed": 4}))
    doors = _bare_doors(tmp_path, lambda _request: httpx.Response(200, json=[]))
    with pytest.raises(
        RuntimeError, match="2026-09-12-run-2 is not ok; only a passing run is promoted"
    ):
        await promote(doors, tmp_path, "hatch", run_dir, log=io.StringIO())


async def test_promote_refuses_when_the_working_brain_is_gone(tmp_path: Path) -> None:
    from membrane.capability import promote

    run_dir = tmp_path / "hatch" / "2026-09-12-run-3"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {},
                "hatched": {
                    "rule_id": "r",
                    "key_id": "k",
                    "recipe_id": "rcp",
                    "checkpoint_id": "c",
                    "ingress_url": "u",
                },
                "key_dir": "/keys/hatch",
                "pubkey": "ssh-ed25519 AAAA mike",
                "model": "claude-opus-5",
                "default_effort": None,
                "reasks": [],
            }
        )
    )
    (run_dir / "final-list.json").write_text(json.dumps({"proposals": []}))
    doors = _bare_doors(tmp_path, lambda _request: httpx.Response(200, json=[]))
    with pytest.raises(RuntimeError, match="no working brain on the account; was the run kept"):
        await promote(doors, tmp_path, "hatch", run_dir, log=io.StringIO())


async def test_promote_refuses_a_run_that_does_not_name_its_key_and_model(tmp_path: Path) -> None:
    """A run recorded before the lineage carried the hatcher's key cannot be
    promoted: a dependent started from it would sign with a key the promoted
    identity hook has never seen."""
    from membrane.capability import promote

    run_dir = tmp_path / "hatch" / "2026-09-12-run-4"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {"commit": "abc", "dirty": False},
                "hatched": {
                    "rule_id": "ir_1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-0",
                    "ingress_url": "u",
                    "server_id": None,
                },
                "started_from": "hatch",
            }
        )
    )
    (run_dir / "final-list.json").write_text(json.dumps({"proposals": []}))
    checkpoints = [{"id": "ck-b", "label": "brain", "created_at": "t", "recipe_id": "rcp-brain"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            label = request.url.params.get("label")
            return httpx.Response(
                200, json=[c for c in checkpoints if not label or c["label"] == label]
            )
        raise AssertionError(f"promote copied before it checked: {request.url.path}")

    doors = _bare_doors(tmp_path, handler)
    with pytest.raises(
        RuntimeError, match="does not name key_dir, pubkey, model, default_effort, reasks"
    ):
        await promote(doors, tmp_path, "hatch", run_dir, log=io.StringIO())


async def test_promote_refuses_a_run_that_does_not_count_its_re_asks(tmp_path: Path) -> None:
    """A run recorded before #170 does not say how many rows it was asked twice,
    and the count is kept beside the score forever: it cannot be inferred later."""
    from membrane.capability import promote

    run_dir = tmp_path / "hatch" / "2026-09-12-run-5"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {"commit": "abc", "dirty": False},
                "hatched": {
                    "rule_id": "ir_1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-0",
                    "ingress_url": "u",
                    "server_id": None,
                },
                "key_dir": "/keys/hatch",
                "pubkey": "ssh-ed25519 AAAA mike",
                "model": "claude-opus-5",
                "default_effort": None,
                "started_from": "hatch",
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"promote asked before it checked: {request.url.path}")

    doors = _bare_doors(tmp_path, handler)
    with pytest.raises(RuntimeError, match="does not name reasks"):
        await promote(doors, tmp_path, "hatch", run_dir, log=io.StringIO())


async def test_promote_refuses_a_run_directory_outside_out(tmp_path: Path) -> None:
    """`--out docs/embryo` and a run directory somewhere else: the record's `run`
    is the path a dependent resolves against `--out`, so a run from elsewhere is
    refused before anything is copied."""
    from membrane.capability import promote

    run_dir = tmp_path / "elsewhere" / "2026-09-12-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"ok": True}))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"promote reached the API: {request.url.path}")

    doors = _bare_doors(tmp_path, handler)
    out = tmp_path / "docs"
    with pytest.raises(RuntimeError, match=f"{run_dir} is not under {out}; promote takes"):
        await promote(doors, out, "hatch", run_dir, log=io.StringIO())


async def test_promote_refuses_a_brain_that_is_not_this_runs(tmp_path: Path) -> None:
    """The working `brain` on the account is from another hatch's recipe: promoting
    it would name this run's evidence over another run's state."""
    from membrane.capability import promote

    run_dir = tmp_path / "hatch" / "2026-09-12-run-5"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {},
                "hatched": {
                    "rule_id": "ir_1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-0",
                    "ingress_url": "u",
                    "server_id": None,
                },
                "key_dir": "/keys/hatch",
                "pubkey": "ssh-ed25519 AAAA mike",
                "model": "claude-opus-5",
                "default_effort": None,
                "reasks": [],
                "started_from": "hatch",
            }
        )
    )
    (run_dir / "final-list.json").write_text(json.dumps({"proposals": []}))
    checkpoints = [{"id": "ck-b", "label": "brain", "created_at": "t", "recipe_id": "rcp-other"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            label = request.url.params.get("label")
            return httpx.Response(
                200, json=[c for c in checkpoints if not label or c["label"] == label]
            )
        raise AssertionError(f"promote copied before it checked: {request.url.path}")

    doors = _bare_doors(tmp_path, handler)
    with pytest.raises(
        RuntimeError, match="the brain on the account is from recipe rcp-other, not rcp-brain"
    ):
        await promote(doors, tmp_path, "hatch", run_dir, log=io.StringIO())


def test_read_promotion_of_a_file_that_is_not_a_promotion_record(tmp_path: Path) -> None:
    from membrane.capability import promotion_path, read_promotion

    path = promotion_path(tmp_path, "hatch")
    path.parent.mkdir(parents=True)
    path.write_text("# Promoted: hatch\n\nSomeone rewrote this by hand.\n")
    with pytest.raises(RuntimeError, match=f"{path} is not a promotion record"):
        read_promotion(tmp_path, "hatch")


async def test_start_from_forks_the_promoted_labels_into_the_working_ones(tmp_path: Path) -> None:
    from membrane.capability import Promotion, start_from

    p = Promotion(
        capability="hatch",
        run="hatch/r1",
        membrane={},
        promoted_at="t",
        labels={"brain": "pb", "verb/counter": "pc"},
        rule_id="ir_1",
        key_id="key-1",
        brain_recipe="rcp-brain",
        recipe_ids=("rcp-brain",),
        key_dir="/keys",
        pubkey="ssh-ed25519 AAAA mike",
        model="claude-opus-5",
        default_effort=None,
        reasks=0,
        started_from=None,
    )
    checkpoints = [
        {"id": "pb", "label": "capability/hatch/brain", "created_at": "t"},
        {"id": "pc", "label": "capability/hatch/verb/counter", "created_at": "t"},
    ]
    copies: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            label = request.url.params.get("label")
            return httpx.Response(200, json=[c for c in checkpoints if c["label"] == label])
        if request.url.path.endswith("/fork"):
            return httpx.Response(200, json={"computer_id": "comp-1", "checkpoint_id": "x"})
        if request.url.path == "/computers/comp-1/checkpoint":
            label = json.loads(request.content)["label"]
            copies.append((request.url.path, label))
            return httpx.Response(200, json={"checkpoint_id": f"new-{label}"})
        return httpx.Response(200, json={"status": "deleted"})

    doors = _bare_doors(tmp_path, handler)
    hatched = await start_from(doors, p, log=io.StringIO())
    assert [label for _, label in copies] == ["brain", "verb/counter"]
    assert hatched == Hatched(
        ingress_url="",
        rule_id="ir_1",
        key_id="key-1",
        recipe_id="rcp-brain",
        checkpoint_id="new-brain",
        server_id=None,
    )


async def test_teardown_with_a_lineage_keeps_its_key_rule_and_recipes(tmp_path: Path) -> None:
    from membrane.capability import Promotion

    p = Promotion(
        capability="hatch",
        run="hatch/r1",
        membrane={},
        promoted_at="t",
        labels={"brain": "pb"},
        rule_id="ir_1",
        key_id="key-1",
        brain_recipe="rcp-brain",
        recipe_ids=("rcp-brain", "rcp-counter"),
        key_dir="/keys",
        pubkey="ssh-ed25519 AAAA mike",
        model="claude-opus-5",
        default_effort=None,
        reasks=0,
        started_from=None,
    )
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/checkpoints":
            return httpx.Response(
                200,
                json=[
                    {"id": "w", "label": "brain", "recipe_id": None},
                    {"id": "v", "label": "verb/counter", "recipe_id": None},
                    {"id": "p", "label": "capability/hatch/brain", "recipe_id": None},
                    {"id": "n", "label": None, "recipe_id": "rcp-new"},
                ],
            )
        return httpx.Response(200, json={"status": "deleted"})

    doors = _bare_doors(tmp_path, handler)
    hatched = Hatched(
        ingress_url="", rule_id="ir_1", key_id="key-1", recipe_id="rcp-brain", checkpoint_id="x"
    )
    listing = {"proposals": [{"recipe_id": "rcp-counter"}, {"recipe_id": "rcp-new"}]}
    await doors.teardown(hatched, listing, lineage=p)
    deleted = [path for method, path in seen if method == "DELETE"]
    assert deleted == ["/checkpoints/w", "/checkpoints/v", "/checkpoints/n", "/recipes/rcp-new"]


# ---------------------------------------------------------------- keys and signatures


def test_load_key_reads_the_line_already_there_and_names_a_directory_without_one(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError, match=f"no key in {empty}"):
        load_key(empty)
    assert new_key(tmp_path) == load_key(tmp_path)


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


# ---------------------------------------------------------------- hatch over a fake door


GROW = "Grow yourself another verb, and let me use that one."
GRANT = "Let me use everything you have."
USE = "Use what I may ask of you."
GHOST = "Let me use a verb you do not have."
SELF = "Say in your own words what you have become."
SECURITY = load(CAPABILITIES / "security.md")  # rows 11-14, and the provide phrase


REPAIR_PHRASES = frozenset(
    phrase
    for repair in (HATCH.repair, SECURITY.repair)
    for phrase in (repair.build, repair.refused, repair.provide, repair.stalled, repair.silent)
    if phrase is not None
)


class FakeDoors:
    """A door whose membrane is a small state machine: proposals are made on the
    turns hatch expects them, approvals move them to building, and each
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
        grant_late: bool = False,
        growing: bool = False,
        secret: bool = False,
        path_in_reply: str | None = "```\n/verb/token\n```",
        stall_on_repair: bool = False,
    ) -> None:
        # A membrane that grows a verb with a `requires` at row 11 (spec §7.2), and
        # what the replies of rows 11 and 13 say about where root should put the
        # token; `None` is a reply that names no path at all.
        self.secret = secret
        self.path_in_reply = path_in_reply
        self.provided_at: list[tuple[str, str, str]] = []
        self.policy_first = policy_first
        self.deadline_first = deadline_first
        # The policy, not the catalog, is what stops a verb from being invoked
        # (2026-09-13-run-3): the grant is by name, and a turn that may not invoke
        # says so and calls nothing.
        self.grant_late = grant_late
        # A membrane that grows a verb and widens the grant to it on every `GROW`.
        self.growing = growing
        self.grown = 0
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
        self.count_calls = 0  # invocations of the counter verb, not `count` messages
        # The agent that answers a repair phrase with prose and no call at all
        # (2026-09-16-run-2 `3-repair-3`, run-3 `3-repair-1`).
        self.stall_on_repair = stall_on_repair

    def _may_invoke(self, name: str) -> bool:
        """Whether the policy in force lets `ssh:mike` invoke the verb."""
        if not self.grant_late:
            return True
        invoke = self.policy["principals"].get("ssh:mike", {}).get("invoke")
        return invoke == "*" or (isinstance(invoke, list) and name in invoke)

    def _count(self, n: int) -> str:
        return self.counts[n - 1] if n <= len(self.counts) else f"{n}\n"

    def _grant(self, invoke: list[str]) -> dict[str, Any]:
        """The policy in force with `ssh:mike`'s invocation grant replaced."""
        return {
            "principals": {
                "ssh:mike": {"invoke": invoke, "propose": True},
                "anonymous": {"invoke": [], "propose": False},
            },
            "hooks": ["verify_ssh"],
            "door": "open",
        }

    def _widened(self) -> dict[str, Any]:
        """The policy in force, with `ssh:mike` allowed to invoke everything."""
        policy = json.loads(json.dumps(self.policy))
        policy["principals"]["ssh:mike"]["invoke"] = "*"
        return dict(policy)

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
            if "requires" in extra:
                doc["verb"]["requires"] = extra["requires"]
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
        if text == WORDS["1"]:
            return self._reply(audit_line(), "I have remember, try and propose. My door is closed.")
        if text.startswith(WORDS["2"][:30]) and self.deadline_first:
            # the trial's build outlived the turn (live run 2026-09-09-run-5)
            self.deadline_first = False
            self.pending_door = True
            audit = audit_line(tools=[{"name": "try", "status": "building", "trial": "t-1"}])
            audit["stopped"] = "deadline"
            return self._reply(audit, "I ran out of time before finishing this turn.")
        if text == HATCH.repair.build and getattr(self, "pending_door", False):
            self.pending_door = False
            text = WORDS["2"]  # the check of the trial ends in the two proposals
        said = "Proposed."
        if text.startswith(WORDS["2"][:30]):
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
        elif text == HATCH.repair.build:
            self.repairs += 1
            failed = [n for n, e in self.catalog.items() if e["status"] == "failed"]
            for name in failed:
                self.fail_first.discard(name)
                made.append(
                    self._propose("verb", name, supersedes=self.catalog[name]["proposal_id"])
                )
        elif text == "where should I put it?":
            # security's provide phrase: the agent answers with the path alone
            said = "Put it at\n```\n/verb/token\n```"
        tools = [{"name": "propose", "status": "ok", "id": p["id"]} for p in made]
        if not tools and text in REPAIR_PHRASES and not self.stall_on_repair:
            # A repair turn that looked and fixed nothing is not a repair turn that
            # froze: the driver tells them apart by the tool list (`stalled`), and
            # before it did, this fake collapsed both into an empty one. The default
            # is the agent that acted and still did not fix it, which is what every
            # test written before the distinction existed meant.
            tools = [{"name": "remember", "status": "ok"}]
        audit = audit_line(
            tools=tools,
            proposals=[{"id": p["id"], "sha256": "x"} for p in made],
        )
        return self._reply(audit, said)

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
                audit_line(door="ingress", principal="anonymous", offered=[], memory_written=False),
                "I do not know you; I will not act or remember.",
            )
        offered = ["propose", "remember", "try", *sorted(self.catalog)]
        made: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        reply = "You are ssh:mike."
        if msg == WORDS["6"]:
            made.append(
                self._propose(
                    "policy",
                    "authz",
                    policy={
                        "principals": {
                            # the gated modes widen by name, as run 3 did, so the
                            # verbs proposed later are not yet invocable
                            "ssh:mike": {
                                "invoke": ["verify_ssh"] if self.grant_late else "*",
                                "propose": True,
                            },
                            "anonymous": {"invoke": [], "propose": False},
                        },
                        "hooks": ["verify_ssh"],
                        "door": "open",
                    },
                )
            )
            reply = "Recorded."
        elif msg == WORDS["7"]:
            tools.append({"name": "try", "status": "done", "trial": "t-1"})
            made.append(self._propose("verb", "page_title"))
            reply = "Proposed page_title."
        elif msg == WORDS["8"]:
            if "page_title" in self.catalog and self._may_invoke("page_title"):
                tools.append(
                    {
                        "name": "page_title",
                        "status": "ok",
                        "exit_code": 0,
                        "computer_id": "comp-title",
                    }
                )
                reply = self.title_reply
            elif "page_title" in self.catalog:
                reply = "I have no such tool in my hands."
            else:
                reply = "I have no such verb."
        elif msg == WORDS["9"]:
            made.append(self._propose("verb", "counter", state="chain"))
            reply = "Proposed counter."
        elif msg == WORDS["9-count-1"]:
            said = len(
                [
                    s
                    for s in self.sent
                    if s[2] and isinstance(s[2], dict) and s[2].get("msg") == WORDS["9-count-1"]
                ]
            )
            if self.grant_late and said == 2:
                # asked to count a second time and still unable to, the agent sees
                # that its own grant is what stops it and widens it
                made.append(self._propose("policy", "widen", policy=self._widened()))
            if "counter" in self.catalog and self._may_invoke("counter"):
                self.count_calls += 1
                n = self.count_calls
                tools.append(
                    {
                        "name": "counter",
                        "status": "ok",
                        "exit_code": 0,
                        "computer_id": f"comp-count-{n}",
                        "chain_head": f"ck-{n}",
                    }
                )
                reply = f"The count is {self._count(n).strip()}."
            else:
                reply = "I have no such tool in my hands."
        elif self.secret and msg.startswith("Give yourself a verb that reads the page at "):
            tools.append({"name": "try", "status": "done", "trial": "t-9"})
            made.append(
                self._propose(
                    "verb",
                    "secret_page",
                    state="chain",
                    requires=[{"kind": "secret", "name": "page_token"}],
                )
            )
            reply = "Proposed secret_page. The trial saw the 401." + (
                f"\nPut it at\n{self.path_in_reply}"
                if self.path_in_reply
                else "\nSomewhere on its disk."
            )
        elif self.secret and msg == "read the page":
            entry = self.catalog.get("secret_page")
            if entry and entry["status"] == "ready" and entry["provided"] == ["page_token"]:
                tools.append(
                    {
                        "name": "secret_page",
                        "status": "ok",
                        "exit_code": 0,
                        "computer_id": "comp-secret",
                        "chain_head": "ck-secret-1",
                    }
                )
                reply = "The page behind the token says: perfect number 8128."
            elif entry:
                tools.append(
                    {
                        "name": "secret_page",
                        "status": "error",
                        "error": (
                            "blocked: secret_page requires page_token; "
                            "root places it and says provide"
                        ),
                    }
                )
                reply = "Blocked: root has not provided page_token."
            else:
                reply = "I have no such verb."
        elif self.secret and msg == "Give yourself a second verb that needs the same token.":
            made.append(
                self._propose(
                    "verb",
                    "secret_length",
                    state="chain",
                    requires=[{"kind": "secret", "name": "page_token"}],
                )
            )
            reply = "Proposed secret_length." + (
                f" Same place:\n{self.path_in_reply}"
                if self.path_in_reply
                else " Wherever the first one went."
            )
        elif self.growing and msg == GROW:
            self.grown += 1
            name = f"extra{self.grown}"
            made.append(self._propose("verb", name))
            made.append(self._propose("policy", "grow", policy=self._grant([name])))
            reply = f"Proposed {name}, and a grant of it alone."
        elif self.growing and msg == GRANT:
            ready = sorted(
                n
                for n, e in self.catalog.items()
                if e["status"] == "ready" and n not in self.policy["hooks"]
            )
            made.append(self._propose("policy", "grant", policy=self._grant(ready)))
            reply = "Proposed a grant of every verb I have."
        elif self.growing and msg == SELF:
            made.append(self._propose("prompt", "self"))
            reply = "Proposed a new self-description."
        elif self.growing and msg == GHOST:
            made.append(self._propose("policy", "ghost", policy=self._grant(["nope"])))
            reply = "Proposed a grant of a verb I never grew."
        elif self.growing and msg == USE:
            granted = self.policy["principals"]["ssh:mike"]["invoke"]
            called: str | None = next(
                (n for n in granted if self.catalog.get(n, {}).get("status") == "ready"), None
            )
            if called is None:
                reply = "I have no such tool in my hands."
            else:
                tools.append(
                    {
                        "name": called,
                        "status": "ok",
                        "exit_code": 0,
                        "computer_id": f"comp-{called}",
                    }
                )
                reply = f"I called {called}."
        audit = audit_line(
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
        if argv[0] == "provide":
            verb, name = argv[1], argv[2]
            self.catalog[verb]["provided"].append(name)
            return f"{verb}: {name} provided (1/1)\n"
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
        if proposal["kind"] == "prompt":
            # the self-description: "applied", and nothing about who may invoke what
            proposal["status"] = "applied"
            return f"{pid} applied: prompt replaced; effective from the next turn\n"
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
            "chain": f"verb/{name}",
            "requires": proposal["verb"].get("requires", []),
            "provided": [],
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
        counts = self.count_calls
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

    async def provision(self, verb: str, chain: str, recipe_id: str, path: str, secret: str) -> str:
        for name in ("create", "upload", "checkpoint", "destroy"):
            self.sent.append(("api", name, {"verb": verb}))
            self.commands += 1
        self.provided_at.append((verb, path, secret))
        return f"ck-{verb}-provisioned"

    async def check_computer(self, computer_id: str) -> dict[str, Any]:
        if computer_id == "comp-secret":
            return {
                "computer_id": computer_id,
                "gone": True,
                "stdout": "The page behind the token says: perfect number 8128.\n",
                "exit_code": 0,
            }
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
            "stdout": self._count(n),
            "exit_code": 0,
        }

    async def recipes(self) -> set[str]:
        return {"rcp-pre", "rcp-brain", *(p["recipe_id"] for p in self.proposals if p["recipe_id"])}


def _keys(tmp_path: Path) -> tuple[Path, str]:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    return key_dir, new_key(key_dir)


async def test_speak_refuses_a_row_whose_template_the_context_cannot_fill(tmp_path: Path) -> None:
    """`{url}` is a legal template (a capability that serves something to the agent
    fills it), but this run's context has only `key`: the words would be spoken
    with a `KeyError` halfway through, so no turn is spoken at all."""
    from membrane.capabilities import Capability, Row

    doors = FakeDoors()
    key_dir, pubkey = _keys(tmp_path)
    cap = Capability(
        name="serve",
        depends=(),
        postconditions=(),
        rows=(
            Row("1", "root say", "Hello.", "A reply."),
            Row("2", "signed", "Read {url}", "The page."),
        ),
        repair=HATCH.repair,
        path=tmp_path / "serve.md",
        module=None,
    )
    with pytest.raises(
        RuntimeError, match=r"row 2 needs \{url\} and the run's context has \['key'\]"
    ):
        await speak(cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO())
    assert doors.sent == []


async def test_the_happy_path_reaches_every_postcondition(tmp_path: Path) -> None:
    doors = FakeDoors()
    key_dir, pubkey = _keys(tmp_path)
    log = io.StringIO()
    turns, final, reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=log
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    assert labels == ["1", "2", "4", "5", "6", "7", "8", "9", "9-count-1", "9-count-2"]
    # Turn 6's policy widens `ssh:mike` to "*", but the only ready verb is the door
    # hook, so it gained nobody anything and no row is asked again.
    assert reasks == []
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
    assert set(public[0][2]) == {"msg", "sig"} and public[1][2] == {"msg": WORDS["4"]}
    final = await doors.listing()
    checks = {
        c["computer_id"]: c
        for c in [
            await doors.check_computer(cid)
            for cid in ("comp-title", "comp-count-1", "comp-count-2")
        ]
    }
    result = judge(
        list(CHECKS),
        Judged(
            turns=turns,
            final=final,
            recipes_after=await doors.recipes(),
            preexisting={"rcp-pre"},
            brain_recipe="rcp-brain",
            checks=checks,
            sent=[(s[0], s[1]) for s in doors.sent],
            context={},
        ),
    )
    assert list(result) == list(CHECKS)
    assert all(v["ok"] for v in result.values()), {k: v for k, v in result.items() if not v["ok"]}
    assert result["page_title"]["evidence"]["computer_id"] == "comp-title"
    assert result["counter"]["evidence"]["counts"] == [1, 2]
    assert "Turn 8" in log.getvalue()
    assert "-again-" not in log.getvalue()


async def test_a_policy_change_that_gains_only_a_hook_re_asks_nothing(tmp_path: Path) -> None:
    """The happy path's turn 6 widens `ssh:mike` to `"*"`, but the catalog then holds
    the door hook and nothing else, and a hook is not a verb the rows came for
    (#117). Nobody gained an invocation, so no row is asked again: the trigger is
    what the change made possible, not that a change happened."""
    doors = FakeDoors()
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert doors.policy["principals"]["ssh:mike"]["invoke"] == "*"
    assert doors.policy["hooks"] == ["verify_ssh"]
    assert reasks == []
    assert not [t.label for t in turns if "-again-" in t.label]


async def test_a_policy_change_makes_the_driver_re_ask_the_rows_that_called_no_verb(
    tmp_path: Path,
) -> None:
    """2026-09-13-run-3, which the driver can now recover from: the agent widens
    its `invoke` grant verb by verb, so row 8 and the two counts find no tool in
    their hands, and the widening comes only at the last row. The re-ask asks those
    three once more, now that the answer is possible, and all seven pass."""
    doors = FakeDoors(grant_late=True)
    key_dir, pubkey = _keys(tmp_path)
    log = io.StringIO()
    turns, _final, reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=log
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    assert reasks == ["8", "9-count-1", "9-count-2"]
    assert labels[labels.index("9-count-2") :] == [
        "9-count-2",
        "8-again-1",
        "9-count-1-again-1",
        "9-count-2-again-1",
    ]
    eight = by_label(turns, "8")
    assert eight is not None and eight.label == "8-again-1"
    assert [c["name"] for c in eight.audit["tools"]] == ["page_title"]
    assert "Turn 8-again-1 (re-ask after policy change)" in log.getvalue()
    final = await doors.listing()
    checks = {
        cid: await doors.check_computer(cid)
        for cid in [c["computer_id"] for t in turns for c in tool_computers(t)]
    }
    result = judge(
        list(CHECKS),
        Judged(
            turns=turns,
            final=final,
            recipes_after=await doors.recipes(),
            preexisting={"rcp-pre"},
            brain_recipe="rcp-brain",
            checks=checks,
            sent=[(s[0], s[1]) for s in doors.sent],
            context={},
        ),
    )
    assert all(v["ok"] for v in result.values()), {k: v for k, v in result.items() if not v["ok"]}
    assert result["authorization"]["evidence"]["exercised"] == ["counter", "page_title"]
    assert result["counter"]["evidence"]["counts"] == [1, 2]


async def test_a_row_that_proposed_a_verb_is_not_re_asked(tmp_path: Path) -> None:
    """Rows 7 and 9 ask for a verb and get one; the grant that follows does not
    make them answerable, it answers the row after them. A row that proposed a verb
    was never waiting on the policy, so the widening at the last row re-asks the
    rows that tried to invoke and leaves the two proposing rows alone."""
    doors = FakeDoors(grant_late=True)
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    seven, nine = by_label(turns, "7"), by_label(turns, "9")
    assert seven is not None and nine is not None
    # both proposed a verb, and neither called one: only the second fact excuses them
    assert seven.audit["proposals"] and nine.audit["proposals"]
    assert "7" not in reasks and "9" not in reasks
    assert "7-again-1" not in labels and "9-again-1" not in labels
    assert "8-again-1" in labels and "9-count-1-again-1" in labels


def _growth(tmp_path: Path) -> Any:
    """A capability against a membrane that grows a verb and moves the grant to it
    alone on every `GROW`: each change gains its principal exactly one verb, and
    the rows between them show what the trigger does and does not fire on."""
    from membrane.capabilities import Capability, Row

    return Capability(
        name="growth",
        depends=(),
        postconditions=(),
        rows=(
            Row("1", "root say", WORDS["1"], ""),
            Row("early", "signed", "Anyone there?", ""),  # spoken at a closed door
            Row("2", "root say", WORDS["2"], ""),
            Row("ask", "signed", "What can I ask of you?", ""),
            Row("grow-1", "signed", GROW, ""),
            Row("use", "signed", USE, ""),
            Row("grow-2", "signed", GROW, ""),
            Row("grow-3", "signed", GROW, ""),
            Row("grant", "signed", GRANT, ""),
        ),
        repair=HATCH.repair,
        path=tmp_path / "growth.md",
        module=None,
    )


async def test_a_row_is_re_asked_at_most_twice(tmp_path: Path) -> None:
    """The bound, which is what makes the goto backwards finite: three widenings
    each gain `ssh:mike` a verb, and the row that asked what it may ask is spoken
    again after the first two and not after the third.

    The same run shows what the other two conditions exclude: `early` spoke at a
    closed door and has no principal for a grant to move for, `use` called the verb
    it was granted, and every `grow` row proposed the verb it was waiting on."""
    doors = FakeDoors(growing=True)
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, reasks = await speak(
        _growth(tmp_path), doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    assert sorted(doors.catalog) == ["extra1", "extra2", "extra3", "verify_ssh"]
    assert reasks.count("ask") == MAX_REASKS
    assert not [label for label in labels if "-again-3" in label]
    assert labels[: labels.index("grant")] == [
        "1",
        "early",
        "2",
        "ask",
        "grow-1",
        "ask-again-1",
        "use",
        "grow-2",
        "ask-again-2",
        "grow-3",
    ]
    assert "early" not in reasks and "use" not in reasks
    assert not [label for label in reasks if label.startswith("grow")]
    # the verb the `use` row called is why it was left alone
    used = by_label(turns, "use")
    assert used is not None and [c["name"] for c in used.audit["tools"]] == ["extra1"]
    # every re-ask is the row's own words through the row's own door
    again = [t for t in turns if t.label.startswith("ask-again-")]
    assert {t.words for t in again} == {"What can I ask of you?"}
    assert [t.door for t in again] == ["ingress"] * MAX_REASKS


async def test_a_grant_of_a_verb_that_is_not_in_the_catalog_re_asks_nothing(
    tmp_path: Path,
) -> None:
    """A gain is a verb the principal can actually invoke: a grant naming `nope`,
    which was never grown, moves the policy and hands nobody a tool, so the row
    that came up empty is not asked again on the strength of it."""
    from membrane.capabilities import Capability, Row

    cap = Capability(
        name="ghost",
        depends=(),
        postconditions=(),
        rows=(
            Row("1", "root say", WORDS["1"], ""),
            Row("2", "root say", WORDS["2"], ""),
            Row("ask", "signed", "What can I ask of you?", ""),
            Row("ghost", "signed", GHOST, ""),
        ),
        repair=HATCH.repair,
        path=tmp_path / "ghost.md",
        module=None,
    )
    doors = FakeDoors(growing=True)
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, reasks = await speak(
        cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert doors.policy["principals"]["ssh:mike"]["invoke"] == ["nope"]
    assert "nope" not in doors.catalog
    assert reasks == []
    assert [t.label for t in turns] == ["1", "2", "ask", "ghost"]


async def test_an_applied_prompt_is_not_a_policy_change(tmp_path: Path) -> None:
    """The membrane answers an approved self-description `p-N applied: prompt
    replaced…`, the same word it uses for a policy, and a self-description changes
    nothing about what anyone may invoke. Reading the word rather than the
    proposal's kind cleared the window at the prompt, and the widening that came
    after it then had no rows left to ask again."""
    from membrane.capabilities import Capability, Row

    cap = Capability(
        name="selfsaid",
        depends=(),
        postconditions=(),
        rows=(
            Row("1", "root say", WORDS["1"], ""),
            Row("2", "root say", WORDS["2"], ""),
            Row("ask", "signed", "What can I ask of you?", ""),
            Row("self", "signed", SELF, ""),
            Row("grow-1", "signed", GROW, ""),
        ),
        repair=HATCH.repair,
        path=tmp_path / "selfsaid.md",
        module=None,
    )
    doors = FakeDoors(growing=True)
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, reasks = await speak(
        cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    applied = [p for p in doors.proposals if p["kind"] == "prompt"]
    assert applied and all(p["status"] == "applied" for p in applied)
    # the prompt re-asked nobody and cost nobody their place in the window: the
    # widening that follows still reaches the row spoken before the prompt
    assert reasks == ["ask", "self"]
    assert [t.label for t in turns] == [
        "1",
        "2",
        "ask",
        "self",
        "grow-1",
        "ask-again-1",
        "self-again-1",
    ]


async def test_a_re_ask_that_changes_the_policy_again_starts_another_round(
    tmp_path: Path,
) -> None:
    """The last row of the growth capability proposes a grant of everything, which
    gains back the two verbs the widenings revoked, so it is re-asked — a row that
    proposed only a policy still qualifies. Its re-ask proposes the same grant
    again, which settles as another policy change and starts a second round; that
    round gains nobody anything and re-asks no one, which is where it stops."""
    doors = FakeDoors(growing=True)
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, reasks = await speak(
        _growth(tmp_path), doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    assert reasks == ["ask", "ask", "grant"]
    assert labels[labels.index("grant") :] == ["grant", "grant-again-1"]
    grant, again = by_label(turns, "grant"), turns[-1]
    assert grant is again and again.label == "grant-again-1"
    assert again.approvals and any(" applied" in a["result"] for a in again.approvals)
    assert doors.policy["principals"]["ssh:mike"]["invoke"] == ["extra1", "extra2", "extra3"]


async def test_speak_walks_the_rows_in_order_and_takes_the_final_list(tmp_path: Path) -> None:
    from membrane.capabilities import Capability, Row

    cap = Capability(
        name="tiny",
        depends=(),
        postconditions=("root_unforgeable",),
        rows=(
            Row("1", "root say", WORDS["1"], ""),
            Row("2", "root say", WORDS["2"], ""),
            Row("4", "signed", "Who am I?", ""),
            Row("5", "unsigned", "Who am I?", ""),
            Row("10", "root list", "", ""),
        ),
        repair=HATCH.repair,
        path=tmp_path / "tiny.md",
        module=None,
    )
    doors = FakeDoors()
    key_dir, pubkey = _keys(tmp_path)
    turns, final, _reasks = await speak(
        cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert [t.label for t in turns] == ["1", "2", "4", "5"]
    assert [t.door for t in turns] == ["api", "api", "ingress", "ingress-unsigned"]
    assert turns[1].words.endswith(pubkey)  # the template was filled
    assert final is not None and final["door"]["status"] == "open"
    # a root list row is not a turn, but it is a command
    assert ("api", "list", ()) in doors.sent


async def test_speak_without_a_root_list_row_returns_no_final(tmp_path: Path) -> None:
    from membrane.capabilities import Capability, Row

    cap = Capability(
        name="tiny",
        depends=(),
        postconditions=(),
        rows=(Row("1", "root say", WORDS["1"], ""),),
        repair=HATCH.repair,
        path=tmp_path / "tiny.md",
        module=None,
    )
    key_dir, pubkey = _keys(tmp_path)
    turns, final, _reasks = await speak(
        cap, FakeDoors(), key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert [t.label for t in turns] == ["1"] and final is None


async def test_every_row_settles_so_a_failed_build_after_a_signed_row_is_repaired(
    tmp_path: Path,
) -> None:
    """Capabilities design §4: no per-row settle flag. FakeDoors fails page_title's
    first build; row 7 is a signed row, and the repair runs after it."""
    doors = FakeDoors(fail_first={"page_title"})
    key_dir, pubkey = _keys(tmp_path)
    log = io.StringIO()
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=log
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    assert "3-repair-1" in labels and labels.index("3-repair-1") > labels.index("7")
    assert "build failed for page_title; repair 1" in log.getvalue()


async def test_verbs_are_approved_before_the_policies_that_name_them(tmp_path: Path) -> None:
    """Live run 2026-09-09-run-5: the model proposed the door policy as p-1 and the
    hook as p-2; approving in id order had the policy refused ("hook ... is not a
    verb in the catalog") and the door stayed closed until the next pass."""
    doors = FakeDoors(policy_first=True)
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
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
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
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
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
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
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    assert "3-repair-1" in labels, labels
    repair = turns[labels.index("3-repair-1")]
    assert repair.words == HATCH.repair.refused == "check your inbox"


async def test_a_refusal_earns_one_repair_round_not_one_per_settle(tmp_path: Path) -> None:
    """A proposal the model never repairs keeps its reason forever, and settle()
    runs after turns 6, 7 and 9 as well as turn 2. Without a memo each of those
    would buy three more turns of the model's time on a refusal it has already
    been shown and declined to fix (2026-09-10-postcut-run-3, killed by hand
    while it did exactly that)."""
    doors = FakeDoors(refuse={"p-1"})
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    repairs = [t.label for t in turns if t.label.startswith("3-repair-")]
    assert repairs == ["3-repair-1"], repairs


async def test_a_refused_proposal_is_not_approved_again_under_the_same_state(
    tmp_path: Path,
) -> None:
    """`repaired` stops the model being *asked* about a refusal twice; it does not
    stop the driver *approving* it twice. Every later settle re-offered the same
    abandoned proposal and the membrane refused it again for the same reason, and
    each refusal went back into the model's inbox: 2026-09-16-run-3 delivered one
    `effect communicate` refusal 28 times for a proposal the model had already
    superseded, which reads in the evidence as 28 fresh mistakes."""
    doors = FakeDoors(refuse={"p-1"})
    key_dir, pubkey = _keys(tmp_path)
    log = io.StringIO()
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=log
    )
    offered = [a["id"] for t in turns for a in t.approvals]
    # The decide, and the one retry round that settles a refusal. Nothing after.
    assert offered.count("p-1") == 2, offered
    assert "not re-approving p-1" in log.getvalue()
    assert all("refused" in a["result"] for t in turns for a in t.approvals if a["id"] == "p-1")


async def test_a_refused_proposal_is_offered_again_once_the_catalog_moves(
    tmp_path: Path,
) -> None:
    """The negative control for the memo above, and the reason the skip is sound
    rather than a heuristic: `refuse_approval` and `refuse_policy` read the catalog
    and the policy and nothing else, so the skip holds only while that pair is
    unchanged. Move it and the same proposal is put to the membrane again, because
    the answer may now be different."""
    from membrane.capabilities import Row

    doors = FakeDoors(refuse={"p-1"})

    async def root(text: str) -> tuple[dict[str, Any], str]:
        made = [doors._propose("verb", text)] if text in ("alpha", "beta") else []
        return doors._reply(
            audit_line(
                tools=[{"name": "propose", "status": "ok", "id": p["id"]} for p in made]
                or [{"name": "remember", "status": "ok"}],
                proposals=[{"id": p["id"], "sha256": "x"} for p in made],
            ),
            "Done.",
        )

    doors.root_say = root  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    cap = replace(
        HATCH,
        rows=(
            Row("one", "root say", "alpha", ""),  # p-1: refused, and never repaired
            Row("two", "root say", "beta", ""),  # p-2: builds, so the catalog moves
        ),
    )
    log = io.StringIO()
    turns, _final, _reasks = await speak(
        cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=log
    )
    offered = [(t.label, a["id"]) for t in turns for a in t.approvals]
    # Skipped while the pair held -- row two's own settle still sees an empty catalog
    assert "not re-approving p-1" in log.getvalue()
    # and put to the membrane again the moment beta went ready.
    assert [label for label, pid in offered if pid == "p-1" and label != "one"], offered
    assert doors.catalog["beta"]["status"] == "ready"


async def test_a_failed_build_is_repaired_with_turn_3(tmp_path: Path) -> None:
    doors = FakeDoors(fail_first={"verify_ssh"})
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    assert labels[:4] == ["1", "2", "3-repair-1", "4"]
    repair = turns[2]
    assert repair.words == HATCH.repair.build and repair.door == "api"
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
    turns, final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    labels = [t.label for t in turns]
    assert labels[:6] == ["1", "2", "3-repair-1", "3-repair-2", "3-repair-3", "4"]
    # the hook never became ready, so the signed knock is anonymous
    assert turns[5].audit["principal"] == "anonymous"
    final = await doors.listing()
    result = judge(
        list(CHECKS),
        Judged(
            turns=turns,
            final=final,
            recipes_after=set(),
            preexisting=set(),
            brain_recipe="rcp-brain",
            checks={},
            sent=[],
            context={},
        ),
    )
    assert result["authentication"]["ok"] is False


async def test_a_repair_turn_that_called_no_tool_at_all_is_asked_again(tmp_path: Path) -> None:
    """Root says `check your inbox`, the model writes prose about what it will do and
    stops, and nothing truncated it: `unfinished` wants a truncating stop reason and
    does not see this, so the row used to settle on it in silence. Two runs of the
    same model produced one each (2026-09-16-run-2 `3-repair-3`, run-3 `3-repair-1`).
    Read off the audit's tool list, never off the prose that says what it meant."""
    doors = FakeDoors(refuse={"p-1"}, stall_on_repair=True)
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    repairs = [(t.label, t.words) for t in turns if t.label.startswith("3-repair-")]
    assert repairs == [
        ("3-repair-1", HATCH.repair.refused),
        ("3-repair-2", "you called nothing; act"),
        ("3-repair-3", "you called nothing; act"),
    ], repairs


async def test_a_repair_turn_that_acted_and_still_failed_is_not_a_stall(tmp_path: Path) -> None:
    """The negative control: the trigger is the absence of every call, not the
    absence of a fix. An agent that looked, acted and did not manage it has answered
    the repair, and telling it `you called nothing` would be a lie it cannot use."""
    doors = FakeDoors(refuse={"p-1"})  # the same run, with a repair turn that acts
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    repairs = [t.label for t in turns if t.label.startswith("3-repair-")]
    assert repairs == ["3-repair-1"], repairs


async def test_a_proposes_row_that_proposed_nothing_is_asked_again(tmp_path: Path) -> None:
    """Hatch 2, 6, 7 and 9 cannot reach their outcome without a proposal, and the
    heading says so. Without the trigger a row ends with the model narrating the
    verb it intends to grow, having proposed nothing, and the run walks on
    (2026-09-16-run-3 turn 2: twenty `try` calls and no `propose`). The trigger reads
    the turn's proposal list, not the sentence about what it was going to do."""
    from membrane.capabilities import Row
    from membrane.capability import MAX_REPAIRS

    doors = FakeDoors()

    async def root(text: str) -> tuple[dict[str, Any], str]:
        return doors._reply(
            audit_line(tools=[{"name": "try", "status": "ok"}]),
            "I will grow a verb that does this.",
        )

    doors.root_say = root  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    cap = replace(HATCH, rows=(Row("grow", "root say", "Grow a verb.", "", proposes=True),))
    turns, _final, _reasks = await speak(
        cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    assert [t.words for t in turns if t.label.startswith("3-repair-")] == [
        "you proposed nothing; propose"
    ] * MAX_REPAIRS


async def test_a_row_that_does_not_propose_is_never_silent(tmp_path: Path) -> None:
    """The first negative control: the heading is what makes a row answerable this
    way. Hatch 1, 4, 5 and 8 ask a question whose whole outcome is a reply, and a
    turn that answers one without proposing is right, not silent."""
    from membrane.capabilities import Row

    doors = FakeDoors()

    async def root(text: str) -> tuple[dict[str, Any], str]:
        return doors._reply(
            audit_line(tools=[{"name": "try", "status": "ok"}]), "Here is my answer."
        )

    doors.root_say = root  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    cap = replace(HATCH, rows=(Row("ask", "root say", "What are you?", ""),))
    turns, _final, _reasks = await speak(
        cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    assert not [t for t in turns if t.label.startswith("3-repair-")]


async def test_a_proposes_row_that_proposed_is_not_silent_on_its_continuation(
    tmp_path: Path,
) -> None:
    """The second negative control, and the one that bit. A proposal produces an
    external result, which buys a continuation, and the continuation is settled by a
    second call: a row that proposed on its own turn and then continued must not be
    read as having proposed nothing, so the memo of which rows have proposed outlives
    the call."""
    from membrane.capabilities import Row

    doors = FakeDoors()

    async def root(text: str) -> tuple[dict[str, Any], str]:
        made = [doors._propose("verb", "alpha")] if text == "Grow a verb." else []
        return doors._reply(
            audit_line(
                tools=[{"name": "propose", "status": "ok", "id": p["id"]} for p in made]
                or [{"name": "try", "status": "ok"}],
                proposals=[{"id": p["id"], "sha256": "x"} for p in made],
            ),
            "Done.",
        )

    doors.root_say = root  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    cap = replace(HATCH, rows=(Row("grow", "root say", "Grow a verb.", "", proposes=True),))
    turns, _final, _reasks = await speak(
        cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    assert any("-continue-" in t.label for t in turns), [t.label for t in turns]
    assert not [t for t in turns if t.label.startswith("3-repair-")]


async def test_a_proposes_row_behind_a_closed_door_is_not_silent(tmp_path: Path) -> None:
    """The third negative control. Hatch 6, 7 and 9 all speak through the public
    door, and a run whose identity verb never built leaves all three closed: the
    membrane answers alone, the agent is never asked anything, and a turn nothing was
    put to cannot have declined to propose. Root saying `you proposed nothing` there
    would spend the whole repair budget on rows that never ran."""
    doors = FakeDoors(open_door=False)
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    closed = [t for t in turns if t.door.startswith("ingress")]
    assert closed and all(t.audit.get("closed") for t in closed)
    assert not [t for t in turns if t.label.startswith("3-repair-")]


async def test_a_closed_door_makes_every_public_turn_a_refusal(tmp_path: Path) -> None:
    doors = FakeDoors(open_door=False)
    key_dir, pubkey = _keys(tmp_path)
    turns, final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    public = [t for t in turns if t.door.startswith("ingress")]
    assert public and all(t.audit["principal"] is None for t in public)
    final = await doors.listing()
    result = judge(
        list(CHECKS),
        Judged(
            turns=turns,
            final=final,
            recipes_after=set(),
            preexisting=set(),
            brain_recipe="rcp-brain",
            checks={},
            sent=[],
            context={},
        ),
    )
    failed = {k for k, v in result.items() if not v["ok"]}
    assert failed == {"authentication", "authorization", "page_title", "counter"}
    assert result["root_unforgeable"]["evidence"] == {"public_principals": []}


async def test_a_refused_approval_is_recorded_and_the_verdict_sees_it(tmp_path: Path) -> None:
    doors = FakeDoors(refuse={"p-2"})
    key_dir, pubkey = _keys(tmp_path)
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert turns[1].approvals[1]["result"].startswith("p-2 refused")
    assert doors.policy["door"] == "closed"


async def test_the_asking_approver_reads_the_pilot(tmp_path: Path) -> None:
    doors = FakeDoors()
    key_dir, pubkey = _keys(tmp_path)
    stdin = io.StringIO("approve\nreject not like this\napprove\napprove\napprove\n")
    stdout = io.StringIO()
    turns, _final, _reasks = await speak(
        HATCH, doors, key_dir, {"key": pubkey}, AskApprover(stdin, stdout), log=io.StringIO()
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
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


# ---------------------------------------------------------------- hatching and a whole run


def _stub_hatch(tmp_path: Path, *, fail: bool = False) -> Path:
    script = tmp_path / "hatch.sh"
    body = "#!/bin/sh\necho building >&2\n"
    if fail:
        body += "echo boom >&2\nexit 1\n"
    else:
        body += (
            "env | grep -E '^(MSHKN_API_URL|MSHKN_API_KEY|BRAIN_API_URL|MEMBRANE_MODEL"
            "|MEMBRANE_MODEL_ID|MEMBRANE_EFFORT|MEMBRANE_BODY_EXTRA|ANTHROPIC_API_KEY"
            "|ANTHROPIC_BASE_URL|OPENAI_API_KEY)='"
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


def _settings() -> RunSettings:
    return RunSettings(
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
        "ANTHROPIC_API_KEY=sk-a\nANTHROPIC_BASE_URL=https://api.anthropic.com\n"
        "BRAIN_API_URL=https://api.mshkn.dev\nMEMBRANE_BODY_EXTRA=\nMEMBRANE_EFFORT=\n"
        "MEMBRANE_MODEL=anthropic\nMEMBRANE_MODEL_ID=claude-opus-5\nMSHKN_API_KEY=k\n"
        "MSHKN_API_URL=http://api\nOPENAI_API_KEY=oa\n"
    )


def test_a_failed_hatch_raises_with_its_stderr(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        hatch(_settings(), _stub_hatch(tmp_path, fail=True), log=io.StringIO())


def test_hatch_hands_the_brain_the_gateway_key_under_the_one_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One model key in /brain/.env, whatever kind it is: a brain checkpoint must not
    carry a credential for a service it cannot reach (#92)."""
    out = tmp_path / "env.txt"
    monkeypatch.setenv("HATCH_ENV_OUT", str(out))
    settings = replace(
        _settings(),
        base_url="https://ai-gateway.vercel.sh",
        gateway_api_key="vck-1",
        body_extra='{"providerOptions": {"gateway": {"only": ["anthropic"]}}}',
    )
    hatch(settings, _stub_hatch(tmp_path), log=io.StringIO())
    written = out.read_text()
    assert "ANTHROPIC_API_KEY=vck-1\n" in written
    assert "ANTHROPIC_BASE_URL=https://ai-gateway.vercel.sh\n" in written
    assert (
        'MEMBRANE_BODY_EXTRA={"providerOptions": {"gateway": {"only": ["anthropic"]}}}\n' in written
    )
    assert "sk-a" not in written


async def test_run_once_hatches_speaks_judges_records_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    api = FakeApi(recipes=[{"recipe_id": "rcp-pre"}])
    transport = httpx.MockTransport(api.handler)
    monkeypatch.setattr("membrane.capability.transport_for", lambda _url: transport)
    out_dir = tmp_path / "docs" / "2026-09-09-run-1"
    summary = await run_once(
        _settings(),
        HATCH,
        out_dir,
        AutoApprover(),
        hatch_script=_stub_hatch(tmp_path),
        key_dir=tmp_path / "keys",
        keep=False,
        log=io.StringIO(),
        out=tmp_path / "docs",
    )
    # every membrane command was answered by the fake's default (a root reply), so the
    # door was never opened and the postconditions that need it fail honestly
    assert summary["ok"] is False and summary["model"] == "claude-opus-5"
    assert summary["hatched"]["rule_id"] == "rule-1" and summary["passed"] < len(CHECKS)
    assert summary["usage"]["input_tokens"] > 0 and summary["cost_usd"] > 0
    assert set(summary["postconditions"]) == set(CHECKS)
    assert summary["capability"] == "hatch" and summary["started_from"] == "hatch"
    assert (out_dir / "run.json").exists() and (out_dir / "transcript.md").exists()
    assert (out_dir / "final-list.json").exists() and any((out_dir / "commands").iterdir())
    run_doc = json.loads((out_dir / "run.json").read_text())
    assert run_doc["turns"][0]["label"] == "1"
    # no policy was ever applied, so no row was asked twice: the count is zero
    assert run_doc["reasks"] == []
    # what the run was given, and what each turn's calls actually spent (#122)
    assert run_doc["default_effort"] is None
    assert run_doc["turns"][0]["effort"] == ["medium"]
    # a run not given `--effort off` supports the axis (#127)
    assert summary["effort_supported"] is True
    deletes = [p for m, p, _ in api.requests if m == "DELETE"]
    assert (
        deletes[:2] == ["/ingress_rules/rule-1", "/keys/key-1"] and "/recipes/rcp-brain" in deletes
    )


async def test_run_once_checks_hook_computers_from_every_turn_not_only_row_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#167: the driver used to collect hook computers from the turn labelled
    "4" alone (hatch's signed knock). A dependent capability's identity hook can
    run on any row of its own, so the collection is every turn's hooks now,
    deduplicated. This capability has no row "4" at all; the old code would have
    checked nothing."""
    from membrane.capabilities import Capability, Row

    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    cap = Capability(
        name="two-hooks",
        depends=(),
        postconditions=(),
        rows=(
            Row("a", "root say", "Say A", "outcome"),
            Row("b", "root say", "Say B", "outcome"),
        ),
        repair=HATCH.repair,
        path=tmp_path / "two-hooks.md",
        module=None,
    )
    listing: dict[str, Any] = {"catalog": {}, "proposals": [], "policy": {"principals": {}}}
    checked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/checkpoints":
            return httpx.Response(200, json=[])
        if request.method == "GET" and path == "/recipes":
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.endswith("/status"):
            checked.append(path.split("/")[2])
            return httpx.Response(404, json={})
        if request.method == "GET" and path.endswith("/exec_log"):
            return httpx.Response(404, json={"detail": "no log"})
        if request.method == "POST" and path == "/checkpoints/fork":
            command = str(json.loads(request.content)["exec"])
            if command == "membrane root list":
                return httpx.Response(
                    200,
                    json={
                        "computer_id": "c",
                        "exec_exit_code": 0,
                        "exec_stdout": json.dumps(listing),
                    },
                )
            text = base64.b64decode(command.split()[-1]).decode()
            hook_id = "hook-a" if text == "Say A" else "hook-b"
            audit = audit_line(hooks=[{"name": "identity", "computer_id": hook_id}])
            return httpx.Response(
                200,
                json={
                    "computer_id": "c",
                    "exec_exit_code": 0,
                    "exec_stdout": _out(audit, "Noted."),
                },
            )
        raise AssertionError((request.method, path))

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )

    async def fake_teardown(
        self: Doors, h: Hatched, listing_arg: Any, *, lineage: Any = None, log: Any = None
    ) -> None:
        return None

    monkeypatch.setattr(Doors, "teardown", fake_teardown)
    summary = await run_once(
        _settings(),
        cap,
        tmp_path / "run",
        AutoApprover(),
        hatch_script=_stub_hatch(tmp_path),
        key_dir=tmp_path / "keys",
        keep=False,
        log=io.StringIO(),
        out=tmp_path,
    )
    assert set(checked) == {"hook-a", "hook-b"}
    assert summary["ok"] is True  # no postconditions named: 0 passed of 0


async def test_run_once_records_effort_unsupported_when_the_run_was_given_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`effort_supported` names a condition the run was hatched under (#127), not an
    outcome: `--effort off` makes it False even though nothing else about this run
    differs from `test_run_once_hatches_speaks_judges_records_and_tears_down`."""
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    api = FakeApi(recipes=[{"recipe_id": "rcp-pre"}])
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    summary = await run_once(
        replace(_settings(), default_effort=EFFORT_OFF),
        HATCH,
        tmp_path / "docs" / "2026-09-13-run-1",
        AutoApprover(),
        hatch_script=_stub_hatch(tmp_path),
        key_dir=tmp_path / "keys",
        keep=False,
        log=io.StringIO(),
        out=tmp_path / "docs",
    )
    assert summary["default_effort"] == EFFORT_OFF
    assert summary["effort_supported"] is False


async def test_run_once_refuses_an_account_that_already_has_a_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi(checkpoints=[{"id": "ck", "label": "brain", "recipe_id": "r"}])
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    with pytest.raises(RuntimeError, match="already has a brain"):
        await run_once(
            _settings(),
            HATCH,
            tmp_path / "run",
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
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

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )
    out_dir = tmp_path / "run"
    with pytest.raises(RuntimeError, match="boom"):
        await run_once(
            _settings(),
            HATCH,
            out_dir,
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )
    summary = json.loads((out_dir / "run.json").read_text())
    assert summary["ok"] is False and "boom" in summary["error"] and summary["commands"] == 1
    assert summary["reasks"] == []  # it fell over at turn 1, with nothing re-asked
    assert summary["hatched"]["rule_id"] == "rule-1"
    assert "commit" in summary["membrane"]  # an aborted run names its code too
    # an aborted run names the conditions it ran under, not only the ones it reached
    assert summary["effort_supported"] is True


async def test_an_aborted_run_names_the_base_url_it_spoke_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partway failure is exactly the case where attributing it to the endpoint matters:
    the aborted-run record must carry `base_url` too, not only the successful one."""
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/checkpoints/fork":
            return httpx.Response(
                200,
                json={
                    "computer_id": "c1",
                    "exec_exit_code": 1,
                    "exec_stdout": "boom",
                    "exec_stderr": "boom",
                },
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )
    out_dir = tmp_path / "run"
    settings = replace(
        _settings(), base_url="https://ai-gateway.vercel.sh", gateway_api_key="vck-1"
    )
    with pytest.raises(RuntimeError, match="boom"):
        await run_once(
            settings,
            HATCH,
            out_dir,
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )
    summary = json.loads((out_dir / "run.json").read_text())
    assert summary["base_url"] == "https://ai-gateway.vercel.sh"


async def test_an_aborted_run_names_effort_unsupported_when_it_was_given_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`effort_supported` is fixed at hatch, before the first model call, so an aborted
    run names it exactly as a passing one would (#127)."""
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/checkpoints/fork":
            return httpx.Response(
                200,
                json={
                    "computer_id": "c1",
                    "exec_exit_code": 1,
                    "exec_stdout": "boom",
                    "exec_stderr": "boom",
                },
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )
    out_dir = tmp_path / "run"
    settings = replace(_settings(), default_effort=EFFORT_OFF)
    with pytest.raises(RuntimeError, match="boom"):
        await run_once(
            settings,
            HATCH,
            out_dir,
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )
    summary = json.loads((out_dir / "run.json").read_text())
    assert summary["default_effort"] == EFFORT_OFF
    assert summary["effort_supported"] is False


async def test_an_aborted_run_names_the_exception_type_when_its_message_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`httpx.ConnectError` can carry no message at all (2026-09-13-run-1); the
    summary's "error" must still say what happened, not go blank."""
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))

    async def fail_speak(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("")

    monkeypatch.setattr("membrane.capability.speak", fail_speak)
    api = FakeApi()
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    out_dir = tmp_path / "run"
    with pytest.raises(httpx.ConnectError):
        await run_once(
            _settings(),
            HATCH,
            out_dir,
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )
    summary = json.loads((out_dir / "run.json").read_text())
    assert summary["ok"] is False and summary["error"] == "ConnectError"


async def test_an_aborted_run_keeps_the_re_asks_it_had_already_spoken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reasks` is the driver's own count, and an abort is evidence too: the run
    that fell over after asking row 8 again says so, rather than reporting none."""
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))

    async def speak_then_fail(*_args: Any, **kwargs: Any) -> Any:
        kwargs["reasks"].append("8")
        raise RuntimeError("the door went away")

    monkeypatch.setattr("membrane.capability.speak", speak_then_fail)
    api = FakeApi()
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    out_dir = tmp_path / "run"
    with pytest.raises(RuntimeError, match="the door went away"):
        await run_once(
            _settings(),
            HATCH,
            out_dir,
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )
    summary = json.loads((out_dir / "run.json").read_text())
    assert summary["ok"] is False and summary["reasks"] == ["8"]


async def test_keep_skips_the_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    api = FakeApi()
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    await run_once(
        _settings(),
        HATCH,
        tmp_path / "run",
        AutoApprover(),
        hatch_script=_stub_hatch(tmp_path),
        key_dir=tmp_path / "keys",
        keep=True,
        log=io.StringIO(),
        out=tmp_path,
    )
    assert not [p for m, p, _ in api.requests if m == "DELETE"]


async def test_run_once_of_a_dependent_without_a_promotion_names_both_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No promotion for `hatch` on disk: the message names the run and the
    promote command that would produce one."""
    from membrane.capabilities import Capability

    monkeypatch.setattr(
        "membrane.capability.transport_for",
        lambda _url: httpx.MockTransport(lambda _request: httpx.Response(200, json=[])),
    )
    cap = Capability(
        name="security",
        depends=("hatch",),
        postconditions=(),
        rows=(),
        repair=HATCH.repair,
        path=tmp_path / "security.md",
        module=None,
    )
    with pytest.raises(
        RuntimeError, match=r"capability run hatch --keep.*capability promote hatch"
    ):
        await run_once(
            _settings(),
            cap,
            tmp_path / "run",
            AutoApprover(),
            hatch_script=tmp_path / "unused.sh",
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )


async def test_run_once_refuses_a_dependency_missing_from_the_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`coding` depends on `security` then `hatch`, but only `hatch` is promoted
    (as a hatch, `started_from=None`): `security` is not in its ancestry."""
    from membrane.capabilities import Capability
    from membrane.capability import Promotion, write_promotion

    write_promotion(
        tmp_path,
        Promotion(
            capability="hatch",
            run="hatch/r1",
            membrane={},
            promoted_at="t",
            labels={},
            rule_id="ir_1",
            key_id="key-1",
            brain_recipe="rcp",
            recipe_ids=(),
            key_dir="/keys",
            pubkey="ssh-ed25519 AAAA mike",
            model="claude-opus-5",
            default_effort=None,
            reasks=0,
            started_from=None,
        ),
    )
    monkeypatch.setattr(
        "membrane.capability.transport_for",
        lambda _url: httpx.MockTransport(lambda _request: httpx.Response(200, json=[])),
    )
    cap = Capability(
        name="coding",
        depends=("security", "hatch"),
        postconditions=(),
        rows=(),
        repair=HATCH.repair,
        path=tmp_path / "coding.md",
        module=None,
    )
    with pytest.raises(
        RuntimeError, match="coding depends on security, not in the ancestry of hatch"
    ):
        await run_once(
            _settings(),
            cap,
            tmp_path / "run",
            AutoApprover(),
            hatch_script=tmp_path / "unused.sh",
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )


async def test_run_once_of_a_dependent_signs_with_its_lineages_key_and_reports_its_membrane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start_from` is monkeypatched (it has its own test); this test is about
    `run_once` wiring: the summary's `started_from` is the promotion's `run`,
    teardown gets the same promotion as its lineage, the words carry the lineage's
    public key (the promoted identity hook trusts no other), and the membrane,
    model and effort recorded are the hatch's, not this working tree's and not
    the defaults of this command line."""
    from membrane.capabilities import Capability, Row
    from membrane.capability import Promotion, write_promotion

    key_dir = tmp_path / "lineage-keys"
    key_dir.mkdir()
    pubkey = new_key(key_dir)
    lineage = Promotion(
        capability="hatch",
        run="hatch/2026-09-12-run-1",
        membrane={"commit": "abc", "dirty": False},
        promoted_at="t",
        labels={"brain": "pb"},
        rule_id="ir_1",
        key_id="key-1",
        brain_recipe="rcp-brain",
        recipe_ids=("rcp-brain",),
        key_dir=str(key_dir),
        pubkey=pubkey,
        model="claude-opus-5",
        default_effort="high",
        reasks=0,
        started_from=None,
    )
    write_promotion(tmp_path, lineage)
    cap = Capability(
        name="security",
        depends=("hatch",),
        postconditions=(),
        rows=(Row("2", "root say", "My public key is {key}", "A reply."),),
        repair=HATCH.repair,
        path=tmp_path / "security.md",
        module=None,
    )
    hatched = Hatched(
        ingress_url="",
        rule_id="ir_1",
        key_id="key-1",
        recipe_id="rcp-brain",
        checkpoint_id="new-brain",
    )

    async def fake_start_from(doors: Doors, promotion: Promotion, *, log: Any) -> Hatched:
        assert promotion == lineage
        return hatched

    monkeypatch.setattr("membrane.capability.start_from", fake_start_from)
    teardown_calls: list[tuple[Hatched, Any, Promotion | None]] = []

    async def fake_teardown(
        self: Doors, h: Hatched, listing: Any, *, lineage: Promotion | None = None, log: Any = None
    ) -> None:
        teardown_calls.append((h, listing, lineage))

    monkeypatch.setattr(Doors, "teardown", fake_teardown)
    listing: dict[str, Any] = {"catalog": {}, "proposals": []}
    spoken: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            return httpx.Response(200, json=[])
        if request.url.path == "/recipes":
            return httpx.Response(200, json=[])
        if request.url.path == "/checkpoints/fork":
            command = str(json.loads(request.content)["exec"])
            if command == "membrane root list":
                return httpx.Response(
                    200,
                    json={
                        "computer_id": "c",
                        "exec_exit_code": 0,
                        "exec_stdout": json.dumps(listing),
                    },
                )
            spoken.append(base64.b64decode(command.split()[-1]).decode())
            return httpx.Response(
                200,
                json={
                    "computer_id": "c",
                    "exec_exit_code": 0,
                    "exec_stdout": _out(audit_line(), "Noted."),
                },
            )
        raise AssertionError(request.url.path)

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )
    summary = await run_once(
        _settings(),
        cap,
        tmp_path / "run",
        AutoApprover(),
        hatch_script=tmp_path / "unused.sh",
        # what `_run` would pass: for a dependent it is the lineage's directory that counts
        key_dir=tmp_path / "unused-keys",
        keep=False,
        log=io.StringIO(),
        out=tmp_path,
    )
    assert summary["started_from"] == lineage.run
    assert teardown_calls == [(hatched, listing, lineage)]
    assert spoken == [f"My public key is {pubkey}"]
    assert summary["key_dir"] == str(key_dir) and summary["pubkey"] == pubkey
    assert summary["membrane"] == {"commit": "abc", "dirty": False}
    assert summary["model"] == "claude-opus-5" and summary["default_effort"] == "high"
    assert not (tmp_path / "unused-keys").exists()  # a dependent generates no key


async def test_run_once_of_a_dependent_names_its_lineages_base_url_not_the_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#127 fix round 1: `model_id` and `default_effort` were already overridden from
    `lineage` (the test above) because a forked brain runs the /brain/.env of its
    own hatch, whatever this working tree and command line say. `base_url` and
    `body_extra` were not: `security` (a dependent, which `_run` correctly refuses
    `--base-url` for) wrote `settings.base_url` — the direct API default — into
    its `run.json` even when the hatch it forked from spoke through a gateway.
    That is silent and it corrupts the one artifact `docs/embryo/README.md`'s
    "Reading a run spoken through a gateway" section tells a reader to key off."""
    from membrane.capabilities import Capability, Row
    from membrane.capability import Promotion, write_promotion

    key_dir = tmp_path / "lineage-keys"
    key_dir.mkdir()
    pubkey = new_key(key_dir)
    lineage = Promotion(
        capability="hatch",
        run="hatch/2026-09-12-run-1",
        membrane={"commit": "abc", "dirty": False},
        promoted_at="t",
        labels={"brain": "pb"},
        rule_id="ir_1",
        key_id="key-1",
        brain_recipe="rcp-brain",
        recipe_ids=("rcp-brain",),
        key_dir=str(key_dir),
        pubkey=pubkey,
        model="claude-opus-5",
        default_effort=None,
        reasks=0,
        started_from=None,
        base_url="https://ai-gateway.vercel.sh",
        body_extra='{"providerOptions": {"gateway": {"only": ["anthropic"]}}}',
    )
    write_promotion(tmp_path, lineage)
    cap = Capability(
        name="security",
        depends=("hatch",),
        postconditions=(),
        rows=(Row("2", "root say", "My public key is {key}", "A reply."),),
        repair=HATCH.repair,
        path=tmp_path / "security.md",
        module=None,
    )
    hatched = Hatched(
        ingress_url="",
        rule_id="ir_1",
        key_id="key-1",
        recipe_id="rcp-brain",
        checkpoint_id="new-brain",
    )

    async def fake_start_from(doors: Doors, promotion: Promotion, *, log: Any) -> Hatched:
        return hatched

    monkeypatch.setattr("membrane.capability.start_from", fake_start_from)

    async def fake_teardown(
        self: Doors, h: Hatched, listing: Any, *, lineage: Promotion | None = None, log: Any = None
    ) -> None:
        return None

    monkeypatch.setattr(Doors, "teardown", fake_teardown)
    listing: dict[str, Any] = {"catalog": {}, "proposals": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            return httpx.Response(200, json=[])
        if request.url.path == "/recipes":
            return httpx.Response(200, json=[])
        if request.url.path == "/checkpoints/fork":
            command = str(json.loads(request.content)["exec"])
            if command == "membrane root list":
                return httpx.Response(
                    200,
                    json={
                        "computer_id": "c",
                        "exec_exit_code": 0,
                        "exec_stdout": json.dumps(listing),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "computer_id": "c",
                    "exec_exit_code": 0,
                    "exec_stdout": _out(audit_line(), "Noted."),
                },
            )
        raise AssertionError(request.url.path)

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )
    # `_settings()` defaults to the direct API: the discriminating case is that the
    # dependent's own settings say one thing and the lineage it forked from says
    # another, and the recorded evidence must be the lineage's.
    settings = _settings()
    assert settings.base_url != lineage.base_url
    summary = await run_once(
        settings,
        cap,
        tmp_path / "run",
        AutoApprover(),
        hatch_script=tmp_path / "unused.sh",
        key_dir=tmp_path / "unused-keys",
        keep=False,
        log=io.StringIO(),
        out=tmp_path,
    )
    assert summary["base_url"] == "https://ai-gateway.vercel.sh"
    assert summary["body_extra"] == '{"providerOptions": {"gateway": {"only": ["anthropic"]}}}'


async def test_run_once_of_a_dependent_refuses_a_key_that_is_not_the_lineages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key directory holds a key, but not the one row 2 handed the agent: the
    promoted hook would call every signed row `anonymous`, so the run refuses
    before it spends an API call."""
    from membrane.capabilities import Capability
    from membrane.capability import Promotion, write_promotion

    key_dir = tmp_path / "lineage-keys"
    key_dir.mkdir()
    new_key(key_dir)  # a fresh key, not the one the promotion names
    write_promotion(
        tmp_path,
        Promotion(
            capability="hatch",
            run="hatch/2026-09-12-run-1",
            membrane={},
            promoted_at="t",
            labels={"brain": "pb"},
            rule_id="ir_1",
            key_id="key-1",
            brain_recipe="rcp-brain",
            recipe_ids=("rcp-brain",),
            key_dir=str(key_dir),
            pubkey="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIthehatchersownkeyline mike",
            model="claude-opus-5",
            default_effort=None,
            reasks=0,
            started_from=None,
        ),
    )
    cap = Capability(
        name="security",
        depends=("hatch",),
        postconditions=(),
        rows=(),
        repair=HATCH.repair,
        path=tmp_path / "security.md",
        module=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the run reached the API: {request.url.path}")

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )
    with pytest.raises(
        RuntimeError, match="holds a key that is not the one hatch/2026-09-12-run-1"
    ):
        await run_once(
            _settings(),
            cap,
            tmp_path / "run",
            AutoApprover(),
            hatch_script=tmp_path / "unused.sh",
            key_dir=tmp_path / "unused-keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )


async def test_run_once_of_a_dependent_names_the_directory_that_has_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from membrane.capabilities import Capability
    from membrane.capability import Promotion, write_promotion

    write_promotion(
        tmp_path,
        Promotion(
            capability="hatch",
            run="hatch/r1",
            membrane={},
            promoted_at="t",
            labels={},
            rule_id="ir_1",
            key_id="key-1",
            brain_recipe="rcp",
            recipe_ids=(),
            key_dir=str(tmp_path / "gone"),
            pubkey="ssh-ed25519 AAAA mike",
            model="claude-opus-5",
            default_effort=None,
            reasks=0,
            started_from=None,
        ),
    )
    cap = Capability(
        name="security",
        depends=("hatch",),
        postconditions=(),
        rows=(),
        repair=HATCH.repair,
        path=tmp_path / "security.md",
        module=None,
    )
    monkeypatch.setattr(
        "membrane.capability.transport_for",
        lambda _url: httpx.MockTransport(lambda _request: httpx.Response(200, json=[])),
    )
    with pytest.raises(RuntimeError, match=f"no key in {tmp_path / 'gone'}"):
        await run_once(
            _settings(),
            cap,
            tmp_path / "run",
            AutoApprover(),
            hatch_script=tmp_path / "unused.sh",
            key_dir=tmp_path / "unused-keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
        )


def test_main_parses_and_runs_n_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=http://api\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n"
    )
    calls: list[tuple[Path, str, bool]] = []

    async def fake_run_once(
        settings: RunSettings, capability: Any, out_dir: Path, approver: Any, **kwargs: Any
    ) -> dict[str, Any]:
        calls.append((out_dir, type(approver).__name__, kwargs["keep"]))
        out_dir.mkdir(parents=True)
        (out_dir / "run.json").write_text(
            json.dumps({"capability": capability.name, "started_from": "hatch"})
        )
        return {
            "ok": len(calls) == 1,
            "passed": 7 if len(calls) == 1 else 5,
            "cost_usd": 1.25,
            "model": settings.model_id,
        }

    monkeypatch.setattr("membrane.capability.run_once", fake_run_once)
    code = main(
        [
            "run",
            "hatch",
            "--key-dir",
            str(tmp_path / "keys"),
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
    assert [c[0] for c in calls] == [
        tmp_path / "docs" / "hatch" / "2026-09-09-run-1",
        tmp_path / "docs" / "hatch" / "2026-09-09-run-2",
    ]
    assert calls[0][1] == "AutoApprover" and calls[0][2] is True
    run_doc = json.loads((calls[0][0] / "run.json").read_text())
    assert run_doc["capability"] == "hatch" and run_doc["started_from"] == "hatch"
    # a third invocation numbers itself after the directories that exist
    monkeypatch.setattr("membrane.capability.run_once", fake_run_once)
    calls.clear()
    code = main(
        [
            "run",
            "hatch",
            "--key-dir",
            str(tmp_path / "keys"),
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
    assert (
        code == 0
        and calls[0][0] == tmp_path / "docs" / "hatch" / "2026-09-09-run-3"
        and calls[0][1] == "AskApprover"
    )


def test_main_reports_an_aborting_transport_error_by_its_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport error must abort the run instead of escaping `main` as a
    traceback (2026-09-13-run-1); the log names the exception's type."""
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=http://api\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n"
    )

    async def fake_run_once(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("")

    monkeypatch.setattr("membrane.capability.run_once", fake_run_once)
    log = io.StringIO()
    code = main(
        [
            "run",
            "hatch",
            "--key-dir",
            str(tmp_path / "keys"),
            "--env",
            str(env),
            "--out",
            str(tmp_path / "docs"),
        ],
        log=log,
    )
    assert code == 1
    assert "aborted: ConnectError" in log.getvalue()


def test_main_reports_missing_settings(tmp_path: Path) -> None:
    log = io.StringIO()
    assert main(["run", "hatch", "--env", str(tmp_path / "none")], log=log) == 2
    assert "MSHKN_API_URL" in log.getvalue()


def test_main_names_an_unknown_capability(tmp_path: Path) -> None:
    log = io.StringIO()
    assert main(["run", "nope", "--env", str(tmp_path / ".env")], log=log) == 2
    assert "no capability named 'nope'" in log.getvalue() and "hatch" in log.getvalue()


CAPABILITY = """---
name: {name}
depends: {depends}
postconditions:
{checks}
---

# {name}

### 1 · root say

```
Hello.
```

A reply.

## Repair

- build: `check your build`
- refused: `check your inbox`
"""


def _capability(directory: Path, name: str, depends: str = "[]", checks: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        CAPABILITY.format(name=name, depends=depends, checks=checks or "  - authentication")
    )


def test_main_reports_a_cycle_before_it_hatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from membrane.capabilities import catalog

    caps = tmp_path / "capabilities"
    _capability(caps, "a", depends="[b]")
    _capability(caps, "b", depends="[a]")
    monkeypatch.setattr("membrane.capability.catalog", lambda: catalog(caps))
    log = io.StringIO()
    assert main(["run", "a", "--env", str(tmp_path / "none")], log=log) == 2
    assert "cycle: a -> b -> a" in log.getvalue()


def test_main_reports_a_postcondition_no_one_wrote_before_it_hatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from membrane.capabilities import catalog

    caps = tmp_path / "capabilities"
    _capability(caps, "a", checks="  - authentication\n  - reads_the_mind")
    monkeypatch.setattr("membrane.capability.catalog", lambda: catalog(caps))
    log = io.StringIO()
    assert main(["run", "a", "--env", str(tmp_path / "none")], log=log) == 2
    assert "a names postconditions no one wrote: reads_the_mind" in log.getvalue()
    assert "nothing_by_hand" in log.getvalue()


def test_main_refuses_a_model_for_a_capability_that_does_not_hatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brain a dependent forks runs the model and effort of its hatch, written
    into `/brain/.env` then; a `--model` here would be recorded and never spoken."""
    from membrane.capabilities import catalog

    caps = tmp_path / "capabilities"
    _capability(caps, "hatch")
    _capability(caps, "security", depends="[hatch]")  # the inline list form
    monkeypatch.setattr("membrane.capability.catalog", lambda: catalog(caps))
    log = io.StringIO()
    code = main(
        ["run", "security", "--model", "claude-sonnet-5", "--env", str(tmp_path / "none")], log=log
    )
    assert code == 2
    assert "security starts from hatch's promotion" in log.getvalue()
    assert (
        "--model, --effort, --base-url and --body-extra belong to a capability that hatches"
        in log.getvalue()
    )

    log_base_url = io.StringIO()
    code = main(
        [
            "run",
            "security",
            "--base-url",
            "https://ai-gateway.vercel.sh",
            "--env",
            str(tmp_path / "none"),
        ],
        log=log_base_url,
    )
    assert code == 2
    assert "security starts from hatch's promotion" in log_base_url.getvalue()

    log_body_extra = io.StringIO()
    code = main(
        [
            "run",
            "security",
            "--body-extra",
            '{"providerOptions": {"gateway": {"only": ["anthropic"]}}}',
            "--env",
            str(tmp_path / "none"),
        ],
        log=log_body_extra,
    )
    assert code == 2
    assert "security starts from hatch's promotion" in log_body_extra.getvalue()


def test_main_writes_the_runs_key_where_key_dir_says_and_the_run_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private key lives on the operator's machine, never under `docs/`: a
    dependent's `run_once` reads it back from the directory `run.json` names."""
    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    api = FakeApi(recipes=[{"recipe_id": "rcp-pre"}])
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=http://api\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n"
    )
    key_dir = tmp_path / "keys"
    out = tmp_path / "docs"
    code = main(
        [
            *("run", "hatch"),
            *("--env", str(env)),
            *("--out", str(out)),
            *("--date", "2026-09-09"),
            *("--hatch", str(_stub_hatch(tmp_path))),
            *("--key-dir", str(key_dir)),
        ],
        log=io.StringIO(),
    )
    assert code == 1  # the fake answers every command with a root reply; the checks fail honestly
    assert (key_dir / "id").exists() and (key_dir / "id.pub").exists()
    run_doc = json.loads((out / "hatch" / "2026-09-09-run-1" / "run.json").read_text())
    assert run_doc["key_dir"] == str(key_dir)
    assert run_doc["pubkey"] == (key_dir / "id.pub").read_text().strip()


def test_main_promote_reports_missing_settings(tmp_path: Path) -> None:
    log = io.StringIO()
    code = main(
        ["promote", "hatch", str(tmp_path / "run"), "--env", str(tmp_path / "none")], log=log
    )
    assert code == 2
    assert "MSHKN_API_URL" in log.getvalue()


def test_main_promote_runs_through_a_mocked_transport_and_writes_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from membrane.capability import promotion_path

    out = tmp_path / "docs"
    run_dir = out / "hatch" / "2026-09-12-run-9"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {"commit": "abc", "dirty": False},
                "hatched": {
                    "rule_id": "ir_1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-0",
                    "ingress_url": "u",
                    "server_id": None,
                },
                "key_dir": "/keys/hatch",
                "pubkey": "ssh-ed25519 AAAA mike",
                "model": "claude-opus-5",
                "default_effort": None,
                "reasks": [],
                "started_from": "hatch",
            }
        )
    )
    (run_dir / "final-list.json").write_text(json.dumps({"proposals": []}))
    checkpoints = [{"id": "ck-b", "label": "brain", "created_at": "t", "recipe_id": "rcp-brain"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            label = request.url.params.get("label")
            return httpx.Response(
                200, json=[c for c in checkpoints if not label or c["label"] == label]
            )
        if request.url.path.endswith("/fork"):
            return httpx.Response(200, json={"computer_id": "comp-1", "checkpoint_id": "x"})
        if request.url.path == "/computers/comp-1/checkpoint":
            return httpx.Response(200, json={"checkpoint_id": "promoted-brain"})
        return httpx.Response(200, json={"status": "deleted"})

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )
    env = tmp_path / ".env"
    env.write_text("MSHKN_API_URL=http://api\nMSHKN_API_KEY=k\n")
    log = io.StringIO()
    code = main(["promote", "hatch", str(run_dir), "--env", str(env), "--out", str(out)], log=log)
    assert code == 0
    assert promotion_path(out, "hatch").exists()


def test_main_promote_reports_a_failed_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`<run-dir>/run.json` does not exist: `promote` raises (an `OSError`),
    `_promote` catches it and reports rather than letting it escape `main`."""
    monkeypatch.setattr(
        "membrane.capability.transport_for",
        lambda _url: httpx.MockTransport(lambda _request: httpx.Response(200, json=[])),
    )
    env = tmp_path / ".env"
    env.write_text("MSHKN_API_URL=http://api\nMSHKN_API_KEY=k\n")
    log = io.StringIO()
    code = main(
        [
            "promote",
            "hatch",
            str(tmp_path / "missing-run"),
            "--env",
            str(env),
            "--out",
            str(tmp_path / "docs"),
        ],
        log=log,
    )
    assert code == 1
    assert "promote failed" in log.getvalue()


def test_main_promote_reports_an_api_error_instead_of_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live `capability promote` that hits a 5xx (or a connection error) must
    exit 1 with a message, not escape `main` as an unhandled `httpx.HTTPError`."""
    out = tmp_path / "docs"
    run_dir = out / "hatch" / "2026-09-12-run-9"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "membrane": {"commit": "abc", "dirty": False},
                "hatched": {
                    "rule_id": "ir_1",
                    "key_id": "key-1",
                    "recipe_id": "rcp-brain",
                    "checkpoint_id": "ck-0",
                    "ingress_url": "u",
                    "server_id": None,
                },
                "key_dir": "/keys/hatch",
                "pubkey": "ssh-ed25519 AAAA mike",
                "model": "claude-opus-5",
                "default_effort": None,
                "reasks": [],
                "started_from": "hatch",
            }
        )
    )
    (run_dir / "final-list.json").write_text(json.dumps({"proposals": []}))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkpoints":
            return httpx.Response(500, json={"detail": "boom"})
        raise AssertionError(request.url.path)

    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(handler)
    )
    env = tmp_path / ".env"
    env.write_text("MSHKN_API_URL=http://api\nMSHKN_API_KEY=k\n")
    log = io.StringIO()
    code = main(["promote", "hatch", str(run_dir), "--env", str(env), "--out", str(out)], log=log)
    assert code == 1
    assert "promote failed" in log.getvalue()


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


def test_membrane_version_names_the_commit_and_whether_the_tree_was_dirty(
    tmp_path: Path,
) -> None:
    version = membrane_version()
    assert len(version["commit"]) == 40 and isinstance(version["dirty"], bool)
    # outside a repository there is no commit to name
    assert membrane_version(tmp_path) == {"commit": None, "dirty": None}


# ---------------------------------------------------------------- a capability's module


SERVED = """---
name: served
depends: []
postconditions:
  - authentication
  - served_page
---

# served

### 1 · root say

```
the page is at {url}
```

A reply that read it.

### 2 · root list

The final state.

## Repair

- build: `check your build`
- refused: `check your inbox`
"""

PREPARE_MODULE = '''"""served's own apparatus: what its run needs, and what it takes away."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from membrane.postconditions import CHECKS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

FLAG = Path("__FLAG__")
EXTRA = __EXTRA__


def served_page(judged: Any) -> dict[str, Any]:
    """What the run was spoken with is what this check is judged on."""
    return {"ok": judged.context.get("url") == "http://page", "evidence": {}}


CHECKS["served_page"] = served_page


@contextlib.asynccontextmanager
async def prepare(doors: Any, log: Any) -> AsyncIterator[Mapping[str, str]]:
    log.write(f"the page is served ({len(doors.sent)} commands so far)\\n")
    try:
        yield EXTRA
    finally:
        FLAG.write_text("torn down")
'''

CHECK_MODULE = """from membrane.postconditions import CHECKS


def secret_page(judged):
    return {"ok": True, "evidence": "read"}


CHECKS["secret_page"] = secret_page
"""


def _served(tmp_path: Path, extra: str) -> Path:
    """A capability whose module prepares the context its one row templates."""
    from membrane.capabilities import load

    caps = tmp_path / "capabilities"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / "served.md").write_text(SERVED)
    (caps / "served.py").write_text(
        PREPARE_MODULE.replace("__FLAG__", str(tmp_path / "exited")).replace("__EXTRA__", extra)
    )
    assert load(caps / "served.md").module == caps / "served.py"
    return caps / "served.md"


def _said(api: FakeApi) -> list[str]:
    return [
        base64.b64decode(body["exec"].removeprefix("membrane root say ")).decode()
        for method, path, body in api.requests
        if method == "POST"
        and path == "/checkpoints/fork"
        and body["exec"].startswith("membrane root say ")
    ]


async def test_run_once_speaks_what_the_modules_prepare_yields_and_exits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability's module is its own apparatus (capabilities design §4): the
    driver enters `prepare` after hatching, merges what it yields over `key`, and
    exits it after the final listing, so what it started is gone before judging."""
    from membrane.capabilities import load, load_module

    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    monkeypatch.setitem(CHECKS, "served_page", lambda _judged: {"ok": False, "evidence": {}})
    capability = load(_served(tmp_path, '{"url": "http://page"}'))
    api = FakeApi()
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    log = io.StringIO()
    summary = await run_once(
        _settings(),
        capability,
        tmp_path / "run",
        AutoApprover(),
        hatch_script=_stub_hatch(tmp_path),
        key_dir=tmp_path / "keys",
        keep=False,
        log=log,
        out=tmp_path,
        module=load_module(capability),
    )
    assert _said(api) == ["the page is at http://page"]
    assert summary["capability"] == "served"
    assert set(summary["postconditions"]) == {"authentication", "served_page"}
    # the checks are judged on the context the rows were spoken with, module and all
    assert summary["postconditions"]["served_page"]["ok"] is True
    assert (tmp_path / "exited").read_text() == "torn down"
    assert "the page is served (0 commands so far)" in log.getvalue()


async def test_a_modules_prepare_may_not_take_the_runs_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`key` is the run's own: the hatcher's public key line, which the rows name
    and the checks judge. A module that sets it is refused before a row is spoken."""
    from membrane.capabilities import load, load_module

    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    monkeypatch.setitem(CHECKS, "served_page", lambda _judged: {"ok": False, "evidence": {}})
    capability = load(_served(tmp_path, '{"url": "http://page", "key": "not mine"}'))
    api = FakeApi()
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )
    with pytest.raises(RuntimeError, match=r"served\.py's prepare must not set 'key'"):
        await run_once(
            _settings(),
            capability,
            tmp_path / "run",
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
            module=load_module(capability),
        )
    assert _said(api) == []
    assert json.loads((tmp_path / "run" / "run.json").read_text())["ok"] is False


def test_main_loads_the_module_before_it_checks_the_postcondition_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check only this capability needs is registered by its module at import,
    so `_run` must load the module before it validates the names it reads."""
    from membrane.capabilities import catalog

    caps = tmp_path / "capabilities"
    _capability(caps, "a", checks="  - secret_page")
    (caps / "a.py").write_text(CHECK_MODULE)
    monkeypatch.setattr("membrane.capability.catalog", lambda: catalog(caps))
    log = io.StringIO()
    assert "secret_page" not in CHECKS
    assert main(["run", "a", "--env", str(tmp_path / "none")], log=log) == 2
    assert CHECKS["secret_page"].__name__ == "secret_page"
    assert "postconditions no one wrote" not in log.getvalue()
    assert "MSHKN_API_URL" in log.getvalue()  # it got as far as the settings


def test_main_reports_a_module_that_will_not_import_before_it_hatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module is the operator's own code and it is imported before anything is
    hatched; what it raises on the way in is a line naming the file, not a
    traceback out of `main`."""
    from membrane.capabilities import catalog

    caps = tmp_path / "capabilities"
    _capability(caps, "a")
    (caps / "a.py").write_text('raise RuntimeError("boom")\n')
    monkeypatch.setattr("membrane.capability.catalog", lambda: catalog(caps))
    log = io.StringIO()
    assert main(["run", "a", "--env", str(tmp_path / "none")], log=log) == 2
    assert "a.py could not be imported: boom" in log.getvalue()


async def test_a_run_that_fails_mid_speak_still_exits_the_modules_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module's scaffolding is inside the run's `try`: a row that raises tears
    down what the module started, and the error is still the run's."""
    from membrane.capabilities import load, load_module

    monkeypatch.setenv("HATCH_ENV_OUT", str(tmp_path / "env.txt"))
    monkeypatch.setitem(CHECKS, "served_page", lambda _judged: {"ok": False, "evidence": {}})
    capability = load(_served(tmp_path, '{"url": "http://page"}'))
    api = FakeApi()
    monkeypatch.setattr(
        "membrane.capability.transport_for", lambda _url: httpx.MockTransport(api.handler)
    )

    async def boom(*args: Any, **kwargs: Any) -> tuple[list[Turn], dict[str, Any] | None]:
        raise RuntimeError("boom")

    monkeypatch.setattr("membrane.capability.speak", boom)
    with pytest.raises(RuntimeError, match="boom"):
        await run_once(
            _settings(),
            capability,
            tmp_path / "run",
            AutoApprover(),
            hatch_script=_stub_hatch(tmp_path),
            key_dir=tmp_path / "keys",
            keep=False,
            log=io.StringIO(),
            out=tmp_path,
            module=load_module(capability),
        )
    assert (tmp_path / "exited").read_text() == "torn down"
    assert json.loads((tmp_path / "run" / "run.json").read_text())["error"] == "RuntimeError: boom"


# ---------------------------------------------------------------- root provides (§7.2)


def test_paths_in_reads_a_fenced_path_first_and_inline_code_second() -> None:
    """Spec §7.2 asks for the path in a fenced block; an inline `/path` is the
    same answer (security plan decision 2). The proposal documents the membrane
    appends after the reply are not read, so an entrypoint's path is not one."""
    from membrane.capability import paths_in

    fenced = "Put the token at\n\n```\n/verb/token\n```\n\nthen say provide."
    assert paths_in(fenced) == ["/verb/token"]
    assert paths_in("Put it at `/verb/secrets/token`, please.") == ["/verb/secrets/token"]
    both = "Write `/etc/x` if you like, but the verb reads\n```\n/verb/token\n```"
    assert paths_in(both) == ["/verb/token"]
    two = "```\n/verb/a\n```\nand\n```\n/verb/b\n```\nand again `/verb/a`"
    assert paths_in(two) == ["/verb/a", "/verb/b"]
    script = "```bash\ncat /verb/token\n```"  # a block that is not a bare path
    assert paths_in(script) == []
    trailing = 'I need it.\nproposal p-3\n{"verb": {"entrypoint": "`/verb/run.sh`"}}'
    assert paths_in(trailing) == []
    assert paths_in("no path here") == []
    # a backtick pair inside a fenced script is shell command substitution, not a
    # path root should read; the inline fallback must not see it either
    substitution = "```bash\ncat `/verb/token`\n```"
    assert paths_in(substitution) == []


def test_unprovided_verbs_reads_ready_entries_with_names_root_has_not_provided() -> None:
    from membrane.capability import unprovided_verbs

    listing = {
        "catalog": {
            "verify_ssh": {
                "status": "ready",
                "recipe_id": "r1",
                "chain": "verb/verify_ssh",
                "requires": [],
                "provided": [],
            },
            "secret_page": {
                "status": "ready",
                "recipe_id": "r2",
                "chain": "verb/secret_page",
                "requires": [{"kind": "secret", "name": "page_token"}],
                "provided": [],
            },
            "later": {
                "status": "building",
                "recipe_id": "r3",
                "chain": "verb/later",
                "requires": [{"kind": "secret", "name": "page_token"}],
                "provided": [],
            },
            "done": {
                "status": "ready",
                "recipe_id": "r4",
                "chain": "verb/done",
                "requires": [{"kind": "secret", "name": "a"}, {"kind": "secret", "name": "b"}],
                "provided": ["a"],
            },
        }
    }
    assert unprovided_verbs(listing) == [
        ("secret_page", "page_token", "r2", "verb/secret_page"),
        ("done", "b", "r4", "verb/done"),
    ]


async def test_provision_creates_from_the_recipe_when_the_chain_is_empty_and_records_by_name(
    tmp_path: Path,
) -> None:
    """Spec §7.2: create, upload, checkpoint under the chain, destroy, with the
    account key and outside every door. The secret is the upload's body and no
    recorded detail carries it."""
    api = FakeApi()
    doors = _doors(api, tmp_path)
    ckpt = await doors.provision(
        "secret_page", "verb/secret_page", "rcp-sp", "/verb/token", "s3cr3t-value"
    )
    assert ckpt == "ck-1"
    assert api.computers == [{"id": "comp-1", "recipe_id": "rcp-sp"}]
    assert api.uploads == [("comp-1", "/verb/token", b"s3cr3t-value")]
    assert api.created_checkpoints[0]["label"] == "verb/secret_page"
    assert [(m, p) for m, p, _ in api.requests if m == "DELETE"] == [
        ("DELETE", "/computers/comp-1")
    ]
    assert [(s.door, s.name) for s in doors.sent] == [
        ("api", "create"),
        ("api", "upload"),
        ("api", "checkpoint"),
        ("api", "destroy"),
    ]
    assert doors.sent[0].detail == {"verb": "secret_page", "from": "rcp-sp"}
    assert doors.sent[0].computer_id == "comp-1"
    assert doors.sent[1].detail == {"verb": "secret_page", "path": "/verb/token", "bytes": 12}
    assert doors.sent[2].detail == {
        "verb": "secret_page",
        "label": "verb/secret_page",
        "checkpoint_id": "ck-1",
    }
    for written in (tmp_path / "run" / "commands").iterdir():
        assert "s3cr3t-value" not in written.read_text(), written


async def test_provision_forks_the_chain_head_when_there_is_one(tmp_path: Path) -> None:
    api = FakeApi(
        checkpoints=[
            {
                "id": "ck-old",
                "label": "verb/secret_page",
                "created_at": "2026-09-13T00:00:00",
                "recipe_id": "rcp-sp",
            }
        ]
    )
    doors = _doors(api, tmp_path)
    await doors.provision("secret_page", "verb/secret_page", "rcp-sp", "/verb/token", "t")
    assert api.computers == [{"id": "comp-1", "from": "ck-old"}]
    assert doors.sent[0].detail == {"verb": "secret_page", "from": "ck-old"}


async def test_provision_destroys_the_computer_when_the_upload_fails(tmp_path: Path) -> None:
    api = FakeApi()
    original = api.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/upload"):
            api.requests.append((request.method, request.url.path, None))
            return httpx.Response(500, json={"detail": "no"})
        return original(request)

    transport = httpx.MockTransport(handler)
    authed = httpx.AsyncClient(
        base_url="http://api", headers={"Authorization": "Bearer k"}, transport=transport
    )
    doors = Doors(authed, authed, "rule-1", Record(tmp_path / "run"))
    with pytest.raises(httpx.HTTPStatusError):
        await doors.provision("secret_page", "verb/secret_page", "rcp-sp", "/verb/token", "t")
    assert ("DELETE", "/computers/comp-1", None) in api.requests
    assert [s.name for s in doors.sent] == ["create", "upload", "destroy"]


async def test_provision_destroy_failing_does_not_replace_the_uploads_own_exception(
    tmp_path: Path,
) -> None:
    """The destroy in `provision`'s `finally` must be suppressed like
    `copy_label`'s (Minor 2): unsuppressed, a transport error raised while
    cleaning up after a failed upload would replace the upload's own
    `HTTPStatusError` and hide what actually went wrong."""
    api = FakeApi()
    original = api.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/upload"):
            api.requests.append((request.method, request.url.path, None))
            return httpx.Response(500, json={"detail": "no"})
        if request.method == "DELETE":
            api.requests.append((request.method, request.url.path, None))
            raise httpx.ConnectError("down")
        return original(request)

    transport = httpx.MockTransport(handler)
    authed = httpx.AsyncClient(
        base_url="http://api", headers={"Authorization": "Bearer k"}, transport=transport
    )
    doors = Doors(authed, authed, "rule-1", Record(tmp_path / "run"))
    with pytest.raises(httpx.HTTPStatusError):
        await doors.provision("secret_page", "verb/secret_page", "rcp-sp", "/verb/token", "t")
    assert ("DELETE", "/computers/comp-1", None) in api.requests
    assert [s.name for s in doors.sent] == ["create", "upload", "destroy"]


async def test_after_a_row_settles_root_provides_where_the_reply_said(tmp_path: Path) -> None:
    """Spec §7.2: a driver rule keyed on state. Row 11's settle finds a ready verb
    with an unprovided name and a path in the reply, places the run's token there
    on the verb's chain, says provide, and row 12 then reads the page."""
    doors = FakeDoors(secret=True)
    key_dir, pubkey = _keys(tmp_path)
    # the door and the grant hatch's promotion would have left behind
    await speak(HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO())
    doors.sent.clear()
    log = io.StringIO()
    turns, final, reasks = await speak(
        SECURITY,
        doors,
        key_dir,
        {"key": pubkey, "url": "https://page/page", "token": "tok-1"},
        AutoApprover(),
        log=log,
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert [t.label for t in turns] == ["11", "12", "13"] and reasks == []
    assert doors.provided_at == [
        ("secret_page", "/verb/token", "tok-1"),
        ("secret_length", "/verb/token", "tok-1"),
    ]
    assert turns[0].provisions == [
        {
            "verb": "secret_page",
            "name": "page_token",
            "path": "/verb/token",
            "checkpoint": "ck-secret_page-provisioned",
            "result": "secret_page: page_token provided (1/1)",
        }
    ]
    assert turns[2].provisions[0]["verb"] == "secret_length"
    assert turns[1].audit["tools"][0]["status"] == "ok"
    names = [s[1] for s in doors.sent]
    assert names.count("provide") == 2 and names.count("create") == 2
    assert "provided page_token for secret_page at /verb/token" in log.getvalue()
    assert final is not None and final["catalog"]["secret_page"]["provided"] == ["page_token"]
    # the commands the placement spent belong to the row's turn
    assert len(turns[0].commands) >= 6


async def test_a_reply_with_no_path_earns_the_provide_repair_phrase(tmp_path: Path) -> None:
    doors = FakeDoors(secret=True, path_in_reply=None)
    key_dir, pubkey = _keys(tmp_path)
    await speak(HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO())
    log = io.StringIO()
    turns, _, _ = await speak(
        SECURITY,
        doors,
        key_dir,
        {"key": pubkey, "url": "https://page/page", "token": "tok-1"},
        AutoApprover(),
        log=log,
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert [t.label for t in turns][:3] == ["11", "3-repair-1", "12"]
    assert turns[1].words == "where should I put it?"
    assert doors.provided_at[0] == ("secret_page", "/verb/token", "tok-1")
    assert "no path for secret_page requires page_token; repair 1" in log.getvalue()


async def test_without_a_token_in_the_context_nothing_is_placed_and_nothing_repaired(
    tmp_path: Path,
) -> None:
    """A capability that serves nothing has no token to place: the verb stays
    unprovided, the run says so, and no repair turn is spent on it."""
    doors = FakeDoors(secret=True)
    key_dir, pubkey = _keys(tmp_path)
    await speak(HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO())
    log = io.StringIO()
    turns, _, _ = await speak(
        SECURITY,
        doors,
        key_dir,
        {"key": pubkey, "url": "https://page/page"},
        AutoApprover(),
        log=log,
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert [t.label for t in turns] == ["11", "12", "13"]
    assert doors.provided_at == [] and turns[0].provisions == []
    assert "the run has no token to place" in log.getvalue()
    assert turns[1].audit["tools"][0]["status"] == "error"


async def test_the_pilot_can_override_or_skip_a_placement(tmp_path: Path) -> None:
    doors = FakeDoors(secret=True)
    key_dir, pubkey = _keys(tmp_path)
    await speak(HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO())
    # every proposal approved; the first placement overridden, the second skipped
    stdin = io.StringIO("approve\n/verb/secrets/token\napprove\nskip\n")
    stdout = io.StringIO()
    turns, final, _ = await speak(
        SECURITY,
        doors,
        key_dir,
        {"key": pubkey, "url": "https://page/page", "token": "tok-1"},
        AskApprover(stdin, stdout),
        log=io.StringIO(),
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert doors.provided_at == [("secret_page", "/verb/secrets/token", "tok-1")]
    assert "provide secret_page page_token: path [/verb/token] | skip> " in stdout.getvalue()
    assert turns[2].provisions == []
    # A skip is the pilot's answer for the run: no repair turn is spent asking the
    # agent again, and the pilot is not prompted a second time.
    assert [t.label for t in turns] == ["11", "12", "13"]
    assert stdout.getvalue().count("provide secret_length page_token:") == 1
    assert final is not None and final["catalog"]["secret_length"]["provided"] == []


async def test_a_capability_with_no_provide_phrase_says_so_instead_of_repairing(
    tmp_path: Path,
) -> None:
    """`Repair.provide` is None for a capability that provides nothing (hatch's is).
    A requirement with no path is then logged and left where it is: there are no
    words to say, so no repair turn is spoken and none of the run's budget goes."""
    doors = FakeDoors(secret=True, path_in_reply=None)
    key_dir, pubkey = _keys(tmp_path)
    await speak(HATCH, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO())
    log = io.StringIO()
    turns, _, _ = await speak(
        replace(SECURITY, repair=HATCH.repair),
        doors,
        key_dir,
        {"key": pubkey, "url": "https://page/page", "token": "tok-1"},
        AutoApprover(),
        log=log,
    )
    # These assertions concern script rows and repairs; continuations have their own tests.
    turns = [t for t in turns if "-continue-" not in t.label]
    assert [t.label for t in turns] == ["11", "12", "13"]
    assert doors.provided_at == []
    assert "no path for secret_page requires page_token and security" in log.getvalue()
    assert "has no provide phrase" in log.getvalue()


def test_the_asking_approver_places_with_the_parsed_path_by_default() -> None:
    stdout = io.StringIO()
    assert AskApprover(io.StringIO("\n"), stdout).place("v", "n", "/verb/t") == "/verb/t"
    assert AskApprover(io.StringIO("skip\n"), stdout).place("v", "n", "/verb/t") is None
    assert AskApprover(io.StringIO("relative\n/abs\n"), stdout).place("v", "n", None) == "/abs"
    assert AskApprover(io.StringIO(""), stdout).place("v", "n", "/verb/t") is None
    assert AutoApprover().place("v", "n", "/verb/t") == "/verb/t"


async def test_success_continues_staged_hook_then_policy_before_public_rows(tmp_path: Path) -> None:
    """The failed hatch: the agent waits for the hook before proposing its policy."""
    doors = FakeDoors()
    original = doors.root_say

    async def staged(text: str) -> tuple[dict[str, Any], str]:
        if text.startswith(WORDS["2"][:30]):
            result = await original(text)
            doors.proposals.pop()  # the agent has not proposed its policy yet
            result[0]["proposals"] = result[0]["proposals"][:1]
            return result
        if text.startswith("External results:") and doors.policy["door"] == "closed":
            assert (
                text
                == 'External results: [["proposal", "p-1", "ready"]]. Continue with the request.'
            )
            doors._propose("policy", "door", policy=doors._grant([]))
        return await original(text)

    doors.root_say = staged  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    continuations: list[str] = []
    turns, final, _ = await speak(
        HATCH,
        doors,
        key_dir,
        {"key": pubkey},
        AutoApprover(),
        log=io.StringIO(),
        continuations=continuations,
    )
    assert [t.label for t in turns[:6]] == ["1", "2", "2-continue-1", "2-continue-2", "4", "5"]
    assert turns[4].audit["principal"] == "ssh:mike"
    assert final is not None and final["policy"]["door"] == "open"
    assert continuations == [
        "2-continue-1",
        "2-continue-2",
        "6-continue-1",
        "7-continue-1",
        "9-continue-1",
    ]
    assert doors.count_calls == 2  # result delivery never replays the count operation
    assert sum('"p-1", "ready"' in t.words for t in turns) == 1


@pytest.mark.parametrize("door", ["root say", "signed", "unsigned"])
async def test_continuations_preserve_origin_door_and_are_bounded(
    tmp_path: Path, door: str
) -> None:
    from membrane.capabilities import Row
    from membrane.capability import MAX_CONTINUATIONS

    doors = FakeDoors()
    seen: list[tuple[str, Any]] = []

    def propose() -> tuple[dict[str, Any], str]:
        doors._propose("prompt", "self")
        return audit_line(), "Waiting for approval."

    async def root(text: str) -> tuple[dict[str, Any], str]:
        seen.append(("root", text))
        return propose()

    async def public(payload: Any) -> tuple[dict[str, Any], str]:
        seen.append(("public", payload))
        return propose()

    doors.root_say = root  # type: ignore[method-assign]
    doors.public_say = public  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    cap = replace(HATCH, rows=(Row("request", door, "Become something.", ""),))
    log = io.StringIO()
    turns, _, _ = await speak(cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=log)
    assert len(seen) == MAX_CONTINUATIONS + 1
    assert "continuation budget exhausted" in log.getvalue()
    assert len(doors.proposals) == MAX_CONTINUATIONS + 1
    assert all(p["status"] == "applied" for p in doors.proposals)
    assert [t.label for t in turns] == ["request", *[f"request-continue-{n}" for n in range(1, 4)]]
    if door == "root say":
        assert all(route == "root" for route, _ in seen)
    else:
        assert all(route == "public" for route, _ in seen)
        assert all(("sig" in payload) == (door == "signed") for _, payload in seen)
        assert all(
            set(payload) == ({"msg", "sig"} if door == "signed" else {"msg"}) for _, payload in seen
        )


@pytest.mark.parametrize("signed", [True, False])
async def test_public_continuation_repairs_never_escalate_to_root(
    tmp_path: Path, signed: bool
) -> None:
    from membrane.capabilities import Row

    doors = FakeDoors()
    payloads: list[Any] = []

    async def public(payload: Any) -> tuple[dict[str, Any], str]:
        payloads.append(payload)
        if len(payloads) == 1:
            doors._propose("prompt", "self")
        audit = audit_line(
            door="ingress",
            principal="ssh:mike" if signed else "anonymous",
            # The repair turn acts and still does not fix it, which is the case this
            # test is about. A repair turn that calls nothing is a stall, and would
            # buy two further repairs that have nothing to do with escalation.
            tools=[{"name": "remember", "status": "ok"}] if len(payloads) > 2 else [],
        )
        if len(payloads) == 2:
            audit["stopped"] = "deadline"
        return audit, "Done."

    async def root(text: str) -> tuple[dict[str, Any], str]:
        pytest.fail("A public continuation must never acquire root authority")

    doors.public_say = public  # type: ignore[method-assign]
    doors.root_say = root  # type: ignore[method-assign]
    key_dir, pubkey = _keys(tmp_path)
    cap = replace(HATCH, rows=(Row("request", "signed" if signed else "unsigned", "Change.", ""),))
    turns, _, _ = await speak(
        cap, doors, key_dir, {"key": pubkey}, AutoApprover(), log=io.StringIO()
    )
    assert [t.label for t in turns] == ["request", "request-continue-1", "3-repair-1"]
    assert all(("sig" in payload) == signed for payload in payloads)
    assert payloads[-1]["msg"] == HATCH.repair.build
