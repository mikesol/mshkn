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

import pytest
from httpx import ASGITransport, AsyncClient
from membrane.cli import run
from membrane.declarations import parse_verb, render_command
from membrane.mshkn import Mshkn
from membrane.scripted import COUNTER, PAGE_TITLE, VERIFY_SSH

from mshkn.host import ExecResult
from tests.support_embryo import LITURGY, b64, split_output

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .conftest import Flow

EMBRYO = Path(__file__).resolve().parents[2] / "embryo"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFlowTestKeyFlowTestKeyFlowTestKeyFlowTestKe"
BRAIN_SCOPES = {
    "recipes": {"create": True, "read": True},
    "computers": {"create_from": "*"},
    "labels": ["verb/"],
}


async def _fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0.01)  # recipe polls: the fake build finishes in the background at once


class Embryo:
    """The two doors, in process: root's commands and the public say."""

    def __init__(self, flow: Flow, brain: Path, api: Mshkn) -> None:
        self.flow, self.brain, self.api = flow, brain, api

    async def _run(self, argv: list[str]) -> str:
        out, code = await run(argv, brain_dir=self.brain, api=self.api, sleep=_fast_sleep)
        assert code == 0, out
        await self.flow.runtime.tasks.drain(timeout=5.0)
        return out

    async def root_say(self, text: str) -> tuple[dict[str, Any], str]:
        out = await self._run(["root", "say", b64(text)])
        return split_output(out)

    async def public_say(self, payload: Any) -> tuple[dict[str, Any], str]:
        return split_output(await self._run(["say", b64(payload)]))

    async def root(self, *argv: str) -> str:
        return await self._run(["root", *argv])

    async def listing(self) -> dict[str, Any]:
        return dict(json.loads(await self.root("list")))

    def script_output(
        self, decl: dict[str, Any], params: dict[str, Any], stdout: str, code: int = 0
    ) -> str:
        cmd = render_command(parse_verb(decl), params)
        self.flow.host.guest.script[cmd] = ExecResult(code, stdout, "")
        return cmd


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
    )
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
    assert audit["principal"] == "root" and audit["tools"] == [] and "door is closed" in reply
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
    assert (await embryo.listing())["door"] == {"status": "open", "hooks": ["verify_ssh"]}

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

    # turn 6: authorization
    signed6 = {"msg": LITURGY[6], "sig": "c2ln"}
    embryo.script_output(hook, {"payload": json.dumps(signed6)}, "mike\n")
    audit, reply = await embryo.public_say(signed6)
    assert audit["principal"] == "ssh:mike" and audit["proposals"][0]["id"] == "p-4"
    assert (await embryo.root("approve", "p-4")).startswith("p-4 applied")
    assert (await embryo.listing())["policy"]["principals"]["ssh:mike"]["propose"] is True

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
    assert listing["door"]["status"] == "open"
    # memory: root's and mike's facts are recalled; anonymous left nothing
    state = json.loads((embryo.brain / "state.json").read_text())
    anonymous_turns = [w["principal"] for w in state["window"]].count("anonymous")
    assert anonymous_turns == 2  # turn 5, and the early knock


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
