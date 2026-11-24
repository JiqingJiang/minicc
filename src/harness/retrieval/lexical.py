"""Shared Chinese/English tokenization, BM25 scoring, and cosine similarity."""

from __future__ import annotations

from collections import Counter
import logging
import math
import re

_TOKEN_RE = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[-_][a-z0-9]+)*", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "how",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "that", "the",
    "this", "to", "use", "using", "with",
}


def tokenize(text: str, *, drop_stopwords: bool = False) -> list[str]:
    """Segment Chinese with jieba search mode (silencing startup logs) and extract English terms."""
    terms: list[str] = []
    for chunk in _TOKEN_RE.findall(text.casefold()):
        if _CJK_RE.fullmatch(chunk):
            try:
                import jieba

                jieba.setLogLevel(logging.ERROR)
                terms.extend(term.strip() for term in jieba.cut_for_search(chunk) if term.strip())
            except ImportError:
                terms.extend(chunk[pos : pos + 2] for pos in range(max(1, len(chunk) - 1)))
        else:
            if chunk not in _STOP_WORDS or not drop_stopwords:
                if len(chunk) > 1:
                    terms.append(chunk)
            for part in chunk.replace("_", "-").split("-"):
                if len(part) > 1 and part != chunk and (not drop_stopwords or part not in _STOP_WORDS):
                    terms.append(part)
    return terms


def bm25_score(
    query: set[str],
    document: Counter[str],
    df: dict[str, int],
    doc_count: int,
    average_length: float,
) -> float:
    if not query or not document:
        return 0.0
    k1, b = 1.2, 0.75
    doc_length = sum(document.values())
    score = 0.0
    for term in query & document.keys():
        frequency = float(document[term])
        inverse_frequency = math.log(1 + (doc_count - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        denominator = frequency + k1 * (1 - b + b * doc_length / max(1.0, average_length))
        score += inverse_frequency * frequency * (k1 + 1) / denominator
    return score


def cosine_similarity(first: list[float], second: list[float]) -> float | None:
    if len(first) != len(second) or not first:
        return None
    dot = sum(a * b for a, b in zip(first, second, strict=True))
    norm_a = math.sqrt(sum(value * value for value in first))
    norm_b = math.sqrt(sum(value * value for value in second))
    return dot / (norm_a * norm_b) if norm_a and norm_b else None


def normalize_whitespace(text: str) -> str:
    return " ".join(text.casefold().split())
