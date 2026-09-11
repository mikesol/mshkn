"""Phase 14: the embryo on the live host (spec §11 tier 3). hatch.sh hatches it
with the scripted model; the liturgy is spoken through both doors; the
postconditions of §11 are checked against `list` and by invoking the verbs.

Tests run in order and share one hatched embryo; an earlier failure fails the rest.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio

from tests.support_embryo import LITURGY, b64, split_output

from .conftest import API_KEY, API_URL, HEADERS

# One hatched embryo per module, so the fixtures and the tests share one event loop.
pytestmark = pytest.mark.asyncio(loop_scope="module")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

EMBRYO = Path(__file__).resolve().parents[2] / "embryo"
BRAIN_API_URL = os.environ.get("MSHKN_BRAIN_API_URL", "https://api.mshkn.dev")
TURN_TIMEOUT = 330.0
TURN_WAIT = 3600.0
BUILD_TIMEOUT = 600.0
# The host's idle reaper fires at 1800s and does not count the scripted model
# server's background process as activity, so a long poll must touch it well
# before then; well under half the reaper's window, not on every iteration.
TOUCH_INTERVAL = 300.0

# `approve` on a verb proposal (membrane.proposals.approve): the id, the status the
# recipe was in the instant it was submitted, the verb and its recipe.
APPROVED_VERB = re.compile(r"^(p-\d+) (ready|building): verb (\S+) recipe (rcp-\S+)\n$")


@dataclass
class Hatched:
    ingress_url: str
    rule_id: str
    key_id: str
    recipe_id: str
    checkpoint_id: str
    key_dir: Path
    pubkey: str
    notes: dict[str, Any] = field(default_factory=dict)
    server_id: str | None = None


async def _fork_brain(client: httpx.AsyncClient, command: str) -> str:
    """Root's door: fork the head of brain with a membrane command; 409 means a turn is running."""
    deadline = time.monotonic() + TURN_TIMEOUT
    while True:
        resp = await client.post(
            "/checkpoints/fork",
            json={
                "label": "brain",
                "exec": command,
                "self_destruct": True,
                "exclusive": "error_on_conflict",
            },
            timeout=TURN_TIMEOUT,
        )
        if resp.status_code == 409 and time.monotonic() < deadline:
            await asyncio.sleep(3)
            continue
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["exec_exit_code"] == 0, body
        return str(body["exec_stdout"])


def _sign(key_dir: Path, message: str) -> dict[str, str]:
    msg = key_dir / "msg"
    msg.write_bytes(message.encode())
    sig = key_dir / "msg.sig"
    sig.unlink(missing_ok=True)
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key_dir / "id"), "-n", "mshkn", str(msg)],
        check=True,
        capture_output=True,
    )
    return {"msg": message, "sig": sig.read_text()}  # ASCII armor, sent verbatim (#123)


class Doors:
    def __init__(
        self, client: httpx.AsyncClient, public: httpx.AsyncClient, hatched: Hatched
    ) -> None:
        self.client, self.public, self.hatched = client, public, hatched
        # every principal the public door minted, in order, for §10.1 in T14.7
        self.public_principals: list[str] = []
        self._touched_at = time.monotonic()

    async def root_say(self, text: str) -> tuple[dict[str, Any], str]:
        return await self._spoken(await self.root("say", b64(text)))

    async def root(self, *argv: str) -> str:
        return await _fork_brain(self.client, "membrane root " + " ".join(argv))

    async def listing(self) -> dict[str, Any]:
        return dict(json.loads(await self.root("list")))

    async def await_turn(self, turn: int) -> tuple[dict[str, Any], str]:
        """`list` until the turn is in the window (relay design §7): the reply and the
        closing audit line live there, written by the fork that closed the turn."""
        deadline = time.monotonic() + TURN_WAIT
        while True:
            listing = await self.listing()
            for entry in listing["window"]:
                if entry["turn"] == turn:
                    return dict(entry["audit"]), str(entry["output"])
            if time.monotonic() >= deadline:
                raise RuntimeError(f"turn {turn} did not close within {TURN_WAIT:.0f} s")
            await self._keep_alive()
            await asyncio.sleep(3)

    async def _spoken(self, out: str) -> tuple[dict[str, Any], str]:
        audit, rest = split_output(out)
        if "job" not in audit:
            if "queued" in audit:
                raise RuntimeError("a turn was still pending when the next was spoken")
            return audit, rest
        return await self.await_turn(int(audit["turn"]))

    async def touch(self) -> None:
        """The scripted server is a computer with only a background process; the idle
        reaper does not count that as activity (relay design §12), so a command keeps it."""
        if self.hatched.server_id:
            resp = await self.client.post(
                f"/computers/{self.hatched.server_id}/exec",
                json={"command": "true"},
                timeout=60.0,
            )
            assert resp.status_code == 200, resp.text
        self._touched_at = time.monotonic()

    async def _keep_alive(self) -> None:
        """Called from inside a long polling loop: touches the scripted server once
        `TOUCH_INTERVAL` has passed since the last touch, not on every iteration."""
        if time.monotonic() - self._touched_at >= TOUCH_INTERVAL:
            await self.touch()

    async def approve_verb(self, proposal_id: str, verb: str) -> str:
        """root approve on a verb proposal; asserts the whole reply and returns the
        catalog status the verb was born in. Both statuses are correct: the service
        dedupes recipes by content hash, so a declaration a trial already built is
        `ready` the moment it is approved, and one built for the first time is
        `building` (ruling P3)."""
        out = await self.root("approve", proposal_id)
        match = APPROVED_VERB.match(out)
        assert match is not None, out
        assert match.group(1) == proposal_id and match.group(3) == verb, out
        return match.group(2)

    async def public_say(self, payload: Any) -> tuple[dict[str, Any], str]:
        deadline = time.monotonic() + TURN_TIMEOUT
        while True:
            resp = await self.public.post(
                f"/ingress/{self.hatched.rule_id}",
                json={"b64": b64(payload)},
                headers={"content-type": "application/json"},
                timeout=TURN_TIMEOUT,
            )
            if resp.status_code == 409 and time.monotonic() < deadline:
                await asyncio.sleep(3)
                continue
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["exec_exit_code"] == 0, body
            out = str(body["exec_stdout"])
            break
        # The principal is recorded from the start audit line (the ack), before
        # waiting for the turn to close, so an anonymous knock that is offered no
        # tool and never reaches the relay is still counted (§10.1 in T14.7).
        audit, _ = split_output(out)
        self.public_principals.append(str(audit["principal"]))
        return await self._spoken(out)

    async def wait_ready(self, verb: str) -> dict[str, Any]:
        deadline = time.monotonic() + BUILD_TIMEOUT
        while time.monotonic() < deadline:
            listing = await self.listing()
            entry = listing["catalog"].get(verb)
            if entry and entry["status"] in ("ready", "failed"):
                return listing
            await self._keep_alive()
            await asyncio.sleep(5)
        raise TimeoutError(f"{verb} did not build in {BUILD_TIMEOUT}s")


@pytest.fixture(scope="module")
def hatched(tmp_path_factory: pytest.TempPathFactory) -> Hatched:
    key_dir = tmp_path_factory.mktemp("keys")
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_dir / "id")], check=True
    )
    pubkey = " ".join((key_dir / "id.pub").read_text().split()[:2])
    with httpx.Client(base_url=API_URL, headers=HEADERS, timeout=60.0) as plain:
        preexisting = {r["recipe_id"] for r in plain.get("/recipes").json()}
    env = {
        **os.environ,
        "MSHKN_API_URL": API_URL,
        "MSHKN_API_KEY": API_KEY,
        "BRAIN_API_URL": BRAIN_API_URL,
        "MEMBRANE_MODEL": "scripted",
        # Emptied, not inherited: hatch.sh writes whichever of these it is given into the
        # brain's .env, and the scripted model must not be handed a developer's model keys
        # (its `[ -n ... ]` guards skip an empty value).
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
    }
    started = time.monotonic()
    proc = subprocess.run(
        [str(EMBRYO / "hatch.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    print(f"T14.1 hatched in {time.monotonic() - started:.0f}s")
    body = json.loads(proc.stdout.strip().splitlines()[-1])
    return Hatched(**body, key_dir=key_dir, pubkey=pubkey, notes={"preexisting": preexisting})


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def doors(hatched: Hatched) -> AsyncIterator[Doors]:
    async with (
        httpx.AsyncClient(base_url=API_URL, headers=HEADERS, timeout=TURN_TIMEOUT) as client,
        # The public door carries no credential. It is dialled through MSHKN_API_URL
        # rather than the rule's own ingress_url so the suite depends on the API the
        # rest of the tier uses, not on public DNS and TLS; the two name one rule
        # (asserted in T14.1).
        httpx.AsyncClient(base_url=API_URL, timeout=TURN_TIMEOUT) as public,
    ):
        doors = Doors(client, public, hatched)
        yield doors

        # Teardown, best effort: the account is left as this module found it. The door and
        # the key go, then every checkpoint the run made — the brain chain, the counter's
        # chain, and the one each self-destructing verb computer left behind, which holds
        # its recipe open — and then the recipes themselves. Every step is wrapped, so one
        # failing delete cannot abort the ones after it and leave the account dirty; a
        # failing test has already said what went wrong.
        async def drop(path: str) -> None:
            with suppress(Exception):
                await client.delete(path)

        recipes = {hatched.recipe_id}
        with suppress(Exception):
            listing = await doors.listing()
            recipes.update(p["recipe_id"] for p in listing["proposals"] if p["recipe_id"])
        await drop(f"/ingress_rules/{hatched.rule_id}")
        if hatched.server_id:
            await drop(f"/computers/{hatched.server_id}")
        await drop(f"/keys/{hatched.key_id}")
        with suppress(Exception):
            for ckpt in (await client.get("/checkpoints")).json():
                if ckpt["label"] in ("brain", "verb/counter") or ckpt["recipe_id"] in recipes:
                    await drop(f"/checkpoints/{ckpt['id']}")
        for recipe_id in recipes:
            await drop(f"/recipes/{recipe_id}")


class TestPhase14Embryo:
    async def test_t14_1_turn_1_names_its_tools_and_the_closed_door(self, doors: Doors) -> None:
        await doors.touch()
        assert doors.hatched.ingress_url.endswith(f"/ingress/{doors.hatched.rule_id}")
        audit, reply = await doors.root_say(LITURGY[1])
        assert audit["principal"] == "root" and audit["door"] == "api" and audit["tools"] == []
        assert "remember, try and propose" in reply and "door is closed" in reply
        listing = await doors.listing()
        assert listing["door"]["status"] == "closed" and listing["proposals"] == []

    async def test_t14_2_turn_2_and_3_the_hook_builds_and_the_door_opens(
        self, doors: Doors
    ) -> None:
        await doors.touch()
        started = time.monotonic()
        audit, reply = await doors.root_say(LITURGY[2].format(key=doors.hatched.pubkey))
        assert [t["name"] for t in audit["tools"]] == ["try", "propose", "propose"], audit
        pids = [p["id"] for p in audit["proposals"]]
        assert len(pids) == 2
        hook_pid, door_pid = pids
        # the reply carries both proposals whole, so curl alone is enough to read them (§6)
        assert f"proposal {hook_pid}" in reply and '"asserts": "ssh"' in reply, reply
        assert f"proposal {door_pid}" in reply, reply
        print(f"T14.2 {hook_pid} approved as {await doors.approve_verb(hook_pid, 'verify_ssh')}")
        listing = await doors.wait_ready("verify_ssh")
        while listing["catalog"]["verify_ssh"]["status"] == "failed":
            # spec §9 turn 3: the log is in the inbox; the model trials a fix and proposes it
            # with supersedes
            audit, _ = await doors.root_say(LITURGY[3])
            fix = audit["proposals"][0]["id"]
            print(f"T14.2 build failed; fix {fix} proposed")
            assert await doors.approve_verb(fix, "verify_ssh") == "building"
            listing = await doors.wait_ready("verify_ssh")
        assert (await doors.root("approve", door_pid)).startswith(f"{door_pid} applied")
        listing = await doors.listing()
        # the hook is ready above, so the door is open *and* able to name a caller
        assert listing["door"] == {
            "status": "open",
            "hooks": ["verify_ssh"],
            "hooks_ready": ["verify_ssh"],
        }
        print(f"T14.2 hook ready and door open in {time.monotonic() - started:.0f}s")

    async def test_t14_3_signed_is_mike_unsigned_is_anonymous(self, doors: Doors) -> None:
        await doors.touch()
        audit, reply = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[4]))
        assert audit["principal"] == "ssh:mike", audit
        assert reply.startswith("You are ssh:mike.")
        audit, reply = await doors.public_say({"msg": LITURGY[4]})
        assert audit["principal"] == "anonymous" and audit["memory_written"] is False
        # §10.7 read from outside the brain: an unsigned knock is offered no tool
        # at all, whatever the model then chose to say
        assert audit["offered"] == [], audit
        assert "will not act or remember" in reply
        forged = {"msg": LITURGY[4], "sig": _sign(doors.hatched.key_dir, "something else")["sig"]}
        audit, _ = await doors.public_say(forged)
        assert audit["principal"] == "anonymous"

    async def test_t14_4_turn_6_authorization(self, doors: Doors) -> None:
        await doors.touch()
        audit, _ = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[6]))
        pid = audit["proposals"][0]["id"]
        assert (await doors.root("approve", pid)).startswith(f"{pid} applied")
        listing = await doors.listing()
        assert listing["policy"]["principals"]["ssh:mike"] == {"invoke": "*", "propose": True}
        assert listing["policy"]["principals"]["anonymous"] == {"invoke": [], "propose": False}

    async def test_t14_5_page_title_from_a_self_destructed_computer(self, doors: Doors) -> None:
        await doors.touch()
        audit, _ = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[7]))
        assert [t["name"] for t in audit["tools"]] == ["try", "propose"], audit
        assert audit["tools"][0]["status"] == "done", audit
        pid = audit["proposals"][0]["id"]
        # The trial built this very Dockerfile, so approval reuses that recipe (ruling P3).
        assert await doors.approve_verb(pid, "page_title") == "ready"
        listing = await doors.wait_ready("page_title")
        assert listing["catalog"]["page_title"]["status"] == "ready", listing["proposals"]
        audit, reply = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[8]))
        assert reply.startswith("Example Domain"), reply
        cid = audit["tools"][0]["computer_id"]
        assert (await doors.client.get(f"/computers/{cid}/status")).status_code == 404
        log = await doors.client.get(f"/computers/{cid}/exec_log")
        assert log.status_code == 200 and "Example Domain" in log.json()["stdout"]

    async def test_t14_6_counter_chain_has_two_checkpoints(self, doors: Doors) -> None:
        await doors.touch()
        audit, _ = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[9]))
        # #118: the chain verb is trialled twice on a scratch chain first, which is the
        # only way a trial can show that its disk survived an invocation.
        assert [t["name"] for t in audit["tools"]] == ["try", "propose"], audit
        trial = audit["tools"][0]
        assert trial["status"] == "done", trial
        # the audit summarises each invocation without its output (#118): two clean
        # exits, and two distinct chain heads.
        assert [r["exit_code"] for r in trial["runs"]] == [0, 0], trial
        heads = [r["chain_head"] for r in trial["runs"]]
        assert all(heads) and heads[0] != heads[1], trial
        # Distinct heads alone cannot tell a fork of run 1 from an unrelated fresh
        # chain — both are random ids. The counter's own output can: each run's
        # computer self-destructed, but its exec_log outlives it (T14.5 reads one the
        # same way), and 1 then 2 is the disk surviving the first invocation.
        said: list[str] = []
        for run in trial["runs"]:
            log = await doors.client.get(f"/computers/{run['computer_id']}/exec_log")
            assert log.status_code == 200, log.text
            said.append(log.json()["stdout"].strip())
        assert said == ["1", "2"], said
        # the scratch chain is discarded with the trial; the verb's own chain is untouched
        scratch = await doors.client.get(
            "/checkpoints", params={"label": f"verb/trial/{trial['trial']}"}
        )
        assert scratch.json() == [], scratch.text
        pid = audit["proposals"][0]["id"]
        # The trial built this very Dockerfile, so approval reuses that recipe (ruling P3).
        assert await doors.approve_verb(pid, "counter") == "ready"
        listing = await doors.wait_ready("counter")
        assert listing["catalog"]["counter"]["status"] == "ready", listing["proposals"]
        _, one = await doors.public_say(_sign(doors.hatched.key_dir, "count"))
        _, two = await doors.public_say(_sign(doors.hatched.key_dir, "count"))
        assert one.startswith("1") and two.startswith("2"), (one, two)
        chain = (await doors.client.get("/checkpoints", params={"label": "verb/counter"})).json()
        assert len(chain) == 2

    async def test_t14_7_postconditions_and_the_audit_outside_the_brain(self, doors: Doors) -> None:
        await doors.touch()
        listing = await doors.listing()
        assert set(listing["catalog"]) == {"verify_ssh", "page_title", "counter"}
        assert all(e["status"] == "ready" for e in listing["catalog"].values())
        assert listing["catalog"]["counter"]["chain_length"] == 2
        assert listing["principals"] == ["ssh:mike"]
        assert listing["door"]["status"] == "open"
        # §10.1: public input never becomes root. The liturgy knocks nine times on the
        # public door, and no knock was ever granted root, whatever it claimed to be.
        assert len(doors.public_principals) == 9, doors.public_principals
        assert set(doors.public_principals) == {"ssh:mike", "anonymous"}, doors.public_principals
        # no undeclared capability: every recipe the run added to the account is the brain's
        # or a proposal's
        recipes = {r["recipe_id"] for r in (await doors.client.get("/recipes")).json()}
        declared = {
            doors.hatched.recipe_id,
            *(p["recipe_id"] for p in listing["proposals"] if p["recipe_id"]),
        }
        undeclared = recipes - declared - doors.hatched.notes["preexisting"]
        assert undeclared == set(), undeclared
        # the audit sink is mshkn's exec_log, reached from the ingress log (T13.14): every public
        # turn's computer carries its audit line before its reply
        logs = (await doors.client.get(f"/ingress_rules/{doors.hatched.rule_id}/logs")).json()
        completed = [e for e in logs if e["status"] == "completed" and e["computer_id"]]
        assert len(completed) == 9, logs
        log = await doors.client.get(f"/computers/{completed[0]['computer_id']}/exec_log")
        assert log.status_code == 200, log.text
        assert log.json()["stdout"].startswith("audit "), log.json()["stdout"][:200]
        # the turn was a chain of forks: the ingress log's computer holds the start audit
        # line and the acknowledgement; the relay job it names was delivered to `brain`
        first = log.json()["stdout"].splitlines()
        assert first[0].startswith("audit ") and json.loads(first[1])["job"].startswith("rj-")
        job = await doors.client.get(f"/relay/{json.loads(first[1])['job']}")
        assert job.status_code == 200 and job.json()["delivery"]["label"] == "brain"
        assert job.json()["delivery"]["status"] == "delivered"
