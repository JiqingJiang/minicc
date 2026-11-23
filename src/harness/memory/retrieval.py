"""Bounded hybrid ranking of memory candidates."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from src.harness.memory.store import Memory
from src.harness.memory.tokenization import tokenize


@dataclass(frozen=True)
class MemoryHit:
    memory: Memory
    score: float
    reasons: tuple[str, ...]


def rank_memories(
    memories: list[Memory],
    query: str,
    *,
    query_embedding: tuple[str, list[float]] | None = None,
    limit: int = 5,
    threshold: float = 0.12,
) -> list[MemoryHit]:
    query_terms = set(tokenize(query))
    if not memories or (not query_terms and query_embedding is None):
        return []

    documents = [Counter(tokenize(" ".join((item.content, item.category, *item.keywords, *item.entities)))) for item in memories]
    document_frequency: dict[str, int] = {}
    for terms in documents:
        for term in terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    average_length = sum(sum(terms.values()) for terms in documents) / max(1, len(documents))
    query_vector = query_embedding[1] if query_embedding else None
    model_name = query_embedding[0] if query_embedding else None
    hits: list[MemoryHit] = []
    for item, terms in zip(memories, documents, strict=True):
        lexical = _bm25(query_terms, terms, document_frequency, len(memories), average_length)
        lexical_score = lexical / (lexical + 1.8) if lexical else 0.0
        semantic_score = None
        if query_vector and item.embedding and item.embedding_model == model_name:
            semantic_score = _cosine(query_vector, item.embedding)
        reasons = []
        if lexical:
            reasons.append("keyword overlap")
        phrase = _normalize(query) in _normalize(item.content) if query.strip() else False
        if phrase:
            reasons.append("exact phrase")
            lexical_score = min(1.0, lexical_score + 0.15)
        if semantic_score is not None:
            reasons.append("semantic similarity")
            # Shift cosine to a non-negative range; preserve lexical precision.
            semantic_score = max(0.0, min(1.0, (semantic_score - 0.15) / 0.7))
            score = 0.62 * lexical_score + 0.38 * semantic_score
        else:
            score = lexical_score
        if item.status == "conflict":
            reasons.append("unresolved conflict group")
        if item.scope == "project":
            score = min(1.0, score + 0.015)
        if score >= threshold:
            hits.append(MemoryHit(item, score, tuple(reasons)))
    hits.sort(key=lambda hit: (hit.score, hit.memory.effective_at or hit.memory.created_at), reverse=True)

    # If one side of an unresolved conflict is recalled, include its peers so
    # the model cannot accidentally treat a partial result as settled truth.
    selected = hits[:limit]
    selected_ids = {hit.memory.id for hit in selected}
    selected_groups = {hit.memory.conflict_group for hit in selected if hit.memory.conflict_group}
    if selected_groups:
        ranked_by_id = {hit.memory.id: hit for hit in hits}
        for memory in memories:
            if memory.conflict_group in selected_groups and memory.id not in selected_ids:
                selected.append(ranked_by_id.get(memory.id) or MemoryHit(memory, 0.0, ("unresolved conflict peer",)))
                selected_ids.add(memory.id)
    return selected


def _bm25(query: set[str], document: Counter[str], df: dict[str, int], count: int, average_length: float) -> float:
    if not query or not document:
        return 0.0
    k1, b = 1.2, 0.75
    score = 0.0
    for term in query & document.keys():
        frequency = float(document[term])
        inverse_frequency = math.log(1 + (count - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        document_length = sum(document.values())
        denominator = frequency + k1 * (1 - b + b * document_length / max(1.0, average_length))
        score += inverse_frequency * frequency * (k1 + 1) / denominator
    return score


def _cosine(first: list[float], second: list[float]) -> float | None:
    if len(first) != len(second) or not first:
        return None
    dot = sum(a * b for a, b in zip(first, second, strict=True))
    norm_a = math.sqrt(sum(value * value for value in first))
    norm_b = math.sqrt(sum(value * value for value in second))
    return dot / (norm_a * norm_b) if norm_a and norm_b else None


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())
