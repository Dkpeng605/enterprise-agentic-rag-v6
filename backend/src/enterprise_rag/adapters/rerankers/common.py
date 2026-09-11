"""Shared client-safe failures and validation for reranking adapters."""

from collections.abc import Sequence

from enterprise_rag.domain.errors import AppError
from enterprise_rag.ports.reranker import RerankCandidate


class RerankerError(AppError):
    """A stable reranker failure that never exposes provider response bodies."""


def validate_request(
    query: str, candidates: Sequence[RerankCandidate], top_k: int
) -> None:
    if not query.strip():
        raise ValueError("query must not be blank")
    if not candidates or not 0 < top_k <= len(candidates):
        raise ValueError("top_k must fit within a non-empty candidate set")
    ids = [candidate.candidate_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("rerank candidates must have unique IDs")
