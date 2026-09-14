"""Discoverable local Provider profiles and safe restart-bound selection."""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from enterprise_rag.adapters.embeddings.fastembed_local import (
    FASTEMBED_MODEL_PROFILES,
)
from enterprise_rag.ports.provider import ProviderKind
from enterprise_rag.ports.registry import ProviderRegistry

DEFAULT_RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
MULTILINGUAL_RERANKER_MODEL = "jinaai/jina-reranker-v2-base-multilingual"


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """A non-secret Provider choice exposed to the system administrator."""

    kind: str
    key: str
    name: str
    model: str
    label: str
    capabilities: tuple[str, ...]
    is_remote: bool
    dimension: int | None = None
    input_token_limit: int | None = None
    language_note: str | None = None
    note: str | None = None

    def to_dict(self, *, selected: bool) -> dict[str, object]:
        return {
            "kind": self.kind,
            "key": self.key,
            "name": self.name,
            "model": self.model,
            "label": self.label,
            "capabilities": list(self.capabilities),
            "is_remote": self.is_remote,
            "dimension": self.dimension,
            "input_token_limit": self.input_token_limit,
            "language_note": self.language_note,
            "note": self.note,
            "selected": selected,
            "available": True,
            "requires_restart": True,
        }


def _embedding_options() -> tuple[ProviderOption, ...]:
    options: list[ProviderOption] = []
    default_model = next(iter(FASTEMBED_MODEL_PROFILES))
    for model, profile in FASTEMBED_MODEL_PROFILES.items():
        label = "MiniLM 多语默认" if model == default_model else "BGE 中文 512"
        options.append(
            ProviderOption(
                kind=ProviderKind.EMBEDDING.value,
                key=model,
                name="local_multilingual_minilm",
                model=model,
                label=label,
                capabilities=("documents", "query", "normalized", "model_tokenizer"),
                is_remote=False,
                dimension=profile.dimension,
                input_token_limit=profile.registry_input_token_limit,
                language_note=profile.language_note,
                note=(
                    "运行时上限以实际 tokenizer 为准"
                    if model == default_model
                    else "已验证 512 维、512 input tokens"
                ),
            )
        )
    return tuple(options)


_OPTIONS: tuple[ProviderOption, ...] = (
    *_embedding_options(),
    ProviderOption(
        kind=ProviderKind.RERANKER.value,
        key=MULTILINGUAL_RERANKER_MODEL,
        name="local_cross_encoder",
        model=MULTILINGUAL_RERANKER_MODEL,
        label="Jina 多语 Reranker",
        capabilities=("cross-encoder", "onnx", "local"),
        is_remote=False,
        language_note="multilingual",
        note="适合中文/多语 Mac 演示",
    ),
    ProviderOption(
        kind=ProviderKind.RERANKER.value,
        key=DEFAULT_RERANKER_MODEL,
        name="local_cross_encoder",
        model=DEFAULT_RERANKER_MODEL,
        label="MS MARCO MiniLM",
        capabilities=("cross-encoder", "onnx", "local"),
        is_remote=False,
        language_note="English-focused",
        note="英文模型，中文场景需谨慎",
    ),
)


def load_provider_selection(path: Path) -> dict[str, str]:
    """Read the optional restart-bound selection written by the admin UI."""

    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("The Provider selection file is invalid.") from error
    if not isinstance(payload, dict):
        raise RuntimeError("The Provider selection file must contain an object.")
    allowed = {"embedding_model", "embedding_dimension", "reranker_model", "llm_model"}
    result: dict[str, str] = {}
    for key, value in payload.items():
        if key not in allowed or not isinstance(value, str) or not value.strip():
            raise RuntimeError("The Provider selection file contains an invalid field.")
        result[key] = value
    return result


class RuntimeProviderCatalog:
    """Expose the live registry and persist validated profile choices for restart."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        selection_path: Path,
        current_models: dict[str, str],
        current_embedding_dimension: int | None = None,
        current_embedding_input_token_limit: int | None = None,
    ) -> None:
        self._registry = registry
        self._selection_path = selection_path
        self._current_models = dict(current_models)
        self._current_embedding_dimension = current_embedding_dimension
        self._current_embedding_input_token_limit = current_embedding_input_token_limit
        self._selection = load_provider_selection(selection_path)

    def to_dict(self) -> dict[str, object]:
        selected = {
            "embedding": self._current_models.get("embedding", ""),
            "reranker": self._current_models.get("reranker", ""),
            "llm": self._current_models.get("llm", ""),
        }
        pending_restart = any(
            self._selection.get(field) != selected[kind]
            for field, kind in (
                ("embedding_model", "embedding"),
                ("reranker_model", "reranker"),
                ("llm_model", "llm"),
            )
            if field in self._selection
        )
        return {
            "providers": [info.to_dict() for info in self._registry.list_info()],
            "options": [self._option_dict(option, selected=selected) for option in _OPTIONS],
            "selection": {
                "embedding_model": selected["embedding"],
                "reranker_model": selected["reranker"],
                "llm_model": selected["llm"],
                "pending_restart": pending_restart,
                **(
                    {"embedding_dimension": str(self._current_embedding_dimension)}
                    if self._current_embedding_dimension is not None
                    else {}
                ),
                **(
                    {
                        "embedding_input_token_limit": str(
                            self._current_embedding_input_token_limit
                        )
                    }
                    if self._current_embedding_input_token_limit is not None
                    else {}
                ),
            },
        }

    def _option_dict(
        self, option: ProviderOption, *, selected: dict[str, str]
    ) -> dict[str, object]:
        is_selected = selected.get(option.kind, "") == option.key
        payload = option.to_dict(selected=is_selected)
        if is_selected and option.kind == ProviderKind.EMBEDDING.value:
            if self._current_embedding_dimension is not None:
                payload["dimension"] = self._current_embedding_dimension
            if self._current_embedding_input_token_limit is not None:
                payload["input_token_limit"] = self._current_embedding_input_token_limit
        return payload

    def select(self, *, kind: str, key: str) -> dict[str, object]:
        option = next(
            (item for item in _OPTIONS if item.kind == kind and item.key == key),
            None,
        )
        if option is None:
            raise ValueError("The requested Provider profile is not available.")
        field = {
            ProviderKind.EMBEDDING.value: "embedding_model",
            ProviderKind.RERANKER.value: "reranker_model",
        }.get(kind)
        if field is None:
            raise ValueError("This Provider kind is not restart-selectable.")
        selection = dict(self._selection)
        selection[field] = option.model
        if option.dimension is not None:
            selection["embedding_dimension"] = str(option.dimension)
        self._write_selection(selection)
        self._selection = selection
        return self.to_dict()

    def _write_selection(self, selection: dict[str, str]) -> None:
        self._selection_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix="provider-selection-",
            suffix=".json",
            dir=self._selection_path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(selection, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._selection_path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def selected_runtime_model(
    selection: dict[str, str], *, kind: str, default: str
) -> str:
    field = {
        ProviderKind.EMBEDDING.value: "embedding_model",
        ProviderKind.RERANKER.value: "reranker_model",
        ProviderKind.LLM.value: "llm_model",
    }[kind]
    return selection.get(field, default)


def selected_embedding_dimension(selection: dict[str, str], default: int) -> int:
    value = selection.get("embedding_dimension")
    return int(value) if value is not None else default
