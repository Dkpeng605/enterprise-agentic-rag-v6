import math

import pytest

from enterprise_rag.adapters.embeddings import HashingDenseEmbedding
from enterprise_rag.domain import ErrorCode
from enterprise_rag.domain.errors import AppError


@pytest.mark.anyio
async def test_hashing_dense_embedding_is_normalized_deterministic_and_offline() -> None:
    embedding = HashingDenseEmbedding(dimension=32)

    first, second = await embedding.embed_documents(("企业 RAG policy", "企业 RAG policy"))
    query = await embedding.embed_query("企业 RAG policy")

    assert first == second == query
    assert len(first) == 32
    assert math.isclose(sum(value * value for value in first), 1.0)
    assert embedding.info().name == "hashing_dense_e2e"
    assert embedding.info().is_remote is False


@pytest.mark.anyio
async def test_hashing_dense_embedding_rejects_non_lexical_input_and_close() -> None:
    embedding = HashingDenseEmbedding()

    with pytest.raises(AppError) as invalid:
        await embedding.embed_query("!!!")
    assert invalid.value.code is ErrorCode.EMBEDDING_INPUT_INVALID

    await embedding.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await embedding.embed_query("policy")
