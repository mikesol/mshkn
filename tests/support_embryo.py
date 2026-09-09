"""Fakes shared by the embryo's unit tests: mshkn in memory, a stub model, a
list-backed memory. The flow tier uses the real app instead of FakeMshkn."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from membrane.liturgy import LITURGY as LITURGY  # re-exported for the tiers
from membrane.memory import Provenance, visible_from
from membrane.model import Completion, ToolCall, zero_usage
from membrane.mshkn import CheckpointInfo, Deferred, MshknError, RecipeInfo, RelayJob, RunResult


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


def split_output(out: str) -> tuple[dict[str, Any], str]:
    """A turn's whole stdout, split into its parsed audit line and the reply
    (everything after it, proposals included)."""
    first, _, rest = out.partition("\n")
    assert first.startswith("audit ")
    return dict(json.loads(first[len("audit ") :])), rest


def message_of(
    completion: Completion, *, stop_reason: str | None = None, usage: dict[str, int] | None = None
) -> dict[str, Any]:
    """A Messages API message carrying the completion, as the relay stores one."""
    return {
        "id": "msg_scripted",
        "type": "message",
        "role": "assistant",
        "model": "scripted",
        "content": completion.content,
        "stop_reason": stop_reason or ("tool_use" if completion.calls else "end_turn"),
        "stop_sequence": None,
        "usage": usage or zero_usage(),
    }


def in_progress_job() -> RelayJob:
    return RelayJob(
        id="", status="in_progress", error=None, response_status=None, response_body=None
    )


def failed_job(error: str) -> RelayJob:
    return RelayJob(id="", status="failed", error=error, response_status=None, response_body=None)


def http_error_job(status: int, body: Any) -> RelayJob:
    return RelayJob(
        id="", status="completed", error=None, response_status=status, response_body=body
    )


@dataclass
class FakeMshkn:
    recipes: dict[str, RecipeInfo] = field(default_factory=dict)
    recipe_statuses: dict[str, list[str]] = field(default_factory=dict)
    reject_dockerfiles: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    chains: dict[str, list[str]] = field(default_factory=dict)
    busy_labels: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # The relay as the brain sees it: every posted job, and the canned answers
    # handed out in order, one per job, on its first read. An entry may be a
    # message dict (a completed job) or a RelayJob (in progress, failed, non-2xx).
    relay_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    relay_answers: list[dict[str, Any] | RelayJob] = field(default_factory=list)
    relay_results: dict[str, RelayJob] = field(default_factory=dict)
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

    async def create_relay_job(
        self, *, target: str, headers: dict[str, str], body: dict[str, Any]
    ) -> str:
        self.calls.append(
            ("create_relay_job", {"target": target, "headers": headers, "body": body})
        )
        job_id = self._next("rj")
        self.relay_jobs[job_id] = {"target": target, "headers": headers, "body": body}
        return job_id

    async def get_relay_job(self, job_id: str) -> RelayJob:
        self.calls.append(("get_relay_job", {"job_id": job_id}))
        if job_id not in self.relay_jobs:
            raise MshknError(404, "Relay job not found")
        if job_id not in self.relay_results:
            answer: dict[str, Any] | RelayJob = (
                self.relay_answers.pop(0)
                if self.relay_answers
                else message_of(text_completion("(no script)"))
            )
            if isinstance(answer, RelayJob):
                self.relay_results[job_id] = RelayJob(
                    id=job_id,
                    status=answer.status,
                    error=answer.error,
                    response_status=answer.response_status,
                    response_body=answer.response_body,
                )
            else:
                self.relay_results[job_id] = RelayJob(
                    id=job_id,
                    status="completed",
                    error=None,
                    response_status=200,
                    response_body=answer,
                )
        return self.relay_results[job_id]


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

    timeouts: list[float | None] = field(default_factory=list)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> Completion:
        self.calls.append((system, [dict(m) for m in messages], list(tools)))
        self.timeouts.append(timeout)
        if not self.script:
            return Completion(
                text="(no script)", calls=(), content=[{"type": "text", "text": "(no script)"}]
            )
        return self.script.pop(0)
