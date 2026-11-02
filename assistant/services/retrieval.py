"""Machine-scoped retrieval. The machine filter is applied in the database query, before any
similarity is computed, so another machine's passages can never enter the candidate set."""

import re
from collections import Counter
from dataclasses import dataclass

import numpy as np
from django.conf import settings

from ..models import DocumentChunk, Machine
from . import embeddings

_TOKEN_RE = re.compile(r'[a-z0-9]+')
_STOPWORDS = frozenset(
    'a an and are as at be by can could do does for from has have how i if in is it its me my of on or '
    'should that the their there this to was we what when where which who why will with you your'.split()
)


@dataclass
class RetrievedChunk:
    chunk: DocumentChunk
    score: float
    semantic: float
    lexical: float
    source_id: str = ''


def content_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def lexical_overlap(query_tokens: set[str], text: str) -> float:
    """Fraction of query terms present in the passage. Complements embeddings on exact codes
    such as E17, RF-20 or AF-10, which sentence embeddings represent poorly."""
    if not query_tokens:
        return 0.0
    return len(query_tokens & content_tokens(text)) / len(query_tokens)


def retrieve(
    machine: Machine,
    question: str,
    *,
    top_k: int | None = None,
    min_score: float | None = None,
    query_vector: np.ndarray | None = None,
) -> list[RetrievedChunk]:
    top_k = top_k or settings.RAG_TOP_K
    min_score = settings.RAG_MIN_SCORE if min_score is None else min_score

    # 1. Machine isolation happens here, in SQL, before any scoring.
    candidates = list(DocumentChunk.objects.filter(machine=machine).order_by('pk'))
    if not candidates:
        return []
    if any(c.machine_id != machine.pk for c in candidates):  # defence in depth
        raise RuntimeError('Cross-machine chunk leaked into candidate set')

    # 2. Score only the already-scoped candidates.
    if query_vector is None:
        query_vector = embeddings.embed_query(question)
    matrix = np.vstack([c.vector for c in candidates])
    semantic = matrix @ np.asarray(query_vector, dtype=np.float32)

    query_tokens = content_tokens(question)
    scored = []
    for chunk, sem in zip(candidates, semantic):
        lex = lexical_overlap(query_tokens, chunk.chunk_text)
        score = float(sem) + settings.RAG_LEXICAL_WEIGHT * lex
        scored.append(RetrievedChunk(chunk, score, float(sem), lex))

    scored.sort(key=lambda r: r.score, reverse=True)
    eligible = [r for r in scored if r.score >= min_score]
    selected = eligible[:top_k]

    # Let every manual of the selected machine contribute its best passages, so answers that need
    # both HP-200 manuals are not crowded out by one manual's chunks.
    per_document = Counter(r.chunk.document_id for r in selected)
    for item in eligible[top_k:]:
        if per_document[item.chunk.document_id] < settings.RAG_MIN_PER_DOCUMENT:
            selected.append(item)
            per_document[item.chunk.document_id] += 1
    selected.sort(key=lambda r: r.score, reverse=True)

    for index, item in enumerate(selected, start=1):
        item.source_id = f'S{index}'
    return selected
