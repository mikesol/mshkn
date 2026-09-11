"""The membrane speaks to mshkn's REST API with the scoped key and nothing else."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from membrane.config import Settings
from membrane.mshkn import Deferred, Mshkn, MshknError, RunResult

from tests.support_embryo import FakeMshkn


def _client(handler: Any) -> Mshkn:
    return Mshkn(
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://m",
            headers={"Authorization": "Bearer mk-scoped"},
        )
    )


async def test_connect_uses_the_scoped_key_and_the_api_url(tmp_path: Any) -> None:
    settings = Settings(
        brain=tmp_path,
        api_url="http://api",
        api_key="mk-x",
        model="scripted",
        model_id="m",
        anthropic_api_key=None,
        openai_api_key=None,
    )
    client = Mshkn.connect(settings)
    assert client.http.base_url.host == "api"
    assert client.http.headers["Authorization"] == "Bearer mk-x"
    await client.aclose()


async def test_create_recipe_returns_info_and_raises_on_422() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if "FROM mshkn-base" not in body["dockerfile"]:
            return httpx.Response(422, json={"detail": "recipes must be built FROM mshkn-base"})
        return httpx.Response(
            202, json={"recipe_id": "rcp-1", "status": "pending", "content_hash": "h"}
        )

    api = _client(handler)
    info = await api.create_recipe("FROM mshkn-base\nRUN true")
    assert (info.id, info.status, info.build_log) == ("rcp-1", "pending", None)
    assert seen[0].headers["Authorization"] == "Bearer mk-scoped"
    with pytest.raises(MshknError) as exc:
        await api.create_recipe("FROM python:3.12")
    assert exc.value.status == 422 and "mshkn-base" in exc.value.detail


async def test_get_recipe_carries_the_build_log() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/recipes/rcp-1"
        return httpx.Response(
            200,
            json={
                "recipe_id": "rcp-1",
                "status": "failed",
                "content_hash": "h",
                "build_log": "boom",
            },
        )

    info = await _client(handler).get_recipe("rcp-1")
    assert info.status == "failed" and info.build_log == "boom"


async def test_create_computer_posts_exec_and_self_destruct() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {
            "recipe_id": "rcp-1",
            "exec": "echo hi",
            "self_destruct": True,
            "needs": {"ram": "256MB", "cores": 1},
            "label": "verb/counter",
        }
        return httpx.Response(
            200,
            json={
                "computer_id": "comp-1",
                "url": "u",
                "exec_exit_code": 0,
                "exec_stdout": "hi\n",
                "exec_stderr": "",
                "created_checkpoint_id": "ckpt-1",
            },
        )

    result = await _client(handler).create_computer(
        recipe_id="rcp-1",
        command="echo hi",
        needs={"ram": "256MB", "cores": 1},
        label="verb/counter",
        timeout=30.0,
    )
    assert result == RunResult("comp-1", 0, "hi\n", "", "ckpt-1")


async def test_fork_label_returns_a_run_or_a_deferral() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "computer_id": "comp-2",
                    "checkpoint_id": "ckpt-1",
                    "exec_exit_code": 3,
                    "exec_stdout": "2",
                    "exec_stderr": "",
                    "created_checkpoint_id": "ckpt-2",
                },
            ),
            httpx.Response(202, json={"deferred_id": "def-1", "status": "queued"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/checkpoints/fork"
        assert body["label"] == "verb/counter" and body["exclusive"] == "defer_on_conflict"
        assert body["self_destruct"] is True and body["exec"] == "count"
        return next(responses)

    api = _client(handler)
    assert await api.fork_label(label="verb/counter", command="count", timeout=30.0) == RunResult(
        "comp-2", 3, "2", "", "ckpt-2"
    )
    assert await api.fork_label(label="verb/counter", command="count", timeout=30.0) == Deferred(
        "def-1"
    )


async def test_list_checkpoints_is_newest_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["label"] == "verb/counter"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "a",
                    "checkpoint_id": "a",
                    "label": "verb/counter",
                    "created_at": "2026-09-08T01:00:00",
                    "r2_prefix": "x",
                    "pinned": False,
                    "parent_id": None,
                },
                {
                    "id": "b",
                    "checkpoint_id": "b",
                    "label": "verb/counter",
                    "created_at": "2026-09-08T02:00:00",
                    "r2_prefix": "x",
                    "pinned": False,
                    "parent_id": "a",
                },
            ],
        )

    heads = await _client(handler).list_checkpoints("verb/counter")
    assert [c.id for c in heads] == ["b", "a"] and heads[0].parent_id == "a"


async def test_other_errors_carry_status_and_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Scope labels does not cover 'brain'"})

    with pytest.raises(MshknError) as exc:
        await _client(handler).list_checkpoints("brain")
    assert exc.value.status == 403 and "labels" in exc.value.detail
    with pytest.raises(MshknError) as exc:
        await _client(lambda _request: httpx.Response(500, text="oops")).get_recipe("x")
    assert exc.value.status == 500 and exc.value.detail == "oops"


async def test_error_with_non_dict_json_body_falls_back_to_raw_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=["not", "a", "dict"])

    with pytest.raises(MshknError) as exc:
        await _client(handler).get_recipe("x")
    assert exc.value.status == 404 and exc.value.detail == '["not","a","dict"]'


async def test_create_computer_without_a_label_omits_it_from_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "label" not in body
        return httpx.Response(
            200,
            json={
                "computer_id": "comp-3",
                "url": "u",
                "exec_exit_code": 0,
                "exec_stdout": "",
                "exec_stderr": "",
                "created_checkpoint_id": None,
            },
        )

    result = await _client(handler).create_computer(
        recipe_id="rcp-1", command="echo hi", needs={}, label=None, timeout=30.0
    )
    assert result == RunResult("comp-3", 0, "", "", None)


async def test_a_transport_error_is_an_mshkn_error() -> None:
    """#109: a timeout during a trial's create_computer crashed the turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    api = Mshkn(httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler)))
    with pytest.raises(MshknError) as info:
        await api.get_recipe("rcp-1")
    assert info.value.status == 0 and "ReadTimeout" in info.value.detail
    await api.aclose()


async def test_create_relay_job_posts_the_target_headers_and_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"job_id": "rj-1", "status": "queued"})

    job_id = await _client(handler).create_relay_job(
        target="https://api.anthropic.com/v1/messages",
        headers={"x-api-key": "sk", "anthropic-version": "2023-06-01"},
        body={"model": "m", "messages": []},
    )
    assert job_id == "rj-1"
    assert seen[0].method == "POST" and seen[0].url.path == "/relay"
    assert json.loads(seen[0].content) == {
        "target": "https://api.anthropic.com/v1/messages",
        "method": "POST",
        "forward_headers": {"x-api-key": "sk", "anthropic-version": "2023-06-01"},
        "body": {"model": "m", "messages": []},
    }


async def test_get_relay_job_reads_the_status_the_response_and_the_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/relay/")
        if request.url.path.endswith("rj-done"):
            return httpx.Response(
                200,
                json={
                    "job_id": "rj-done",
                    "status": "completed",
                    "error": None,
                    "response": {"status": 200, "headers": {}, "body": {"content": []}},
                    "delivery": None,
                },
            )
        if request.url.path.endswith("rj-wait"):
            return httpx.Response(
                200,
                json={
                    "job_id": "rj-wait",
                    "status": "in_progress",
                    "error": None,
                    "response": None,
                    "delivery": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "job_id": "rj-bad",
                "status": "failed",
                "error": "HTTP 529",
                "response": {"status": 529, "headers": {}, "body": "Overloaded"},
                "delivery": None,
            },
        )

    api = _client(handler)
    done = await api.get_relay_job("rj-done")
    assert (done.status, done.response_status, done.response_body, done.error) == (
        "completed",
        200,
        {"content": []},
        None,
    )
    wait = await api.get_relay_job("rj-wait")
    assert (
        wait.status == "in_progress" and wait.response_status is None and wait.response_body is None
    )
    bad = await api.get_relay_job("rj-bad")
    assert bad.status == "failed" and bad.error == "HTTP 529" and bad.response_status == 529


async def test_delete_checkpoint_calls_the_route() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "deleted"})

    api = _client(handler)
    await api.delete_checkpoint("ckpt-1")
    assert seen == [("DELETE", "/checkpoints/ckpt-1")]
    await api.aclose()


async def test_delete_checkpoint_raises_on_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Scope labels does not cover 'verb/trial/t-1'"})

    api = _client(handler)
    with pytest.raises(MshknError) as exc:
        await api.delete_checkpoint("ckpt-1")
    assert exc.value.status == 403 and "Scope labels" in exc.value.detail
    await api.aclose()


async def test_the_fake_forgets_a_deleted_checkpoint() -> None:
    api = FakeMshkn()
    api.chains["verb/trial/t-1"] = ["ckpt-a", "ckpt-b"]
    await api.delete_checkpoint("ckpt-a")
    assert api.chains["verb/trial/t-1"] == ["ckpt-b"]
    await api.delete_checkpoint("ckpt-b")
    assert "verb/trial/t-1" not in api.chains
    assert ("delete_checkpoint", {"checkpoint_id": "ckpt-b"}) in api.calls
