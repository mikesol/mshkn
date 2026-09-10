"""The DNA executes end to end (spec §11 tier 2): the membrane in process against
the real app over the fake host, a scripted model playing the liturgy. A trial,
proposals, approval, builds (one failing first), a pre-turn hook, the door
opening, an ephemeral verb and a chain verb with two checkpoints."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from membrane.cli import run
from membrane.config import load_settings
from membrane.declarations import parse_verb, render_command
from membrane.memory import USER_ID, Mem0Store
from membrane.mshkn import Mshkn
from membrane.scripted import COUNTER, PAGE_TITLE, VERIFY_SSH, ScriptedModel
from membrane.state import Brain

from mshkn.host import ExecResult
from tests.support_embryo import LITURGY, b64, scripted_asgi, split_output

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .conftest import Flow

EMBRYO = Path(__file__).resolve().parents[2] / "embryo"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFlowTestKeyFlowTestKeyFlowTestKeyFlowTestKe"
BRAIN_SCOPES = {
    "recipes": {"create": True, "read": True},
    "computers": {"create_from": "*"},
    "labels": ["verb/"],
    "relay": {
        "targets": ["http://model/"],
        "deliver": {"label": "brain", "exec": "membrane resume"},
    },
}


async def _fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0.01)  # recipe polls: the fake build finishes in the background at once


class Embryo:
    """The two doors, in process. A say acknowledges; the relay's wake-up forks
    `brain` on the fake host, which runs nothing, so the helper plays the
    wake-up itself: it drains the relay's task and settles through `list`."""

    def __init__(self, flow: Flow, brain: Path, api: Mshkn) -> None:
        self.flow, self.brain, self.api = flow, brain, api
        self.notes: list[str] = []

    async def _run(self, argv: list[str]) -> str:
        out, code = await run(
            argv, brain_dir=self.brain, api=self.api, sleep=_fast_sleep, err=self.notes.append
        )
        assert code == 0, out
        await self.flow.runtime.tasks.drain(timeout=5.0)
        return out

    async def listing(self) -> dict[str, Any]:
        return dict(json.loads(await self._run(["root", "list"])))

    async def _await_turn(self, turn: int) -> tuple[dict[str, Any], str]:
        """`list` until the turn is in the window: each `list` settles a job that
        has answered, which may post the next; the relay's task runs in between."""
        for _ in range(60):
            listing = await self.listing()
            for entry in listing["window"]:
                if entry["turn"] == turn:
                    return dict(entry["audit"]), str(entry["output"])
            assert listing["pending"] is not None, f"turn {turn} neither pending nor in the window"
        raise AssertionError(f"turn {turn} never closed")

    async def _door(self, argv: list[str]) -> tuple[dict[str, Any], str]:
        audit, rest = split_output(await self._run(argv))
        if "job" not in audit:
            return audit, rest  # a closed door or a bad payload answers at once
        return await self._await_turn(int(audit["turn"]))

    async def root_say(self, text: str) -> tuple[dict[str, Any], str]:
        return await self._door(["root", "say", b64(text)])

    async def public_say(self, payload: Any) -> tuple[dict[str, Any], str]:
        return await self._door(["say", b64(payload)])

    async def root(self, *argv: str) -> str:
        return await self._run(["root", *argv])

    def script_output(
        self, decl: dict[str, Any], params: dict[str, Any], stdout: str, code: int = 0
    ) -> str:
        cmd = render_command(parse_verb(decl), params)
        self.flow.host.guest.script[cmd] = ExecResult(code, stdout, "")
        return cmd

    def memory_texts(self) -> list[str]:
        """Every fact mem0 holds for the brain, read directly off disk with
        get_all (no embedding similarity, so nothing can be missed by a query
        that scores badly). Only safe between commands: `run()` always closes
        its own store before returning (membrane.cli.run's `owned_memory`
        finally-clause), so the qdrant path is free whenever `_run` has
        returned."""
        settings = load_settings(self.brain)
        store = Mem0Store.open(settings, self.brain / "memory")
        try:
            found = store.memory.get_all(filters={"user_id": USER_ID}, top_k=100)
            return [str(r["memory"]) for r in found.get("results", [])]
        finally:
            store.close()


@pytest.fixture
async def embryo(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Embryo]:
    async def build_image(cmd: str) -> str:
        # Every build of the hook fails until it carries a supersedes line, so the liturgy's
        # "check your build" turn (spec §9 turn 3) runs: the trial fails, the approved build
        # fails, the fix builds. The real service deletes a failed recipe when its text is
        # resubmitted, so the same Dockerfile is built again on approval.
        dockerfile = Path(cmd.split()[-1], "Dockerfile").read_text()
        if "allowed_signers" in dockerfile and "# supersedes" not in dockerfile:
            raise RuntimeError("docker build failed (rc=1):\nE: Unable to locate package jq")
        return "ok"

    async def shell(cmd: str, check: bool = True) -> str:
        return ""

    monkeypatch.setattr(flow.runtime.recipes, "_build_image", build_image)
    monkeypatch.setattr(flow.runtime.recipes, "_run", shell)
    minted = await flow.client.post("/keys", json={"scopes": BRAIN_SCOPES, "label": "brain"})
    assert minted.status_code == 200, minted.text
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / "seed.md").write_text((EMBRYO / "seed.md").read_text())
    (brain / "policy.json").write_text((EMBRYO / "policy.json").read_text())
    (brain / ".env").write_text(
        "MSHKN_API_URL=http://flow\nMSHKN_API_KEY=x\nMEMBRANE_MODEL=scripted\n"
        "ANTHROPIC_BASE_URL=http://model\n"
    )
    flow.targets["model"] = ASGITransport(app=scripted_asgi(ScriptedModel()))
    # the wake-up needs a `brain` head: on the fake host a bare computer, checkpointed and gone
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    base = (await flow.client.post("/computers", json={})).json()["computer_id"]
    await flow.client.post(f"/computers/{base}/checkpoint", json={"label": "brain"})
    await flow.client.delete(f"/computers/{base}")
    http = AsyncClient(
        transport=ASGITransport(app=flow.app),
        base_url="http://flow",
        headers={"Authorization": f"Bearer {minted.json()['secret']}"},
    )
    yield Embryo(flow, brain, Mshkn(http))
    await http.aclose()


async def test_the_liturgy(embryo: Embryo, flow: Flow) -> None:
    # turn 1: root say; three tools, closed door, no proposals
    audit, reply = await embryo.root_say(LITURGY[1])
    assert audit["principal"] == "root" and audit["tools"] == [] and audit["proposals"] == []
    # spec §9 turn 1's outcome: "a reply naming its three tools honestly and that
    # its public door is closed. No proposals."
    assert "remember" in reply and "try" in reply and "propose" in reply
    assert "door is closed" in reply
    assert "proposal p-" not in reply
    listing = await embryo.listing()
    assert listing["proposals"] == [] and listing["door"]["status"] == "closed"

    # turn 2: a trial, a hook proposal and a door proposal; approve both
    hook = VERIFY_SSH(KEY)
    embryo.script_output(hook, {"payload": json.dumps({"msg": "probe", "sig": ""})}, "", code=1)
    audit, reply = await embryo.root_say(LITURGY[2].format(key=KEY))
    assert [t["name"] for t in audit["tools"]] == ["try", "propose", "propose"]
    assert audit["tools"][0]["status"] == "failed"  # the hook's first build fails, as scripted
    assert [p["id"] for p in audit["proposals"]] == ["p-1", "p-2"]
    assert "proposal p-1" in reply and '"asserts": "ssh"' in reply
    assert (await embryo.root("approve", "p-1")).startswith("p-1 building")
    # the hook is in the catalog (building) with an asserts namespace, so the door may open now;
    # until the hook is ready, a public say yields anonymous (spec §6 step 1)
    assert (await embryo.root("approve", "p-2")).startswith("p-2 applied")
    # the door is open and the hook is declared, but its recipe is still building,
    # so no hook can name a caller yet: hooks_ready is what root reads for that
    assert (await embryo.listing())["door"] == {
        "status": "open",
        "hooks": ["verify_ssh"],
        "hooks_ready": [],
    }

    # turn 3: the build failed; the log is in the inbox; the model re-proposes
    # with supersedes; approve
    listing = await embryo.listing()
    assert listing["catalog"]["verify_ssh"]["status"] == "failed"
    assert (await embryo.public_say({"msg": "early", "sig": "x"}))[0]["principal"] == "anonymous"
    audit, reply = await embryo.root_say(LITURGY[3])
    assert audit["tools"][0]["name"] == "propose" and "proposal p-3" in reply
    assert json.loads(reply.split("proposal p-3\n", 1)[1])["supersedes"] == "p-1"
    assert (await embryo.root("approve", "p-3")).startswith("p-3 building")
    listing = await embryo.listing()
    assert listing["catalog"]["verify_ssh"]["status"] == "ready"
    assert {p["id"]: p["status"] for p in listing["proposals"]}["p-1"] == "superseded"

    # turn 4: signed → ssh:mike; turn 5: unsigned → anonymous, nothing remembered
    signed = {"msg": LITURGY[4], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed)}, "mike\n")
    audit, reply = await embryo.public_say(signed)
    assert audit["principal"] == "ssh:mike" and reply.startswith("You are ssh:mike.")
    unsigned = {"msg": LITURGY[4]}
    embryo.script_output(hook, {"payload": json.dumps(unsigned)}, "", code=1)
    audit, reply = await embryo.public_say(unsigned)
    assert audit["principal"] == "anonymous" and audit["memory_written"] is False
    assert "will not act or remember" in reply
    # spec §9 turn 5's outcome checked against the real on-disk mem0 store, not
    # just the flag that gates the write: no fact was ever attributed to
    # anonymous (turn 3's early knock included), while root's and mike's
    # authenticated exchanges up to here (e.g. turn 4's "Who am I?") are there.
    texts = embryo.memory_texts()
    assert texts and not any(t.startswith("anonymous:") for t in texts)
    assert any(t.startswith("ssh:mike:") and "Who am I" in t for t in texts)
    assert any(t.startswith("root:") for t in texts)

    # turn 6: authorization — load-bearing for invocation (spec §9): turns 8 and 9
    # need ssh:mike to be allowed to invoke (propose was granted at turn 2), and
    # anonymous must be allowed nothing.
    signed6 = {"msg": LITURGY[6], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed6)}, "mike\n")
    audit, reply = await embryo.public_say(signed6)
    assert audit["principal"] == "ssh:mike" and audit["proposals"][0]["id"] == "p-4"
    assert (await embryo.root("approve", "p-4")).startswith("p-4 applied")
    policy = (await embryo.listing())["policy"]["principals"]
    assert policy["ssh:mike"] == {"invoke": "*", "propose": True}
    assert policy["anonymous"] == {"invoke": [], "propose": False}
    # behavioural proof, not just the document: an anonymous knock now, under
    # the applied policy, is offered no tools at all (§10.7: anonymous may
    # never propose, and its invoke list is empty).
    unsigned6 = {"msg": "anyone?"}
    embryo.script_output(hook, {"payload": json.dumps(unsigned6)}, "", code=1)
    audit, reply = await embryo.public_say(unsigned6)
    assert audit["principal"] == "anonymous" and audit["tools"] == []
    # the audit line says what was offered, not only what was called, so §10.7
    # is readable from the exec_log without trusting the model to stay quiet
    assert audit["offered"] == []

    # turn 7: page_title, trialled first, then proposed and approved
    signed7 = {"msg": LITURGY[7], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed7)}, "mike\n")
    embryo.script_output(PAGE_TITLE, {"url": "https://example.com"}, "Example Domain\n")
    audit, reply = await embryo.public_say(signed7)
    assert [t["name"] for t in audit["tools"]] == ["try", "propose"]
    assert audit["tools"][0]["status"] == "done"
    # Ruling P3: the trial already built the identical Dockerfile to a ready recipe, and the
    # real RecipeService dedupes by content hash, so approving p-5 reuses it and reports ready
    # immediately rather than building again.
    assert (await embryo.root("approve", "p-5")).startswith("p-5 ready")
    assert (await embryo.listing())["catalog"]["page_title"]["status"] == "ready"

    # turn 8: Example Domain from a self-destructed computer
    signed8 = {"msg": LITURGY[8], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed8)}, "mike\n")
    audit, reply = await embryo.public_say(signed8)
    assert reply.startswith("Example Domain")
    cid = audit["tools"][0]["computer_id"]
    assert (await flow.client.get(f"/computers/{cid}/status")).status_code == 404
    log = await flow.client.get(f"/computers/{cid}/exec_log")
    assert log.status_code == 200 and "Example Domain" in log.json()["stdout"]
    # the turn was a chain of forks: the relay woke `brain` once per model call, and
    # every wake-up ran `membrane resume <job>` on the chain's head (relay design §5)
    assert audit["forks"] >= 1 and audit["job"].startswith("rj-")
    wake_ups = [cmd for _, cmd in flow.host.guest.commands if cmd.startswith("membrane resume ")]
    assert f"membrane resume {audit['job']}" in wake_ups
    job = await flow.client.get(f"/relay/{audit['job']}")
    assert job.status_code == 200 and job.json()["delivery"]["status"] == "delivered"
    assert job.json()["response"]["body"]["stop_reason"] == "end_turn"

    # turn 9: a chain verb; two invocations; 1 then 2; two checkpoints
    signed9 = {"msg": LITURGY[9], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed9)}, "mike\n")
    audit, reply = await embryo.public_say(signed9)
    assert (await embryo.root("approve", "p-6")).startswith("p-6 building")
    assert (await embryo.listing())["catalog"]["counter"]["status"] == "ready"
    count = {"msg": "count", "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(count)}, "mike\n")
    counter_cmd = embryo.script_output(COUNTER, {}, "1\n")
    audit, reply = await embryo.public_say(count)
    assert reply.startswith("1") and audit["tools"][0]["chain_head"] is not None
    flow.host.guest.script[counter_cmd] = ExecResult(0, "2\n", "")
    audit, reply = await embryo.public_say(count)
    assert reply.startswith("2")
    chain = (await flow.client.get("/checkpoints", params={"label": "verb/counter"})).json()
    assert len(chain) == 2

    # turn 10: the final state is the evidence
    listing = await embryo.listing()
    assert set(listing["catalog"]) == {"verify_ssh", "page_title", "counter"}
    assert all(e["status"] == "ready" for e in listing["catalog"].values())
    assert listing["catalog"]["counter"]["chain_length"] == 2
    assert listing["principals"] == ["ssh:mike"]
    assert listing["door"] == {
        "status": "open",
        "hooks": ["verify_ssh"],
        "hooks_ready": ["verify_ssh"],
    }
    # memory: root's and mike's facts are recalled from the real store; the three
    # anonymous knocks along the way (the early one, turn 5, and turn 6's) left
    # nothing (spec §9, §10.7).
    texts = embryo.memory_texts()
    assert texts and not any(t.startswith("anonymous:") for t in texts)
    assert any(t.startswith("root:") for t in texts)
    assert any(t.startswith("ssh:mike:") for t in texts)


class _GatedTransport(httpx.AsyncBaseTransport):
    """Wraps the scripted model so its answer waits for `gate`: the model is
    still thinking until the test lets it speak. Draining the relay's task with
    the gate shut would just time out and misreport as a hang, so the queuing
    test below drives both `say`s through a raw `run()`, undrained, and only
    calls `gate.set()` once the queued state has been observed (ruling:
    team-lead, 2026-09-09 — a race on whether the in-process model call
    finishes during some other await is not an acceptable way to prove a
    still-pending turn)."""

    def __init__(self, gate: asyncio.Event, inner: httpx.AsyncBaseTransport) -> None:
        self.gate, self.inner = gate, inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self.gate.wait()
        return await self.inner.handle_async_request(request)


async def test_a_say_while_a_turn_is_pending_is_queued_and_runs_next(
    embryo: Embryo, flow: Flow
) -> None:
    gate = asyncio.Event()
    flow.targets["model"] = _GatedTransport(gate, ASGITransport(app=scripted_asgi(ScriptedModel())))

    async def _raw(argv: list[str]) -> dict[str, Any]:
        out, code = await run(
            argv, brain_dir=embryo.brain, api=embryo.api, sleep=_fast_sleep, err=embryo.notes.append
        )
        assert code == 0, out
        return split_output(out)[0]

    first = await _raw(["root", "say", b64(LITURGY[1])])
    assert first["started"] is True and first["turn"] == 1
    # the model is still gated: the relay job cannot have completed, so this
    # second say settles nothing and queues behind the first
    queued = await _raw(["root", "say", b64("And what is your public door?")])
    assert queued["queued"] == 1
    state = Brain(embryo.brain).state()
    assert len(state.queue) == 1
    assert state.queue[0].principal == "root" and state.queue[0].door == "api"

    gate.set()
    audit1, reply1 = await embryo._await_turn(1)
    audit2, _reply2 = await embryo._await_turn(2)
    assert "embryo" in reply1 and audit1["stopped"] == "done"
    assert audit2["stopped"] == "done" and audit2["principal"] == "root"
    window = (await embryo.listing())["window"]
    assert [e["turn"] for e in window] == [1, 2] and (await embryo.listing())["queue"] == []
    assert not embryo.notes or all(n.startswith("audit ") for n in embryo.notes)


async def test_the_transform_dry_runs_to_the_public_say(flow: Flow) -> None:
    rule = await flow.client.post(
        "/ingress_rules",
        json={
            "name": "brain",
            "starlark_source": (EMBRYO / "ingress.star").read_text(),
            "response_mode": "sync",
        },
    )
    assert rule.status_code == 200, rule.text
    b64_payload = b64("hello")
    dry = await flow.client.post(
        f"/ingress_rules/{rule.json()['id']}/test",
        json={
            "method": "POST",
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"b64": b64_payload}),
        },
    )
    assert dry.status_code == 200, dry.text
    assert dry.json()["validation_errors"] == []
    assert dry.json()["starlark_result"]["exec"] == f"membrane say {b64_payload}"


def test_hatch_script_parses() -> None:
    assert subprocess.run(["bash", "-n", str(EMBRYO / "hatch.sh")], check=False).returncode == 0
