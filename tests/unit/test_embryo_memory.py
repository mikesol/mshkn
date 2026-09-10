"""State is free for authenticated principals (spec §2); every memory carries
its provenance and anonymous sees none (§6, §7)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from membrane.config import Settings
from membrane.memory import (
    EXTRACTION_MAX_TOKENS,
    EXTRACTION_MODEL_ID,
    HashEmbedder,
    Mem0Store,
    Provenance,
    extraction_llm,
    visible_from,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        brain=tmp_path,
        api_url="u",
        api_key="k",
        model="scripted",
        model_id="m",
        anthropic_api_key=None,
        openai_api_key=None,
    )


def test_importing_the_package_switches_mem0_telemetry_off() -> None:
    import membrane  # noqa: F401

    assert os.environ["MEM0_TELEMETRY"] == "False"


def test_visibility_by_principal() -> None:
    assert visible_from("root") is None
    assert visible_from("anonymous") == []
    assert visible_from("ssh:mike") == ["ssh:mike", "root"]


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    e = HashEmbedder()
    a = e.embed("the hatcher is mike", "add")
    assert a == e.embed("the hatcher is mike", "search") and len(a) == 64
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9
    assert e.embed_batch(["x", "y"]) == [e.embed("x", "add"), e.embed("y", "add")]


def test_scripted_store_recalls_by_provenance_and_persists(tmp_path: Path) -> None:
    store = Mem0Store.open(_settings(tmp_path), tmp_path / "memory")
    store.add("the hatcher is called mike", Provenance("root", "api", 1))
    store.add("mike likes pistachios", Provenance("ssh:mike", "ingress", 4))
    store.add("bob said hello", Provenance("telegram:bob", "ingress", 5))
    assert store.recall("who is mike", principal="anonymous") == []
    root_sees = store.recall("mike", principal="root")
    assert {"the hatcher is called mike", "mike likes pistachios"} <= set(root_sees)
    mike_sees = store.recall("mike", principal="ssh:mike")
    assert "bob said hello" not in mike_sees and "the hatcher is called mike" in mike_sees
    bob_sees = store.recall("hello", principal="telegram:bob")
    assert "bob said hello" in bob_sees and "mike likes pistachios" not in bob_sees
    store.close()
    again = Mem0Store.open(_settings(tmp_path), tmp_path / "memory")
    assert "bob said hello" in again.recall("hello", principal="root")
    again.close()
    assert (tmp_path / "memory" / "history.db").exists()


def test_anthropic_mode_uses_openai_embeddings_and_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class FakeMemory:
        def __init__(self, config: Any) -> None:
            seen["config"] = config

    monkeypatch.setattr("membrane.memory.Memory", FakeMemory)
    settings = Settings(
        brain=tmp_path,
        api_url="u",
        api_key="k",
        model="anthropic",
        model_id="claude-opus-5",
        anthropic_api_key="a",
        openai_api_key="o",
    )
    store = Mem0Store.open(settings, tmp_path / "m")
    config = seen["config"]
    assert config.embedder.provider == "openai" and config.embedder.config["api_key"] == "o"
    assert config.llm.provider == "anthropic" and config.llm.config["api_key"] == "a"
    assert config.vector_store.config.embedding_model_dims == 1536
    assert store.infer is True


def test_extraction_runs_on_a_budget_that_fits_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#107: mem0's default 2000-token budget is shared between thinking and the
    JSON, so a long deliberation truncates the document and no fact is stored."""
    seen: dict[str, Any] = {}

    class FakeMemory:
        def __init__(self, config: Any) -> None:
            seen["config"] = config

    monkeypatch.setattr("membrane.memory.Memory", FakeMemory)
    settings = Settings(
        brain=tmp_path,
        api_url="u",
        api_key="k",
        model="anthropic",
        model_id="claude-opus-5",
        anthropic_api_key="a",
        openai_api_key="o",
    )
    Mem0Store.open(settings, tmp_path / "m")
    llm = seen["config"].llm.config
    assert llm["model"] == EXTRACTION_MODEL_ID
    assert llm["max_tokens"] == EXTRACTION_MAX_TOKENS


def test_add_reports_whether_mem0_stored_a_fact(tmp_path: Path) -> None:
    class FakeMemory:
        def __init__(self, results: list[dict[str, Any]]) -> None:
            self.results = results

        def add(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"results": self.results}

    stored = Mem0Store(
        FakeMemory([{"id": "1", "memory": "mike hatched me", "event": "ADD"}]), infer=True
    )
    assert stored.add("mike hatched me", Provenance("root", "api", 1)) is True
    empty = Mem0Store(FakeMemory([]), infer=True)
    assert empty.add("mike hatched me", Provenance("root", "api", 1)) is False


def test_extraction_sends_no_parameter_the_installed_sdk_rejects() -> None:
    """The live run's defect (#107 follow-on): mem0 sends `temperature` for every
    model whose family is `haiku`, and anthropic 1.4.0's `messages.create` has no
    such parameter, so every extraction raised. Opus never hit it: mem0 suppresses
    sampling parameters for Opus >= 4.7."""
    import inspect

    import anthropic
    from mem0.configs.llms.anthropic import AnthropicConfig
    from mem0.llms.anthropic import AnthropicLLM

    llm = AnthropicLLM(AnthropicConfig(**extraction_llm("k")["config"]))
    sent: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        sent.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")])

    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    llm.generate_response([{"role": "user", "content": "hello"}])

    accepted = set(inspect.signature(anthropic.Anthropic(api_key="k").messages.create).parameters)
    assert set(sent) - accepted == set()
    assert sent["max_tokens"] == EXTRACTION_MAX_TOKENS and sent["model"] == EXTRACTION_MODEL_ID
