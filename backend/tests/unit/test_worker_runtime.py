from pathlib import Path

import pytest

from enterprise_rag.config import AppSettings
from enterprise_rag.worker_runtime import build_production_worker


def _settings(
    tmp_path: Path,
    *,
    environment: str = "development",
    embedding: str = "openai_compatible",
) -> AppSettings:
    return AppSettings.model_validate(
        {
            "app": {"environment": environment},
            "providers": {
                "embedding": embedding,
                "vision": "none",
                "sparse_encoder": "hashing_lexical",
            },
            "ingestion": {
                "object_store_root": str(tmp_path / "objects"),
                "embedding_dimension": 3,
            },
            "credentials": {
                "database_url": "postgresql+asyncpg://example.test/rag",
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_api_key": "test-key",
                "embedding_model": "test-embedding",
            },
        }
    )


@pytest.mark.anyio
async def test_production_worker_composes_remote_embedding_and_persistent_pipeline(
    tmp_path: Path,
) -> None:
    runtime = await build_production_worker(_settings(tmp_path))

    try:
        assert runtime.index_revision.startswith("semantic-")
        assert runtime.worker.owner.startswith("enterprise-rag-worker:")
    finally:
        await runtime.aclose()
        await runtime.aclose()


@pytest.mark.anyio
async def test_production_worker_rejects_local_embedding_provider(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        environment="production",
        embedding="local_multilingual_minilm",
    )

    with pytest.raises(RuntimeError, match="requires an OpenAI-compatible Embedding Provider"):
        await build_production_worker(settings)
