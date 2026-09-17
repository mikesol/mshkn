"""Each capability's DNA executes end to end (capabilities design §9): the membrane
in process against the real app over the fake host, a scripted model playing
the rows. Hatch: a trial, proposals, approval, builds (one failing first), a
pre-turn hook, the door opening, an ephemeral verb, and a chain verb trialled
twice on a scratch chain before it is proposed and then run to two checkpoints
of its own. Security: hatch's state grown the short way, then the three rows of
§7.2 — a verb with `requires` refused until root has placed the token on its
chain at the path the reply named, the page read from a computer that is gone,
and a second verb needing the same token — judged by the five checks it names."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from membrane.capabilities import CAPABILITIES, load, load_module
from membrane.capability import INSPECT, Record, paths_in
from membrane.capability import Doors as DriverDoors
from membrane.cli import run
from membrane.config import load_settings
from membrane.declarations import parse_verb, render_command
from membrane.memory import USER_ID, Mem0Store
from membrane.mshkn import Mshkn
from membrane.postconditions import CHECKS, Judged, Turn, judge
from membrane.scripted import (
    COUNTER,
    PAGE_TITLE,
    READ_URL,
    SECRET_LENGTH,
    SECRET_PAGE,
    SECRET_PATH,
    TRIAL_QUERY,
    VERIFY_SSH,
    WEB_SEARCH,
    ScriptedModel,
)
from membrane.state import Brain

from mshkn.host import ExecResult
from tests.support_embryo import HATCH, WORDS, b64, scripted_asgi, split_output

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .conftest import Flow

EMBRYO = Path(__file__).resolve().parents[2] / "embryo"
SECURITY = load(CAPABILITIES / "security.md")
SEARCH = load(CAPABILITIES / "web-search.md")
# What the scripted provider answers row 12 with: Brave's field names, so the
# fixture is not written against the check's own vocabulary, and two entries that
# carry a URL each -- which is the only thing `searched` looks for.
SEARCH_RESULTS = json.dumps(
    {
        "type": "search",
        "web": {
            "results": [
                {"title": "Firecracker", "url": "https://firecracker-microvm.github.io/"},
                {"title": "The NSDI paper", "url": "https://www.usenix.org/conference/nsdi20"},
            ]
        },
    }
)
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

    def __init__(self, flow: Flow, brain: Path, api: Mshkn, model: ScriptedModel) -> None:
        self.flow, self.brain, self.api, self.model = flow, brain, api, model
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


DEFAULT_ENV = (
    "MSHKN_API_URL=http://flow\nMSHKN_API_KEY=x\nMEMBRANE_MODEL=scripted\n"
    "ANTHROPIC_BASE_URL=http://model\n"
)
# What a gateway run's .env carries that a direct run's does not (task 7, spec §11):
# a namespaced model id, the effort axis turned off, and an operator's pinned upstream
# riding through as `body_extra`.
GATEWAY_ENV = (
    "MSHKN_API_URL=http://flow\nMSHKN_API_KEY=x\nMEMBRANE_MODEL=scripted\n"
    "ANTHROPIC_BASE_URL=http://model\nMEMBRANE_MODEL_ID=anthropic/scripted-1\n"
    "MEMBRANE_EFFORT=off\n"
    'MEMBRANE_BODY_EXTRA={"providerOptions": {"gateway": {"only": ["anthropic"]}}}\n'
)


@asynccontextmanager
async def _hatched(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> AsyncIterator[Embryo]:
    """The shared body of `embryo` and `embryo_gateway`: a minted brain key, a brain
    directory carrying the given `.env`, and the scripted model behind the relay's
    target. The two fixtures differ only in `env` (task 7)."""

    async def build_image(cmd: str) -> str:
        # Every build of the hook fails until it carries a supersedes line, so hatch's
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
    (brain / ".env").write_text(env)
    model = ScriptedModel()
    flow.targets["model"] = ASGITransport(app=scripted_asgi(model))
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
    try:
        yield Embryo(flow, brain, Mshkn(http), model)
    finally:
        await http.aclose()


@pytest.fixture
async def embryo(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Embryo]:
    async with _hatched(flow, tmp_path, monkeypatch, DEFAULT_ENV) as embryo:
        yield embryo


@pytest.fixture
async def embryo_gateway(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Embryo]:
    async with _hatched(flow, tmp_path, monkeypatch, GATEWAY_ENV) as embryo:
        yield embryo


async def test_hatch(embryo: Embryo, flow: Flow) -> None:
    assert [r.door for r in HATCH.rows[:2]] == ["root say", "root say"]
    assert [r.door for r in HATCH.rows[2:10]] == ["signed", "unsigned"] + ["signed"] * 6

    # turn 1: root say; the four built-in tools, closed door, no proposals
    audit, reply = await embryo.root_say(WORDS["1"])
    assert audit["principal"] == "root" and audit["tools"] == [] and audit["proposals"] == []
    # spec §9 turn 1's outcome: "a reply naming its tools honestly and that its
    # public door is closed. No proposals." Read as names, never as a count: a count
    # marks down a model that honestly names a tool the outcome forgot (#117, #131).
    assert all(name in reply for name in ("remember", "effort", "try", "propose"))
    assert "door is closed" in reply
    assert "proposal p-" not in reply
    listing = await embryo.listing()
    assert listing["proposals"] == [] and listing["door"]["status"] == "closed"

    # turn 2: a trial, a hook proposal and a door proposal; approve both
    hook = VERIFY_SSH(KEY)
    embryo.script_output(hook, {"payload": json.dumps({"msg": "probe", "sig": ""})}, "", code=1)
    audit, reply = await embryo.root_say(WORDS["2"].format(key=KEY))
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
    audit, reply = await embryo.root_say(HATCH.repair.build)
    assert audit["tools"][0]["name"] == "propose" and "proposal p-3" in reply
    assert json.loads(reply.split("proposal p-3\n", 1)[1])["supersedes"] == "p-1"
    assert (await embryo.root("approve", "p-3")).startswith("p-3 building")
    listing = await embryo.listing()
    assert listing["catalog"]["verify_ssh"]["status"] == "ready"
    assert {p["id"]: p["status"] for p in listing["proposals"]}["p-1"] == "superseded"

    # turn 4: signed → ssh:mike; turn 5: unsigned → anonymous, nothing remembered
    signed = {"msg": WORDS["4"], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed)}, "mike\n")
    audit, reply = await embryo.public_say(signed)
    assert audit["principal"] == "ssh:mike" and reply.startswith("You are ssh:mike.")
    unsigned = {"msg": WORDS["4"]}
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
    signed6 = {"msg": WORDS["6"], "sig": "c2ln"}
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
    signed7 = {"msg": WORDS["7"], "sig": "c2ln"}
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
    signed8 = {"msg": WORDS["8"], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed8)}, "mike\n")
    audit, reply = await embryo.public_say(signed8)
    assert reply.startswith("Example Domain")
    # #124 (2026-09-13-run-2): the script now says something before it
    # remembers, and the turn's reply carries both -- every text block the
    # model said, not only the last response's.
    assert "Example Domain" in reply and "Noted." in reply
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

    # turn 9: a chain verb, trialled on a scratch chain first (#118), then proposed
    signed9 = {"msg": WORDS["9"], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed9)}, "mike\n")
    counter_cmd = embryo.script_output(COUNTER, {}, "1\n")
    flow.host.guest.script_sequence[counter_cmd] = [
        ExecResult(0, "1\n", ""),
        ExecResult(0, "2\n", ""),
    ]
    audit, reply = await embryo.public_say(signed9)
    trial = audit["tools"][0]
    assert trial["name"] == "try" and trial["status"] == "done", audit
    # the audit carries what each invocation did and where it left the chain, never
    # what it printed; two distinct heads are the disk surviving the first invocation
    assert [r["exit_code"] for r in trial["runs"]] == [0, 0], trial
    heads = [r["chain_head"] for r in trial["runs"]]
    assert all(heads) and heads[0] != heads[1], trial
    # the same command really ran twice, which is what a chain verb is for
    assert [c for _, c in flow.host.guest.commands].count(counter_cmd) == 2
    # the scratch chain is discarded with the trial; the verb's own chain is untouched
    scratch = (
        await flow.client.get("/checkpoints", params={"label": f"verb/trial/{trial['trial']}"})
    ).json()
    assert scratch == [], scratch
    assert (await flow.client.get("/checkpoints", params={"label": "verb/counter"})).json() == []
    # Ruling P3 again: the trial built this exact Dockerfile, so approving p-6 reuses
    # the deduped recipe and reports ready rather than building a second time.
    assert (await embryo.root("approve", "p-6")).startswith("p-6 ready")
    assert (await embryo.listing())["catalog"]["counter"]["status"] == "ready"
    # the trial consumed the sequence above, so the verb's own two invocations get
    # their own 1 then 2
    flow.host.guest.script_sequence[counter_cmd] = [
        ExecResult(0, "1\n", ""),
        ExecResult(0, "2\n", ""),
    ]
    count = {"msg": "count", "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(count)}, "mike\n")
    audit, reply = await embryo.public_say(count)
    assert reply.startswith("1")
    first_head = audit["tools"][0]["chain_head"]
    assert first_head is not None
    audit, reply = await embryo.public_say(count)
    assert reply.startswith("2")
    second_head = audit["tools"][0]["chain_head"]
    # #139: each invocation reports the checkpoint it created, so a new head per call
    # is what proves the chain advanced. Counting the label's checkpoints asserts
    # retained history instead, which #93 retention prunes down to the head alone --
    # on the live host the first invocation's checkpoint is collected within seconds,
    # and this tier passed only because the fake host has no reaper.
    assert second_head is not None and second_head != first_head
    chain = (await flow.client.get("/checkpoints", params={"label": "verb/counter"})).json()
    assert second_head in {c["id"] for c in chain}  # the head is never pruned

    # turn 10: the final state is the evidence
    listing = await embryo.listing()
    assert set(listing["catalog"]) == {"verify_ssh", "page_title", "counter"}
    assert all(e["status"] == "ready" for e in listing["catalog"].values())
    assert listing["catalog"]["counter"]["chain_head"] == second_head
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


async def _grown(embryo: Embryo) -> dict[str, Any]:
    """The state hatch's promotion leaves behind, grown the short way: the hook
    (its first build fails and is repaired, as the fixture scripts), the door,
    and the policy under which ssh:mike may propose and invoke everything.
    Returns the hook declaration the signed rows are scripted against."""
    hook = VERIFY_SSH(KEY)
    embryo.script_output(hook, {"payload": json.dumps({"msg": "probe", "sig": ""})}, "", code=1)
    await embryo.root_say(WORDS["2"].format(key=KEY))
    assert (await embryo.root("approve", "p-1")).startswith("p-1 building")
    assert (await embryo.root("approve", "p-2")).startswith("p-2 applied")
    await embryo.listing()  # polls the failed build into the inbox
    await embryo.root_say(HATCH.repair.build)
    assert (await embryo.root("approve", "p-3")).startswith("p-3 building")
    assert (await embryo.listing())["catalog"]["verify_ssh"]["status"] == "ready"
    signed6 = {"msg": WORDS["6"], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed6)}, "mike\n")
    audit, _ = await embryo.public_say(signed6)
    assert (await embryo.root("approve", audit["proposals"][0]["id"])).endswith(
        "applied: policy replaced; effective from the next turn\n"
    )
    return hook


async def test_security(embryo: Embryo, flow: Flow, tmp_path: Path) -> None:
    """Spec §7.2 end to end on the fake host: the page is prepared by security's
    module, the agent (scripted) asks for a verb with `requires` and says where
    the token goes, invocation is refused until root has placed it, the driver
    places it on the verb's chain from the path it read, root says provide, the
    page is read from a computer on that chain, and the token is on no brain."""
    preexisting = {r["recipe_id"] for r in (await flow.client.get("/recipes")).json()}
    hook = await _grown(embryo)
    security = load_module(SECURITY)
    assert security is not None
    driver = DriverDoors(flow.client, flow.client, "", Record(tmp_path / "run"))
    sent: list[tuple[str, str]] = []
    # the driver's inspection after the run: the fake guest answers the one command it runs
    flow.host.guest.stream_script[INSPECT] = [
        ("stdout", "---"),
        ("stdout", "MSHKN_API_URL"),
        ("stdout", "MSHKN_API_KEY"),
        ("stdout", "MEMBRANE_MODEL"),
        ("stdout", "ANTHROPIC_BASE_URL"),
    ]
    log = io.StringIO()
    turns: list[Turn] = []
    try:
        async with security.prepare(driver, log) as context:
            url, token = context["url"], context["token"]
            assert url.startswith("https://8000-") and url.endswith("/page")
            # the server is a computer of its own on the account, from the brain's
            # (here: no) recipe, with the token and the script uploaded, never on a
            # command line
            server = [
                c for _, c in flow.host.guest.commands if c.startswith("python3 /tmp/page.py")
            ]
            assert server == ["python3 /tmp/page.py"]
            assert all(token not in c for _, c in flow.host.guest.commands)

            # row 11: a trial that sees the 401, a proposal with requires, a path in the reply
            words11 = SECURITY.row("11").words.format(url=url)
            page_cmd = embryo.script_output(SECRET_PAGE(url), {}, security.PAGE_BODY)
            flow.host.guest.script_sequence[page_cmd] = [
                ExecResult(22, "", "curl: (22) The requested URL returned error: 401"),
                ExecResult(0, security.PAGE_BODY, ""),
            ]
            signed11 = {"msg": words11, "sig": "c2ln"}
            embryo.script_output(hook, {"payload": json.dumps(signed11)}, "mike\n")
            audit11, reply11 = await embryo.public_say(signed11)
            turns.append(Turn("11", "ingress", words11, audit11, reply11, [], []))
            assert [t["name"] for t in audit11["tools"]] == ["try", "propose"], audit11
            assert audit11["tools"][0]["runs"][0]["exit_code"] == 22, audit11
            pid = audit11["proposals"][0]["id"]
            assert (await embryo.root("approve", pid)).startswith(f"{pid} ready")
            listing = await embryo.listing()
            entry = listing["catalog"]["secret_page"]
            assert entry["requires"] == [{"kind": "secret", "name": "page_token"}]
            assert entry["provided"] == [] and entry["chain"] == "verb/secret_page"
            assert paths_in(reply11) == [SECRET_PATH]

            # refused until root has provided: offered, called, blocked, no computer created
            read = {"msg": SECURITY.row("12").words, "sig": "c2ln"}
            embryo.script_output(hook, {"payload": json.dumps(read)}, "mike\n")
            audit_blocked, reply_blocked = await embryo.public_say(read)
            assert "secret_page" in audit_blocked["offered"]
            assert audit_blocked["tools"][0]["status"] == "error"
            assert audit_blocked["tools"][0]["error"] == (
                "blocked: secret_page requires page_token; root places it and says provide"
            )
            assert "blocked" in reply_blocked
            blocked_chain = await flow.client.get(
                "/checkpoints", params={"label": "verb/secret_page"}
            )
            assert blocked_chain.json() == []

            # root provides: the driver's four commands, then provide through root's door
            placed = await driver.provision(
                "secret_page", entry["chain"], entry["recipe_id"], paths_in(reply11)[0], token
            )
            sent += [(s.door, s.name) for s in driver.sent]
            chain = (
                await flow.client.get("/checkpoints", params={"label": "verb/secret_page"})
            ).json()
            assert [c["id"] for c in chain] == [placed]
            uploaded = [
                data for (_, path), data in flow.host.guest.files.items() if path == SECRET_PATH
            ]
            assert uploaded == [token.encode()]
            assert await embryo.root("provide", "secret_page", "page_token") == (
                "secret_page: page_token provided (1/1)\n"
            )
            sent.append(("api", "provide"))
            provided = await embryo.listing()
            assert provided["catalog"]["secret_page"]["provided"] == ["page_token"]
            # what `provide` put in the inbox is there for the next turn to read
            assert provided["inbox"] == 1

            # row 12: the body, from a computer on the verb's chain that is gone
            embryo.script_output(hook, {"payload": json.dumps(read)}, "mike\n")
            audit12, reply12 = await embryo.public_say(read)
            turns.append(Turn("12", "ingress", read["msg"], audit12, reply12, [], []))
            assert reply12.startswith(security.PAGE_BODY.strip()), reply12
            call = audit12["tools"][0]
            assert call["status"] == "ok" and call["chain_head"] not in (None, placed)
            assert (await embryo.listing())["inbox"] == 0  # the turn drained it
            heads = {
                c["id"]: c
                for c in (
                    await flow.client.get("/checkpoints", params={"label": "verb/secret_page"})
                ).json()
            }
            # the invocation forked root's placement
            assert heads[call["chain_head"]]["parent_id"] == placed
            gone = await flow.client.get(f"/computers/{call['computer_id']}/status")
            assert gone.status_code == 404
            exec_log = await flow.client.get(f"/computers/{call['computer_id']}/exec_log")
            assert security.PAGE_BODY.strip() in exec_log.json()["stdout"]
            checks = {
                call["computer_id"]: {
                    "computer_id": call["computer_id"],
                    "gone": True,
                    "stdout": exec_log.json()["stdout"],
                    "exit_code": 0,
                }
            }

            # row 13: a second verb needing the same token; the scripted answer is a second copy
            words13 = SECURITY.row("13").words
            length_cmd = embryo.script_output(SECRET_LENGTH(url), {}, "54\n")
            flow.host.guest.script_sequence[length_cmd] = [
                ExecResult(22, "", "401"),
                ExecResult(0, "54\n", ""),
            ]
            signed13 = {"msg": words13, "sig": "c2ln"}
            embryo.script_output(hook, {"payload": json.dumps(signed13)}, "mike\n")
            audit13, reply13 = await embryo.public_say(signed13)
            turns.append(Turn("13", "ingress", words13, audit13, reply13, [], []))
            pid13 = audit13["proposals"][0]["id"]
            assert (await embryo.root("approve", pid13)).startswith(f"{pid13} ready")
            entry13 = (await embryo.listing())["catalog"]["secret_length"]
            assert entry13["requires"] == [{"kind": "secret", "name": "page_token"}]
            before = len(driver.sent)
            await driver.provision(
                "secret_length", entry13["chain"], entry13["recipe_id"], paths_in(reply13)[0], token
            )
            sent += [(s.door, s.name) for s in driver.sent[before:]]
            assert (await embryo.root("provide", "secret_length", "page_token")).startswith(
                "secret_length: page_token provided"
            )
            sent.append(("api", "provide"))
            final = await embryo.listing()

        # the server is gone; the driver inspects the brain the run ended with,
        # the way `run_once` does once `run_context` has exited
        brain = await driver.inspect_brain(token, log)
        assert brain["files_with_token"] == []
        assert "MSHKN_API_KEY" in brain["env_names"]
        # no command the host ever ran carried the token, the placements and the
        # two invocations included: it reached a guest as an upload's body only
        assert all(token not in c for _, c in flow.host.guest.commands)
        # the token is on no brain, and the flow tier can say so for real: the brain
        # directory and mem0's store are on this machine
        assert token not in (embryo.brain / "state.json").read_text()
        assert all(token not in text for text in embryo.memory_texts())
        assert token not in log.getvalue()
        for written in (tmp_path / "run" / "commands").iterdir():
            assert token not in written.read_text()
        recipes_after = {r["recipe_id"] for r in (await flow.client.get("/recipes")).json()}
        verdict = judge(
            SECURITY.postconditions,
            Judged(
                turns=turns,
                final=final,
                recipes_after=recipes_after,
                preexisting=preexisting,
                brain_recipe="rcp-none",
                checks=checks,
                sent=sent,
                context={"key": KEY, "url": url, "token": token},
                brain=brain,
            ),
        )
        assert {name: v["ok"] for name, v in verdict.items()} == dict.fromkeys(
            SECURITY.postconditions, True
        ), verdict
        assert set(final["catalog"]) == {"verify_ssh", "secret_page", "secret_length"}
        assert final["catalog"]["secret_length"]["provided"] == ["page_token"]
    finally:
        # the module's registrations are global; they do not leak into the next test
        CHECKS.pop("secret_page", None)


async def test_web_search(
    embryo: Embryo, flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The web-search design end to end on the fake host: the operator's key (not
    the run's) reaches a verb's chain by the same path security's token did, a
    search runs on that chain, an unkeyed verb reads the capability's own page,
    and both new checks judge the run. Row 15 is not spoken -- it is scored by
    nothing but the invariants, and what an agent answers it with is a question
    for a live run, not for a scripted one.

    What this proves that `test_security` does not: a credential that came from
    outside the run is never on a command line, in the brain, in memory, in the
    log or in the recorded commands -- and `searched` reads its refusal-before-
    the-key off the record of the row that tried it."""
    monkeypatch.setenv("SEARCH_API_URL", "https://search.example/res")
    monkeypatch.setenv("SEARCH_API_KEY", "sk-operator-flow-0123456789")
    monkeypatch.setenv("SEARCH_QUERY", "what is a Firecracker microVM")
    preexisting = {r["recipe_id"] for r in (await flow.client.get("/recipes")).json()}
    hook = await _grown(embryo)
    web_search = load_module(SEARCH)
    assert web_search is not None
    driver = DriverDoors(flow.client, flow.client, "", Record(tmp_path / "run"))
    sent: list[tuple[str, str]] = []
    flow.host.guest.stream_script[INSPECT] = [
        ("stdout", "---"),
        ("stdout", "MSHKN_API_URL"),
        ("stdout", "MSHKN_API_KEY"),
        ("stdout", "MEMBRANE_MODEL"),
    ]
    log = io.StringIO()
    turns: list[Turn] = []
    checks: dict[str, dict[str, Any]] = {}

    async def _gone(call: dict[str, Any]) -> str:
        """The exec log of a call's computer, once the computer is gone."""
        cid = str(call["computer_id"])
        assert (await flow.client.get(f"/computers/{cid}/status")).status_code == 404
        stdout = str((await flow.client.get(f"/computers/{cid}/exec_log")).json()["stdout"])
        checks[cid] = {
            "computer_id": cid,
            "gone": True,
            "stdout": stdout,
        }
        return stdout

    try:
        async with web_search.prepare(driver, log) as context:
            endpoint, key = context["url"], context["token"]
            assert endpoint == "https://search.example/res" and key.startswith("sk-operator")
            assert context["page"].startswith("https://8000-") and context["page"].endswith("/page")

            # row 11: a trial refused 401 without the key, a proposal with requires,
            # a path in the reply
            words11 = SEARCH.row("11").words.format(url=endpoint)
            trial = embryo.script_output(WEB_SEARCH(endpoint), {"query": TRIAL_QUERY}, "")
            flow.host.guest.script[trial] = ExecResult(
                22, "", "curl: (22) The requested URL returned error: 401"
            )
            signed11 = {"msg": words11, "sig": "c2ln"}
            embryo.script_output(hook, {"payload": json.dumps(signed11)}, "mike\n")
            audit11, reply11 = await embryo.public_say(signed11)
            turns.append(Turn("11", "ingress", words11, audit11, reply11, [], []))
            assert [t["name"] for t in audit11["tools"]] == ["try", "propose"], audit11
            assert audit11["tools"][0]["runs"][0]["exit_code"] == 22, audit11
            pid = audit11["proposals"][0]["id"]
            assert (await embryo.root("approve", pid)).startswith(f"{pid} ready")
            entry = (await embryo.listing())["catalog"]["web_search"]
            assert entry["requires"] == [{"kind": "secret", "name": "search_key"}]
            assert entry["provided"] == [] and entry["chain"] == "verb/web_search"

            # root places the operator's key on the verb's chain, at the path the reply named
            placed = await driver.provision(
                "web_search", entry["chain"], entry["recipe_id"], paths_in(reply11)[0], key
            )
            sent += [(s.door, s.name) for s in driver.sent]
            assert await embryo.root("provide", "web_search", "search_key") == (
                "web_search: search_key provided (1/1)\n"
            )
            sent.append(("api", "provide"))

            # row 12: the provider's answer, from a computer on the verb's chain
            query = context["query"]
            embryo.script_output(WEB_SEARCH(endpoint), {"query": query}, SEARCH_RESULTS)
            words12 = SEARCH.row("12").words.format(query=query)
            signed12 = {"msg": words12, "sig": "c2ln"}
            embryo.script_output(hook, {"payload": json.dumps(signed12)}, "mike\n")
            audit12, reply12 = await embryo.public_say(signed12)
            turns.append(Turn("12", "ingress", words12, audit12, reply12, [], []))
            call12 = audit12["tools"][0]
            assert call12["status"] == "ok" and call12["chain_head"] not in (None, placed)
            assert SEARCH_RESULTS.strip() in await _gone(call12)

            # row 13: an unkeyed verb, and nothing to provide for it
            words13 = SEARCH.row("13").words
            signed13 = {"msg": words13, "sig": "c2ln"}
            embryo.script_output(hook, {"payload": json.dumps(signed13)}, "mike\n")
            audit13, reply13 = await embryo.public_say(signed13)
            turns.append(Turn("13", "ingress", words13, audit13, reply13, [], []))
            pid13 = audit13["proposals"][0]["id"]
            # row 13 gives the agent no URL to try the verb against, so nothing has
            # built it yet: approval starts the build, and `list` polls it to ready.
            assert (await embryo.root("approve", pid13)).startswith(f"{pid13} building")
            entry13 = (await embryo.listing())["catalog"]["read_url"]
            assert entry13["status"] == "ready"
            assert entry13.get("requires", []) == []

            # row 14: this capability's own page, read by that verb
            words14 = SEARCH.row("14").words.format(page=context["page"])
            embryo.script_output(READ_URL, {"url": context["page"]}, web_search.PAGE_BODY)
            signed14 = {"msg": words14, "sig": "c2ln"}
            embryo.script_output(hook, {"payload": json.dumps(signed14)}, "mike\n")
            audit14, reply14 = await embryo.public_say(signed14)
            turns.append(Turn("14", "ingress", words14, audit14, reply14, [], []))
            call14 = audit14["tools"][0]
            assert call14["name"] == "read_url" and "chain_head" not in call14
            assert web_search.PAGE_BODY.strip() in await _gone(call14)
            final = await embryo.listing()

        # the page's computer is gone; the brain is inspected for the operator's key
        brain = await driver.inspect_brain(key, log)
        assert brain["files_with_token"] == [] and "MSHKN_API_KEY" in brain["env_names"]
        # a key that outlives the run reached a guest as an upload's body and nowhere else
        assert all(key not in c for _, c in flow.host.guest.commands)
        assert key not in (embryo.brain / "state.json").read_text()
        assert all(key not in text for text in embryo.memory_texts())
        assert key not in log.getvalue()
        for written in (tmp_path / "run" / "commands").iterdir():
            assert key not in written.read_text()
        recipes_after = {r["recipe_id"] for r in (await flow.client.get("/recipes")).json()}
        verdict = judge(
            SEARCH.postconditions,
            Judged(
                turns=turns,
                final=final,
                recipes_after=recipes_after,
                preexisting=preexisting,
                brain_recipe="rcp-none",
                checks=checks,
                sent=sent,
                context=context,
                brain=brain,
            ),
        )
        assert {name: v["ok"] for name, v in verdict.items()} == dict.fromkeys(
            SEARCH.postconditions, True
        ), verdict
        # Both halves of the clause, because in run-1 only the reply carried the
        # 401 and the check passed on that fallback alone while the log path was
        # dead: `blocked` can be produced by nothing but `trial_runs`, so asserting
        # it pins the trial's computer being read at all.
        tried = verdict["searched"]["evidence"]["tried_before_the_key"]
        assert tried["refusals"] == ["401"]
        assert len(tried["blocked"]) == 1
        assert set(final["catalog"]) == {"verify_ssh", "web_search", "read_url"}
    finally:
        for name in ("searched", "read_page"):
            CHECKS.pop(name, None)


async def test_a_gateway_shaped_brain_speaks_the_liturgy(
    embryo_gateway: Embryo, flow: Flow
) -> None:
    """A namespaced model id, no effort axis and a pinned upstream: the three things
    a gateway run carries that a direct run does not, all the way through a turn
    (task 7, spec §11). `flow` has no `last_model_body`, so this reads the request
    the fake `/v1/messages` endpoint actually received off the scripted model
    itself (`ScriptedModel.last_body`, set by `scripted_asgi`).

    The prompt (not one of hatch's words) drives the scripted model to call the
    `effort` tool asking for `max` before it answers: `prior_for` caps at `high`
    (== `API_DEFAULT`), so a tool list alone can never push `resolve` above the
    unset default, and an assertion that never sees a non-None candidate would
    pass whether or not the operator's `effort_enabled=False` guard exists. Only
    a model-requested effort above `high` discriminates (coordinator review,
    task 7 fix round 1) — with the guard removed, the second call's body would
    carry `output_config: {"effort": "max"}`; with it restored, neither call ever
    does, regardless of what was requested."""
    audit, _reply = await embryo_gateway.root_say("Answer this at your maximum effort.")
    assert audit["effort"] == [None, None]
    body = embryo_gateway.model.last_body
    assert body is not None
    assert "output_config" not in body
    assert body["model"] == "anthropic/scripted-1"
    assert body["providerOptions"] == {"gateway": {"only": ["anthropic"]}}


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

    first = await _raw(["root", "say", b64(WORDS["1"])])
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
