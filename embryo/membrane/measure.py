"""The measure of the first real agent (spec §11, #101).

Root's tool, run on the operator's machine and never in the brain: hatch with a
real model, speak the liturgy through both doors, approve what root would
approve, check the seven postconditions against `list` and the verbs, write the
transcript, every command and the verdict under `docs/embryo/`, tear down.

    uv run measure --runs 3            # keys and the API from .env
    uv run measure --approve ask       # the pilot reads each proposal on stdin
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

import httpx

from membrane.config import DEFAULT_MODEL_ID, EFFORTS, parse_env
from membrane.declarations import RESERVED_NAMESPACES
from membrane.liturgy import COUNT, LITURGY
from membrane.model import add_usage, zero_usage
from membrane.principals import ANONYMOUS, ROOT, namespace_of

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

DEFAULT_BRAIN_API_URL = "https://api.mshkn.dev"
DEFAULT_OUT = Path("docs/embryo")
REQUIRED = ("MSHKN_API_URL", "MSHKN_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
OPTIONAL = ("BRAIN_API_URL",)
HATCH = Path(__file__).resolve().parents[1] / "hatch.sh"
VERIFIED = "ssh:mike"
RESERVED_TOOLS = frozenset({"remember", "try", "propose"})
POSTCONDITIONS = (
    "authentication",
    "root_unforgeable",
    "authorization",
    "page_title",
    "counter",
    "no_undeclared_capability",
    "nothing_by_hand",
)
ROOT_COMMANDS = frozenset({"say", "list", "approve", "reject"})
TURN_TIMEOUT = 330.0
TURN_WAIT = 3600.0
BUILD_TIMEOUT = 600.0
CONFLICT_INTERVAL = 3.0
BUILD_INTERVAL = 5.0
MAX_REPAIRS = 3
INT_RE = re.compile(r"-?\d+")


# ---------------------------------------------------------------- settings and cost


@dataclass(frozen=True)
class MeasureSettings:
    api_url: str
    api_key: str
    brain_api_url: str
    anthropic_api_key: str
    openai_api_key: str
    model_id: str
    effort: str | None = None


def load_measure_settings(
    env_file: Path,
    environ: Mapping[str, str],
    *,
    model_id: str | None = None,
    effort: str | None = None,
) -> MeasureSettings:
    """The operator's `.env` (git-ignored) under the environment: the four
    required keys, `BRAIN_API_URL` if the brain dials a different address."""
    values = parse_env(env_file.read_text()) if env_file.exists() else {}
    values.update({k: v for k, v in environ.items() if k in REQUIRED or k in OPTIONAL})
    missing = [name for name in REQUIRED if not values.get(name)]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}: put them in {env_file} or the environment")
    return MeasureSettings(
        api_url=values["MSHKN_API_URL"],
        api_key=values["MSHKN_API_KEY"],
        brain_api_url=values.get("BRAIN_API_URL") or DEFAULT_BRAIN_API_URL,
        anthropic_api_key=values["ANTHROPIC_API_KEY"],
        openai_api_key=values["OPENAI_API_KEY"],
        model_id=model_id or DEFAULT_MODEL_ID,
        effort=effort,
    )


@dataclass(frozen=True)
class Price:
    """USD per million tokens, from the Claude API reference (cached 2026-06-24)."""

    input: float
    output: float


PRICES = {"claude-opus-5": Price(input=5.0, output=25.0)}
CACHE_WRITE = 1.25  # of the input price
CACHE_READ = 0.1


def cost_usd(usage: Mapping[str, int], model_id: str) -> float:
    price = PRICES[model_id]
    return (
        usage.get("input_tokens", 0) * price.input
        + usage.get("cache_creation_input_tokens", 0) * price.input * CACHE_WRITE
        + usage.get("cache_read_input_tokens", 0) * price.input * CACHE_READ
        + usage.get("output_tokens", 0) * price.output
    ) / 1_000_000


# ---------------------------------------------------------------- the record


@dataclass
class Turn:
    label: str  # "1".."9", "3-repair-<k>", "9-count-<k>"
    door: str  # api | ingress | ingress-unsigned
    words: str
    audit: dict[str, Any]
    reply: str
    commands: list[int]
    approvals: list[dict[str, Any]]


@dataclass(frozen=True)
class Sent:
    n: int
    door: str
    name: str
    detail: Any
    stdout: str
    status: int
    seconds: float
    computer_id: str | None = None
    stderr: str = ""


class Record:
    """One run's evidence directory: `commands/NNN-<door>-<name>.json` for every
    command sent (every `list` included), `transcript.md`, `final-list.json`,
    `run.json`."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.commands_dir = directory / "commands"
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def command(
        self,
        door: str,
        name: str,
        detail: Any,
        stdout: str,
        status: int,
        seconds: float,
        *,
        computer_id: str | None = None,
        stderr: str = "",
    ) -> int:
        self.n += 1
        doc = {
            "n": self.n,
            "door": door,
            "command": name,
            "detail": detail,
            "status": status,
            "seconds": seconds,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "computer_id": computer_id,
            "stdout": stdout,
            "stderr": stderr,
        }
        path = self.commands_dir / f"{self.n:03d}-{door}-{name}.json"
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        return self.n

    def transcript(self, model_id: str, turns: list[Turn]) -> Path:
        lines = [f"# Transcript: {self.directory.name}", "", f"Model: `{model_id}`.", ""]
        for turn in turns:
            lines += [f"## Turn {turn.label} ({turn.door})", ""]
            lines += ["**Words:**", "", "```", turn.words, "```", ""]
            lines += ["**Audit:**", "", "```json", json.dumps(turn.audit, indent=1), "```", ""]
            lines += ["**Reply:**", "", "```", turn.reply.rstrip("\n"), "```", ""]
            if turn.approvals:
                lines += ["**Approvals:**", ""]
                lines += [f"- `{a['id']}` {a['decision']}: {a['result']}" for a in turn.approvals]
                lines.append("")
            commands = ", ".join(f"{n:03d}" for n in turn.commands)
            lines += [f"Commands: {commands}", ""]
        path = self.directory / "transcript.md"
        path.write_text("\n".join(lines))
        return path

    def final_list(self, listing: dict[str, Any]) -> Path:
        path = self.directory / "final-list.json"
        path.write_text(json.dumps(listing, indent=1, sort_keys=True) + "\n")
        return path

    def summary(self, doc: dict[str, Any]) -> Path:
        path = self.directory / "run.json"
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        return path


# ---------------------------------------------------------------- the doors


class DoorsApi(Protocol):
    sent: list[Any]

    async def root_say(self, text: str) -> tuple[dict[str, Any], str]: ...

    async def public_say(self, payload: Any) -> tuple[dict[str, Any], str]: ...

    async def root(self, *argv: str) -> str: ...

    async def listing(self) -> dict[str, Any]: ...

    async def wait_builds(self) -> dict[str, Any]: ...

    async def check_computer(self, computer_id: str) -> dict[str, Any]: ...

    async def recipes(self) -> set[str]: ...


@dataclass(frozen=True)
class Hatched:
    ingress_url: str
    rule_id: str
    key_id: str
    recipe_id: str
    checkpoint_id: str
    server_id: str | None = None


def b64(obj: Any) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return base64.b64encode(text.encode()).decode()


def split_output(out: str) -> tuple[dict[str, Any], str]:
    first, _, rest = out.partition("\n")
    if not first.startswith("audit "):
        raise RuntimeError(f"a turn's output does not start with an audit line: {out[:200]!r}")
    return dict(json.loads(first[len("audit ") :])), rest


def transport_for(api_url: str) -> httpx.AsyncBaseTransport | None:  # noqa: ARG001 — the tests swap it
    """The transport of the two clients; tests replace it with a mock."""
    return None


class Doors:
    """Root's door (the account key, fork by label) and the public one (the
    ingress rule, no credential), both dialled at the API's address."""

    def __init__(
        self,
        api: httpx.AsyncClient,
        public: httpx.AsyncClient,
        rule_id: str,
        record: Record,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api, self.public, self.rule_id, self.record = api, public, rule_id, record
        self.sleep, self.now = sleep, now
        self.sent: list[Sent] = []

    def _record(
        self,
        door: str,
        name: str,
        detail: Any,
        stdout: str,
        status: int,
        seconds: float,
        *,
        computer_id: str | None = None,
        stderr: str = "",
    ) -> int:
        n = self.record.command(
            door, name, detail, stdout, status, seconds, computer_id=computer_id, stderr=stderr
        )
        self.sent.append(Sent(n, door, name, detail, stdout, status, seconds, computer_id, stderr))
        return n

    async def _turn(
        self, door: str, name: str, detail: Any, send: Callable[[], Awaitable[httpx.Response]]
    ) -> str:
        """Send until it is not a 409 (a turn in progress), then require exit 0."""
        deadline = self.now() + TURN_TIMEOUT
        started = self.now()
        while True:
            response = await send()
            if response.status_code == 409 and self.now() < deadline:
                await self.sleep(CONFLICT_INTERVAL)
                continue
            break
        seconds = self.now() - started
        if response.status_code != 200:
            self._record(door, name, detail, response.text, response.status_code, seconds)
            raise RuntimeError(f"{door} {name}: HTTP {response.status_code} {response.text[:500]}")
        body = response.json()
        stdout = str(body.get("exec_stdout") or "")
        stderr = str(body.get("exec_stderr") or "")
        self._record(
            door,
            name,
            detail,
            stdout,
            response.status_code,
            seconds,
            computer_id=body.get("computer_id"),
            stderr=stderr,
        )
        if body.get("exec_exit_code") != 0:
            raise RuntimeError(
                f"{door} {name} on {body.get('computer_id')}: exit {body.get('exec_exit_code')}\n"
                f"stdout: {stdout[-1000:]}\nstderr: {stderr[-3000:]}"
            )
        return stdout

    async def root(self, *argv: str) -> str:
        command = "membrane root " + " ".join(argv)
        detail: Any = argv[1:] if argv[0] != "say" else base64.b64decode(argv[1]).decode()

        async def send() -> httpx.Response:
            return await self.api.post(
                "/checkpoints/fork",
                json={
                    "label": "brain",
                    "exec": command,
                    "self_destruct": True,
                    "exclusive": "error_on_conflict",
                },
                timeout=TURN_TIMEOUT,
            )

        return await self._turn("api", argv[0], detail, send)

    async def await_turn(self, turn: int) -> tuple[dict[str, Any], str]:
        """`list` until the turn is in the window (relay design §7): the reply and the
        closing audit line live there, written by the fork that closed the turn."""
        deadline = self.now() + TURN_WAIT
        while True:
            listing = await self.listing()
            for entry in listing["window"]:
                if entry["turn"] == turn:
                    return dict(entry["audit"]), str(entry["output"])
            if self.now() >= deadline:
                raise RuntimeError(f"turn {turn} did not close within {TURN_WAIT:.0f} s")
            await self.sleep(BUILD_INTERVAL)

    async def _spoken(self, out: str) -> tuple[dict[str, Any], str]:
        audit, rest = split_output(out)
        if "job" not in audit:
            if "queued" in audit:
                raise RuntimeError("a turn was still pending when the next was spoken")
            return audit, rest
        return await self.await_turn(int(audit["turn"]))

    async def root_say(self, text: str) -> tuple[dict[str, Any], str]:
        return await self._spoken(await self.root("say", b64(text)))

    async def public_say(self, payload: Any) -> tuple[dict[str, Any], str]:
        async def send() -> httpx.Response:
            return await self.public.post(
                f"/ingress/{self.rule_id}",
                json={"b64": b64(payload)},
                headers={"content-type": "application/json"},
                timeout=TURN_TIMEOUT,
            )

        return await self._spoken(await self._turn("ingress", "say", payload, send))

    async def listing(self) -> dict[str, Any]:
        return dict(json.loads(await self.root("list")))

    async def wait_builds(self) -> dict[str, Any]:
        """`list` until no verb is `building`, or the build timeout passes."""
        deadline = self.now() + BUILD_TIMEOUT
        while True:
            listing = await self.listing()
            building = [n for n, e in listing["catalog"].items() if e["status"] == "building"]
            if not building or self.now() >= deadline:
                return listing
            await self.sleep(BUILD_INTERVAL)

    async def check_computer(self, computer_id: str) -> dict[str, Any]:
        """Whether a verb's computer is gone (self-destructed) and what its exec log holds."""
        status = await self.api.get(f"/computers/{computer_id}/status")
        log = await self.api.get(f"/computers/{computer_id}/exec_log")
        body = log.json() if log.status_code == 200 else {}
        return {
            "computer_id": computer_id,
            "gone": status.status_code == 404,
            "stdout": body.get("stdout"),
            "exit_code": body.get("exit_code"),
        }

    async def recipes(self) -> set[str]:
        response = await self.api.get("/recipes")
        response.raise_for_status()
        return {str(r["recipe_id"]) for r in response.json()}

    async def checkpoints(self, label: str | None = None) -> list[dict[str, Any]]:
        params = {"label": label} if label else {}
        response = await self.api.get("/checkpoints", params=params)
        response.raise_for_status()
        return [dict(c) for c in response.json()]

    async def teardown(self, hatched: Hatched, listing: dict[str, Any] | None) -> None:
        """The account as the run found it, best effort: the scripted model's server
        (if any), the door, the key, every checkpoint on `brain`, on a verb chain or
        from a proposal's recipe, then the recipes. A failing delete does not stop
        the ones after it."""

        async def drop(path: str) -> None:
            with suppress(httpx.HTTPError):
                await self.api.delete(path)

        recipes = {hatched.recipe_id}
        if listing:
            recipes.update(p["recipe_id"] for p in listing["proposals"] if p.get("recipe_id"))
        if hatched.server_id is not None:
            await drop(f"/computers/{hatched.server_id}")
        await drop(f"/ingress_rules/{hatched.rule_id}")
        await drop(f"/keys/{hatched.key_id}")
        try:
            checkpoints = await self.checkpoints()
        except httpx.HTTPError:
            checkpoints = []
        for ckpt in checkpoints:
            label = ckpt.get("label") or ""
            if label == "brain" or label.startswith("verb/") or ckpt.get("recipe_id") in recipes:
                await drop(f"/checkpoints/{ckpt['id']}")
        for recipe_id in recipes:
            await drop(f"/recipes/{recipe_id}")


# ---------------------------------------------------------------- keys, hatching, approvers


def new_key(key_dir: Path) -> str:
    """An ed25519 key named `mike`, the way a person's key names its owner;
    returns the public key line the liturgy's turn 2 carries."""
    key_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "mike", "-f", str(key_dir / "id")],
        check=True,
        capture_output=True,
    )
    return (key_dir / "id.pub").read_text().strip()


def sign(key_dir: Path, message: str) -> dict[str, str]:
    msg = key_dir / "msg"
    msg.write_bytes(message.encode())
    sig = key_dir / "msg.sig"
    sig.unlink(missing_ok=True)
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key_dir / "id"), "-n", "mshkn", str(msg)],
        check=True,
        capture_output=True,
    )
    return {"msg": message, "sig": base64.b64encode(sig.read_bytes()).decode()}


def membrane_version(where: Path | None = None) -> dict[str, Any]:
    """The commit the membrane was built from, and whether the tree had uncommitted
    changes: hatch.sh builds the wheel from the working tree, so the evidence must
    say which code spoke."""
    cwd = where or HATCH.parent
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "."],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(status.strip())}


def hatch(settings: MeasureSettings, script: Path, *, log: TextIO) -> Hatched:
    """`embryo/hatch.sh` with the real model; the keys reach only the brain's `.env`."""
    env = {
        **os.environ,
        "MSHKN_API_URL": settings.api_url,
        "MSHKN_API_KEY": settings.api_key,
        "BRAIN_API_URL": settings.brain_api_url,
        "MEMBRANE_MODEL": "anthropic",
        "MEMBRANE_MODEL_ID": settings.model_id,
        "MEMBRANE_EFFORT": settings.effort or "",
        "ANTHROPIC_API_KEY": settings.anthropic_api_key,
        "OPENAI_API_KEY": settings.openai_api_key,
    }
    proc = subprocess.run(
        [str(script)], env=env, capture_output=True, text=True, timeout=900, check=False
    )
    log.write(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"hatch failed ({proc.returncode}): {proc.stderr[-2000:]}")
    return Hatched(**json.loads(proc.stdout.strip().splitlines()[-1]))


class Approver(Protocol):
    def decide(self, proposal: dict[str, Any]) -> str | None:
        """None approves; a string rejects with that reason."""


class AutoApprover:
    """Approves every pending proposal; the membrane's invariants are the guard."""

    def decide(self, proposal: dict[str, Any]) -> str | None:  # noqa: ARG002
        return None


class AskApprover:
    """The pilot: prints the proposal whole and reads `approve` or `reject <reason>`."""

    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self.stdin, self.stdout = stdin, stdout

    def decide(self, proposal: dict[str, Any]) -> str | None:
        self.stdout.write(json.dumps(proposal, indent=1) + "\n")
        while True:
            self.stdout.write(f"{proposal['id']}: approve | reject <reason>> ")
            self.stdout.flush()
            line = self.stdin.readline()
            if not line:
                return "no pilot"
            answer = line.strip()
            if answer == "approve":
                return None
            if answer.startswith("reject"):
                return answer[len("reject") :].strip() or "rejected"


# ---------------------------------------------------------------- the liturgy


async def speak_liturgy(
    doors: DoorsApi, key_dir: Path, pubkey: str, approver: Approver, *, log: TextIO
) -> list[Turn]:
    """The ten turns of spec §9, each followed by approvals, a wait for builds
    and at most MAX_REPAIRS rounds of turn 3 when a build fails."""
    turns: list[Turn] = []

    def mark() -> int:
        return len(doors.sent)

    def spent(since: int) -> list[int]:
        """The command numbers sent since `since` (commands are numbered from 1, in order)."""
        return list(range(since + 1, len(doors.sent) + 1))

    async def decide(turn: Turn, proposals: list[dict[str, Any]]) -> None:
        """Approve or reject each proposal, verbs before the policies and prompts
        that may name them (a door policy is refused until its hook is in the
        catalog, §10.6), recording every result."""
        order = {"verb": 0, "policy": 1, "prompt": 2}
        for proposal in sorted(proposals, key=lambda p: order.get(str(p.get("kind")), 3)):
            reason = approver.decide(proposal)
            if reason is None:
                result = await doors.root("approve", proposal["id"])
                decision = "approve"
            else:
                result = await doors.root("reject", proposal["id"], b64(reason))
                decision = f"reject: {reason}"
            turn.approvals.append(
                {"id": proposal["id"], "decision": decision, "result": result.rstrip("\n")}
            )
            log.write(f"  {proposal['id']} {decision}: {result.rstrip()}\n")

    async def approve_pending(turn: Turn) -> dict[str, Any]:
        """Decide every pending proposal, wait for the builds, and give whatever was
        refused one more chance once the builds are in; returns the listing."""
        since = mark()
        listing = await doors.listing()
        await decide(turn, [p for p in listing["proposals"] if p["status"] == "pending"])
        listing = await doors.wait_builds()
        refused = {a["id"] for a in turn.approvals if "refused" in a["result"]}
        again = [p for p in listing["proposals"] if p["status"] == "pending" and p["id"] in refused]
        if again:
            await decide(turn, again)
            listing = await doors.wait_builds()
        turn.commands += spent(since)
        return listing

    def unfinished(turn: Turn) -> bool:
        """Ended on the deadline, the cap or the token budget without proposing: the
        trial it started is in the inbox, and turn 3 lets it finish (liturgy turn 3)."""
        stopped = turn.audit.get("stopped")
        return stopped in ("deadline", "cap", "max_tokens") and not turn.audit.get("proposals")

    async def settle(turn: Turn) -> None:
        """Approvals, builds, and at most MAX_REPAIRS rounds of turn 3 for a failed
        build or a turn that ran out before proposing."""
        listing = await approve_pending(turn)
        current = turn
        repairs = 0
        while repairs < MAX_REPAIRS:
            failed = sorted(n for n, e in listing["catalog"].items() if e["status"] == "failed")
            if not failed and not unfinished(current):
                return
            repairs += 1
            why = f"build failed for {', '.join(failed)}" if failed else "the turn ran out"
            log.write(f"  {why}; turn 3, repair {repairs}\n")
            current = await root_turn(f"3-repair-{repairs}", LITURGY[3])
            listing = await approve_pending(current)

    async def root_turn(label: str, words: str) -> Turn:
        since = mark()
        log.write(f"Turn {label} (root): {words[:80]}\n")
        audit, reply = await doors.root_say(words)
        turn = Turn(label, "api", words, audit, reply, spent(since), [])
        turns.append(turn)
        calls, stopped = audit.get("model_calls", 0), audit.get("stopped")
        log.write(f"  {calls} model calls, {stopped}; {reply.strip()[:120]}\n")
        return turn

    async def public_turn(label: str, words: str, *, signed: bool) -> Turn:
        since = mark()
        door = "ingress" if signed else "ingress-unsigned"
        log.write(f"Turn {label} ({door}): {words[:80]}\n")
        payload = sign(key_dir, words) if signed else {"msg": words}
        audit, reply = await doors.public_say(payload)
        turn = Turn(label, door, words, audit, reply, spent(since), [])
        turns.append(turn)
        log.write(f"  principal {audit.get('principal')}; {reply.strip()[:120]}\n")
        return turn

    await settle(await root_turn("1", LITURGY[1]))
    await settle(await root_turn("2", LITURGY[2].format(key=pubkey)))
    await public_turn("4", LITURGY[4], signed=True)
    await public_turn("5", LITURGY[4], signed=False)
    await settle(await public_turn("6", LITURGY[6], signed=True))
    await settle(await public_turn("7", LITURGY[7], signed=True))
    await public_turn("8", LITURGY[8], signed=True)
    await settle(await public_turn("9", LITURGY[9], signed=True))
    await public_turn("9-count-1", COUNT, signed=True)
    await public_turn("9-count-2", COUNT, signed=True)
    return turns


# ---------------------------------------------------------------- the verdict


def _by_label(turns: list[Turn], label: str) -> Turn | None:
    return next((t for t in turns if t.label == label), None)


def _tool_computer(turn: Turn | None, chain: bool = False) -> dict[str, Any] | None:
    if turn is None:
        return None
    for call in turn.audit.get("tools", []):
        if "computer_id" in call and (not chain or "chain_head" in call):
            return dict(call)
    return None


def _first_int(text: str | None) -> int | None:
    match = INT_RE.search(text or "")
    return int(match.group()) if match else None


def verdict(
    turns: list[Turn],
    final: dict[str, Any],
    *,
    recipes_after: set[str],
    preexisting: set[str],
    brain_recipe: str,
    checks: Mapping[str, dict[str, Any]],
    sent: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """The seven postconditions of spec §11, each with the evidence it was judged on."""
    result: dict[str, dict[str, Any]] = {}
    policy = final.get("policy", {}).get("principals", {})
    catalog = final.get("catalog", {})

    signed = _by_label(turns, "4")
    unsigned = _by_label(turns, "5")
    signed_p = signed.audit.get("principal") if signed else None
    unsigned_p = unsigned.audit.get("principal") if unsigned else None
    hook_runs = signed.audit.get("hooks", []) if signed else []
    hook_logs = [checks[r["computer_id"]] for r in hook_runs if r.get("computer_id") in checks]
    result["authentication"] = {
        "ok": signed_p == VERIFIED and unsigned_p == ANONYMOUS,
        "evidence": {
            "signed": signed_p,
            "unsigned": unsigned_p,
            "hooks": hook_runs,
            "hook_logs": hook_logs,
        },
    }

    public = [
        t.audit.get("principal")
        for t in turns
        if t.door.startswith("ingress") and t.audit.get("principal") is not None
    ]
    forged = [p for p in public if p == ROOT or namespace_of(str(p)) in RESERVED_NAMESPACES]
    result["root_unforgeable"] = {"ok": not forged, "evidence": {"public_principals": public}}

    anon = policy.get(ANONYMOUS)
    verified = policy.get(VERIFIED, {})
    invoke = verified.get("invoke")
    may_invoke_all = invoke == "*" or (
        isinstance(invoke, list) and set(catalog) <= set(invoke) and bool(catalog)
    )
    anon_offered = unsigned.audit.get("offered") if unsigned else None
    result["authorization"] = {
        "ok": anon == {"invoke": [], "propose": False}
        and verified.get("propose") is True
        and may_invoke_all
        and anon_offered == [],
        "evidence": {"anonymous": anon, "verified": verified, "anonymous_offered": anon_offered},
    }

    eight = _by_label(turns, "8")
    call = _tool_computer(eight)
    check = checks.get(call["computer_id"], {}) if call else {}
    reply = eight.reply.strip() if eight else None
    result["page_title"] = {
        "ok": bool(eight)
        and "Example Domain" in (reply or "")
        and check.get("gone") is True
        and "Example Domain" in (check.get("stdout") or ""),
        "evidence": {
            "reply": reply,
            "computer_id": call["computer_id"] if call else None,
            "gone": check.get("gone"),
            "stdout": check.get("stdout"),
        },
    }

    counts: list[int | None] = []
    computer_ids: list[str] = []
    chain_lengths: list[int] = []
    for label in ("9-count-1", "9-count-2"):
        call = _tool_computer(_by_label(turns, label), chain=True)
        if call is None:
            continue
        computer_ids.append(call["computer_id"])
        counts.append(_first_int(checks.get(call["computer_id"], {}).get("stdout")))
        chain_lengths.append(int(catalog.get(call["name"], {}).get("chain_length") or 0))
    result["counter"] = {
        "ok": counts == [1, 2] and chain_lengths == [2, 2],
        "evidence": {
            "counts": counts,
            "computer_ids": computer_ids,
            "chain_lengths": chain_lengths,
        },
    }

    ready_proposed = {
        p["verb"]["name"]
        for p in final.get("proposals", [])
        if p.get("kind") == "verb" and p.get("status") == "ready" and p.get("verb")
    }
    not_ready = sorted(n for n, e in catalog.items() if e.get("status") != "ready")
    unproposed = sorted(set(catalog) - ready_proposed)
    offered: set[str] = set()
    for t in turns:
        if t.audit.get("principal") == VERIFIED:
            offered.update(t.audit.get("offered", []))
    unexpected_tools = sorted(offered - RESERVED_TOOLS - set(catalog))
    # A recipe is declared when it is the brain's, a proposal's, or a trial's (§5):
    # `try` is a tool the audit line records, and its build is on the account.
    declared = {
        brain_recipe,
        *(p["recipe_id"] for p in final.get("proposals", []) if p.get("recipe_id")),
        *(t["recipe_id"] for t in final.get("trials", []) if t.get("recipe_id")),
    }
    undeclared_recipes = sorted(recipes_after - declared - preexisting)
    result["no_undeclared_capability"] = {
        "ok": not (not_ready or unproposed or unexpected_tools or undeclared_recipes),
        "evidence": {
            "catalog": sorted(catalog),
            "not_ready": not_ready,
            "unproposed": unproposed,
            "unexpected_tools": unexpected_tools,
            "undeclared_recipes": undeclared_recipes,
        },
    }

    commands: dict[str, int] = {}
    for door, name in sent:
        key = f"{door} {name}"
        commands[key] = commands.get(key, 0) + 1
    by_hand = [k for k in commands if k.split(" ", 1)[1] not in ROOT_COMMANDS]
    result["nothing_by_hand"] = {"ok": not by_hand, "evidence": {"commands": commands}}
    return result


# ---------------------------------------------------------------- a run


def _usage_total(turns: list[Turn]) -> tuple[dict[str, int], int]:
    usage = zero_usage()
    calls = 0
    for turn in turns:
        usage = add_usage(usage, turn.audit.get("usage", {}))
        calls += int(turn.audit.get("model_calls", 0))
    return usage, calls


async def run_once(
    settings: MeasureSettings,
    out_dir: Path,
    approver: Approver,
    *,
    hatch_script: Path,
    key_dir: Path,
    keep: bool,
    log: TextIO,
) -> dict[str, Any]:
    """Hatch, speak, judge, record, tear down. Returns `run.json`'s document."""
    record = Record(out_dir)
    started = datetime.now(UTC)
    clock = time.monotonic()
    transport = transport_for(settings.api_url)
    async with (
        httpx.AsyncClient(
            base_url=settings.api_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=TURN_TIMEOUT,
            transport=transport,
        ) as api,
        httpx.AsyncClient(
            base_url=settings.api_url, timeout=TURN_TIMEOUT, transport=transport
        ) as public,
    ):
        doors = Doors(api, public, "", record)
        if await doors.checkpoints("brain"):
            raise RuntimeError("the account already has a brain; tear it down before measuring")
        preexisting = await doors.recipes()
        pubkey = new_key(key_dir)
        # Named before hatching: hatch.sh builds the wheel and uploads the priors from
        # the working tree at this moment, whatever is committed later in the run.
        version = membrane_version()
        log.write(f"hatching with {settings.model_id} (membrane {version['commit']})\n")
        hatched = hatch(settings, hatch_script, log=log)
        doors.rule_id = hatched.rule_id
        log.write(f"hatched: {json.dumps(asdict(hatched))}\n")
        final: dict[str, Any] | None = None
        try:
            try:
                turns = await speak_liturgy(doors, key_dir, pubkey, approver, log=log)
            except Exception as exc:
                # An aborted run is evidence too: what was hatched, how far it got, why.
                record.summary(
                    {
                        "run": out_dir.name,
                        "model": settings.model_id,
                        "started": started.isoformat(timespec="seconds"),
                        "ended": datetime.now(UTC).isoformat(timespec="seconds"),
                        "membrane": version,
                        "hatched": asdict(hatched),
                        "commands": len(doors.sent),
                        "ok": False,
                        "error": str(exc),
                    }
                )
                raise
            log.write("Turn 10 (root): list\n")
            final = await doors.listing()
            record.final_list(final)
            computer_ids = [
                c["computer_id"]
                for label in ("8", "9-count-1", "9-count-2")
                if (c := _tool_computer(_by_label(turns, label))) is not None
            ]
            # the hook computers of the signed knock: their logs say why a caller
            # was or was not named
            signed = _by_label(turns, "4")
            computer_ids += [
                r["computer_id"]
                for r in (signed.audit.get("hooks", []) if signed else [])
                if r.get("computer_id")
            ]
            checks = {cid: await doors.check_computer(cid) for cid in computer_ids}
            judged = verdict(
                turns,
                final,
                recipes_after=await doors.recipes(),
                preexisting=preexisting,
                brain_recipe=hatched.recipe_id,
                checks=checks,
                sent=[(s.door, s.name) for s in doors.sent],
            )
            usage, model_calls = _usage_total(turns)
            passed = sum(1 for v in judged.values() if v["ok"])
            summary = {
                "run": out_dir.name,
                "model": settings.model_id,
                "effort": settings.effort,
                "membrane": version,
                "started": started.isoformat(timespec="seconds"),
                "ended": datetime.now(UTC).isoformat(timespec="seconds"),
                "seconds": round(time.monotonic() - clock, 1),
                "approver": type(approver).__name__,
                "hatched": asdict(hatched),
                "turns": [
                    {
                        "label": t.label,
                        "door": t.door,
                        "principal": t.audit.get("principal"),
                        "model_calls": t.audit.get("model_calls", 0),
                        "usage": t.audit.get("usage", zero_usage()),
                        "stopped": t.audit.get("stopped"),
                        "tools": [c.get("name") for c in t.audit.get("tools", [])],
                        "proposals": [p["id"] for p in t.audit.get("proposals", [])],
                        "approvals": t.approvals,
                    }
                    for t in turns
                ],
                "commands": len(doors.sent),
                "model_calls": model_calls,
                "usage": usage,
                "cost_usd": round(cost_usd(usage, settings.model_id), 4),
                "postconditions": judged,
                "passed": passed,
                "ok": passed == len(POSTCONDITIONS),
            }
            record.transcript(settings.model_id, turns)
            record.summary(summary)
            tokens = f"{usage['input_tokens']} in / {usage['output_tokens']} out"
            log.write(
                f"{out_dir.name}: {passed}/{len(POSTCONDITIONS)} postconditions, "
                f"{model_calls} model calls, {tokens}, ${summary['cost_usd']}\n"
            )
            for name, v in judged.items():
                log.write(f"  {'ok ' if v['ok'] else 'NOT'} {name}\n")
            return summary
        finally:
            if keep:
                log.write("keeping the brain (--keep)\n")
            else:
                await doors.teardown(hatched, final)


def _next_run_dir(out: Path, date: str) -> Path:
    n = 1
    while (out / f"{date}-run-{n}").exists():
        n += 1
    return out / f"{date}-run-{n}"


def main(argv: list[str] | None = None, *, log: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(prog="measure", description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--approve", choices=("auto", "ask"), default="auto")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model", default=None, help=f"model id (default {DEFAULT_MODEL_ID})")
    parser.add_argument(
        "--effort", choices=EFFORTS, default=None, help="output_config.effort (default the API's)"
    )
    parser.add_argument("--date", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--keep", action="store_true", help="leave the brain on the account")
    parser.add_argument("--hatch", type=Path, default=HATCH)
    args = parser.parse_args(argv)
    try:
        settings = load_measure_settings(
            args.env, os.environ, model_id=args.model, effort=args.effort
        )
    except ValueError as exc:
        log.write(f"{exc}\n")
        return 2
    approver: Approver = (
        AskApprover(sys.stdin, sys.stdout) if args.approve == "ask" else AutoApprover()
    )
    all_ok = True
    for _ in range(args.runs):
        out_dir = _next_run_dir(args.out, args.date)
        # The hatcher's private key lives in a temp dir, never beside the evidence.
        key_dir = Path(tempfile.mkdtemp(prefix="measure-keys-"))
        try:
            summary = asyncio.run(
                run_once(
                    settings,
                    out_dir,
                    approver,
                    hatch_script=args.hatch,
                    key_dir=key_dir,
                    keep=args.keep,
                    log=log,
                )
            )
        except RuntimeError as exc:
            # The run's directory holds what it had (run.json names the error); the
            # brain was torn down; the next run is a new embryo.
            log.write(f"{out_dir.name} aborted: {str(exc)[:300]}\n")
            all_ok = False
            continue
        all_ok = all_ok and bool(summary["ok"])
    return 0 if all_ok else 1
