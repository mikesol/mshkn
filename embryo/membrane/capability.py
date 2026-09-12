"""The measure of the first real agent (spec §11, #101).

Root's tool, run on the operator's machine and never in the brain: hatch with a
real model, speak a capability's rows through both doors, approve what root
would approve, check the postconditions it names against `list` and the
verbs, write the transcript, every command and the verdict under
`docs/embryo/`, tear down.

    uv run capability run hatch --runs 3       # keys and the API from .env
    uv run capability run hatch --approve ask  # the pilot reads each proposal on stdin
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

import httpx

from membrane.capabilities import TEMPLATE_RE, CapabilityError, catalog, order
from membrane.config import DEFAULT_MODEL_ID, parse_env
from membrane.effort import EFFORTS
from membrane.model import add_usage, zero_usage
from membrane.postconditions import CHECKS, Judged, Turn, by_label, judge, tool_computers

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from membrane.capabilities import Capability

DEFAULT_BRAIN_API_URL = "https://api.mshkn.dev"
DEFAULT_OUT = Path("docs/embryo")
KEYS = (".mshkn", "keys")  # under the operator's home; never under the repository
REQUIRED = ("MSHKN_API_URL", "MSHKN_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
OPTIONAL = ("BRAIN_API_URL",)
HATCH = Path(__file__).resolve().parents[1] / "hatch.sh"
TURN_TIMEOUT = 330.0
TURN_WAIT = 3600.0
BUILD_TIMEOUT = 600.0
CONFLICT_INTERVAL = 3.0
BUILD_INTERVAL = 5.0
MAX_REPAIRS = 3


# ---------------------------------------------------------------- settings and cost


@dataclass(frozen=True)
class RunSettings:
    api_url: str
    api_key: str
    brain_api_url: str
    anthropic_api_key: str
    openai_api_key: str
    model_id: str
    # The floor under every model call of the run, not the effort of any of them:
    # the turn raises it from its tool list and the model's own request (#122).
    default_effort: str | None = None


def load_run_settings(
    env_file: Path,
    environ: Mapping[str, str],
    *,
    model_id: str | None = None,
    effort: str | None = None,
) -> RunSettings:
    """The operator's `.env` (git-ignored) under the environment: the four
    required keys, `BRAIN_API_URL` if the brain dials a different address."""
    values = parse_env(env_file.read_text()) if env_file.exists() else {}
    values.update({k: v for k, v in environ.items() if k in REQUIRED or k in OPTIONAL})
    missing = [name for name in REQUIRED if not values.get(name)]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}: put them in {env_file} or the environment")
    return RunSettings(
        api_url=values["MSHKN_API_URL"],
        api_key=values["MSHKN_API_KEY"],
        brain_api_url=values.get("BRAIN_API_URL") or DEFAULT_BRAIN_API_URL,
        anthropic_api_key=values["ANTHROPIC_API_KEY"],
        openai_api_key=values["OPENAI_API_KEY"],
        model_id=model_id or DEFAULT_MODEL_ID,
        default_effort=effort,
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


PROMOTED_PREFIX = "capability/"


@dataclass(frozen=True)
class Promotion:
    """What `promote` wrote: which run, which membrane, the checkpoint ids under
    each promoted label, and the lineage a dependent reuses (its ingress rule,
    scoped key, recipes, the hatcher's signing key and the model the brain was
    hatched with). `started_from` is the promotion this run began on, so records
    chain back to a hatch."""

    capability: str
    run: str
    membrane: dict[str, Any]
    promoted_at: str
    labels: dict[str, str]
    rule_id: str
    key_id: str
    brain_recipe: str
    recipe_ids: tuple[str, ...]
    key_dir: str
    pubkey: str
    model: str
    default_effort: str | None
    started_from: str | None


def promoted_label(name: str, working: str) -> str:
    return f"{PROMOTED_PREFIX}{name}/{working}"


def promotion_path(out: Path, name: str) -> Path:
    return out / name / "PROMOTED.md"


def write_promotion(out: Path, p: Promotion) -> Path:
    path = promotion_path(out, p.capability)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {**asdict(p), "recipe_ids": list(p.recipe_ids)}
    lines = [
        f"# Promoted: {p.capability}",
        "",
        f"Run `{p.run}`, membrane `{p.membrane.get('commit')}`, promoted {p.promoted_at}.",
        "Dependents start from these labels; `capability promote` overwrites this file.",
        "",
        "| Working label | Promoted label | Checkpoint |",
        "|---|---|---|",
        *(f"| `{w}` | `{promoted_label(p.capability, w)}` | `{c}` |" for w, c in p.labels.items()),
        "",
        "```json",
        json.dumps(doc, indent=1, sort_keys=True),
        "```",
        "",
    ]
    path.write_text("\n".join(lines))
    return path


def read_promotion(out: Path, name: str) -> Promotion | None:
    path = promotion_path(out, name)
    if not path.exists():
        return None
    text = path.read_text()
    try:
        block = text.split("```json\n", 1)[1].split("\n```", 1)[0]
        doc = json.loads(block)
        doc["recipe_ids"] = tuple(doc["recipe_ids"])
        return Promotion(**doc)
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{path} is not a promotion record: {exc}") from exc


def ancestry(out: Path, name: str) -> list[str]:
    """The capability names from `name` back to its hatch, following `started_from`."""
    chain: list[str] = []
    current: str | None = name
    while current is not None and current not in chain:
        record = read_promotion(out, current)
        if record is None:
            break
        chain.append(current)
        current = record.started_from.split("/", 1)[0] if record.started_from else None
    return chain


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
        record: Record | None,
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
        if self.record is None:
            return 0
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

    async def head(self, label: str) -> dict[str, Any] | None:
        """The newest checkpoint on a label: what a fork by label would advance."""
        found = await self.checkpoints(label)
        if not found:
            return None
        return max(found, key=lambda c: str(c.get("created_at", "")))

    async def copy_label(self, src: str, dst: str) -> str:
        """A new checkpoint under `dst` with the contents of `src`'s head: fork the
        head into a computer, checkpoint it under the new label, destroy it. The
        source chain is untouched. Returns the new checkpoint's id."""
        head = await self.head(src)
        if head is None:
            raise RuntimeError(f"no checkpoint on {src}")
        forked = await self.api.post(f"/checkpoints/{head['id']}/fork", json={})
        forked.raise_for_status()
        computer_id = str(forked.json()["computer_id"])
        try:
            taken = await self.api.post(f"/computers/{computer_id}/checkpoint", json={"label": dst})
            taken.raise_for_status()
            return str(taken.json()["checkpoint_id"])
        finally:
            with suppress(httpx.HTTPError):
                await self.api.delete(f"/computers/{computer_id}")

    async def working_labels(self) -> list[str]:
        """`brain` and every `verb/<name>` chain on the account, once each, sorted;
        never a trial's scratch chain and never a promoted label."""
        labels = {
            str(c["label"])
            for c in await self.checkpoints()
            if c.get("label")
            and (c["label"] == "brain" or c["label"].startswith("verb/"))
            and not str(c["label"]).startswith("verb/trial/")
        }
        return sorted(labels)

    async def teardown(
        self,
        hatched: Hatched,
        listing: dict[str, Any] | None,
        *,
        lineage: Promotion | None = None,
    ) -> None:
        """The account as the run found it, best effort. Without a lineage: the
        scripted model's server (if any), the door, the key, every checkpoint on
        `brain`, on a verb chain or from a proposal's recipe, then the recipes.
        With one: only the working checkpoints and the recipes this run added;
        the lineage's key, rule, recipes and promoted labels stay, since the
        next run of a dependent starts from them. A failing delete does not stop
        the ones after it."""

        async def drop(path: str) -> None:
            with suppress(httpx.HTTPError):
                await self.api.delete(path)

        recipes = {hatched.recipe_id}
        if listing:
            recipes.update(p["recipe_id"] for p in listing["proposals"] if p.get("recipe_id"))
        if lineage is not None:
            recipes -= set(lineage.recipe_ids)
        if hatched.server_id is not None:
            await drop(f"/computers/{hatched.server_id}")
        if lineage is None:
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
    returns the public key line hatch's row 2 carries."""
    key_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "mike", "-f", str(key_dir / "id")],
        check=True,
        capture_output=True,
    )
    return (key_dir / "id.pub").read_text().strip()


def load_key(key_dir: Path) -> str:
    """The public key line already in `key_dir`: what a dependent must sign with,
    because the promoted identity hook trusts the key its hatch baked in."""
    path = key_dir / "id.pub"
    if not path.exists():
        raise RuntimeError(f"no key in {key_dir}")
    return path.read_text().strip()


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
    # `ssh-keygen -Y sign` emits ASCII armor, which is already JSON-safe. Sending
    # it verbatim keeps the envelope self-evident: `sig` is what the signer printed.
    # Base64 over the armor was a second encoding the seed had to disclose, and
    # 2026-09-10-postcut-run-4 lost authentication to it: the hook fed the value
    # straight to ssh-keygen, as anyone would, and got exit 1 with nothing to read.
    return {"msg": message, "sig": sig.read_text()}


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


def hatch(settings: RunSettings, script: Path, *, log: TextIO) -> Hatched:
    """`embryo/hatch.sh` with the real model; the keys reach only the brain's `.env`."""
    env = {
        **os.environ,
        "MSHKN_API_URL": settings.api_url,
        "MSHKN_API_KEY": settings.api_key,
        "BRAIN_API_URL": settings.brain_api_url,
        "MEMBRANE_MODEL": "anthropic",
        "MEMBRANE_MODEL_ID": settings.model_id,
        "MEMBRANE_EFFORT": settings.default_effort or "",
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


# ---------------------------------------------------------------- promotion


async def promote(doors: Doors, out: Path, name: str, run_dir: Path, *, log: TextIO) -> Promotion:
    """A kept, passing run becomes the checkpoint dependents start from: every
    working head is copied under `capability/<name>/`, the record is written, and
    the working labels are dropped. The run's key, rule and recipes stay: they are
    the lineage the record names."""
    summary = json.loads((run_dir / "run.json").read_text())
    if not summary.get("ok"):
        raise RuntimeError(f"{run_dir.name} is not ok; only a passing run is promoted")
    try:
        run = str(run_dir.relative_to(out))
    except ValueError:
        raise RuntimeError(
            f"{run_dir} is not under {out}; promote takes the run's evidence directory under --out"
        ) from None
    lacking = [k for k in ("key_dir", "pubkey", "model", "default_effort") if k not in summary]
    if lacking:
        raise RuntimeError(
            f"{run_dir.name}/run.json does not name {', '.join(lacking)}; a promotion carries the "
            "key its dependents sign with and the model its brain was hatched with, so a run "
            "recorded without them cannot be promoted"
        )
    hatched = Hatched(**summary["hatched"])
    final = json.loads((run_dir / "final-list.json").read_text())
    working = await doors.working_labels()
    if "brain" not in working:
        raise RuntimeError("no working brain on the account; was the run kept (--keep)?")
    head = await doors.head("brain") or {}
    if head.get("recipe_id") != hatched.recipe_id:
        raise RuntimeError(
            f"the brain on the account is from recipe {head.get('recipe_id')}, not "
            f"{hatched.recipe_id}; is this the run you meant to promote?"
        )
    labels: dict[str, str] = {}
    try:
        for label in working:
            labels[label] = await doors.copy_label(label, promoted_label(name, label))
            log.write(f"promoted {label} -> {promoted_label(name, label)} ({labels[label]})\n")
    except Exception:
        log.write(
            f"promotion of {name} failed after copying {', '.join(labels)}; "
            "those promoted labels are on the account and no record names them\n"
        )
        raise
    recipe_ids = sorted(
        {hatched.recipe_id, *(p["recipe_id"] for p in final["proposals"] if p.get("recipe_id"))}
    )
    started = summary.get("started_from")
    record = Promotion(
        capability=name,
        run=run,
        membrane=dict(summary.get("membrane", {})),
        promoted_at=datetime.now(UTC).isoformat(timespec="seconds"),
        labels=labels,
        rule_id=hatched.rule_id,
        key_id=hatched.key_id,
        brain_recipe=hatched.recipe_id,
        recipe_ids=tuple(recipe_ids),
        key_dir=str(summary["key_dir"]),
        pubkey=str(summary["pubkey"]),
        model=str(summary["model"]),
        default_effort=summary["default_effort"],
        started_from=None if started in (None, "hatch") else str(started),
    )
    path = write_promotion(out, record)
    log.write(f"wrote {path}\n")
    for ckpt in await doors.checkpoints():
        label = ckpt.get("label") or ""
        if label in working:
            try:
                response = await doors.api.delete(f"/checkpoints/{ckpt['id']}")
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.write(f"could not drop {label} ({ckpt['id']}): {exc}\n")
            else:
                log.write(f"dropped {label} ({ckpt['id']})\n")
    return record


async def start_from(doors: Doors, promotion: Promotion, *, log: TextIO) -> Hatched:
    """The working labels forked from a promotion: `brain` and each verb chain.
    The lineage's rule, key and brain recipe are reused as they are."""
    checkpoint_id = ""
    for working in promotion.labels:
        src = promoted_label(promotion.capability, working)
        new = await doors.copy_label(src, working)
        log.write(f"started {working} from {src} ({new})\n")
        if working == "brain":
            checkpoint_id = new
    return Hatched(
        ingress_url="",
        rule_id=promotion.rule_id,
        key_id=promotion.key_id,
        recipe_id=promotion.brain_recipe,
        checkpoint_id=checkpoint_id,
        server_id=None,
    )


# ---------------------------------------------------------------- speaking a capability


async def speak(
    capability: Capability,
    doors: DoorsApi,
    key_dir: Path,
    context: Mapping[str, str],
    approver: Approver,
    *,
    log: TextIO,
) -> tuple[list[Turn], dict[str, Any] | None]:
    """The capability's rows in order, each followed by approvals, a wait for
    builds and at most MAX_REPAIRS repair turns. A `root list` row takes the
    listing and is not a turn; the last one taken is returned as the final state."""
    for row in capability.rows:
        for name in TEMPLATE_RE.findall(row.words):
            if name not in context:
                raise RuntimeError(
                    f"row {row.label} needs {{{name}}} and the run's context has {sorted(context)}"
                )
    turns: list[Turn] = []
    final: dict[str, Any] | None = None

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
        trial it started is in the inbox, and a repair turn lets it finish."""
        stopped = turn.audit.get("stopped")
        return stopped in ("deadline", "cap", "max_tokens") and not turn.audit.get("proposals")

    repaired: set[str] = set()
    repairs = 0

    async def settle(turn: Turn) -> None:
        """Approvals, builds, and at most MAX_REPAIRS repair turns in the whole run
        for a failed build, a refused approval, or a turn that ran out before
        proposing (capabilities design §4: every row settles).

        A refusal leaves its proposal `pending` with the reason on its `log`, and
        the catalog untouched, so a build-only trigger walks straight past it
        (2026-09-10-postcut-run-2). A repair is spoken through root's door, so it
        reaches the model whatever door the row used."""
        nonlocal repairs
        listing = await approve_pending(turn)
        current = turn
        while repairs < MAX_REPAIRS:
            failed = sorted(n for n, e in listing["catalog"].items() if e["status"] == "failed")
            # Once per refusal, not once per settle: a proposal the model never
            # repairs stays pending with its reason forever, and every later
            # settle would otherwise buy it more turns of the model's time
            # (2026-09-10-postcut-run-3).
            refused = sorted(
                p["id"]
                for p in listing["proposals"]
                if p["status"] in ("pending", "blocked")
                and p.get("log")
                and p["id"] not in repaired
            )
            if not failed and not refused and not unfinished(current):
                return
            repairs += 1
            if failed:
                why, words = f"build failed for {', '.join(failed)}", capability.repair.build
            elif refused:
                repaired.update(refused)
                why, words = f"approval refused for {', '.join(refused)}", capability.repair.refused
            else:
                why, words = "the turn ran out", capability.repair.build
            log.write(f"  {why}; repair {repairs}\n")
            current = await root_turn(f"3-repair-{repairs}", words)
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

    for row in capability.rows:
        words = row.words.format(**context)
        if row.door == "root list":
            log.write(f"Turn {row.label} (root): list\n")
            final = await doors.listing()
        elif row.door == "root say":
            await settle(await root_turn(row.label, words))
        else:
            await settle(await public_turn(row.label, words, signed=row.door == "signed"))
    return turns, final


# ---------------------------------------------------------------- a run


def _usage_total(turns: list[Turn]) -> tuple[dict[str, int], int]:
    usage = zero_usage()
    calls = 0
    for turn in turns:
        usage = add_usage(usage, turn.audit.get("usage", {}))
        calls += int(turn.audit.get("model_calls", 0))
    return usage, calls


async def run_once(
    settings: RunSettings,
    capability: Capability,
    out_dir: Path,
    approver: Approver,
    *,
    hatch_script: Path,
    key_dir: Path,
    keep: bool,
    log: TextIO,
    out: Path,
) -> dict[str, Any]:
    """Hatch (or, for a dependent, start from its last dependency's promotion),
    speak, judge, record, tear down. Returns `run.json`'s document.

    A dependent signs with its lineage's key and reports its lineage's membrane,
    model and effort: the promoted hook trusts the key the hatcher's row 2 handed
    over, and the forked brain runs the wheel and the `/brain/.env` of that hatch,
    whatever this working tree and this command line say."""
    record = Record(out_dir)
    started = datetime.now(UTC)
    clock = time.monotonic()
    # Named before hatching: hatch.sh builds the wheel and uploads the priors from
    # the working tree at this moment, whatever is committed later in the run.
    version = membrane_version()
    model_id, default_effort = settings.model_id, settings.default_effort
    pubkey = ""
    lineage: Promotion | None = None
    if capability.depends:
        # Resolved before the first request: a key that cannot sign for this
        # lineage must cost neither an API call nor a fork of the promotion.
        start = capability.depends[-1]
        lineage = read_promotion(out, start)
        if lineage is None:
            raise RuntimeError(
                f"{capability.name} starts from {start}, which has no promotion; "
                f"run `capability run {start} --keep` to a passing run, then "
                f"`capability promote {start} <run-dir>`"
            )
        missing = [d for d in capability.depends[:-1] if d not in ancestry(out, start)]
        if missing:
            raise RuntimeError(
                f"{capability.name} depends on {', '.join(missing)}, not in the ancestry of "
                f"{start}'s promotion ({' <- '.join(ancestry(out, start))})"
            )
        key_dir = Path(lineage.key_dir)
        held = load_key(key_dir)
        if held != lineage.pubkey:
            raise RuntimeError(
                f"{lineage.key_dir} holds a key that is not the one {lineage.run} was hatched "
                f"with; the promoted hook trusts {lineage.pubkey[:40]}…"
            )
        pubkey = lineage.pubkey
        version = dict(lineage.membrane)
        model_id, default_effort = lineage.model, lineage.default_effort
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
        if lineage is not None:
            log.write(f"starting from {lineage.run} ({lineage.membrane.get('commit')})\n")
            hatched = await start_from(doors, lineage, log=log)
        else:
            pubkey = new_key(key_dir)
            log.write(f"hatching with {model_id} (membrane {version['commit']})\n")
            hatched = hatch(settings, hatch_script, log=log)
        doors.rule_id = hatched.rule_id
        log.write(f"hatched: {json.dumps(asdict(hatched))}\n")
        final: dict[str, Any] | None = None
        try:
            try:
                turns, final = await speak(
                    capability, doors, key_dir, {"key": pubkey}, approver, log=log
                )
            except Exception as exc:
                # An aborted run is evidence too: what was hatched, how far it got, why.
                record.summary(
                    {
                        "run": out_dir.name,
                        "capability": capability.name,
                        "model": model_id,
                        "default_effort": default_effort,
                        "key_dir": str(key_dir),
                        "pubkey": pubkey,
                        "started": started.isoformat(timespec="seconds"),
                        "ended": datetime.now(UTC).isoformat(timespec="seconds"),
                        "membrane": version,
                        "hatched": asdict(hatched),
                        "started_from": lineage.run if lineage else "hatch",
                        "commands": len(doors.sent),
                        "ok": False,
                        "error": str(exc),
                    }
                )
                raise
            # A capability without a `root list` row still gets judged on the end state.
            if final is None:
                final = await doors.listing()
            record.final_list(final)
            computer_ids = [c["computer_id"] for t in turns for c in tool_computers(t)]
            # the hook computers of the signed knock: their logs say why a caller
            # was or was not named
            signed = by_label(turns, "4")
            computer_ids += [
                r["computer_id"]
                for r in (signed.audit.get("hooks", []) if signed else [])
                if r.get("computer_id")
            ]
            checks = {cid: await doors.check_computer(cid) for cid in computer_ids}
            judged = judge(
                capability.postconditions,
                Judged(
                    turns=turns,
                    final=final,
                    recipes_after=await doors.recipes(),
                    preexisting=preexisting,
                    brain_recipe=hatched.recipe_id,
                    checks=checks,
                    sent=[(s.door, s.name) for s in doors.sent],
                    context={"key": pubkey},
                ),
            )
            usage, model_calls = _usage_total(turns)
            passed = sum(1 for v in judged.values() if v["ok"])
            summary = {
                "run": out_dir.name,
                "capability": capability.name,
                "model": model_id,
                "default_effort": default_effort,
                "key_dir": str(key_dir),
                "pubkey": pubkey,
                "membrane": version,
                "started": started.isoformat(timespec="seconds"),
                "ended": datetime.now(UTC).isoformat(timespec="seconds"),
                "seconds": round(time.monotonic() - clock, 1),
                "approver": type(approver).__name__,
                "hatched": asdict(hatched),
                "started_from": lineage.run if lineage else "hatch",
                "turns": [
                    {
                        "label": t.label,
                        "door": t.door,
                        "principal": t.audit.get("principal"),
                        "model_calls": t.audit.get("model_calls", 0),
                        "effort": t.audit.get("effort", []),
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
                "cost_usd": round(cost_usd(usage, model_id), 4),
                "postconditions": judged,
                "passed": passed,
                "ok": passed == len(capability.postconditions),
            }
            record.transcript(model_id, turns)
            record.summary(summary)
            tokens = f"{usage['input_tokens']} in / {usage['output_tokens']} out"
            log.write(
                f"{out_dir.name}: {passed}/{len(capability.postconditions)} postconditions, "
                f"{model_calls} model calls, {tokens}, ${summary['cost_usd']}\n"
            )
            for name, v in judged.items():
                log.write(f"  {'ok ' if v['ok'] else 'NOT'} {name}\n")
            return summary
        finally:
            if keep:
                log.write("keeping the brain (--keep)\n")
            else:
                await doors.teardown(hatched, final, lineage=lineage)


def _next_run_dir(out: Path, date: str) -> Path:
    n = 1
    while (out / f"{date}-run-{n}").exists():
        n += 1
    return out / f"{date}-run-{n}"


def main(argv: list[str] | None = None, *, log: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(prog="capability", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="speak a capability to a real model and judge it")
    run.add_argument("name", help="a file under embryo/capabilities/, without .md")
    run.add_argument("--runs", type=int, default=1)
    run.add_argument("--approve", choices=("auto", "ask"), default="auto")
    run.add_argument("--env", type=Path, default=Path(".env"))
    run.add_argument("--out", type=Path, default=DEFAULT_OUT)
    run.add_argument("--model", default=None, help=f"model id (default {DEFAULT_MODEL_ID})")
    run.add_argument(
        "--effort",
        choices=EFFORTS,
        default=None,
        help="the run's default output_config.effort, which a turn may raise (default the API's)",
    )
    run.add_argument("--date", default=datetime.now(UTC).date().isoformat())
    run.add_argument(
        "--key-dir",
        type=Path,
        default=None,
        help="where the run's signing key lives (default ~/.mshkn/keys/<capability>/<run>)",
    )
    run.add_argument("--keep", action="store_true", help="leave the brain on the account")
    run.add_argument("--hatch", type=Path, default=HATCH)
    promote_p = sub.add_parser(
        "promote", help="copy a kept, passing run's heads under capability/<name>/"
    )
    promote_p.add_argument("name")
    promote_p.add_argument("run_dir", type=Path, help="the run's evidence directory")
    promote_p.add_argument("--env", type=Path, default=Path(".env"))
    promote_p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if args.command == "promote":
        return _promote(args, log)
    return _run(args, log)


def _promote(args: argparse.Namespace, log: TextIO) -> int:
    values = parse_env(args.env.read_text()) if args.env.exists() else {}
    values.update({k: v for k, v in os.environ.items() if k in ("MSHKN_API_URL", "MSHKN_API_KEY")})
    missing = [k for k in ("MSHKN_API_URL", "MSHKN_API_KEY") if not values.get(k)]
    if missing:
        log.write(f"missing {', '.join(missing)}: put them in {args.env} or the environment\n")
        return 2

    async def go() -> Promotion:
        async with httpx.AsyncClient(
            base_url=values["MSHKN_API_URL"],
            headers={"Authorization": f"Bearer {values['MSHKN_API_KEY']}"},
            timeout=TURN_TIMEOUT,
            transport=transport_for(values["MSHKN_API_URL"]),
        ) as api:
            doors = Doors(api, api, "", None)
            return await promote(doors, args.out, args.name, args.run_dir, log=log)

    try:
        asyncio.run(go())
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        log.write(f"promote failed: {exc}\n")
        return 1
    return 0


def _run(args: argparse.Namespace, log: TextIO) -> int:
    """The graph, the postcondition names and the settings are resolved before the
    run hatches: a cycle, a dependency that does not exist or a check no one wrote
    costs nothing but a message (capabilities design §5, "Resolve")."""
    known = catalog()
    try:
        capability = known[args.name]
    except KeyError:
        log.write(f"no capability named {args.name!r}; the files are {sorted(known)}\n")
        return 2
    try:
        order(known, args.name)
    except CapabilityError as exc:
        log.write(f"{exc}\n")
        return 2
    unknown = sorted(set(capability.postconditions) - set(CHECKS))
    if unknown:
        log.write(
            f"{capability.name} names postconditions no one wrote: {', '.join(unknown)}; "
            f"the checks are {sorted(CHECKS)}\n"
        )
        return 2
    if capability.depends and (args.model or args.effort):
        log.write(
            f"{capability.name} starts from {capability.depends[-1]}'s promotion, whose brain "
            "runs the model and effort baked into its /brain/.env at hatch: --model and "
            "--effort belong to a capability that hatches\n"
        )
        return 2
    try:
        settings = load_run_settings(args.env, os.environ, model_id=args.model, effort=args.effort)
    except ValueError as exc:
        log.write(f"{exc}\n")
        return 2
    approver: Approver = (
        AskApprover(sys.stdin, sys.stdout) if args.approve == "ask" else AutoApprover()
    )
    all_ok = True
    for _ in range(args.runs):
        out_dir = _next_run_dir(args.out / capability.name, args.date)
        # The hatcher's private key lives on the operator's machine, never beside the
        # evidence: a dependent starts from its lineage's promotion and must sign with
        # the key that hatch's row 2 handed the agent, so the run names its directory.
        key_dir = args.key_dir or Path.home().joinpath(*KEYS, capability.name, out_dir.name)
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            summary = asyncio.run(
                run_once(
                    settings,
                    capability,
                    out_dir,
                    approver,
                    hatch_script=args.hatch,
                    key_dir=key_dir,
                    keep=args.keep,
                    log=log,
                    out=args.out,
                )
            )
        except RuntimeError as exc:
            log.write(f"{out_dir.name} aborted: {str(exc)[:300]}\n")
            all_ok = False
            continue
        all_ok = all_ok and bool(summary["ok"])
    return 0 if all_ok else 1
