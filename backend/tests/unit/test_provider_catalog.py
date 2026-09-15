from pathlib import Path
from typing import cast

import pytest

from enterprise_rag.adapters.embeddings.fastembed_local import BGE_SMALL_ZH_MODEL
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.registry import ProviderRegistry
from enterprise_rag.services.provider_catalog import (
    SILICONFLOW_EMBEDDING_MODEL,
    SILICONFLOW_RERANKER_MODEL,
    RuntimeProviderCatalog,
    load_provider_selection,
    resolve_runtime_provider,
)


class FakeProvider:
    def __init__(self, kind: ProviderKind, name: str) -> None:
        self._info = ProviderInfo(
            kind=kind,
            name=name,
            version="test",
            capabilities=frozenset({"test"}),
            is_remote=False,
            health=ProviderHealth.HEALTHY,
        )

    def info(self) -> ProviderInfo:
        return self._info

    async def aclose(self) -> None:
        return None


def test_provider_catalog_lists_live_registry_and_persists_restart_bound_selection(
    tmp_path: Path,
) -> None:
    registry = ProviderRegistry()
    registry.register(FakeProvider(ProviderKind.EMBEDDING, "local_multilingual_minilm"))
    registry.register(FakeProvider(ProviderKind.RERANKER, "local_cross_encoder"))
    registry.register(FakeProvider(ProviderKind.LLM, "openai_compatible"))
    catalog = RuntimeProviderCatalog(
        registry=registry,
        selection_path=tmp_path / "provider-selection.json",
        current_models={
            "embedding": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            "reranker": "jinaai/jina-reranker-v2-base-multilingual",
            "llm": "minimax-m3",
        },
    )

    payload = catalog.to_dict()
    providers = cast(list[dict[str, object]], payload["providers"])
    options = cast(list[dict[str, object]], payload["options"])
    selection = cast(dict[str, object], payload["selection"])
    assert len(providers) == 3
    assert any(option["key"] == BGE_SMALL_ZH_MODEL for option in options)
    assert {option["key"] for option in options if option["kind"] == "sparse_encoder"} == {
        "hashing_lexical",
        "milvus_builtin_bm25",
    }
    assert selection["pending_restart"] is False

    updated = catalog.select(kind="embedding", key=BGE_SMALL_ZH_MODEL)
    updated_selection = cast(dict[str, object], updated["selection"])

    assert updated_selection["pending_restart"] is True
    assert updated_selection["embedding_model"] == (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    assert updated_selection["pending_embedding_model"] == BGE_SMALL_ZH_MODEL
    assert load_provider_selection(tmp_path / "provider-selection.json") == {
        "embedding_dimension": "512",
        "embedding_provider": "local_multilingual_minilm",
        "embedding_model": BGE_SMALL_ZH_MODEL,
    }


def test_provider_catalog_rejects_non_selectable_provider_kind(tmp_path: Path) -> None:
    catalog = RuntimeProviderCatalog(
        registry=ProviderRegistry(),
        selection_path=tmp_path / "provider-selection.json",
        current_models={},
    )

    try:
        catalog.select(kind="llm", key="minimax-m3")
    except ValueError as error:
        assert "not available" in str(error)
    else:
        raise AssertionError("LLM selection should remain environment-managed")


def test_siliconflow_profiles_require_backend_credentials(tmp_path: Path) -> None:
    catalog = RuntimeProviderCatalog(
        registry=ProviderRegistry(),
        selection_path=tmp_path / "provider-selection.json",
        current_models={},
    )

    options = cast(list[dict[str, object]], catalog.to_dict()["options"])
    remote = {
        str(option["key"]): option
        for option in options
        if option["key"] in {SILICONFLOW_EMBEDDING_MODEL, SILICONFLOW_RERANKER_MODEL}
    }
    assert remote[SILICONFLOW_EMBEDDING_MODEL]["dimension"] == 1024
    assert remote[SILICONFLOW_EMBEDDING_MODEL]["input_token_limit"] == 8192
    assert remote[SILICONFLOW_EMBEDDING_MODEL]["available"] is False
    assert remote[SILICONFLOW_RERANKER_MODEL]["available"] is False

    with pytest.raises(ValueError, match="SILICONFLOW_API_KEY"):
        catalog.select(kind="embedding", key=SILICONFLOW_EMBEDDING_MODEL)


def test_siliconflow_profiles_are_selectable_with_kind_credentials(tmp_path: Path) -> None:
    catalog = RuntimeProviderCatalog(
        registry=ProviderRegistry(),
        selection_path=tmp_path / "provider-selection.json",
        current_models={},
        remote_credentials=frozenset({"embedding", "reranker"}),
    )

    catalog.select(kind="embedding", key=SILICONFLOW_EMBEDDING_MODEL)
    catalog.select(kind="reranker", key=SILICONFLOW_RERANKER_MODEL)

    assert load_provider_selection(tmp_path / "provider-selection.json") == {
        "embedding_dimension": "1024",
        "embedding_model": SILICONFLOW_EMBEDDING_MODEL,
        "embedding_provider": "openai_compatible",
        "reranker_model": SILICONFLOW_RERANKER_MODEL,
        "reranker_provider": "openai_compatible",
    }


def test_sparse_profile_is_restart_selectable_and_persisted(tmp_path: Path) -> None:
    catalog = RuntimeProviderCatalog(
        registry=ProviderRegistry(),
        selection_path=tmp_path / "provider-selection.json",
        current_models={"sparse_encoder": "hashing_lexical"},
    )

    selected = catalog.select(kind="sparse_encoder", key="milvus_builtin_bm25")

    selection = cast(dict[str, object], selected["selection"])
    assert selection["pending_restart"] is True
    assert selection["sparse_encoder"] == "hashing_lexical"
    assert selection["pending_sparse_encoder"] == "milvus_builtin_bm25"
    assert load_provider_selection(tmp_path / "provider-selection.json") == {
        "sparse_encoder": "milvus_builtin_bm25"
    }


def test_provider_catalog_exposes_vision_profiles_and_persists_restart_selection(
    tmp_path: Path,
) -> None:
    registry = ProviderRegistry()
    registry.register(FakeProvider(ProviderKind.VISION, "none"))
    options = cast(
        list[dict[str, object]],
        RuntimeProviderCatalog(
            registry=registry,
            selection_path=tmp_path / "provider-selection.json",
            current_models={"vision": "none"},
            remote_credentials=frozenset({"vision"}),
        ).to_dict()["options"],
    )

    vision = {str(option["key"]): option for option in options if option["kind"] == "vision"}
    assert vision["none"]["selected"] is True
    assert vision["none"]["available"] is True
    assert vision["openai_compatible"]["available"] is True

    catalog = RuntimeProviderCatalog(
        registry=registry,
        selection_path=tmp_path / "provider-selection.json",
        current_models={"vision": "none"},
        remote_credentials=frozenset({"vision"}),
    )
    selected = catalog.select(kind="vision", key="openai_compatible")
    selection = cast(dict[str, object], selected["selection"])

    assert selection["vision_provider"] == "none"
    assert selection["pending_vision_provider"] == "openai_compatible"
    assert load_provider_selection(tmp_path / "provider-selection.json") == {
        "vision_provider": "openai_compatible"
    }


def test_applied_restart_selection_is_current_and_no_longer_reported_as_pending(
    tmp_path: Path,
) -> None:
    selection_path = tmp_path / "provider-selection.json"
    before_restart = RuntimeProviderCatalog(
        registry=ProviderRegistry(),
        selection_path=selection_path,
        current_models={"embedding": BGE_SMALL_ZH_MODEL},
        remote_credentials=frozenset({"embedding"}),
    )
    selected = before_restart.select(
        kind="embedding", key=SILICONFLOW_EMBEDDING_MODEL
    )
    assert cast(dict[str, object], selected["selection"])["pending_restart"] is True

    after_restart = RuntimeProviderCatalog(
        registry=ProviderRegistry(),
        selection_path=selection_path,
        current_models={"embedding": SILICONFLOW_EMBEDDING_MODEL},
        current_embedding_dimension=1024,
        current_embedding_input_token_limit=8192,
        remote_credentials=frozenset({"embedding"}),
    )
    runtime = cast(dict[str, object], after_restart.to_dict()["selection"])

    assert runtime["embedding_model"] == SILICONFLOW_EMBEDDING_MODEL
    assert runtime["embedding_dimension"] == "1024"
    assert runtime["embedding_input_token_limit"] == "8192"
    assert runtime["pending_restart"] is False
    assert "pending_embedding_model" not in runtime


def test_runtime_provider_supports_direct_api_mode_and_ignores_stale_model_only_selection() -> None:
    embedding_provider, embedding_model = resolve_runtime_provider(
        {"embedding_model": "old-local-model", "embedding_dimension": "384"},
        kind="embedding",
        configured_provider="openai_compatible",
        configured_model="BAAI/bge-m3",
        default_model="unused",
    )
    reranker_provider, reranker_model = resolve_runtime_provider(
        {"reranker_model": "old-local-reranker"},
        kind="reranker",
        configured_provider="openai_compatible",
        configured_model="BAAI/bge-reranker-v2-m3",
        default_model="unused",
    )

    assert (embedding_provider, embedding_model) == (
        "openai_compatible",
        "BAAI/bge-m3",
    )
    assert (reranker_provider, reranker_model) == (
        "openai_compatible",
        "BAAI/bge-reranker-v2-m3",
    )


def test_runtime_provider_keeps_legacy_remote_model_selection_compatible() -> None:
    assert resolve_runtime_provider(
        {"embedding_model": SILICONFLOW_EMBEDDING_MODEL},
        kind="embedding",
        configured_provider="local_multilingual_minilm",
        configured_model=None,
        default_model="local-model",
    ) == ("openai_compatible", SILICONFLOW_EMBEDDING_MODEL)
    assert resolve_runtime_provider(
        {"reranker_model": SILICONFLOW_RERANKER_MODEL},
        kind="reranker",
        configured_provider="local_cross_encoder",
        configured_model=None,
        default_model="local-reranker",
    ) == ("openai_compatible", SILICONFLOW_RERANKER_MODEL)


def test_provider_identity_is_part_of_current_and_pending_state(tmp_path: Path) -> None:
    selection_path = tmp_path / "provider-selection.json"
    catalog = RuntimeProviderCatalog(
        registry=ProviderRegistry(),
        selection_path=selection_path,
        current_models={
            "embedding": "same-model",
            "embedding_provider": "local_multilingual_minilm",
            "reranker": "same-reranker",
            "reranker_provider": "local_cross_encoder",
        },
        remote_credentials=frozenset({"embedding", "reranker"}),
    )
    catalog._write_selection(
        {
            "embedding_model": "same-model",
            "embedding_provider": "openai_compatible",
            "reranker_model": "same-reranker",
            "reranker_provider": "openai_compatible",
        }
    )
    catalog = RuntimeProviderCatalog(
        registry=ProviderRegistry(),
        selection_path=selection_path,
        current_models={
            "embedding": "same-model",
            "embedding_provider": "local_multilingual_minilm",
            "reranker": "same-reranker",
            "reranker_provider": "local_cross_encoder",
        },
    )

    selection = cast(dict[str, object], catalog.to_dict()["selection"])

    assert selection["pending_restart"] is True
    assert selection["pending_embedding_provider"] == "openai_compatible"
    assert selection["pending_reranker_provider"] == "openai_compatible"
