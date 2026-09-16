"""Composition tests for selecting the Vision adapter from validated settings."""

import pytest

from enterprise_rag.config import AppSettings, load_settings
from enterprise_rag.services.vision_provider import build_vision_provider


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_factory_keeps_noop_as_the_explicit_default() -> None:
    provider = build_vision_provider(load_settings(environ={}))

    assert provider.info().name == "none"
    assert provider.info().is_remote is False
    await provider.aclose()


@pytest.mark.anyio
async def test_factory_builds_openai_compatible_provider_from_vision_credentials() -> None:
    settings = load_settings(
        environ={
            "VISION_BASE_URL": "https://vision.example/v1",
            "VISION_API_KEY": "vision-secret",
            "VISION_MODEL": "vision-model",
        },
        overrides={"providers": {"vision": "openai_compatible"}},
    )

    provider = build_vision_provider(settings)

    assert provider.info().name == "openai_compatible"
    assert provider.info().version == "vision-model"
    assert provider.info().is_remote is True
    await provider.aclose()


@pytest.mark.anyio
async def test_mac_composition_may_explicitly_reuse_llm_credentials() -> None:
    settings = load_settings(
        environ={
            "LLM_BASE_URL": "https://tokenhub.example/v1",
            "LLM_API_KEY": "llm-secret",
            "LLM_MODEL": "MiniMax-M3",
        },
        overrides={"providers": {"vision": "openai_compatible"}},
    )

    provider = build_vision_provider(settings, reuse_llm_credentials=True)

    assert provider.info().name == "openai_compatible"
    assert provider.info().version == "MiniMax-M3"
    await provider.aclose()


def test_factory_rejects_remote_vision_without_complete_credentials() -> None:
    settings = AppSettings.model_validate(
        {"providers": {"vision": "openai_compatible"}}
    )

    with pytest.raises(RuntimeError, match="VISION_BASE_URL, VISION_API_KEY, VISION_MODEL"):
        build_vision_provider(settings)
