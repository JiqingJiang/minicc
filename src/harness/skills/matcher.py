"""Hybrid lexical and local-semantic ranking of Skill metadata."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace
import logging
import re

from src.harness.retrieval.lexical import bm25_score, cosine_similarity, tokenize
from src.harness.skills.models import Skill

logger = logging.getLogger(__name__)
EmbeddingFunction = Callable[[str], tuple[str, list[float]] | None]


class SkillMatcher:
    def __init__(self, skills: Iterable[Skill], embedder: EmbeddingFunction | None = None) -> None:
        self.skills = tuple(skill for skill in skills if skill.enabled and skill.model_invocable)
        self.embedder = embedder
        self._vectors: dict[str, tuple[str, tuple[int, int], list[float]]] = {}

    def match(self, query: str, *, top_k: int = 5) -> list[Skill]:
        query = query.strip()
        if top_k <= 0 or not query or not self.skills:
            return []
        query_terms = set(tokenize(query, drop_stopwords=True))
        documents = [Counter(tokenize(_index_text(skill), drop_stopwords=True)) for skill in self.skills]
        document_frequency: dict[str, int] = {}
        for document in documents:
            for term in document:
                document_frequency[term] = document_frequency.get(term, 0) + 1
        average_length = sum(sum(document.values()) for document in documents) / max(1, len(documents))
        query_vector = self._embed_query(query)
        if query_vector is not None:
            self._ensure_skill_vectors(query_vector[0])
        hits: list[Skill] = []

        for skill, document in zip(self.skills, documents, strict=True):
            name_terms = set(tokenize(skill.name, drop_stopwords=True))
            tag_terms = set(tokenize(" ".join(skill.tags), drop_stopwords=True))
            description_terms = set(tokenize(skill.description, drop_stopwords=True))
            lexical_raw = bm25_score(query_terms, document, document_frequency, len(documents), average_length)
            lexical = lexical_raw / (lexical_raw + 1.8) if lexical_raw else 0.0
            name_hits = len(query_terms & name_terms)
            tag_hits = len(query_terms & tag_terms)
            description_hits = len(query_terms & description_terms)
            lexical = min(1.0, lexical + 0.12 * name_hits + 0.08 * tag_hits)
            semantic = self._semantic_score(skill, query_vector)
            explicit = _explicit_skill_name(query, skill.name)
            if explicit:
                score = 2.0
            elif semantic is None:
                score = lexical
            else:
                score = 0.62 * lexical + 0.38 * semantic

            if score < 0.12:
                continue
            reasons: list[str] = []
            if explicit:
                reasons.append("explicit skill name")
            if name_hits:
                reasons.append("name match")
            if tag_hits:
                reasons.append("capability tag")
            if description_hits or lexical_raw:
                reasons.append("BM25 description match")
            if semantic is not None:
                reasons.append("semantic similarity")
            hits.append(replace(skill, score=score, reasons=tuple(reasons)))

        hits.sort(key=lambda skill: (-skill.score, skill.name))
        return hits[:top_k]

    def _embed_query(self, query: str) -> tuple[str, list[float]] | None:
        if self.embedder is None:
            return None
        try:
            return self.embedder(query)
        except Exception as error:
            logger.warning("Skill query embedding failed; using BM25 ranking: %s", error)
            return None

    def _semantic_score(
        self, skill: Skill, query_embedding: tuple[str, list[float]] | None,
    ) -> float | None:
        if query_embedding is None or skill.path is None:
            return None
        try:
            stat = skill.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._vectors.get(skill.id)
            if cached is None or cached[1] != signature or cached[0] != query_embedding[0]:
                return None
            cosine = cosine_similarity(query_embedding[1], cached[2])
            if cosine is None:
                return None
            return max(0.0, min(1.0, (cosine - 0.15) / 0.7))
        except Exception as error:
            logger.warning("Skill metadata embedding failed; using BM25 ranking: %s", error)
            return None

    def _ensure_skill_vectors(self, model_name: str) -> None:
        if self.embedder is None:
            return
        missing: list[tuple[Skill, tuple[int, int]]] = []
        for skill in self.skills:
            if skill.path is None:
                continue
            try:
                stat = skill.path.stat()
            except OSError:
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._vectors.get(skill.id)
            if cached is None or cached[1] != signature or cached[0] != model_name:
                missing.append((skill, signature))
        if not missing:
            return
        try:
            batch = getattr(self.embedder, "embed_many", None)
            if callable(batch):
                embedded = batch([_index_text(skill) for skill, _ in missing])
            else:
                embedded = [self.embedder(_index_text(skill)) for skill, _ in missing]
            for (skill, signature), value in zip(missing, embedded, strict=True):
                if value is not None and value[0] == model_name and value[1]:
                    self._vectors[skill.id] = (value[0], signature, value[1])
        except Exception as error:
            logger.warning("Skill metadata embedding failed; using BM25 ranking: %s", error)


def _index_text(skill: Skill) -> str:
    return f"{skill.name} {' '.join(skill.tags)} {skill.description}"


def _explicit_skill_name(query: str, name: str) -> bool:
    for form in {name, name.replace("-", " ")}:
        if re.search(rf"(?<![\w-]){re.escape(form)}(?![\w-])", query, re.IGNORECASE):
            return True
    return False
