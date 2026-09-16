"""Composition helpers for the pluggable Vision Provider."""

from enterprise_rag.adapters.vision import (
    NoopVisionProvider,
    OpenAICompatibleVisionProvider,
)
from enterprise_rag.config.models import AppSettings

VisionProviderInstance = NoopVisionProvider | OpenAICompatibleVisionProvider


def build_vision_provider(
    settings: AppSettings,
    *,
    provider_name: str | None = None,
    reuse_llm_credentials: bool = False,
) -> VisionProviderInstance:
    """Build the configured Vision adapter.

    ``reuse_llm_credentials`` is an explicit composition-root choice for the
    local Mac demo: MiniMax-M3 is configured once as the OpenAI-compatible LLM
    and the same endpoint/model may then caption extracted images. Production
    composition keeps the default strict behavior and requires dedicated
    ``VISION_*`` credentials.
    """

    selected = provider_name or settings.providers.vision
    if selected == "none":
        return NoopVisionProvider()
    if selected == "openai_compatible":
        credentials = settings.credentials
        base_url = credentials.vision_base_url
        api_key = credentials.vision_api_key
        model = credentials.vision_model
        if reuse_llm_credentials and (
            base_url is None or api_key is None or model is None or not model.strip()
        ):
            base_url = credentials.llm_base_url
            api_key = credentials.llm_api_key
            model = credentials.llm_model
        if base_url is None or api_key is None or model is None or not model.strip():
            raise RuntimeError(
                "VISION_BASE_URL, VISION_API_KEY, VISION_MODEL are required for "
                "openai_compatible Vision unless the local Mac composition explicitly "
                "enables LLM credential reuse"
            )
        return OpenAICompatibleVisionProvider(
            base_url=str(base_url),
            api_key=api_key.get_secret_value(),
            model=model,
            timeout_seconds=settings.cost_guard.provider_timeout_seconds,
            max_retries=settings.cost_guard.provider_max_retries,
        )
    raise RuntimeError(f"Unknown Vision Provider: {selected}")
