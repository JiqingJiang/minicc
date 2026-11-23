"""Language-aware terms shared by memory indexing and retrieval."""

from __future__ import annotations

import re


_TOKEN_RE = re.compile(r"[\u3400-\u9fff]+|[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Segment Chinese with jieba's search mode and retain English terms."""
    import jieba

    terms: list[str] = []
    for chunk in _TOKEN_RE.findall(text.casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            terms.extend(term.strip() for term in jieba.cut_for_search(chunk) if term.strip())
        elif len(chunk) > 1:
            terms.append(chunk)
    return terms
