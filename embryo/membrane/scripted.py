"""A model that plays hatch (embryo/capabilities/hatch.md) and security (spec §7 of
docs/superpowers/specs/2026-09-12-capabilities-design.md) deterministically, so the flow and
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
    "I am an embryo. I have four tools: remember, effort, try and propose. I have no verbs, "
    "no principals and no policy of my own yet. My public door is closed until I propose a way "
    "to know who is speaking."
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

SECRET_PATH = "/verb/token"
PAGE_URL_RE = re.compile(r"reads the page at (\S+)\.")


def _curl_page(url: str) -> str:
    """The one line that reads the gated page: an empty bearer when the token is
    not there yet, so a trial (no secrets) sees the page's 401 and `-f` exits 22."""
    return (
        'curl -fsS --max-time 20 -H "Authorization: Bearer $(cat /verb/token 2>/dev/null || true)" '
        f'"{url}"'
    )


def SECRET_PAGE(url: str) -> dict[str, Any]:  # noqa: N802 — a declaration constant with one parameter
    return {
        "name": "secret_page",
        "description": "Reads the page behind the bearer token root placed on this verb's chain.",
        "params": {"type": "object", "properties": {}},
        "dockerfile": (
            "FROM mshkn-base\n"
            "RUN mkdir -p /verb && "
            + _script("#!/bin/bash", "set -euo pipefail", _curl_page(url))
            + " > /verb/read.sh && chmod +x /verb/read.sh\n"
        ),
        "entrypoint": "/verb/read.sh",
        "effect": "read",
        "state": "chain",
        "requires": [{"kind": "secret", "name": "page_token"}],
    }


def SECRET_LENGTH(url: str) -> dict[str, Any]:  # noqa: N802
    """Row 13's scripted answer: a second chain with a second copy of the token."""
    return {
        "name": "secret_length",
        "description": "Reports the byte length of the page behind the token on this verb's chain.",
        "params": {"type": "object", "properties": {}},
        "dockerfile": (
            "FROM mshkn-base\n"
            "RUN mkdir -p /verb && "
            + _script("#!/bin/bash", "set -euo pipefail", _curl_page(url) + " | wc -c")
            + " > /verb/length.sh && chmod +x /verb/length.sh\n"
        ),
        "entrypoint": "/verb/length.sh",
        "effect": "read",
        "state": "chain",
        "requires": [{"kind": "secret", "name": "page_token"}],
    }


PLACEMENT = (
    "The trial got 401 without the token, as it should. Put it at\n\n"
    f"```\n{SECRET_PATH}\n```\n\nand say provide."
)

# web-search (docs/superpowers/specs/2026-09-17-web-search-design.md): the same
# two shapes one capability further on -- a chain verb whose secret is the
# operator's provider key rather than a token the run minted, and an unkeyed verb
# that reads a URL. The placement reply above is the answer to both capabilities'
# `requires` proposal, so nothing about the secret path is scripted twice.
SEARCH_URL_RE = re.compile(r"The service is at (\S+) and wants an API key")
SEARCH_RE = re.compile(r"^search for (.+) and tell me the first three results\.$")
READ_URL_RE = re.compile(r"^read (\S+) and tell me what it says\.$")
TRIAL_QUERY = "a probe before the key is placed"


def WEB_SEARCH(url: str) -> dict[str, Any]:  # noqa: N802 — a declaration constant with one parameter
    """Row 11's scripted answer: a chain verb that sends whatever root placed on
    its chain as a header. An empty header when the key is not there yet, so the
    trial sees the provider's 401 and `-f` exits 22."""
    return {
        "name": "web_search",
        "description": "Searches the web through the provider root holds the key for.",
        "params": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "dockerfile": (
            "FROM mshkn-base\n"
            "RUN mkdir -p /verb && "
            + _script(
                "#!/bin/bash",
                "set -euo pipefail",
                'curl -fsS --max-time 20 -G --data-urlencode "q=$1" '
                f'-H "Authorization: Bearer $(cat {SECRET_PATH} 2>/dev/null || true)" "{url}"',
            )
            + " > /verb/search.sh && chmod +x /verb/search.sh\n"
        ),
        "entrypoint": "/verb/search.sh {{query}}",
        "effect": "read",
        "state": "chain",
        "requires": [{"kind": "secret", "name": "search_key"}],
    }


READ_URL: dict[str, Any] = {
    "name": "read_url",
    "description": "Reads the page at a URL and prints what it says.",
    "params": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    "dockerfile": (
        "FROM mshkn-base\n"
        "RUN mkdir -p /verb && "
        + _script("#!/bin/bash", "set -euo pipefail", 'curl -fsS --max-time 20 "$1"')
        + " > /verb/read_url.sh && chmod +x /verb/read_url.sh\n"
    ),
    "entrypoint": "/verb/read_url.sh {{url}}",
    "effect": "read",
    # Ephemeral, and nothing required: row 13 asks for the cheapest verb there is,
    # and a verb that holds no secret has no reason to hold a chain either.
    "state": "ephemeral",
}


def OPEN_DOOR_POLICY(principal: str) -> dict[str, Any]:  # noqa: N802
    # Ruling P1: the principal named in the door proposal gets propose rights
    # too, since hatch rows 6, 7 and 9 arrive through the public door as
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
        "rationale": f"The capability asked for {title}.",
        "supersedes": supersedes,
    }
    doc[kind] = payload
    return doc


class ScriptedModel:
    def __init__(self) -> None:
        self._n = 0
        # Set by the flow tier's fake `/v1/messages` handler (tests/support_embryo.py's
        # `scripted_asgi`), not by `complete` itself: `complete` only ever sees system,
        # messages and tools, never the envelope around them (model id, output_config,
        # body_extra), so it cannot be the thing that records what the gateway sent
        # (task 7, spec §11).
        self.last_body: dict[str, Any] | None = None

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
    def _text_and_call(text: str, call: ToolCall) -> Completion:
        """A response whose text rides with a tool call (turn 8, #124): the
        script says something, then remembers, so the fix -- a turn's reply is
        every text block it said, not only the last response's -- has something
        real to prove itself against."""
        content: list[dict[str, Any]] = [
            {"type": "text", "text": text},
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input},
        ]
        return Completion(text=text, calls=(call,), content=content)

    @staticmethod
    def _names_called(messages: list[dict[str, Any]], results: list[dict[str, Any]]) -> set[str]:
        """The tool names behind a tool_result list: matched by id against the
        assistant message that made the calls, so one fork's result can be told
        apart from another's."""
        if len(messages) < 2:
            return set()
        prior = messages[-2].get("content")
        if not isinstance(prior, list):
            return set()
        ids = {r.get("tool_use_id") for r in results if isinstance(r, dict)}
        return {
            block["name"]
            for block in prior
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("id") in ids
        }

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

    @staticmethod
    def _find_page_url(messages: list[dict[str, Any]]) -> str | None:
        for message in messages:
            if isinstance(message.get("content"), str):
                match = PAGE_URL_RE.search(message["content"])
                if match:
                    return match.group(1)
        return None

    @staticmethod
    def _proposed_requires(messages: list[dict[str, Any]], results: list[dict[str, Any]]) -> bool:
        """Whether the calls these results answer proposed a verb with `requires`."""
        if len(messages) < 2 or not isinstance(messages[-2].get("content"), list):
            return False
        ids = {r.get("tool_use_id") for r in results if isinstance(r, dict)}
        return any(
            block.get("type") == "tool_use"
            and block.get("id") in ids
            and block.get("name") == "propose"
            and bool(block.get("input", {}).get("verb", {}).get("requires"))
            for block in messages[-2]["content"]
            if isinstance(block, dict)
        )

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
            names = self._names_called(messages, last)
            if "page_title" in names:
                # Turn 8 (#124, 2026-09-13-run-2): the script now says something
                # before it remembers, so a turn's reply carries both -- not only
                # the last response's text -- and proves the fix rather than
                # merely failing to trip it.
                return self._text_and_call(
                    "Example Domain — from a computer that is gone.",
                    self._call(
                        "remember", text="Example Domain, from a computer that self-destructed."
                    ),
                )
            if self._proposed_requires(messages, last):
                return self._text(f"{self._summarise(last)}\n\n{PLACEMENT}")
            if "remember" in names:
                return self._text("Noted.")
            return self._text(self._summarise(last))
        principal, message = parse_input(last)
        offered = {t["name"] for t in tools}
        can_propose = "propose" in offered

        if "hatched you" in message:
            return self._text(BIRTH_TEXT)
        if "at your maximum effort" in message:
            # The only lever that can make `resolve` return a non-None effort when the
            # run's default is unset: `prior_for` caps at `IRREVERSIBLE_EFFORT` ("high"),
            # which equals `API_DEFAULT`, so no tool list alone can push a call above the
            # floor. Only a model-requested effort above "high" can (task 7, gateway test):
            # asking for "max" here is what makes the effort-disabled path discriminating
            # rather than vacuously true.
            if "effort" not in offered:
                return self._text("I have no effort tool yet.")
            return self._calls([self._call("effort", level="max")])
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
        secret = PAGE_URL_RE.search(message)
        if secret and "bearer token" in message and can_propose:
            decl = SECRET_PAGE(secret.group(1))
            return self._calls(
                [
                    self._call("try", verb=decl, params={}),
                    self._call("propose", **_proposal("verb", "secret_page", decl)),
                ]
            )
        if message == "read the page":
            if "secret_page" not in offered:
                return self._text("I have no secret_page verb yet.")
            return self._calls([self._call("secret_page")])
        if "second verb that needs the same token" in message and can_propose:
            url = self._find_page_url(messages)
            if url is None:
                return self._text("I have no page to read a second way.")
            decl = SECRET_LENGTH(url)
            return self._calls(
                [
                    self._call("try", verb=decl, params={}),
                    self._call("propose", **_proposal("verb", "secret_length", decl)),
                ]
            )
        service = SEARCH_URL_RE.search(message)
        if service and can_propose:
            decl = WEB_SEARCH(service.group(1))
            return self._calls(
                [
                    self._call("try", verb=decl, params={"query": TRIAL_QUERY}),
                    self._call("propose", **_proposal("verb", "web_search", decl)),
                ]
            )
        query = SEARCH_RE.match(message)
        if query:
            if "web_search" not in offered:
                return self._text("I have no web_search verb yet.")
            return self._calls([self._call("web_search", query=query.group(1))])
        if "reads the page at a URL I give it" in message and can_propose:
            # No trial: the row names no URL to try it against, and an unkeyed
            # verb has nothing for a trial to show that the invocation will not.
            return self._calls([self._call("propose", **_proposal("verb", "read_url", READ_URL))])
        read = READ_URL_RE.match(message)
        if read:
            if "read_url" not in offered:
                return self._text("I have no read_url verb yet.")
            return self._calls([self._call("read_url", url=read.group(1))])
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
