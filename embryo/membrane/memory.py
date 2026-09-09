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

from membrane.principals import ANONYMOUS, ROOT

if TYPE_CHECKING:
    from pathlib import Path

    from membrane.config import Settings

USER_ID = "brain"
TOP_K = 8
HASH_DIMS = 64
OPENAI_EMBEDDER = "text-embedding-3-small"
OPENAI_DIMS = 1536


@dataclass(frozen=True)
class Provenance:
    principal: str
    door: str
    turn: int


class MemoryStore(Protocol):
    def recall(self, query: str, *, principal: str) -> list[str]: ...

    def add(self, text: str, provenance: Provenance) -> None: ...

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
            llm = {
                "provider": "anthropic",
                "config": {"model": settings.model_id, "api_key": settings.anthropic_api_key},
            }
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

    def add(self, text: str, provenance: Provenance) -> None:
        self.memory.add(
            text,
            user_id=USER_ID,
            infer=self.infer,
            metadata={
                "principal": provenance.principal,
                "door": provenance.door,
                "turn": provenance.turn,
            },
        )

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
