"""Fakes shared by the embryo's unit tests: mshkn in memory, a stub model, a
list-backed memory. The flow tier uses the real app instead of FakeMshkn."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from membrane.memory import Provenance, visible_from
from membrane.model import Completion, ToolCall
from membrane.mshkn import CheckpointInfo, Deferred, MshknError, RecipeInfo, RunResult


def text_completion(text: str) -> Completion:
    return Completion(text=text, calls=(), content=[{"type": "text", "text": text}])


def tool_call_completion(name: str, **input: Any) -> Completion:  # noqa: A002
    call = ToolCall(id=f"tu_{name}", name=name, input=input)
    return Completion(
        text="",
        calls=(call,),
        content=[{"type": "tool_use", "id": call.id, "name": name, "input": input}],
    )


def b64(obj: Any) -> str:
    """A `say` payload: a string is used as-is, anything else is JSON first."""
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return base64.b64encode(text.encode()).decode()


# The liturgy's words (spec §9), fixed and shared by the flow and E2E tiers.
# Turn 2 is a template: the caller formats it with the hatcher's public key.
LITURGY = {
    1: "Hello. I am the one who hatched you. Tell me what you are and what you can do.",
    2: "Your public door is closed because you cannot tell who is speaking. Propose a way to know "
    "that a message there comes from me, and open the door. My public key is {key}",
    3: "check your build",
    4: "Who am I?",
    6: "Decide what a verified person and an anonymous one may ask of you, and record it.",
    7: "Give yourself a verb: given a URL, report the page's title. "
    "It must run on its own computer.",
    8: "page_title https://example.com",
    9: "Give yourself a verb that counts how many times it has been called.",
}


def split_output(out: str) -> tuple[dict[str, Any], str]:
    """A turn's whole stdout, split into its parsed audit line and the reply
    (everything after it, proposals included)."""
    first, _, rest = out.partition("\n")
    assert first.startswith("audit ")
    return dict(json.loads(first[len("audit ") :])), rest


@dataclass
class FakeMshkn:
    recipes: dict[str, RecipeInfo] = field(default_factory=dict)
    recipe_statuses: dict[str, list[str]] = field(default_factory=dict)
    reject_dockerfiles: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    chains: dict[str, list[str]] = field(default_factory=dict)
    busy_labels: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _n: int = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    async def create_recipe(self, dockerfile: str) -> RecipeInfo:
        self.calls.append(("create_recipe", {"dockerfile": dockerfile}))
        if dockerfile in self.reject_dockerfiles:
            raise MshknError(422, self.reject_dockerfiles[dockerfile])
        rid = "rcp-" + hashlib.sha256(dockerfile.encode()).hexdigest()[:8]
        if rid not in self.recipes:
            self.recipes[rid] = RecipeInfo(id=rid, status="pending", build_log=None)
            self.recipe_statuses.setdefault(rid, ["ready"])
        return self.recipes[rid]

    async def get_recipe(self, recipe_id: str) -> RecipeInfo:
        self.calls.append(("get_recipe", {"recipe_id": recipe_id}))
        if recipe_id not in self.recipes:
            raise MshknError(404, "Recipe not found")
        statuses = self.recipe_statuses[recipe_id]
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        log = "--- BUILD FAILED ---\napt: package nope not found" if status == "failed" else "ok"
        self.recipes[recipe_id] = RecipeInfo(id=recipe_id, status=status, build_log=log)
        return self.recipes[recipe_id]

    def _output(self, command: str) -> tuple[int, str, str]:
        return self.outputs.get(command, (0, "", ""))

    async def create_computer(
        self,
        *,
        recipe_id: str,
        command: str,
        needs: dict[str, Any],
        label: str | None,
        timeout: float,
    ) -> RunResult:
        self.calls.append(
            (
                "create_computer",
                {
                    "recipe_id": recipe_id,
                    "command": command,
                    "needs": needs,
                    "label": label,
                    "timeout": timeout,
                },
            )
        )
        if recipe_id not in self.recipes or self.recipes[recipe_id].status != "ready":
            raise MshknError(409, f"Recipe {recipe_id} is not ready")
        code, out, err = self._output(command)
        cid = self._next("comp")
        ckpt = self._next("ckpt")
        if label is not None:
            self.chains.setdefault(label, []).append(ckpt)
        return RunResult(
            computer_id=cid, exit_code=code, stdout=out, stderr=err, created_checkpoint_id=ckpt
        )

    async def fork_label(self, *, label: str, command: str, timeout: float) -> RunResult | Deferred:
        self.calls.append(("fork_label", {"label": label, "command": command, "timeout": timeout}))
        if label not in self.chains:
            raise MshknError(404, f"No checkpoint carries label {label!r}")
        if label in self.busy_labels:
            return Deferred(deferred_id=self._next("def"))
        code, out, err = self._output(command)
        ckpt = self._next("ckpt")
        self.chains[label].append(ckpt)
        return RunResult(
            computer_id=self._next("comp"),
            exit_code=code,
            stdout=out,
            stderr=err,
            created_checkpoint_id=ckpt,
        )

    async def list_checkpoints(self, label: str) -> list[CheckpointInfo]:
        self.calls.append(("list_checkpoints", {"label": label}))
        ids = self.chains.get(label, [])
        return [
            CheckpointInfo(
                id=c,
                label=label,
                created_at=f"2026-09-08T00:00:{i:02d}",
                parent_id=ids[i - 1] if i else None,
            )
            for i, c in reversed(list(enumerate(ids)))
        ]


@dataclass
class ListMemory:
    entries: list[tuple[str, Provenance]] = field(default_factory=list)

    def recall(self, query: str, *, principal: str) -> list[str]:
        visible = visible_from(principal)
        words = set(query.lower().split())
        return [
            text
            for text, prov in self.entries
            if (visible is None or prov.principal in visible) and words & set(text.lower().split())
        ]

    def add(self, text: str, provenance: Provenance) -> None:
        self.entries.append((text, provenance))

    def close(self) -> None:
        return None


@dataclass
class StubModel:
    script: list[Completion] = field(default_factory=list)
    calls: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = field(
        default_factory=list
    )

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Completion:
        self.calls.append((system, [dict(m) for m in messages], list(tools)))
        if not self.script:
            return Completion(
                text="(no script)", calls=(), content=[{"type": "text", "text": "(no script)"}]
            )
        return self.script.pop(0)
