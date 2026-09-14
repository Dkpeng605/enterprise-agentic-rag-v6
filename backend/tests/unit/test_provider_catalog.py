from pathlib import Path
from typing import cast

from enterprise_rag.adapters.embeddings.fastembed_local import BGE_SMALL_ZH_MODEL
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.registry import ProviderRegistry
from enterprise_rag.services.provider_catalog import RuntimeProviderCatalog, load_provider_selection


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
    assert selection["pending_restart"] is False

    updated = catalog.select(kind="embedding", key=BGE_SMALL_ZH_MODEL)
    updated_selection = cast(dict[str, object], updated["selection"])

    assert updated_selection["pending_restart"] is True
    assert load_provider_selection(tmp_path / "provider-selection.json") == {
        "embedding_dimension": "512",
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
