"""Phase 14: the embryo on the live host (spec §11 tier 3). hatch.sh hatches it
with the scripted model; the liturgy is spoken through both doors; the
postconditions of §11 are checked against `list` and by invoking the verbs.

Tests run in order and share one hatched embryo; an earlier failure fails the rest.
"""

from __future__ import annotations

import asyncio
import base64
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
BUILD_TIMEOUT = 600.0

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
    return {"msg": message, "sig": base64.b64encode(sig.read_bytes()).decode()}


class Doors:
    def __init__(
        self, client: httpx.AsyncClient, public: httpx.AsyncClient, hatched: Hatched
    ) -> None:
        self.client, self.public, self.hatched = client, public, hatched

    async def root_say(self, text: str) -> tuple[dict[str, Any], str]:
        return split_output(await _fork_brain(self.client, f"membrane root say {b64(text)}"))

    async def root(self, *argv: str) -> str:
        return await _fork_brain(self.client, "membrane root " + " ".join(argv))

    async def listing(self) -> dict[str, Any]:
        return dict(json.loads(await self.root("list")))

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
            return split_output(str(body["exec_stdout"]))

    async def wait_ready(self, verb: str) -> dict[str, Any]:
        deadline = time.monotonic() + BUILD_TIMEOUT
        while time.monotonic() < deadline:
            listing = await self.listing()
            entry = listing["catalog"].get(verb)
            if entry and entry["status"] in ("ready", "failed"):
                return listing
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
        # Teardown, best effort: the door, the key, every brain and verb checkpoint, then the
        # recipes this run made (the brain's, and every proposal's). Errors are ignored: this
        # is cleanup, and a failing test has already said what went wrong.
        with suppress(Exception):
            listing = await doors.listing()
            hatched.notes["recipes"] = [
                p["recipe_id"] for p in listing["proposals"] if p["recipe_id"]
            ]
        await client.delete(f"/ingress_rules/{hatched.rule_id}")
        await client.delete(f"/keys/{hatched.key_id}")
        for label in ("brain", "verb/counter"):
            for ckpt in (await client.get("/checkpoints", params={"label": label})).json():
                await client.delete(f"/checkpoints/{ckpt['id']}")
        for recipe_id in (*hatched.notes.get("recipes", []), hatched.recipe_id):
            await client.delete(f"/recipes/{recipe_id}")


class TestPhase14Embryo:
    async def test_t14_1_turn_1_names_its_tools_and_the_closed_door(self, doors: Doors) -> None:
        assert doors.hatched.ingress_url.endswith(f"/ingress/{doors.hatched.rule_id}")
        audit, reply = await doors.root_say(LITURGY[1])
        assert audit["principal"] == "root" and audit["door"] == "api" and audit["tools"] == []
        assert "remember, try and propose" in reply and "door is closed" in reply
        listing = await doors.listing()
        assert listing["door"]["status"] == "closed" and listing["proposals"] == []

    async def test_t14_2_turn_2_and_3_the_hook_builds_and_the_door_opens(
        self, doors: Doors
    ) -> None:
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
        assert listing["door"] == {"status": "open", "hooks": ["verify_ssh"]}
        print(f"T14.2 hook ready and door open in {time.monotonic() - started:.0f}s")

    async def test_t14_3_signed_is_mike_unsigned_is_anonymous(self, doors: Doors) -> None:
        audit, reply = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[4]))
        assert audit["principal"] == "ssh:mike", audit
        assert reply.startswith("You are ssh:mike.")
        audit, reply = await doors.public_say({"msg": LITURGY[4]})
        assert audit["principal"] == "anonymous" and audit["memory_written"] is False
        assert "will not act or remember" in reply
        forged = {"msg": LITURGY[4], "sig": _sign(doors.hatched.key_dir, "something else")["sig"]}
        audit, _ = await doors.public_say(forged)
        assert audit["principal"] == "anonymous"

    async def test_t14_4_turn_6_authorization(self, doors: Doors) -> None:
        audit, _ = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[6]))
        pid = audit["proposals"][0]["id"]
        assert (await doors.root("approve", pid)).startswith(f"{pid} applied")
        listing = await doors.listing()
        assert listing["policy"]["principals"]["ssh:mike"] == {"invoke": "*", "propose": True}
        assert listing["policy"]["principals"]["anonymous"] == {"invoke": [], "propose": False}

    async def test_t14_5_page_title_from_a_self_destructed_computer(self, doors: Doors) -> None:
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
        audit, _ = await doors.public_say(_sign(doors.hatched.key_dir, LITURGY[9]))
        pid = audit["proposals"][0]["id"]
        # No trial preceded this one, so approval is the first build of the counter.
        assert await doors.approve_verb(pid, "counter") == "building"
        listing = await doors.wait_ready("counter")
        assert listing["catalog"]["counter"]["status"] == "ready", listing["proposals"]
        _, one = await doors.public_say(_sign(doors.hatched.key_dir, "count"))
        _, two = await doors.public_say(_sign(doors.hatched.key_dir, "count"))
        assert one.startswith("1") and two.startswith("2"), (one, two)
        chain = (await doors.client.get("/checkpoints", params={"label": "verb/counter"})).json()
        assert len(chain) == 2

    async def test_t14_7_postconditions_and_the_audit_outside_the_brain(self, doors: Doors) -> None:
        listing = await doors.listing()
        assert set(listing["catalog"]) == {"verify_ssh", "page_title", "counter"}
        assert all(e["status"] == "ready" for e in listing["catalog"].values())
        assert listing["catalog"]["counter"]["chain_length"] == 2
        assert listing["principals"] == ["ssh:mike"] and "root" not in listing["principals"]
        assert listing["door"]["status"] == "open"
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
        assert len(completed) >= 8, logs
        log = await doors.client.get(f"/computers/{completed[0]['computer_id']}/exec_log")
        assert log.status_code == 200, log.text
        assert log.json()["stdout"].startswith("audit "), log.json()["stdout"][:200]
