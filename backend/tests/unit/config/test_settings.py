from pathlib import Path

import pytest
from pydantic import ValidationError

from enterprise_rag.config import SettingsError, SettingsErrorCode, load_settings


def test_default_settings_are_valid_and_immutable() -> None:
    settings = load_settings(environ={})

    assert settings.app.environment == "development"
    assert settings.providers.vector_store == "milvus_lite"
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
            "SESSION_SECRET",
        )
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


def test_invalid_yaml_root_has_stable_error(tmp_path: Path) -> None:
    config_file = tmp_path / "settings.yaml"
    config_file.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(SettingsError) as raised:
        load_settings(config_file, environ={})

    assert raised.value.code is SettingsErrorCode.CONFIG_FILE_INVALID
    assert raised.value.details == {"path": str(config_file)}
