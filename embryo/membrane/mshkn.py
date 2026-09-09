"""mshkn as the membrane sees it: five calls, made with the scoped key (spec §3, §4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from membrane.config import Settings


@dataclass(frozen=True)
class RecipeInfo:
    id: str
    status: str
    build_log: str | None


@dataclass(frozen=True)
class RunResult:
    computer_id: str
    exit_code: int | None
    stdout: str
    stderr: str
    created_checkpoint_id: str | None


@dataclass(frozen=True)
class Deferred:
    deferred_id: str


@dataclass(frozen=True)
class CheckpointInfo:
    id: str
    label: str | None
    created_at: str
    parent_id: str | None


class MshknError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"mshkn {status}: {detail}")
        self.status = status
        self.detail = detail


class MshknApi(Protocol):
    async def create_recipe(self, dockerfile: str) -> RecipeInfo: ...

    async def get_recipe(self, recipe_id: str) -> RecipeInfo: ...

    async def create_computer(
        self,
        *,
        recipe_id: str,
        command: str,
        needs: dict[str, Any],
        label: str | None,
        timeout: float,
    ) -> RunResult: ...

    async def fork_label(
        self, *, label: str, command: str, timeout: float
    ) -> RunResult | Deferred: ...

    async def list_checkpoints(self, label: str) -> list[CheckpointInfo]: ...


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return str(body["detail"])
    return response.text


def _run(body: dict[str, Any]) -> RunResult:
    return RunResult(
        computer_id=body["computer_id"],
        exit_code=body.get("exec_exit_code"),
        stdout=body.get("exec_stdout") or "",
        stderr=body.get("exec_stderr") or "",
        created_checkpoint_id=body.get("created_checkpoint_id"),
    )


class Mshkn:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    @classmethod
    def connect(cls, settings: Settings) -> Mshkn:
        return cls(
            httpx.AsyncClient(
                base_url=settings.api_url,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=httpx.Timeout(30.0, read=330.0),
            )
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self.http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise MshknError(response.status_code, _detail(response))
        return response

    async def create_recipe(self, dockerfile: str) -> RecipeInfo:
        response = await self._request("POST", "/recipes", json={"dockerfile": dockerfile})
        body = response.json()
        return RecipeInfo(
            id=body["recipe_id"], status=body["status"], build_log=body.get("build_log")
        )

    async def get_recipe(self, recipe_id: str) -> RecipeInfo:
        body = (await self._request("GET", f"/recipes/{recipe_id}")).json()
        return RecipeInfo(
            id=body["recipe_id"], status=body["status"], build_log=body.get("build_log")
        )

    async def create_computer(
        self,
        *,
        recipe_id: str,
        command: str,
        needs: dict[str, Any],
        label: str | None,
        timeout: float,
    ) -> RunResult:
        payload: dict[str, Any] = {
            "recipe_id": recipe_id,
            "exec": command,
            "self_destruct": True,
            "needs": needs,
        }
        if label is not None:
            payload["label"] = label
        response = await self._request("POST", "/computers", json=payload, timeout=timeout)
        return _run(response.json())

    async def fork_label(self, *, label: str, command: str, timeout: float) -> RunResult | Deferred:
        payload = {
            "label": label,
            "exec": command,
            "self_destruct": True,
            "exclusive": "defer_on_conflict",
        }
        response = await self._request("POST", "/checkpoints/fork", json=payload, timeout=timeout)
        body = response.json()
        if response.status_code == 202:
            return Deferred(deferred_id=body["deferred_id"])
        return _run(body)

    async def list_checkpoints(self, label: str) -> list[CheckpointInfo]:
        body = (await self._request("GET", "/checkpoints", params={"label": label})).json()
        rows = [
            CheckpointInfo(
                id=c["id"],
                label=c.get("label"),
                created_at=c["created_at"],
                parent_id=c.get("parent_id"),
            )
            for c in body
        ]
        return sorted(rows, key=lambda c: c.created_at, reverse=True)
