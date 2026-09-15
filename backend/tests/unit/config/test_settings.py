from pathlib import Path

import pytest
from pydantic import ValidationError

from enterprise_rag.config import SettingsError, SettingsErrorCode, load_settings


def test_default_settings_are_valid_and_immutable() -> None:
    settings = load_settings(environ={})

    assert settings.app.environment == "development"
    assert settings.providers.vector_store == "milvus_lite"
    assert settings.providers.ocr == "tesseract"
    assert settings.providers.vision == "none"
    assert settings.providers.reranker == "local_cross_encoder"
    assert settings.ingestion.pdf_ocr_languages == ("chi_sim", "eng")
    assert settings.ingestion.overlap_tokens == 0
    field_name = "low_threshold"
    with pytest.raises(ValidationError):
        setattr(settings.deep, field_name, 0.1)


def test_precedence_is_defaults_then_yaml_then_env_then_override(tmp_path: Path) -> None:
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(
        "deep:\n  low_threshold: 0.20\napp:\n  public_base_url: http://yaml.test\n",
        encoding="utf-8",
    )

    settings = load_settings(
        config_file,
        environ={
            "PUBLIC_BASE_URL": "http://env.test",
            "ENTERPRISE_RAG__DEEP__LOW_THRESHOLD": "0.30",
        },
        overrides={"deep": {"low_threshold": 0.40}},
    )

    assert str(settings.app.public_base_url) == "http://env.test/"
    assert settings.deep.low_threshold == 0.40
    assert settings.deep.high_threshold == 0.80


def test_secret_values_are_masked_in_models() -> None:
    secret = "must-not-appear"
    settings = load_settings(environ={"SESSION_SECRET": secret})

    assert secret not in repr(settings)
    assert settings.credentials.session_secret is not None
    assert settings.credentials.session_secret.get_secret_value() == secret
    assert settings.credentials.model_dump(mode="json")["session_secret"] == "**********"


def test_siliconflow_shared_credentials_are_loaded_and_masked() -> None:
    secret = "siliconflow-must-not-appear"
    settings = load_settings(
        environ={
            "SILICONFLOW_BASE_URL": "https://api.siliconflow.cn/v1",
            "SILICONFLOW_API_KEY": secret,
        }
    )

    assert str(settings.credentials.siliconflow_base_url) == "https://api.siliconflow.cn/v1"
    assert settings.credentials.siliconflow_api_key is not None
    assert settings.credentials.siliconflow_api_key.get_secret_value() == secret
    assert secret not in repr(settings)


def test_mcp_public_base_url_is_independent_from_browser_public_url() -> None:
    settings = load_settings(
        environ={
            "PUBLIC_BASE_URL": "http://127.0.0.1:5173",
            "MCP_PUBLIC_BASE_URL": "http://127.0.0.1:8000",
        }
    )

    assert str(settings.app.public_base_url) == "http://127.0.0.1:5173/"
    assert str(settings.app.mcp_public_base_url) == "http://127.0.0.1:8000/"


def test_production_missing_secrets_has_stable_sanitized_error() -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(
            environ={},
            overrides={"app": {"environment": "production"}, "providers": {"llm": "mock"}},
        )

    assert raised.value.code is SettingsErrorCode.CONFIG_SECRET_MISSING
    assert raised.value.details == {
        "fields": (
            "ADMIN_BOOTSTRAP_EMAIL",
            "ADMIN_BOOTSTRAP_PASSWORD",
            "DATABASE_URL",
            "MCP_TOKEN_PEPPER",
            "METRICS_TOKEN",
            "SESSION_SECRET",
        )
    }


def test_remote_embedding_requires_its_own_production_credentials() -> None:
    environment = {
        "ADMIN_BOOTSTRAP_EMAIL": "admin@example.test",
        "ADMIN_BOOTSTRAP_PASSWORD": "password",
        "DATABASE_URL": "postgresql+asyncpg://example.test/db",
        "MCP_TOKEN_PEPPER": "pepper",
        "METRICS_TOKEN": "metrics",
        "SESSION_SECRET": "session",
    }
    with pytest.raises(SettingsError) as raised:
        load_settings(
            environ=environment,
            overrides={
                "app": {"environment": "production"},
                "providers": {"llm": "mock", "embedding": "openai_compatible"},
            },
        )

    assert raised.value.details == {
        "fields": ("EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL")
    }


def test_production_rejects_local_admin_admin_bootstrap_credentials() -> None:
    environment = {
        "ADMIN_BOOTSTRAP_EMAIL": "admin",
        "ADMIN_BOOTSTRAP_PASSWORD": "admin",
        "DATABASE_URL": "postgresql+asyncpg://example.test/db",
        "MCP_TOKEN_PEPPER": "pepper",
        "METRICS_TOKEN": "metrics",
        "SESSION_SECRET": "session",
    }

    with pytest.raises(SettingsError) as raised:
        load_settings(
            environ=environment,
            overrides={"app": {"environment": "production"}, "providers": {"llm": "mock"}},
        )

    assert raised.value.code is SettingsErrorCode.CONFIG_VALUE_INVALID
    assert raised.value.details == {"fields": ("ADMIN_BOOTSTRAP_EMAIL", "ADMIN_BOOTSTRAP_PASSWORD")}


def test_remote_reranker_requires_its_own_production_credentials() -> None:
    environment = {
        "ADMIN_BOOTSTRAP_EMAIL": "admin@example.test",
        "ADMIN_BOOTSTRAP_PASSWORD": "password",
        "DATABASE_URL": "postgresql+asyncpg://example.test/db",
        "MCP_TOKEN_PEPPER": "pepper",
        "METRICS_TOKEN": "metrics",
        "SESSION_SECRET": "session",
    }
    with pytest.raises(SettingsError) as raised:
        load_settings(
            environ=environment,
            overrides={
                "app": {"environment": "production"},
                "providers": {
                    "llm": "mock",
                    "embedding": "local_multilingual_minilm",
                    "reranker": "openai_compatible",
                },
            },
        )

    assert raised.value.details == {"fields": ("RERANK_API_KEY", "RERANK_BASE_URL", "RERANK_MODEL")}


def test_openai_compatible_vision_settings_are_loaded_and_masked() -> None:
    secret = "vision-must-not-appear"
    settings = load_settings(
        environ={
            "VISION_BASE_URL": "https://vision.example/v1",
            "VISION_API_KEY": secret,
            "VISION_MODEL": "vision-model",
        },
        overrides={"providers": {"vision": "openai_compatible"}},
    )

    assert settings.providers.vision == "openai_compatible"
    assert str(settings.credentials.vision_base_url) == "https://vision.example/v1"
    assert settings.credentials.vision_api_key is not None
    assert settings.credentials.vision_api_key.get_secret_value() == secret
    assert settings.credentials.vision_model == "vision-model"
    assert secret not in repr(settings)


def test_production_remote_vision_requires_its_own_credentials() -> None:
    environment = {
        "ADMIN_BOOTSTRAP_EMAIL": "admin@example.test",
        "ADMIN_BOOTSTRAP_PASSWORD": "password",
        "DATABASE_URL": "postgresql+asyncpg://example.test/db",
        "MCP_TOKEN_PEPPER": "pepper",
        "METRICS_TOKEN": "metrics",
        "SESSION_SECRET": "session",
    }
    with pytest.raises(SettingsError) as raised:
        load_settings(
            environ=environment,
            overrides={
                "app": {"environment": "production"},
                "providers": {"llm": "mock", "vision": "openai_compatible"},
            },
        )

    assert raised.value.code is SettingsErrorCode.CONFIG_SECRET_MISSING
    assert raised.value.details == {
        "fields": ("VISION_API_KEY", "VISION_BASE_URL", "VISION_MODEL")
    }


@pytest.mark.parametrize(
    "deep_config",
    [
        {"low_threshold": -0.1},
        {"high_threshold": 1.1},
        {"low_threshold": 0.8, "high_threshold": 0.8},
        {"low_threshold": 0.9, "high_threshold": 0.8},
    ],
)
def test_illegal_threshold_has_stable_error(deep_config: dict[str, float]) -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(environ={}, overrides={"deep": deep_config})

    assert raised.value.code is SettingsErrorCode.CONFIG_VALUE_INVALID
    assert raised.value.message == "Configuration validation failed."
    assert raised.value.details["fields"] == ("deep",) or raised.value.details["fields"] == (
        f"deep.{next(iter(deep_config))}",
    )


def test_unknown_provider_has_stable_error() -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(environ={}, overrides={"providers": {"embedding": "invented"}})

    assert raised.value.code is SettingsErrorCode.CONFIG_PROVIDER_UNKNOWN
    assert raised.value.details == {"kind": "embedding", "name": "invented"}


def test_nested_environment_can_override_numeric_setting() -> None:
    settings = load_settings(
        environ={"ENTERPRISE_RAG__SECURITY__ANONYMOUS_QUERIES_PER_MINUTE": "7"}
    )

    assert settings.security.anonymous_queries_per_minute == 7


def test_cost_guard_defaults_and_nested_token_budget_override() -> None:
    settings = load_settings(
        environ={
            "ENTERPRISE_RAG__SECURITY__ANONYMOUS_DAILY_INPUT_TOKENS": "123456",
            "ENTERPRISE_RAG__COST_GUARD__ANSWER_MAX_OUTPUT_TOKENS": "7000",
        }
    )

    assert settings.cost_guard.query_timeout_seconds == 90
    assert settings.cost_guard.provider_max_retries == 2
    assert settings.cost_guard.answer_max_output_tokens == 7000
    assert settings.cost_guard.deep_reserved_llm_calls == 18
    assert settings.security.anonymous_daily_input_tokens == 123_456


def test_worker_defaults_and_environment_overrides_are_bounded() -> None:
    settings = load_settings(
        environ={
            "WORKER_POLL_INTERVAL_SECONDS": "0.25",
            "WORKER_RECOVERY_INTERVAL_SECONDS": "12",
            "WORKER_RECOVERY_LIMIT": "25",
            "WORKER_LEASE_SECONDS": "180",
        }
    )

    assert settings.worker.poll_interval_seconds == 0.25
    assert settings.worker.recovery_interval_seconds == 12
    assert settings.worker.recovery_limit == 25
    assert settings.worker.lease_seconds == 180


@pytest.mark.parametrize(
    "worker_config",
    [
        {"poll_interval_seconds": 0},
        {"recovery_interval_seconds": 3_601},
        {"recovery_limit": 0},
        {"lease_seconds": 0},
    ],
)
def test_worker_configuration_rejects_unbounded_values(
    worker_config: dict[str, object],
) -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(environ={}, overrides={"worker": worker_config})

    assert raised.value.code is SettingsErrorCode.CONFIG_VALUE_INVALID


@pytest.mark.parametrize(
    "cost_guard",
    [
        {"query_timeout_seconds": 0},
        {"provider_timeout_seconds": 301},
        {"provider_max_retries": 11},
        {"provider_retry_backoff_seconds": -1},
        {"answer_max_output_tokens": 0},
        {"answer_max_output_tokens": 12_001},
        {"standard_reserved_llm_calls": 0},
    ],
)
def test_cost_guard_configuration_is_bounded(cost_guard: dict[str, object]) -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(environ={}, overrides={"cost_guard": cost_guard})

    assert raised.value.code is SettingsErrorCode.CONFIG_VALUE_INVALID


def test_pdf_ocr_settings_are_bounded_and_require_individual_languages() -> None:
    invalid_settings: tuple[dict[str, object], ...] = (
        {"pdf_ocr_min_chars": -1},
        {"pdf_render_scale": 4.1},
        {"pdf_ocr_languages": []},
        {"pdf_ocr_languages": ["chi_sim+eng"]},
    )
    for ingestion in invalid_settings:
        with pytest.raises(SettingsError) as raised:
            load_settings(environ={}, overrides={"ingestion": ingestion})
        assert raised.value.code is SettingsErrorCode.CONFIG_VALUE_INVALID


def test_invalid_yaml_root_has_stable_error(tmp_path: Path) -> None:
    config_file = tmp_path / "settings.yaml"
    config_file.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(SettingsError) as raised:
        load_settings(config_file, environ={})

    assert raised.value.code is SettingsErrorCode.CONFIG_FILE_INVALID
    assert raised.value.details == {"path": str(config_file)}
