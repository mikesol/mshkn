"""A model that plays the liturgy (spec §9) deterministically, so the flow and
E2E tiers prove the membrane without a third-party key (§11). It reads the
words, not the turn number, and emits real declarations that build on the host."""

from __future__ import annotations

import json
import re
from typing import Any

from membrane.model import Completion, ToolCall

PUBKEY_RE = re.compile(r"(ssh-ed25519 [A-Za-z0-9+/=]+)")
HEADER_RE = re.compile(r"^\[turn \d+ \| principal (\S+) \| door \S+\]")
FAILED_RE = re.compile(r"verb (\w+) failed to build \(proposal (p-\d+)\)")
PAGE_TITLE_RE = re.compile(r"^page_title (\S+)$")

BIRTH_TEXT = (
    "I am an embryo. I have three tools: remember, try and propose. I have no verbs, no principals "
    "and no policy of my own yet. My public door is closed until I propose a way to know who is "
    "speaking."
)
ANON_TEXT = "I do not know who you are, so I will not act or remember."
NOTHING_TEXT = "Nothing in my script answers that."


def _script(*lines: str) -> str:
    """A RUN that writes a shell script with printf: the host's legacy builder has no heredocs."""
    quoted = " ".join("'" + line.replace("'", "'\\''") + "'" for line in lines)
    return f"printf '%s\\n' {quoted}"


def VERIFY_SSH(pubkey: str) -> dict[str, Any]:  # noqa: N802 — a declaration constant with one parameter
    signers = f'mike namespaces="mshkn" {pubkey}'
    return {
        "name": "verify_ssh",
        "description": (
            "Verifies that a payload {msg, sig} carries an OpenSSH signature by the hatcher; "
            "prints mike."
        ),
        "params": {
            "type": "object",
            "properties": {"payload": {"type": "string"}},
            "required": ["payload"],
        },
        "dockerfile": (
            "FROM mshkn-base\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends jq "
            "&& apt-get clean && rm -rf /var/lib/apt/lists/*\n"
            f"RUN mkdir -p /verb && {_script(signers)} > /verb/allowed_signers\n"
            "RUN "
            + _script(
                "#!/bin/bash",
                "set -euo pipefail",
                'printf "%s" "$1" | jq -j .msg > /tmp/msg',
                'printf "%s" "$1" | jq -j .sig > /tmp/sig',
                "ssh-keygen -Y verify -f /verb/allowed_signers -I mike -n mshkn -s /tmp/sig "
                "< /tmp/msg >/dev/null && echo mike",
            )
            + " > /verb/verify.sh && chmod +x /verb/verify.sh\n"
        ),
        "entrypoint": "/verb/verify.sh {{payload}}",
        "effect": "local",
        "state": "ephemeral",
        "asserts": "ssh",
    }


PAGE_TITLE: dict[str, Any] = {
    "name": "page_title",
    "description": "Fetches a URL and reports the text of its <title>.",
    "params": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
    "dockerfile": (
        "FROM mshkn-base\n"
        "RUN mkdir -p /verb && "
        + _script(
            "#!/bin/bash",
            "set -euo pipefail",
            'curl -fsSL --max-time 20 "$1" | tr -d "\\n" | '
            'sed -n "s/.*<title>\\([^<]*\\)<\\/title>.*/\\1/p"',
        )
        + " > /verb/title.sh && chmod +x /verb/title.sh\n"
    ),
    "entrypoint": "/verb/title.sh {{url}}",
    "effect": "read",
    "state": "ephemeral",
}

COUNTER: dict[str, Any] = {
    "name": "counter",
    "description": "Counts how many times it has been called; the count lives on its own chain.",
    "params": {"type": "object", "properties": {}},
    "dockerfile": (
        "FROM mshkn-base\n"
        "RUN mkdir -p /verb && "
        + _script(
            "#!/bin/bash",
            "set -euo pipefail",
            "n=$(cat /verb/count 2>/dev/null || echo 0)",
            "n=$((n+1))",
            'echo "$n" > /verb/count',
            'echo "$n"',
        )
        + " > /verb/count.sh && chmod +x /verb/count.sh\n"
    ),
    "entrypoint": "/verb/count.sh",
    "effect": "local",
    "state": "chain",
}

DECLARATIONS = {"page_title": PAGE_TITLE, "counter": COUNTER}


def OPEN_DOOR_POLICY(principal: str) -> dict[str, Any]:  # noqa: N802
    # Ruling P1: the principal named in the door proposal gets propose rights
    # too, since liturgy turns 6, 7 and 9 arrive through the public door as
    # ssh:mike and must be able to propose.
    return {
        "principals": {
            principal: {"invoke": [], "propose": True},
            "anonymous": {"invoke": [], "propose": False},
        },
        "hooks": ["verify_ssh"],
        "door": "open",
    }


def AUTHZ_POLICY(principal: str) -> dict[str, Any]:  # noqa: N802
    return {
        "principals": {
            principal: {"invoke": "*", "propose": True},
            "anonymous": {"invoke": [], "propose": False},
        },
        "hooks": ["verify_ssh"],
        "door": "open",
    }


def parse_input(text: str) -> tuple[str, str]:
    header = HEADER_RE.match(text)
    principal = header.group(1) if header else "root"
    _, _, message = text.partition("message:\n")
    return principal, message.strip()


def _proposal(
    kind: str, title: str, payload: dict[str, Any] | str, *, supersedes: str | None = None
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "rationale": f"The liturgy asked for {title}.",
        "supersedes": supersedes,
    }
    doc[kind] = payload
    return doc


class ScriptedModel:
    def __init__(self) -> None:
        self._n = 0

    def _call(self, name: str, **input: Any) -> ToolCall:  # noqa: A002
        self._n += 1
        return ToolCall(id=f"scripted_{self._n}", name=name, input=input)

    @staticmethod
    def _text(text: str) -> Completion:
        return Completion(text=text, calls=(), content=[{"type": "text", "text": text}])

    @staticmethod
    def _calls(calls: list[ToolCall]) -> Completion:
        content = [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ]
        return Completion(text="", calls=tuple(calls), content=content)

    @staticmethod
    def _summarise(results: list[dict[str, Any]]) -> str:
        # Ruling P7: tool_result content is always the loop's json.dumps, so a
        # non-JSON block is a bug and must raise, not be swallowed. A finished
        # trial (a "trial" key) is checked before "stdout", since a trial
        # result also has an "id" but reads as "Trial <id>: <status>." per the
        # spec's Interfaces text; a verb result has no "trial" key and still
        # reads as its stripped stdout.
        parts: list[str] = []
        for block in results:
            result = json.loads(block.get("content", "{}"))
            if "trial" in result:
                parts.append(f"Trial {result['trial']}: {result.get('status')}.")
            elif "stdout" in result:
                parts.append(str(result["stdout"]).strip())
            elif "id" in result:
                parts.append(f"Proposed {result['id']}.")
            elif result.get("status") == "error":
                parts.append(f"Error: {result.get('error')}")
        return " ".join(p for p in parts if p)

    @staticmethod
    def _find_key(messages: list[dict[str, Any]]) -> str | None:
        for message in messages:
            if isinstance(message.get("content"), str):
                match = PUBKEY_RE.search(message["content"])
                if match:
                    return match.group(1)
        return None

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> Completion:
        del system, timeout
        last = messages[-1]["content"] if messages else ""
        if isinstance(last, list):
            return self._text(self._summarise(last))
        principal, message = parse_input(last)
        offered = {t["name"] for t in tools}
        can_propose = "propose" in offered

        if "hatched you" in message:
            return self._text(BIRTH_TEXT)
        if "public key is" in message and can_propose:
            key = PUBKEY_RE.search(message)
            if key is None:
                return self._text("I need an ssh-ed25519 public key to verify you.")
            hook = VERIFY_SSH(key.group(1))
            return self._calls(
                [
                    self._call(
                        "try",
                        verb=hook,
                        params={"payload": json.dumps({"msg": "probe", "sig": ""})},
                    ),
                    self._call("propose", **_proposal("verb", "verify_ssh", hook)),
                    self._call(
                        "propose",
                        **_proposal("policy", "open the door", OPEN_DOOR_POLICY("ssh:mike")),
                    ),
                ]
            )
        if "check your build" in message and can_propose:
            failed = FAILED_RE.search(last)
            if failed is None:
                return self._text("Nothing failed that I can see.")
            name, pid = failed.group(1), failed.group(2)
            if name == "verify_ssh":
                key_text = self._find_key(messages)
                if key_text is None:
                    return self._text("I no longer have the key to rebuild the hook.")
                decl = VERIFY_SSH(key_text)
            elif name in DECLARATIONS:
                decl = dict(DECLARATIONS[name])
            else:
                return self._text(f"I have no declaration for {name}.")
            decl["dockerfile"] = decl["dockerfile"].rstrip("\n") + f"\n# supersedes {pid}"
            return self._calls(
                [self._call("propose", **_proposal("verb", name, decl, supersedes=pid))]
            )
        if "Who am I" in message:
            return self._text(ANON_TEXT if principal == "anonymous" else f"You are {principal}.")
        if "verified person and an anonymous one" in message and can_propose:
            return self._calls(
                [
                    self._call(
                        "propose", **_proposal("policy", "authorization", AUTHZ_POLICY(principal))
                    )
                ]
            )
        if "given a URL" in message and can_propose:
            return self._calls(
                [
                    self._call("try", verb=PAGE_TITLE, params={"url": "https://example.com"}),
                    self._call("propose", **_proposal("verb", "page_title", PAGE_TITLE)),
                ]
            )
        page = PAGE_TITLE_RE.match(message)
        if page:
            if "page_title" not in offered:
                return self._text("I have no page_title verb yet.")
            return self._calls([self._call("page_title", url=page.group(1))])
        if "counts how many times" in message and can_propose:
            return self._calls(
                [
                    # #118: a chain verb's persistence is the one thing a single-run
                    # trial cannot show, so the trial runs twice on a scratch chain.
                    self._call("try", verb=COUNTER, runs=[{}, {}]),
                    self._call("propose", **_proposal("verb", "counter", COUNTER)),
                ]
            )
        if message == "count":
            if "counter" not in offered:
                return self._text("I have no counter verb yet.")
            return self._calls([self._call("counter")])
        return self._text(NOTHING_TEXT)
