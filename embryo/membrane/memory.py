"""Memory (spec §7): mem0 in process, on disk under /brain/memory, every
memory tagged with who said it, through which door, on which turn."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mem0 import Memory
from mem0.configs.base import MemoryConfig
from mem0.embeddings.base import EmbeddingBase
from mem0.embeddings.configs import EmbedderConfig
from mem0.utils.factory import EmbedderFactory

from membrane.config import DEFAULT_ANTHROPIC_BASE_URL
from membrane.principals import ANONYMOUS, ROOT

if TYPE_CHECKING:
    from pathlib import Path

    from membrane.config import Settings

USER_ID = "brain"
TOP_K = 8
HASH_DIMS = 64
OPENAI_EMBEDDER = "text-embedding-3-small"
OPENAI_DIMS = 1536
# Fact extraction is a JSON-shaped chore, not the brain's thinking (#107). mem0
# spends one budget on both the model's deliberation and the document it emits,
# so a long deliberation truncates the JSON and mem0 silently stores nothing.
EXTRACTION_MODEL_ID = "claude-haiku-4-5-20251001"
# The same model as the gateway's catalogue names it. Not composed from the id
# above: a gateway is a second naming authority, not a prefix on the first, and
# it publishes no dated slug at all (spec §5.1, corrected 2026-09-15).
EXTRACTION_GATEWAY_MODEL_ID = "anthropic/claude-haiku-4.5"
EXTRACTION_MAX_TOKENS = 4000


@dataclass(frozen=True)
class Provenance:
    principal: str
    door: str
    turn: int


class MemoryStore(Protocol):
    def recall(self, query: str, *, principal: str) -> list[str]: ...

    def add(self, text: str, provenance: Provenance) -> bool: ...

    def close(self) -> None: ...


def visible_from(principal: str) -> list[str] | None:
    """Whose memories a principal may see: root all, anonymous none, others their own and root's."""
    if principal == ROOT:
        return None
    if principal == ANONYMOUS:
        return []
    return [principal, ROOT]


class HashEmbedder(EmbeddingBase):  # type: ignore[misc]
    """Deterministic bag-of-words hashing: no model, no network, same vector every time."""

    def __init__(self, config: Any = None) -> None:
        super().__init__(config)
        self.dims: int = getattr(self.config, "embedding_dims", None) or HASH_DIMS

    def embed(
        self,
        text: str,
        memory_action: Literal["add", "search", "update"] | None = None,  # noqa: ARG002
    ) -> list[float]:
        vector = [0.0] * self.dims
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dims] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


EmbedderFactory.provider_to_class["hash"] = "membrane.memory.HashEmbedder"


def extraction_model_id(base_url: str) -> str:
    """The extraction model as the endpoint at `base_url` names it: the dated id
    for the Anthropic API, the gateway's own slug for a gateway."""
    if base_url == DEFAULT_ANTHROPIC_BASE_URL:
        return EXTRACTION_MODEL_ID
    return EXTRACTION_GATEWAY_MODEL_ID


def extraction_llm(api_key: str | None, base_url: str) -> dict[str, Any]:
    """mem0's LLM config for fact extraction. `enable_sampling_parameters` is not
    a preference: mem0 sends `temperature` for every model whose family is `haiku`,
    and the Anthropic SDK's `messages.create` has no such parameter, so extraction
    raises without it. Opus never showed this, because mem0 already suppresses
    sampling parameters for Opus >= 4.7.

    `anthropic_base_url` is not a preference either. This client is in process and
    never touches the relay, so it is the one caller that would keep dialling
    api.anthropic.com with a gateway key after everything else had moved (§5.1)."""
    return {
        "provider": "anthropic",
        "config": {
            "model": extraction_model_id(base_url),
            "api_key": api_key,
            "anthropic_base_url": base_url,
            "max_tokens": EXTRACTION_MAX_TOKENS,
            "enable_sampling_parameters": False,
        },
    }


class Mem0Store:
    def __init__(self, memory: Memory, *, infer: bool) -> None:
        self.memory = memory
        self.infer = infer

    @classmethod
    def open(cls, settings: Settings, memory_dir: Path) -> Mem0Store:
        memory_dir.mkdir(parents=True, exist_ok=True)
        if settings.model == "scripted":
            embedder = EmbedderConfig.model_construct(
                provider="hash", config={"embedding_dims": HASH_DIMS}
            )
            dims = HASH_DIMS
            llm: dict[str, Any] = {
                "provider": "anthropic",
                "config": {"model": settings.model_id, "api_key": "scripted"},
            }
            infer = False
        else:
            embedder = EmbedderConfig(
                provider="openai",
                config={"model": OPENAI_EMBEDDER, "api_key": settings.openai_api_key},
            )
            dims = OPENAI_DIMS
            llm = extraction_llm(settings.anthropic_api_key, settings.anthropic_base_url)
            infer = True
        config = MemoryConfig(
            vector_store={
                "provider": "qdrant",
                "config": {
                    "path": str(memory_dir / "qdrant"),
                    "on_disk": True,
                    "collection_name": "brain",
                    "embedding_model_dims": dims,
                },
            },
            embedder=embedder,
            llm=llm,
            history_db_path=str(memory_dir / "history.db"),
        )
        return cls(Memory(config), infer=infer)

    def recall(self, query: str, *, principal: str) -> list[str]:
        visible = visible_from(principal)
        if visible == []:
            return []
        filters: dict[str, Any] = {"user_id": USER_ID}
        if visible is not None:
            filters["principal"] = {"in": visible}
        found = self.memory.search(query, filters=filters, top_k=TOP_K)
        return [str(r["memory"]) for r in found.get("results", [])]

    def add(self, text: str, provenance: Provenance) -> bool:
        """Whether mem0 stored anything. It catches its own extraction failures and
        returns no results, so this is the only honest answer to "was it written" (#107)."""
        result = self.memory.add(
            text,
            user_id=USER_ID,
            infer=self.infer,
            metadata={
                "principal": provenance.principal,
                "door": provenance.door,
                "turn": provenance.turn,
            },
        )
        return bool(result.get("results"))

    def close(self) -> None:
        self.memory.vector_store.client.close()
        # mem0's llm and embedder providers each own an SDK client (anthropic.Anthropic,
        # openai.OpenAI) that wraps an httpx.Client; left open, its connection pool leaks a
        # socket and, in some SDK versions, an event loop, both flagged as ResourceWarnings
        # at garbage-collection time. HashEmbedder has no such client.
        for component in (self.memory.llm, self.memory.embedding_model):
            client = getattr(component, "client", None)
            if client is not None and hasattr(client, "close"):
                client.close()
