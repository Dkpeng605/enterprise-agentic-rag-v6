"""Discoverable local Provider profiles and safe restart-bound selection."""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from enterprise_rag.adapters.embeddings.fastembed_local import (
    FASTEMBED_MODEL_PROFILES,
)
from enterprise_rag.ports.provider import ProviderKind
from enterprise_rag.ports.registry import ProviderRegistry

DEFAULT_RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
MULTILINGUAL_RERANKER_MODEL = "jinaai/jina-reranker-v2-base-multilingual"
SILICONFLOW_EMBEDDING_MODEL = "BAAI/bge-m3"
SILICONFLOW_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

_REMOTE_PROFILE_MODELS = {
    "embedding": {SILICONFLOW_EMBEDDING_MODEL},
    "reranker": {SILICONFLOW_RERANKER_MODEL},
}


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """A non-secret Provider choice exposed to the system administrator."""

    kind: str
    key: str
    name: str
    model: str
    label: str
    provider: str
    capabilities: tuple[str, ...]
    is_remote: bool
    dimension: int | None = None
    input_token_limit: int | None = None
    language_note: str | None = None
    note: str | None = None
    credential_hint: str = "SILICONFLOW_API_KEY"

    def to_dict(self, *, selected: bool, available: bool) -> dict[str, object]:
        return {
            "kind": self.kind,
            "key": self.key,
            "name": self.name,
            "model": self.model,
            "label": self.label,
            "provider": self.provider,
            "capabilities": list(self.capabilities),
            "is_remote": self.is_remote,
            "dimension": self.dimension,
            "input_token_limit": self.input_token_limit,
            "language_note": self.language_note,
            "note": self.note,
            "selected": selected,
            "available": available,
            "unavailable_reason": (
                None if available else f"需要在 backend 环境配置 {self.credential_hint}"
            ),
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
                provider="FastEmbed",
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
        kind=ProviderKind.EMBEDDING.value,
        key=SILICONFLOW_EMBEDDING_MODEL,
        name="siliconflow",
        model=SILICONFLOW_EMBEDDING_MODEL,
        label="SiliconFlow BGE-M3",
        provider="SiliconFlow",
        capabilities=("documents", "query", "normalized", "remote", "multilingual"),
        is_remote=True,
        dimension=1024,
        input_token_limit=8192,
        language_note="100+ languages",
        note="1024 维；远程 token 计数为保守估算，切换后重启并执行索引重建",
    ),
    ProviderOption(
        kind=ProviderKind.RERANKER.value,
        key=MULTILINGUAL_RERANKER_MODEL,
        name="local_cross_encoder",
        model=MULTILINGUAL_RERANKER_MODEL,
        label="Jina 多语 Reranker",
        provider="FastEmbed",
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
        provider="FastEmbed",
        capabilities=("cross-encoder", "onnx", "local"),
        is_remote=False,
        language_note="English-focused",
        note="英文模型，中文场景需谨慎",
    ),
    ProviderOption(
        kind=ProviderKind.RERANKER.value,
        key=SILICONFLOW_RERANKER_MODEL,
        name="siliconflow",
        model=SILICONFLOW_RERANKER_MODEL,
        label="SiliconFlow BGE Reranker v2 M3",
        provider="SiliconFlow",
        capabilities=("cross-encoder", "http", "remote", "multilingual"),
        is_remote=True,
        input_token_limit=8192,
        language_note="multilingual",
        note="官方 API /rerank；模型支持长输入，RAG 默认仍只重排候选 Leaf",
    ),
    ProviderOption(
        kind=ProviderKind.VISION.value,
        key="none",
        name="none",
        model="none",
        label="关闭图片 Caption（降级）",
        provider="内置",
        capabilities=("skip_caption",),
        is_remote=False,
        note="图片仍会保存，但不调用远程 Vision Provider",
    ),
    ProviderOption(
        kind=ProviderKind.VISION.value,
        key="openai_compatible",
        name="openai_compatible",
        model="由 VISION_MODEL 环境变量提供",
        label="OpenAI-compatible Vision",
        provider="Configured endpoint",
        capabilities=("image-caption", "chat-completions", "http", "remote"),
        is_remote=True,
        language_note="取决于所配置模型",
        note="通过 /chat/completions 发送 base64 图片；必须配置 Vision endpoint、密钥与模型",
        credential_hint="VISION_BASE_URL + VISION_API_KEY + VISION_MODEL",
    ),
    ProviderOption(
        kind=ProviderKind.SPARSE_ENCODER.value,
        key="hashing_lexical",
        name="hashing_lexical",
        model="blake2b-31bit-logtf-l2-v1",
        label="Hashing Lexical（离线）",
        provider="内置",
        capabilities=("precomputed", "deterministic", "multilingual"),
        is_remote=False,
        note="确定性词法投影；不维护语料 IDF",
    ),
    ProviderOption(
        kind=ProviderKind.SPARSE_ENCODER.value,
        key="milvus_builtin_bm25",
        name="milvus_builtin_bm25",
        model="jieba-v1",
        label="Milvus 原生 BM25",
        provider="Milvus Lite",
        capabilities=("bm25", "corpus_idf", "jieba", "multilingual"),
        is_remote=False,
        language_note="中文 / 中英混合",
        note="由 Milvus Function 维护 TF/IDF；切换后生成新 revision",
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
    allowed = {
        "embedding_provider",
        "embedding_model",
        "embedding_dimension",
        "reranker_provider",
        "reranker_model",
        "llm_model",
        "vision_provider",
        "sparse_encoder",
    }
    result: dict[str, str] = {}
    for key, value in payload.items():
        if key not in allowed or not isinstance(value, str) or not value.strip():
            raise RuntimeError("The Provider selection file contains an invalid field.")
        if key == "embedding_provider" and value not in {
            "local_multilingual_minilm",
            "openai_compatible",
        }:
            raise RuntimeError(
                "The Provider selection file contains an invalid Embedding Provider."
            )
        if key == "reranker_provider" and value not in {
            "local_cross_encoder",
            "openai_compatible",
            "none",
        }:
            raise RuntimeError("The Provider selection file contains an invalid Reranker Provider.")
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
        remote_credentials: frozenset[str] = frozenset(),
    ) -> None:
        self._registry = registry
        self._selection_path = selection_path
        self._current_models = dict(current_models)
        self._current_embedding_dimension = current_embedding_dimension
        self._current_embedding_input_token_limit = current_embedding_input_token_limit
        self._remote_credentials = remote_credentials
        self._selection = load_provider_selection(selection_path)

    def to_dict(self) -> dict[str, object]:
        selected = {
            "embedding": self._current_models.get("embedding", ""),
            "reranker": self._current_models.get("reranker", ""),
            "llm": self._current_models.get("llm", ""),
            "vision": self._current_models.get("vision", ""),
            "sparse_encoder": self._current_models.get("sparse_encoder", ""),
        }
        selected_provider = {
            "embedding": self._current_models.get("embedding_provider")
            or _default_runtime_provider("embedding", selected["embedding"]),
            "reranker": self._current_models.get("reranker_provider")
            or _default_runtime_provider("reranker", selected["reranker"]),
        }
        pending = {
            field: self._selection[field]
            for field, kind in (
                ("embedding_provider", "embedding"),
                ("embedding_model", "embedding"),
                ("reranker_provider", "reranker"),
                ("reranker_model", "reranker"),
                ("llm_model", "llm"),
                ("vision_provider", "vision"),
                ("sparse_encoder", "sparse_encoder"),
            )
            if field in self._selection
            and self._selection[field]
            != (
                selected_provider[kind]
                if field in {"embedding_provider", "reranker_provider"}
                else selected[kind]
            )
        }
        return {
            "providers": [info.to_dict() for info in self._registry.list_info()],
            "options": [self._option_dict(option, selected=selected) for option in _OPTIONS],
            "selection": {
                "embedding_model": selected["embedding"],
                "embedding_provider": selected_provider["embedding"],
                "reranker_model": selected["reranker"],
                "reranker_provider": selected_provider["reranker"],
                "llm_model": selected["llm"],
                "vision_provider": selected["vision"],
                "sparse_encoder": selected["sparse_encoder"],
                "pending_restart": bool(pending),
                **(
                    {"pending_embedding_provider": pending["embedding_provider"]}
                    if "embedding_provider" in pending
                    else {}
                ),
                **(
                    {"pending_embedding_model": pending["embedding_model"]}
                    if "embedding_model" in pending
                    else {}
                ),
                **(
                    {"pending_reranker_provider": pending["reranker_provider"]}
                    if "reranker_provider" in pending
                    else {}
                ),
                **(
                    {"pending_reranker_model": pending["reranker_model"]}
                    if "reranker_model" in pending
                    else {}
                ),
                **(
                    {"pending_llm_model": pending["llm_model"]}
                    if "llm_model" in pending
                    else {}
                ),
                **(
                    {"pending_vision_provider": pending["vision_provider"]}
                    if "vision_provider" in pending
                    else {}
                ),
                **(
                    {"pending_sparse_encoder": pending["sparse_encoder"]}
                    if "sparse_encoder" in pending
                    else {}
                ),
                **(
                    {"embedding_dimension": str(self._current_embedding_dimension)}
                    if self._current_embedding_dimension is not None
                    else {}
                ),
                **(
                    {"embedding_input_token_limit": str(self._current_embedding_input_token_limit)}
                    if self._current_embedding_input_token_limit is not None
                    else {}
                ),
            },
        }

    def _option_dict(
        self, option: ProviderOption, *, selected: dict[str, str]
    ) -> dict[str, object]:
        is_selected = selected.get(option.kind, "") == option.key
        payload = option.to_dict(
            selected=is_selected,
            available=not option.is_remote or option.kind in self._remote_credentials,
        )
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
        if option.is_remote and option.kind not in self._remote_credentials:
            raise ValueError(
                "The requested remote Provider requires "
                f"{option.credential_hint} in the backend environment."
            )
        field = {
            ProviderKind.EMBEDDING.value: "embedding_model",
            ProviderKind.RERANKER.value: "reranker_model",
            ProviderKind.VISION.value: "vision_provider",
            ProviderKind.SPARSE_ENCODER.value: "sparse_encoder",
        }.get(kind)
        if field is None:
            raise ValueError("This Provider kind is not restart-selectable.")
        selection = dict(self._selection)
        provider_field = {
            ProviderKind.EMBEDDING.value: "embedding_provider",
            ProviderKind.RERANKER.value: "reranker_provider",
        }.get(kind)
        if provider_field is not None:
            selection[provider_field] = (
                "openai_compatible" if option.is_remote else option.name
            )
        selection[field] = (
            option.key
            if kind in {ProviderKind.SPARSE_ENCODER.value, ProviderKind.VISION.value}
            else option.model
        )
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


def selected_runtime_model(selection: dict[str, str], *, kind: str, default: str) -> str:
    field = {
        ProviderKind.EMBEDDING.value: "embedding_model",
        ProviderKind.RERANKER.value: "reranker_model",
        ProviderKind.LLM.value: "llm_model",
    }[kind]
    return selection.get(field, default)


def resolve_runtime_provider(
    selection: Mapping[str, str],
    *,
    kind: str,
    configured_provider: str,
    configured_model: str | None,
    default_model: str,
) -> tuple[str, str]:
    """Resolve a restart-bound profile without losing Provider identity.

    Older selection files stored only a model name. Preserve those files by
    recognizing the known remote profiles, while giving an explicit
    ``*_provider`` field precedence for new selections. An environment-level
    OpenAI-compatible configuration wins over an old model-only local profile.
    """

    if kind not in _REMOTE_PROFILE_MODELS:
        raise ValueError(f"runtime Provider selection is unsupported for {kind}")
    model_field = f"{kind}_model"
    provider_field = f"{kind}_provider"
    selected_model = selection.get(model_field)
    configured_model_value = configured_model.strip() if configured_model else ""
    model = (selected_model or configured_model_value or default_model).strip()
    selected_provider = selection.get(provider_field)
    if selected_provider:
        allowed = {
            "embedding": {"openai_compatible", "local_multilingual_minilm"},
            "reranker": {"openai_compatible", "local_cross_encoder", "none"},
        }[kind]
        if selected_provider not in allowed:
            raise ValueError(f"runtime Provider selection is invalid for {kind}")
        return selected_provider, model
    if configured_provider == "openai_compatible":
        # A model-only file predates explicit Provider identity. Do not let a
        # stale local model silently replace explicit API configuration.
        return configured_provider, configured_model_value or model
    if selected_model in _REMOTE_PROFILE_MODELS[kind]:
        return "openai_compatible", model
    return configured_provider, model


def selected_embedding_dimension(selection: dict[str, str], default: int) -> int:
    value = selection.get("embedding_dimension")
    return int(value) if value is not None else default


def _default_runtime_provider(kind: str, model: str) -> str:
    if model in _REMOTE_PROFILE_MODELS.get(kind, set()):
        return "openai_compatible"
    return {
        "embedding": "local_multilingual_minilm",
        "reranker": "local_cross_encoder",
    }.get(kind, "")
