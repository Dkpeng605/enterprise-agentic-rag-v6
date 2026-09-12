"""Load settings with defaults < YAML < environment < explicit override precedence."""

import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError

from enterprise_rag.config.errors import SettingsError, SettingsErrorCode
from enterprise_rag.config.models import AppSettings

CONFIG_FILE_ENV = "ENTERPRISE_RAG_CONFIG_FILE"
NESTED_ENV_PREFIX = "ENTERPRISE_RAG__"

ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "APP_ENVIRONMENT": ("app", "environment"),
    "PUBLIC_BASE_URL": ("app", "public_base_url"),
    "DATABASE_URL": ("credentials", "database_url"),
    "SESSION_SECRET": ("credentials", "session_secret"),
    "ADMIN_BOOTSTRAP_EMAIL": ("credentials", "admin_bootstrap_email"),
    "ADMIN_BOOTSTRAP_PASSWORD": ("credentials", "admin_bootstrap_password"),
    "LLM_BASE_URL": ("credentials", "llm_base_url"),
    "LLM_API_KEY": ("credentials", "llm_api_key"),
    "LLM_MODEL": ("credentials", "llm_model"),
    "EMBEDDING_BASE_URL": ("credentials", "embedding_base_url"),
    "EMBEDDING_API_KEY": ("credentials", "embedding_api_key"),
    "EMBEDDING_MODEL": ("credentials", "embedding_model"),
    "RERANK_BASE_URL": ("credentials", "rerank_base_url"),
    "RERANK_API_KEY": ("credentials", "rerank_api_key"),
    "RERANK_MODEL": ("credentials", "rerank_model"),
    "MCP_TOKEN_PEPPER": ("credentials", "mcp_token_pepper"),
    "METRICS_TOKEN": ("credentials", "metrics_token"),
}

KNOWN_PROVIDERS: dict[str, frozenset[str]] = {
    "llm": frozenset({"mock", "openai_compatible"}),
    "embedding": frozenset({"local_multilingual_minilm", "openai_compatible"}),
    "reranker": frozenset({"local_cross_encoder", "openai_compatible", "none"}),
    "vector_store": frozenset({"milvus_lite"}),
    "splitter": frozenset({"structure_aware"}),
    "evaluator": frozenset({"deterministic"}),
    "ocr": frozenset({"tesseract"}),
    "vision": frozenset({"none"}),
    "sparse_encoder": frozenset({"hashing_lexical"}),
}

PRODUCTION_REQUIRED_ENV = (
    "ADMIN_BOOTSTRAP_EMAIL",
    "ADMIN_BOOTSTRAP_PASSWORD",
    "DATABASE_URL",
    "MCP_TOKEN_PEPPER",
    "METRICS_TOKEN",
    "SESSION_SECRET",
)


def _deep_merge(base: dict[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in incoming.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = deepcopy(value)
    return result


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cursor = target
    for part in path[:-1]:
        child = cursor.setdefault(part, {})
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[path[-1]] = value


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SettingsError(
            SettingsErrorCode.CONFIG_FILE_INVALID,
            "The configuration file cannot be loaded.",
            {"path": str(path)},
        ) from exc
    if content is None:
        return {}
    if not isinstance(content, dict):
        raise SettingsError(
            SettingsErrorCode.CONFIG_FILE_INVALID,
            "The configuration file root must be a mapping.",
            {"path": str(path)},
        )
    return content


def _parse_nested_env(value: str) -> Any:
    parsed = yaml.safe_load(value)
    return value if parsed is None else parsed


def _environment_data(environ: Mapping[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for name, path in ENV_ALIASES.items():
        if name in environ:
            _set_nested(data, path, environ[name])
    for name, value in environ.items():
        if not name.startswith(NESTED_ENV_PREFIX):
            continue
        path = tuple(part.lower() for part in name.removeprefix(NESTED_ENV_PREFIX).split("__"))
        if path and all(path):
            _set_nested(data, path, _parse_nested_env(value))
    return data


def _validation_fields(exc: ValidationError) -> list[str]:
    return sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})


def _validate_provider_names(settings: AppSettings) -> None:
    selected = settings.providers.model_dump()
    for kind, name in selected.items():
        if name not in KNOWN_PROVIDERS[kind]:
            raise SettingsError(
                SettingsErrorCode.CONFIG_PROVIDER_UNKNOWN,
                "A configured provider name is not recognized.",
                {"kind": kind, "name": name},
            )


def _validate_production_secrets(settings: AppSettings) -> None:
    if settings.app.environment != "production":
        return
    credentials = settings.credentials

    def is_missing(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, SecretStr):
            return not value.get_secret_value().strip()
        return isinstance(value, str) and not value.strip()

    missing = [
        env_name
        for env_name in PRODUCTION_REQUIRED_ENV
        if is_missing(getattr(credentials, ENV_ALIASES[env_name][-1]))
    ]
    if settings.providers.llm == "openai_compatible":
        for env_name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
            if is_missing(getattr(credentials, ENV_ALIASES[env_name][-1])):
                missing.append(env_name)
    if settings.providers.embedding == "openai_compatible":
        for env_name in ("EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL"):
            if is_missing(getattr(credentials, ENV_ALIASES[env_name][-1])):
                missing.append(env_name)
    if settings.providers.reranker == "openai_compatible":
        for env_name in ("RERANK_API_KEY", "RERANK_BASE_URL", "RERANK_MODEL"):
            if is_missing(getattr(credentials, ENV_ALIASES[env_name][-1])):
                missing.append(env_name)
    if missing:
        raise SettingsError(
            SettingsErrorCode.CONFIG_SECRET_MISSING,
            "Required production configuration is missing.",
            {"fields": sorted(missing)},
        )


def load_settings(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AppSettings:
    """Create one immutable validated snapshot without logging secret values."""

    source_env = os.environ if environ is None else environ
    selected_path = config_path or source_env.get(CONFIG_FILE_ENV)
    data: dict[str, Any] = {}
    if selected_path:
        data = _read_yaml(Path(selected_path))
    data = _deep_merge(data, _environment_data(source_env))
    if overrides:
        data = _deep_merge(data, overrides)
    try:
        settings = AppSettings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(
            SettingsErrorCode.CONFIG_VALUE_INVALID,
            "Configuration validation failed.",
            {"fields": _validation_fields(exc)},
        ) from exc
    _validate_provider_names(settings)
    _validate_production_secrets(settings)
    return settings
