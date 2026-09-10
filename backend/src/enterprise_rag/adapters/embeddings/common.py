"""Shared batching and response validation for embedding adapters."""

import math
import re
from collections.abc import Sequence

from enterprise_rag.domain.errors import AppError, ErrorCode

_TOKEN_ESTIMATE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")


class EmbeddingError(AppError):
    """A stable client-safe embedding failure."""


def batches(
    texts: Sequence[str], *, max_items: int, max_tokens: int
) -> tuple[tuple[str, ...], ...]:
    result: list[tuple[str, ...]] = []
    current: list[str] = []
    current_tokens = 0
    for text in texts:
        if not text.strip():
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INPUT_INVALID,
                "Embedding input must not contain empty text.",
            )
        token_count = max(1, len(_TOKEN_ESTIMATE.findall(text)))
        if token_count > max_tokens:
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INPUT_INVALID,
                "One embedding input exceeds the configured token budget.",
                {"estimated_tokens": token_count, "max_tokens": max_tokens},
            )
        if current and (len(current) >= max_items or current_tokens + token_count > max_tokens):
            result.append(tuple(current))
            current = []
            current_tokens = 0
        current.append(text)
        current_tokens += token_count
    if current:
        result.append(tuple(current))
    return tuple(result)


def validated_vectors(
    vectors: Sequence[Sequence[float]], *, expected_count: int, dimension: int
) -> list[list[float]]:
    if len(vectors) != expected_count:
        raise EmbeddingError(
            ErrorCode.EMBEDDING_INVALID_RESPONSE,
            "The embedding Provider returned an unexpected vector count.",
            {"expected_count": expected_count, "actual_count": len(vectors)},
        )
    normalized: list[list[float]] = []
    for vector in vectors:
        if len(vector) != dimension:
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INVALID_RESPONSE,
                "The embedding Provider returned an unexpected vector dimension.",
                {"expected_dimension": dimension, "actual_dimension": len(vector)},
            )
        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError, OverflowError) as error:
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INVALID_RESPONSE,
                "The embedding Provider returned a non-numeric vector.",
            ) from error
        if any(not math.isfinite(value) for value in values):
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INVALID_RESPONSE,
                "The embedding Provider returned a non-finite vector.",
            )
        norm = math.sqrt(sum(value * value for value in values))
        if norm <= 0:
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INVALID_RESPONSE,
                "The embedding Provider returned a zero vector.",
            )
        normalized.append([value / norm for value in values])
    return normalized
