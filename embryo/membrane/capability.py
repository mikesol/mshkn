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
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

import httpx

from membrane.capabilities import TEMPLATE_RE, CapabilityError, catalog, load_module, order
from membrane.config import DEFAULT_ANTHROPIC_BASE_URL, DEFAULT_MODEL_ID, EFFORT_OFF, parse_env
from membrane.declarations import RESERVED_TOOL_NAMES
from membrane.effort import EFFORTS
from membrane.model import add_usage, zero_usage
from membrane.postconditions import CHECKS, PROVISION, Judged, Turn, judge, tool_computers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from types import ModuleType

    from membrane.capabilities import Capability, Prepare, Row

DEFAULT_BRAIN_API_URL = "https://api.mshkn.dev"
DEFAULT_OUT = Path("docs/embryo")
KEYS = (".mshkn", "keys")  # under the operator's home; never under the repository
REQUIRED = ("MSHKN_API_URL", "MSHKN_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
OPTIONAL = ("BRAIN_API_URL", "ANTHROPIC_BASE_URL", "AI_GATEWAY_API_KEY", "MEMBRANE_BODY_EXTRA")
HATCH = Path(__file__).resolve().parents[1] / "hatch.sh"
TURN_TIMEOUT = 330.0
TURN_WAIT = 3600.0
BUILD_TIMEOUT = 600.0
CONFLICT_INTERVAL = 3.0
BUILD_INTERVAL = 5.0
MAX_REPAIRS = 3
# A row whose answer a policy change makes possible is asked again, at most twice
# in a run (#170): the bound is what makes a goto backwards finite.
MAX_REASKS = 2
TRANSPORT_RETRIES = 3

FENCED_RE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.S)
INLINE_RE = re.compile(r"`(/[^\s`]+)`")
ABS_PATH_RE = re.compile(r"^/[^\s`]+$")


# ---------------------------------------------------------------- what a reply names


def paths_in(reply: str) -> list[str]:
    """The absolute paths a reply names, for root to place a secret at (spec
    §7.2): every fenced block that is one bare path, in order; failing that,
    every inline code span that is one. Only the model's words are read: the
    proposal documents the membrane appends after the reply hold an entrypoint,
    which is not where the agent asked for anything. Duplicates collapse."""
    text = reply.split("\nproposal p-", 1)[0]
    fenced = [block.strip() for block in FENCED_RE.findall(text)]
    found = [block for block in fenced if ABS_PATH_RE.match(block)]
    if not found:
        found = INLINE_RE.findall(text)
    return list(dict.fromkeys(found))


def unprovided_verbs(listing: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    """(verb, name, recipe_id, chain) for every name a ready verb requires that
    root has not provided, in catalog order."""
    pending: list[tuple[str, str, str, str]] = []
    for verb, entry in listing["catalog"].items():
        if entry["status"] != "ready":
            continue
        for requirement in entry["requires"]:
            if requirement["name"] not in entry["provided"]:
                pending.append((verb, requirement["name"], entry["recipe_id"], entry["chain"]))
    return pending


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
    # Where the relay forwards a model call, and the key for whatever it names. The
    # operator holds both slots so `--base-url` alone flips a run between the direct
    # API and a gateway; only one of them is ever written into the brain's .env.
    base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    gateway_api_key: str | None = None
    # One line of JSON, unparsed on the operator's side (#127): the driver has no use
    # for its contents and only needs to pass it through to `hatch.sh`, which writes
    # it into the brain's `.env` for `membrane.config.load_settings` to parse.
    body_extra: str = ""

    @property
    def model_api_key(self) -> str:
        """The one key the brain is handed, chosen by where `base_url` points.

        A non-default base URL with no gateway key raises rather than falling back:
        `load_run_settings` refuses that combination, but `RunSettings` is a public
        frozen dataclass and nothing stops a caller building one directly, and
        silently handing an Anthropic key to a gateway is the failure this whole
        two-slot design exists to prevent."""
        if self.base_url == DEFAULT_ANTHROPIC_BASE_URL:
            return self.anthropic_api_key
        if not self.gateway_api_key:
            raise ValueError(f"no AI_GATEWAY_API_KEY for base URL {self.base_url}")
        return self.gateway_api_key


def load_run_settings(
    env_file: Path,
    environ: Mapping[str, str],
    *,
    model_id: str | None = None,
    effort: str | None = None,
    base_url: str | None = None,
    body_extra: str | None = None,
) -> RunSettings:
    """The operator's `.env` (git-ignored) under the environment: the four
    required keys, `BRAIN_API_URL` if the brain dials a different address, and the
    two gateway slots of spec §5 — `ANTHROPIC_BASE_URL` (or `--base-url`) and
    `AI_GATEWAY_API_KEY`, plus `MEMBRANE_BODY_EXTRA` (or `--body-extra`) of §8.
    Raises `ValueError` for a missing required key or for a non-default base URL
    with no gateway key to send as `x-api-key`; a malformed `MEMBRANE_BODY_EXTRA`
    is not this function's problem to catch — it travels as an opaque string to
    `hatch.sh` and is only ever parsed by `membrane.config.load_settings`."""
    values = parse_env(env_file.read_text()) if env_file.exists() else {}
    values.update({k: v for k, v in environ.items() if k in REQUIRED or k in OPTIONAL})
    missing = [name for name in REQUIRED if not values.get(name)]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}: put them in {env_file} or the environment")
    url = (base_url or values.get("ANTHROPIC_BASE_URL") or DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")
    gateway_key = values.get("AI_GATEWAY_API_KEY") or None
    if url != DEFAULT_ANTHROPIC_BASE_URL and not gateway_key:
        raise ValueError(
            f"AI_GATEWAY_API_KEY is required when the model base URL is {url}: "
            f"put it in {env_file} or the environment"
        )
    return RunSettings(
        api_url=values["MSHKN_API_URL"],
        api_key=values["MSHKN_API_KEY"],
        brain_api_url=values.get("BRAIN_API_URL") or DEFAULT_BRAIN_API_URL,
        anthropic_api_key=values["ANTHROPIC_API_KEY"],
        openai_api_key=values["OPENAI_API_KEY"],
        model_id=model_id or DEFAULT_MODEL_ID,
        default_effort=effort,
        base_url=url,
        gateway_api_key=gateway_key,
        body_extra=body_extra or values.get("MEMBRANE_BODY_EXTRA") or "",
    )


@dataclass(frozen=True)
class Price:
    """USD per million tokens, from the Claude API reference (cached 2026-06-24)."""

    input: float
    output: float


PRICES = {"claude-opus-5": Price(input=5.0, output=25.0)}
CACHE_WRITE = 1.25  # of the input price
CACHE_READ = 0.1


def bare_model_id(model_id: str) -> str:
    """`anthropic/claude-opus-5` as `claude-opus-5`: a gateway namespaces every id by
    its provider, and the price of a model does not change because of the road taken
    to reach it."""
    return model_id.rsplit("/", 1)[-1]


def cost_usd(usage: Mapping[str, int], model_id: str) -> float | None:
    """USD for one run's usage, or None where the model has no price on file.

    None and not a `KeyError`: this is called at the end of `run_once`, while the
    summary is being assembled, after every turn has been spoken and paid for and
    before anything has been written to disk. A raise there loses the whole record
    of a run that has already cost money."""
    price = PRICES.get(bare_model_id(model_id))
    if price is None:
        return None
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
            if turn.provisions:
                lines += ["**Provided:**", ""]
                lines += [
                    f"- {p['verb']} {p['name']} at {p['path']} -> {p['checkpoint']}: {p['result']}"
                    for p in turn.provisions
                ]
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

    async def provision(
        self, verb: str, chain: str, recipe_id: str, path: str, secret: str
    ) -> str: ...


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
    scoped key, recipes, the hatcher's signing key, and the model, effort, base
    URL and body extra the brain was hatched with). `reasks` is how many times a
    row of that run was asked again after a policy change (#170), kept beside the
    score and never folded into it. `started_from` is the promotion this run began
    on, so records chain back to a hatch.

    `base_url` and `body_extra` default so a promotion written before this pair
    existed still loads (`Promotion(**doc)` in `read_promotion` simply omits
    them from the call); a dependent read from such a record reports the
    Anthropic default, which is what every promotion before this one meant."""

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
    reasks: int
    started_from: str | None
    base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    body_extra: str = ""
    # The run and checkpoint the keys of this lineage were rotated from (spec §7.1
    # step 2): after a capability is promoted, root replaces the model keys its
    # hatch baked into /brain/.env and cancels the hatch-time ones. Written by hand
    # into PROMOTED.md until `capability rotate` exists; `read_promotion` carries it
    # either way, and a record written before it existed simply omits it.
    rotated_from: str | None = None


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
        f"Run `{p.run}`, membrane `{p.membrane.get('commit')}`, promoted {p.promoted_at}, "
        f"promoted from a run with {p.reasks} re-asks.",
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
        """Send until it is not a 409 (a turn in progress), then require exit 0.

        A transport error on the send itself (a dropped connection, a timeout,
        ...) never got a response, so it is retried up to TRANSPORT_RETRIES
        times, each failed attempt recorded with status 0; the last is
        re-raised once the retries are spent. This is safe for `say` too: the
        membrane's `say` is a fork by label with `exclusive: error_on_conflict`,
        so a retry of a `say` that actually reached the server and started a
        turn comes back 409, which the loop above already handles; a retry of
        a `say` that never reached the server is simply safe to repeat.
        """
        deadline = self.now() + TURN_TIMEOUT
        started = self.now()
        attempt = 0
        while True:
            try:
                response = await send()
            except httpx.TransportError as exc:
                attempt += 1
                self._record(
                    door, name, detail, f"{type(exc).__name__}: {exc}", 0, self.now() - started
                )
                if attempt > TRANSPORT_RETRIES:
                    raise
                await self.sleep(CONFLICT_INTERVAL)
                continue
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

    async def provision(self, verb: str, chain: str, recipe_id: str, path: str, secret: str) -> str:
        """Root places what a verb requires (spec §7.2), with the account key and
        outside every door: a computer from the chain's head, or from the verb's
        recipe when the chain has no head yet; the secret uploaded to the path the
        agent named; a checkpoint under the chain; the computer destroyed. The
        four commands are recorded by name, so `nothing_by_hand` can count them
        against `provide`; the secret is the upload's body and no detail names it."""
        started = self.now()
        head = await self.head(chain)
        if head is None:
            created = await self.api.post("/computers", json={"recipe_id": recipe_id})
        else:
            created = await self.api.post(f"/checkpoints/{head['id']}/fork", json={})
        created.raise_for_status()
        computer_id = str(created.json()["computer_id"])
        self._record(
            "api",
            PROVISION[0],
            {"verb": verb, "from": head["id"] if head else recipe_id},
            "",
            created.status_code,
            self.now() - started,
            computer_id=computer_id,
        )
        try:
            started = self.now()
            uploaded = await self.api.post(
                f"/computers/{computer_id}/upload",
                params={"path": path},
                content=secret.encode(),
                headers={"content-type": "application/octet-stream"},
            )
            # Recorded before `raise_for_status`: a failed upload leaves
            # `create, upload, destroy` in the record, which is not the whole
            # provisioning sequence, and `nothing_by_hand` rightly calls it by hand.
            self._record(
                "api",
                PROVISION[1],
                {"verb": verb, "path": path, "bytes": len(secret.encode())},
                "",
                uploaded.status_code,
                self.now() - started,
                computer_id=computer_id,
            )
            uploaded.raise_for_status()
            started = self.now()
            taken = await self.api.post(
                f"/computers/{computer_id}/checkpoint", json={"label": chain}
            )
            taken.raise_for_status()
            checkpoint_id = str(taken.json()["checkpoint_id"])
            self._record(
                "api",
                PROVISION[2],
                {"verb": verb, "label": chain, "checkpoint_id": checkpoint_id},
                "",
                taken.status_code,
                self.now() - started,
                computer_id=computer_id,
            )
        finally:
            started = self.now()
            dropped = await self.api.delete(f"/computers/{computer_id}")
            self._record(
                "api",
                PROVISION[3],
                {"verb": verb},
                "",
                dropped.status_code,
                self.now() - started,
                computer_id=computer_id,
            )
        return checkpoint_id

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
        # Explicit, not inherited: `**os.environ` above happens to carry
        # ANTHROPIC_BASE_URL when the operator exported it, and a run's model
        # endpoint should not depend on whether a shell was configured.
        "ANTHROPIC_API_KEY": settings.model_api_key,
        "ANTHROPIC_BASE_URL": settings.base_url,
        "OPENAI_API_KEY": settings.openai_api_key,
        "MEMBRANE_BODY_EXTRA": settings.body_extra,
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

    def place(self, verb: str, name: str, parsed: str | None) -> str | None:
        """Where root puts what `verb` requires as `name`: `parsed` is the path the
        reply named, or None when it named none. Return the path to use, or None to
        place nothing. Declining a path that was named leaves the requirement
        unplaced for the rest of the run and is never asked about again; declining
        when none was named is the settle's cue to ask the agent where."""


class AutoApprover:
    """Approves every pending proposal; the membrane's invariants are the guard."""

    def decide(self, proposal: dict[str, Any]) -> str | None:  # noqa: ARG002
        return None

    def place(self, verb: str, name: str, parsed: str | None) -> str | None:  # noqa: ARG002
        return parsed


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

    def place(self, verb: str, name: str, parsed: str | None) -> str | None:
        """The pilot reads the reply and can override the path (spec §7.2)."""
        while True:
            self.stdout.write(f"provide {verb} {name}: path [{parsed or 'none'}] | skip> ")
            self.stdout.flush()
            line = self.stdin.readline()
            if not line:
                return None
            answer = line.strip()
            if answer == "skip":
                return None
            if answer == "":
                return parsed
            if answer.startswith("/"):
                return answer


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
    lacking = [
        k for k in ("key_dir", "pubkey", "model", "default_effort", "reasks") if k not in summary
    ]
    if lacking:
        raise RuntimeError(
            f"{run_dir.name}/run.json does not name {', '.join(lacking)}; a promotion carries the "
            "key its dependents sign with, the model its brain was hatched with and the rows the "
            "run had to ask again, so a run recorded without them cannot be promoted"
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
        reasks=len(summary["reasks"]),
        started_from=None if started in (None, "hatch") else str(started),
        # `.get` with the direct-API default, not `summary[...]`: a run recorded
        # before this pair existed still promotes (#127 fix round 1), and a run's
        # own summary always carries them once it does (`run_once` writes both
        # unconditionally), so this is a migration default, not a shrug.
        base_url=summary.get("base_url", DEFAULT_ANTHROPIC_BASE_URL),
        body_extra=summary.get("body_extra", ""),
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


@dataclass(frozen=True)
class Settled:
    """What a row's settle did: whether any approval in it applied a policy, and
    the listing before the first approval and after the last one, which is where
    the re-ask reads the policy change and the catalog it changed against."""

    applied: bool
    before: dict[str, Any]
    after: dict[str, Any]


def granted(policy: Mapping[str, Any], principal: str, ready: set[str]) -> set[str]:
    """The verbs a policy lets a principal invoke: `"*"` is every ready verb in the
    catalog, a list is itself, and a principal the policy does not name has none."""
    entry = policy.get("principals", {}).get(principal) or {}
    invoke = entry.get("invoke", [])
    if invoke == "*":
        return set(ready)
    return set(invoke) if isinstance(invoke, list) else set()


async def speak(
    capability: Capability,
    doors: DoorsApi,
    key_dir: Path,
    context: Mapping[str, str],
    approver: Approver,
    *,
    log: TextIO,
    reasks: list[str] | None = None,
) -> tuple[list[Turn], dict[str, Any] | None, list[str]]:
    """The capability's rows in order, each followed by approvals, a wait for
    builds and at most MAX_REPAIRS repair turns. A `root list` row takes the
    listing and is not a turn; the last one taken is returned as the final state.

    Returns the turns, the final listing and the labels re-asked, in order: after
    a settle applies a policy, every public row spoken since the previous policy
    change that the change made answerable is spoken again (#170). `reasks` is
    appended to as they are spoken, so a caller that passes its own list keeps what
    a run that aborts halfway had already re-asked."""
    for row in capability.rows:
        for name in TEMPLATE_RE.findall(row.words):
            if name not in context:
                raise RuntimeError(
                    f"row {row.label} needs {{{name}}} and the run's context has {sorted(context)}"
                )
    turns: list[Turn] = []
    final: dict[str, Any] | None = None
    reasks = [] if reasks is None else reasks
    # The public rows spoken since the last policy change, and how often each label
    # has been asked again in this run.
    since_policy: list[tuple[Row, Turn]] = []
    reask_counts: dict[str, int] = {}

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

    async def approve_pending(turn: Turn) -> tuple[dict[str, Any], dict[str, Any]]:
        """Decide every pending proposal, wait for the builds, and give whatever was
        refused one more chance once the builds are in; returns the listing as it
        was before any of it was approved and as it is after."""
        since = mark()
        before = listing = await doors.listing()
        await decide(turn, [p for p in listing["proposals"] if p["status"] == "pending"])
        listing = await doors.wait_builds()
        refused = {a["id"] for a in turn.approvals if "refused" in a["result"]}
        again = [p for p in listing["proposals"] if p["status"] == "pending" and p["id"] in refused]
        if again:
            await decide(turn, again)
            listing = await doors.wait_builds()
        turn.commands += spent(since)
        return before, listing

    # The (verb, name) pairs the approver was offered a path for and declined:
    # remembered for the rest of the run, so a pilot's `skip` costs one prompt and
    # no repair instead of spending the run's whole repair budget asking again.
    skipped: set[tuple[str, str]] = set()

    async def provide_pending(
        turn: Turn, listing: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Root provides (capabilities design §7.2): a rule keyed on the membrane's
        state, never on a row. Every ready verb with a name root has not provided
        is paired, in order, with a path the reply named; root places the run's
        `token` there on the verb's chain, outside every door, and says provide.
        Returns the listing afterwards and the requirements no path was given for,
        which the settle answers with the capability's provide phrase.

        The two ways a placement does not happen are not the same. No path in the
        reply is a question for the agent, and the settle asks it. An approver that
        declines a path the reply did name has answered for the run: the pair is
        remembered, earns no repair, and is not prompted for again."""
        pending = unprovided_verbs(listing)
        if not pending:
            return listing, []
        secret = context.get("token")
        if secret is None:
            owed = ", ".join(f"{verb} requires {name}" for verb, name, _, _ in pending)
            log.write(f"  {owed}; the run has no token to place\n")
            return listing, []
        paths = paths_in(turn.reply)
        unplaced: list[str] = []
        placed = False
        for i, (verb, name, recipe_id, chain) in enumerate(pending):
            if (verb, name) in skipped:
                continue
            parsed = paths[i] if i < len(paths) else None
            path = approver.place(verb, name, parsed)
            if path is None:
                if parsed is None:
                    unplaced.append(f"{verb} requires {name}")
                else:
                    skipped.add((verb, name))
                    log.write(f"  {verb} requires {name} at {parsed}: placed nothing\n")
                continue
            since = mark()
            checkpoint = await doors.provision(verb, chain, recipe_id, path, secret)
            result = (await doors.root("provide", verb, name)).rstrip("\n")
            turn.commands += spent(since)
            turn.provisions.append(
                {
                    "verb": verb,
                    "name": name,
                    "path": path,
                    "checkpoint": checkpoint,
                    "result": result,
                }
            )
            log.write(f"  provided {name} for {verb} at {path} ({checkpoint}): {result}\n")
            placed = True
        return (await doors.listing() if placed else listing), unplaced

    def unfinished(turn: Turn) -> bool:
        """Ended on the deadline, the cap or the token budget without proposing: the
        trial it started is in the inbox, and a repair turn lets it finish."""
        stopped = turn.audit.get("stopped")
        return stopped in ("deadline", "cap", "max_tokens") and not turn.audit.get("proposals")

    repaired: set[str] = set()
    repairs = 0

    def policy_applied(turn: Turn, listing: dict[str, Any]) -> bool:
        """Whether an approval on this turn replaced the policy, read from the
        proposal's kind and not from the words of the answer: `applied` is also what
        the membrane says of a prompt (the self-description), and a self-description
        changes nothing about what anyone may invoke."""
        proposals = {p["id"]: p for p in listing.get("proposals", [])}
        return any(
            (proposals.get(a["id"]) or {}).get("kind") == "policy"
            and (proposals.get(a["id"]) or {}).get("status") == "applied"
            for a in turn.approvals
        )

    async def settle(turn: Turn) -> Settled:
        """Approvals, builds, root's provisions, and at most MAX_REPAIRS repair
        turns in the whole run for a failed build, a refused approval, a turn that
        ran out before proposing, or a requirement the reply named no path for
        (capabilities design §4: every row settles). Returns whether any approval in
        the settle — the row's own turn or a repair turn — applied a policy, and the
        listing as it stood before the settle and after it, which is what a re-ask
        round reads the policy change out of.

        A refusal leaves its proposal `pending` with the reason on its `log`, and
        the catalog untouched, so a build-only trigger walks straight past it
        (2026-09-10-postcut-run-2). A repair is spoken through root's door, so it
        reaches the model whatever door the row used."""
        nonlocal repairs
        before, listing = await approve_pending(turn)
        applied = policy_applied(turn, listing)
        current = turn
        while True:
            listing, unplaced = await provide_pending(current, listing)
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
            if not failed and not refused and not unfinished(current) and not unplaced:
                return Settled(applied, before, listing)
            if repairs >= MAX_REPAIRS:
                return Settled(applied, before, listing)
            if failed:
                why, words = f"build failed for {', '.join(failed)}", capability.repair.build
            elif refused:
                repaired.update(refused)
                why, words = f"approval refused for {', '.join(refused)}", capability.repair.refused
            elif unfinished(current):
                why, words = "the turn ran out", capability.repair.build
            elif capability.repair.provide is None:
                log.write(
                    f"  no path for {', '.join(unplaced)} and {capability.name} "
                    "has no provide phrase\n"
                )
                return Settled(applied, before, listing)
            else:
                why, words = f"no path for {', '.join(unplaced)}", capability.repair.provide
            repairs += 1
            log.write(f"  {why}; repair {repairs}\n")
            current = await root_turn(f"3-repair-{repairs}", words)
            _, listing = await approve_pending(current)
            applied = applied or policy_applied(current, listing)

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

    def answerable(turn: Turn, settled: Settled) -> bool:
        """Whether this policy change is what the turn was missing (#170). Three
        facts the driver already holds, all state and no reading of the reply:

        the turn called no catalog verb (its tool names are the membrane's built-ins
        or nothing at all, as a closed door records none); it proposed no verb, so it
        was not waiting on a build of its own (a turn that proposed only a policy
        still qualifies — run 3's last count row proposed the widening it needed);
        and the change gained its principal at least one ready verb that is not a
        door hook, which is what makes the row's words answerable now and were not
        before. The gain is intersected with the ready catalog: a grant naming a
        verb that does not exist, or one still building, hands nobody a tool."""
        called = {c.get("name") for c in turn.audit.get("tools", [])}
        if called - RESERVED_TOOL_NAMES:
            return False
        kinds = {p["id"]: p.get("kind") for p in settled.after.get("proposals", [])}
        if any(kinds.get(p.get("id")) == "verb" for p in turn.audit.get("proposals", [])):
            return False
        principal = turn.audit.get("principal")
        if principal is None:  # a closed door named nobody; no grant moved for it
            return False
        after = settled.after.get("policy", {})
        ready = {
            n for n, e in settled.after.get("catalog", {}).items() if e.get("status") == "ready"
        }
        gained = (
            granted(after, principal, ready)
            - granted(settled.before.get("policy", {}), principal, ready)
        ) & ready
        return bool(gained - set(after.get("hooks", [])))

    async def reask(settled: Settled) -> None:
        """The goto backwards (#170). While a settle has applied a policy, ask again,
        once each and in order, every public row spoken since the previous policy
        change that the change made answerable — the row that proposed the policy
        among them, which is the run-3 case: the widening arrived at the last row and
        nothing came after it. The words are the row's own, through the row's own
        door; nothing is said about why. MAX_REASKS per label makes the loop finite."""
        while settled.applied:
            candidates = [pair for pair in since_policy if answerable(pair[1], settled)]
            since_policy.clear()
            nxt: Settled | None = None
            for row, _turn in candidates:
                if reask_counts.get(row.label, 0) >= MAX_REASKS:
                    continue
                reask_counts[row.label] = reask_counts.get(row.label, 0) + 1
                label = f"{row.label}-again-{reask_counts[row.label]}"
                words = row.words.format(**context)
                log.write(f"Turn {label} (re-ask after policy change): {words[:80]}\n")
                again = await public_turn(label, words, signed=row.door == "signed")
                reasks.append(row.label)
                # a re-asked turn is itself a row spoken since this policy change
                since_policy.append((row, again))
                after = await settle(again)
                if after.applied:
                    # another change: the next round runs from the first of them
                    nxt = Settled(True, nxt.before if nxt else after.before, after.after)
            settled = nxt or Settled(False, settled.before, settled.after)

    for row in capability.rows:
        words = row.words.format(**context)
        if row.door == "root list":
            log.write(f"Turn {row.label} (root): list\n")
            final = await doors.listing()
        elif row.door == "root say":
            await reask(await settle(await root_turn(row.label, words)))
        else:
            turn = await public_turn(row.label, words, signed=row.door == "signed")
            since_policy.append((row, turn))
            await reask(await settle(turn))
    return turns, final, reasks


# ---------------------------------------------------------------- a run


@asynccontextmanager
async def run_context(
    module: ModuleType | None, name: str, pubkey: str, doors: Doors, *, log: TextIO
) -> AsyncIterator[dict[str, str]]:
    """The context the rows' templates are filled from: `key`, the public key line
    of the run's own signing key, and whatever the capability's module prepared
    (capabilities design §4). The module's scaffolding is up for the rows and gone
    after the final listing; `key` is the run's and a module cannot take it."""
    prepare: Prepare | None = None if module is None else getattr(module, "prepare", None)
    if prepare is None:
        yield {"key": pubkey}
        return
    async with prepare(doors, log) as extra:
        if "key" in extra:
            raise RuntimeError(f"{name}.py's prepare must not set 'key'")
        yield {"key": pubkey, **extra}


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
    module: ModuleType | None = None,
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
    base_url, body_extra = settings.base_url, settings.body_extra
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
        # A dependent's brain runs its own hatch's /brain/.env, not this command
        # line's: a dependent that dialled `--base-url` was refused before it got
        # here (`_run`), but `RunSettings` is a public dataclass and a caller who
        # builds one directly must not have its `base_url` written into `run.json`
        # as if the forked brain had actually spoken through it (#127 fix round 1).
        base_url, body_extra = lineage.base_url, lineage.body_extra
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
        reasks: list[str] = []
        try:
            try:
                async with run_context(module, capability.name, pubkey, doors, log=log) as context:
                    turns, final, reasks = await speak(
                        capability, doors, key_dir, context, approver, log=log, reasks=reasks
                    )
                    # A capability without a `root list` row still gets judged on the end state.
                    if final is None:
                        final = await doors.listing()
            except Exception as exc:
                # An aborted run is evidence too: what was hatched, how far it got, why.
                record.summary(
                    {
                        "run": out_dir.name,
                        "capability": capability.name,
                        "model": model_id,
                        "base_url": base_url,
                        "body_extra": body_extra,
                        "default_effort": default_effort,
                        "effort_supported": default_effort != EFFORT_OFF,
                        "key_dir": str(key_dir),
                        "pubkey": pubkey,
                        "started": started.isoformat(timespec="seconds"),
                        "ended": datetime.now(UTC).isoformat(timespec="seconds"),
                        "membrane": version,
                        "hatched": asdict(hatched),
                        "started_from": lineage.run if lineage else "hatch",
                        "commands": len(doors.sent),
                        # what it had re-asked before it fell over, not nothing
                        "reasks": reasks,
                        "ok": False,
                        # `str(exc)` is empty for some exceptions (an httpx.ConnectError
                        # with no message, say): naming the type keeps "error" from
                        # going blank when that happens.
                        "error": f"{type(exc).__name__}: {exc}".rstrip(": "),
                    }
                )
                raise
            record.final_list(final)
            computer_ids = [c["computer_id"] for t in turns for c in tool_computers(t)]
            # the hook computers of every turn, not row 4's alone (#167): a
            # dependent capability's identity hook may run on a row hatch never
            # had, and its log says why a caller was or was not named just as
            # hatch's does.
            for t in turns:
                for r in t.audit.get("hooks", []):
                    if r.get("computer_id"):
                        computer_ids.append(r["computer_id"])
            computer_ids = list(dict.fromkeys(computer_ids))
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
                    # What the rows were spoken with, module and all: a check of a
                    # capability that served something reads the `url` or `token`
                    # its module prepared, after that scaffolding is gone.
                    context=context,
                ),
            )
            usage, model_calls = _usage_total(turns)
            passed = sum(1 for v in judged.values() if v["ok"])
            cost = cost_usd(usage, model_id)
            summary = {
                "run": out_dir.name,
                "capability": capability.name,
                "model": model_id,
                "base_url": base_url,
                "body_extra": body_extra,
                "default_effort": default_effort,
                "effort_supported": default_effort != EFFORT_OFF,
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
                        "provisions": t.provisions,
                    }
                    for t in turns
                ],
                "commands": len(doors.sent),
                # The road, kept apart from the score: a 7/7 with no re-ask and a
                # 7/7 with three reached the same state differently (#170).
                "reasks": reasks,
                "model_calls": model_calls,
                "usage": usage,
                "cost_usd": None if cost is None else round(cost, 4),
                "postconditions": judged,
                "passed": passed,
                "ok": passed == len(capability.postconditions),
            }
            record.transcript(model_id, turns)
            record.summary(summary)
            tokens = f"{usage['input_tokens']} in / {usage['output_tokens']} out"
            priced = "unpriced" if cost is None else f"${round(cost, 4)}"
            log.write(
                f"{out_dir.name}: {passed}/{len(capability.postconditions)} postconditions, "
                f"{model_calls} model calls, {tokens}, {priced}\n"
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
        choices=(EFFORT_OFF, *EFFORTS),
        default=None,
        help="the run's default output_config.effort, which a turn may raise; "
        f"{EFFORT_OFF} keeps the field off the wire for a backend that has no such "
        "parameter (default the API's)",
    )
    run.add_argument(
        "--base-url",
        default=None,
        help=f"where the relay forwards a model call (default {DEFAULT_ANTHROPIC_BASE_URL})",
    )
    run.add_argument(
        "--body-extra",
        default=None,
        help="one line of JSON merged onto every request body, e.g. "
        '\'{"providerOptions": {"gateway": {"only": ["anthropic"]}}}\'',
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
    # Before the names are validated: a capability's module registers the checks
    # only it needs as it is imported (capabilities design §4).
    try:
        module = load_module(capability)
    except Exception as exc:
        # Whatever the module raises as it is imported, the pilot gets a line that
        # names the file and the reason, not a traceback out of `main`.
        log.write(f"{capability.name}.py could not be imported: {exc}\n")
        return 2
    unknown = sorted(set(capability.postconditions) - set(CHECKS))
    if unknown:
        log.write(
            f"{capability.name} names postconditions no one wrote: {', '.join(unknown)}; "
            f"the checks are {sorted(CHECKS)}\n"
        )
        return 2
    if capability.depends and (args.model or args.effort or args.base_url or args.body_extra):
        log.write(
            f"{capability.name} starts from {capability.depends[-1]}'s promotion, whose brain "
            "runs the model, effort, base URL and body extra baked into its /brain/.env at "
            "hatch: --model, --effort, --base-url and --body-extra belong to a capability "
            "that hatches\n"
        )
        return 2
    try:
        settings = load_run_settings(
            args.env,
            os.environ,
            model_id=args.model,
            effort=args.effort,
            base_url=args.base_url,
            body_extra=args.body_extra,
        )
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
                    module=module,
                )
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            log.write(f"{out_dir.name} aborted: {type(exc).__name__}: {str(exc)[:300]}\n")
            all_ok = False
            continue
        all_ok = all_ok and bool(summary["ok"])
    return 0 if all_ok else 1
