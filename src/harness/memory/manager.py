"""Scope routing and lifecycle orchestration shared by tools and Context."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from src.harness.memory.retrieval import MemoryHit, rank_memories
from src.harness.memory.store import MEMORY_CATEGORIES, Memory, MemoryStore

EmbeddingFunction = Callable[[str], tuple[str, list[float]] | None]
PROJECT_ONLY_CATEGORIES = {"project_fact", "decision", "procedure", "reference"}
GLOBAL_CATEGORIES = {"user_preference", "user_correction"}


class MemoryManager:
    def __init__(self, root: Path, project_id: str, embedder: EmbeddingFunction | None = None):
        self.root = root
        self.project_id = project_id
        self.embedder = embedder
        self.stores = {
            "global": MemoryStore(root / "memories.json", "global"),
            "project": MemoryStore(root / "projects" / project_id / "memories.json", "project"),
        }

    def remember(self, arguments: dict, source_ref: str | None) -> tuple[Memory, str]:
        category = arguments["category"]
        scope = arguments["scope"]
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"不支持的记忆类别：{category}")
        if scope not in self.stores:
            raise ValueError("记忆范围必须是 global 或 project。")
        if category in PROJECT_ONLY_CATEGORIES and scope != "project":
            raise ValueError(f"{category} 记忆必须保存在 project 范围。")
        if scope == "global" and category not in GLOBAL_CATEGORIES:
            raise ValueError("global 范围只允许跨项目适用的用户偏好或纠正。")
        store = self.stores[scope]
        embedded = self._embed(arguments["content"])
        embedding, model = embedded if embedded is not None else (None, None)
        return store.remember(
            arguments["content"],
            category=category,
            source_type=arguments["source_type"],
            source_ref=source_ref,
            relation=arguments.get("relation", "new"),
            related_ids=arguments.get("related_ids", []),
            effective_at=arguments.get("effective_at"),
            embedding=embedding,
            embedding_model=model,
        )

    def search(
        self,
        query: str,
        *,
        scope: str = "current",
        include_history: bool = False,
        limit: int = 5,
        threshold: float = 0.12,
    ) -> list[MemoryHit]:
        if scope not in {"current", "global", "project"}:
            raise ValueError("搜索范围必须是 current、global 或 project。")
        if not 1 <= limit <= 10:
            raise ValueError("搜索条数必须在 1 到 10 之间。")
        names = ("global", "project") if scope == "current" else (scope,)
        memories = [
            memory
            for name in names
            for memory in self.stores[name].list(include_history=include_history)
        ]
        self._ensure_embeddings(names, memories)
        return rank_memories(
            memories,
            query,
            query_embedding=self._embed(query),
            limit=limit,
            threshold=threshold,
        )

    def list(self, scope: str, *, include_history: bool = False) -> list[Memory]:
        if scope not in self.stores:
            raise ValueError("记忆范围必须是 global 或 project。")
        return self.stores[scope].list(include_history=include_history)

    def conflicts(self, scope: str) -> list[Memory]:
        self._validate_store_scope(scope)
        return self.stores[scope].conflicts()

    def update(self, scope: str, memory_id: str, content: str) -> Memory:
        self._validate_store_scope(scope)
        embedded = self._embed(content)
        embedding, model = embedded if embedded is not None else (None, None)
        return self.stores[scope].update(memory_id, content, embedding=embedding, embedding_model=model)

    def delete(self, scope: str, memory_id: str) -> None:
        self._validate_store_scope(scope)
        self.stores[scope].delete(memory_id)

    def resolve(self, scope: str, memory_id: str) -> Memory:
        self._validate_store_scope(scope)
        return self.stores[scope].resolve(memory_id)

    def history(self, scope: str, memory_id: str) -> Memory:
        self._validate_store_scope(scope)
        return self.stores[scope].history(memory_id)

    def _validate_store_scope(self, scope: str) -> None:
        if scope not in self.stores:
            raise ValueError("记忆范围必须是 global 或 project。")

    def _embed(self, text: str) -> tuple[str, list[float]] | None:
        if self.embedder is None:
            return None
        try:
            result = self.embedder(text)
            if result is None:
                return None
            model, vector = result
            if len(vector) > 8192 or not vector:
                return None
            return model, vector
        except Exception:
            logging.getLogger(__name__).warning("Memory embedding failed; using keyword retrieval for this operation.")
            return None

    def _ensure_embeddings(self, names: tuple[str, ...], memories: list[Memory]) -> None:
        if self.embedder is None:
            return
        expected_model = getattr(self.embedder, "model_name", None)
        missing = [
            memory for memory in memories
            if not memory.embedding or (expected_model and memory.embedding_model != expected_model)
        ]
        if not missing:
            return
        try:
            batch = getattr(self.embedder, "embed_many", None)
            if callable(batch):
                vectors = batch([memory.content for memory in missing])
            else:
                vectors = [self.embedder(memory.content) for memory in missing]
            by_id: dict[str, tuple[list[float], str]] = {}
            updated: dict[str, Memory] = {}
            for memory, value in zip(missing, vectors, strict=True):
                if value is None:
                    continue
                model, vector = value
                if vector and len(vector) <= 8192:
                    by_id[memory.id] = (vector, model)
                    memory.embedding, memory.embedding_model = vector, model
                    updated[memory.id] = memory
            for name in names:
                scoped = {identity: (vector, model) for identity, (vector, model) in by_id.items() if updated[identity].scope == name}
                self.stores[name].set_embeddings(scoped)
        except Exception:
            logging.getLogger(__name__).warning("Memory embedding failed; using keyword retrieval for this operation.")
