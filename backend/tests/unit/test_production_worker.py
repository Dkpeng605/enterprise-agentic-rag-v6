from pathlib import Path
from typing import cast

import pytest

from enterprise_rag.config import AppSettings
from enterprise_rag.production_worker import (
    ProductionWorkerRuntime,
    _build_vector_store,
    build_production_worker,
)
from enterprise_rag.services import IngestionWorker


def settings(
    tmp_path: Path,
    *,
    environment: str = "development",
    vector_store: str = "milvus_remote",
) -> AppSettings:
    return AppSettings.model_validate(
        {
            "app": {"environment": environment},
            "providers": {
                "embedding": "openai_compatible",
                "vector_store": vector_store,
                "sparse_encoder": "hashing_lexical",
                "vision": "none",
            },
            "ingestion": {
                "object_store_root": str(tmp_path / "objects"),
                "embedding_dimension": 3,
            },
            "credentials": {
                "database_url": "postgresql+asyncpg://example.test/rag",
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_api_key": "embedding-key",
                "embedding_model": "test-embedding",
                "vector_store_uri": "http://milvus.example:19530",
                "vector_store_token": "milvus-token",
                "vector_store_database": "enterprise_rag",
            },
        }
    )


@pytest.mark.anyio
async def test_worker_composes_real_pipeline_against_remote_milvus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import enterprise_rag.adapters.vector_store.milvus_lite as module

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            assert args == ()
            assert kwargs == {
                "uri": "http://milvus.example:19530/",
                "token": "milvus-token",
                "db_name": "enterprise_rag",
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "MilvusClient", FakeClient)
    runtime = await build_production_worker(settings(tmp_path))

    try:
        assert runtime.worker.owner.startswith("enterprise-rag-worker:")
        assert runtime.index_revision.startswith("semantic-")
    finally:
        await runtime.aclose()
        await runtime.aclose()


@pytest.mark.anyio
async def test_worker_closes_acquired_resources_when_composition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import enterprise_rag.production_worker as module

    closed: list[str] = []

    class FakeDatabase:
        def __init__(self, url: str) -> None:
            assert url.startswith("postgresql")

        async def dispose(self) -> None:
            closed.append("database")

    class FakeVectorStore:
        def info(self) -> object:
            return type("Info", (), {"name": "fake-milvus", "version": "1"})()

        async def aclose(self) -> None:
            closed.append("vector")

    monkeypatch.setattr(module, "Database", FakeDatabase)
    monkeypatch.setattr(module, "_build_vector_store", lambda _: FakeVectorStore())

    def fail_splitter(**_: object) -> object:
        raise RuntimeError("splitter construction failed")

    monkeypatch.setattr(module, "StructureAwareSplitter", fail_splitter)

    with pytest.raises(RuntimeError, match="splitter construction failed"):
        await build_production_worker(settings(tmp_path))

    assert closed == ["vector", "database"]


@pytest.mark.anyio
async def test_worker_close_continues_after_one_resource_close_failure() -> None:
    closed: list[str] = []

    async def close_first() -> None:
        closed.append("first")
        raise RuntimeError("close failure")

    async def close_second() -> None:
        closed.append("second")

    runtime = ProductionWorkerRuntime(
        cast(IngestionWorker, object()),
        "semantic-test-v1",
        (close_second, close_first),
    )

    with pytest.raises(RuntimeError, match="close failure"):
        await runtime.aclose()

    assert closed == ["first", "second"]
    await runtime.aclose()
    assert closed == ["first", "second"]


def test_production_worker_rejects_lite_vector_store(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="single-process Milvus Lite"):
        _build_vector_store(
            settings(tmp_path, environment="production", vector_store="milvus_lite")
        )
