"""Shared batching and response validation for embedding adapters."""

import math
import re
from collections.abc import Callable, Sequence

from enterprise_rag.domain.errors import AppError, ErrorCode

_TOKEN_ESTIMATE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")
TokenCounter = Callable[[str], int]


class EmbeddingError(AppError):
    """A stable client-safe embedding failure."""


def estimate_token_count(text: str) -> int:
    """Return the deterministic fallback count used before a model tokenizer is available."""

    return max(1, len(_TOKEN_ESTIMATE.findall(text)))


def batches(
    texts: Sequence[str],
    *,
    max_items: int,
    max_tokens: int,
    token_counter: TokenCounter = estimate_token_count,
    max_input_tokens: int | None = None,
) -> tuple[tuple[str, ...], ...]:
    if max_items <= 0 or max_tokens <= 0:
        raise ValueError("embedding batch limits must be positive")
    if max_input_tokens is not None and max_input_tokens <= 0:
        raise ValueError("max_input_tokens must be positive when provided")
    result: list[tuple[str, ...]] = []
    current: list[str] = []
    current_tokens = 0
    for text in texts:
        if not text.strip():
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INPUT_INVALID,
                "Embedding input must not contain empty text.",
            )
        token_count = max(1, int(token_counter(text)))
        if max_input_tokens is not None and token_count >= max_input_tokens:
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INPUT_INVALID,
                "One embedding input must stay below the model input token limit.",
                {"tokens": token_count, "max_input_tokens": max_input_tokens},
            )
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
