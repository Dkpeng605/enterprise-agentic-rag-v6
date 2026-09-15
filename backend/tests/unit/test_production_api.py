from pathlib import Path

import pytest

from enterprise_rag.config import AppSettings
from enterprise_rag.production_api import build_production_api_app


def settings(tmp_path: Path, *, embedding: str = "openai_compatible") -> AppSettings:
    return AppSettings.model_validate(
        {
            "app": {
                "environment": "production",
                "public_base_url": "https://rag.example.com",
                "mcp_public_base_url": "https://rag.example.com",
                "commit_sha": "test-production-api",
            },
            "providers": {
                "llm": "openai_compatible",
                "embedding": embedding,
                "reranker": "openai_compatible",
                "vector_store": "milvus_remote",
                "sparse_encoder": "milvus_builtin_bm25",
                "vision": "none",
            },
            "ingestion": {
                "object_store_root": str(tmp_path / "objects"),
                "embedding_dimension": 1024,
            },
            "credentials": {
                "database_url": "postgresql+asyncpg://example.test/rag",
                "session_secret": "session-secret-with-more-than-32-bytes",
                "admin_bootstrap_email": "admin@example.com",
                "admin_bootstrap_password": "not-admin-password",
                "mcp_token_pepper": "mcp-pepper-with-more-than-32-bytes",
                "metrics_token": "metrics-token",
                "llm_base_url": "https://llm.example/v1",
                "llm_api_key": "llm-key",
                "llm_model": "test-llm",
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_api_key": "embedding-key",
                "embedding_model": "BAAI/bge-m3",
                "rerank_base_url": "https://rerank.example/v1",
                "rerank_api_key": "rerank-key",
                "rerank_model": "BAAI/bge-reranker-v2-m3",
                "vector_store_uri": "https://milvus.example",
                "vector_store_token": "milvus-token",
                "vector_store_database": "enterprise_rag",
            },
        }
    )


@pytest.mark.anyio
async def test_production_api_composes_remote_pipeline_without_embedded_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import enterprise_rag.adapters.vector_store.milvus_lite as milvus_module

    class FakeMilvusClient:
        instances: list[dict[str, object]] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.instances.append({"args": args, "kwargs": kwargs})

        def close(self) -> None:
            return None

    monkeypatch.setattr(milvus_module, "MilvusClient", FakeMilvusClient)
    application = build_production_api_app(settings(tmp_path))

    assert FakeMilvusClient.instances == [
        {
            "args": (),
            "kwargs": {
                "uri": "https://milvus.example/",
                "token": "milvus-token",
                "db_name": "enterprise_rag",
            },
        }
    ]
    providers = application.state.production_provider_registry.list_info()
    selected = {(item.kind.value, item.name) for item in providers}
    assert {
        ("embedding", "openai_compatible"),
        ("reranker", "openai_compatible"),
        ("llm", "openai_compatible"),
        ("sparse_encoder", "milvus_builtin_bm25"),
        ("vector_store", "milvus_remote"),
    } <= selected
    assert application.state.production_index_revision.startswith("semantic-")
    assert "/api/v1/queries" in application.openapi()["paths"]
    assert not application.state.production_background_tasks

    async with application.router.lifespan_context(application):
        pass


def test_production_api_rejects_local_embedding(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="embedding=local_multilingual_minilm"):
        build_production_api_app(settings(tmp_path, embedding="local_multilingual_minilm"))
