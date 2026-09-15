"""Composition helpers for the pluggable Vision Provider."""

from enterprise_rag.adapters.vision import (
    NoopVisionProvider,
    OpenAICompatibleVisionProvider,
)
from enterprise_rag.config.models import AppSettings

VisionProviderInstance = NoopVisionProvider | OpenAICompatibleVisionProvider


def build_vision_provider(
    settings: AppSettings, *, provider_name: str | None = None
) -> VisionProviderInstance:
    """Build the configured Vision adapter without silently falling back remotely."""

    selected = provider_name or settings.providers.vision
    if selected == "none":
        return NoopVisionProvider()
    if selected == "openai_compatible":
        credentials = settings.credentials
        if (
            credentials.vision_base_url is None
            or credentials.vision_api_key is None
            or credentials.vision_model is None
            or not credentials.vision_model.strip()
        ):
            raise RuntimeError(
                "VISION_BASE_URL, VISION_API_KEY, VISION_MODEL are required for "
                "openai_compatible Vision"
            )
        return OpenAICompatibleVisionProvider(
            base_url=str(credentials.vision_base_url),
            api_key=credentials.vision_api_key.get_secret_value(),
            model=credentials.vision_model,
            timeout_seconds=settings.cost_guard.provider_timeout_seconds,
            max_retries=settings.cost_guard.provider_max_retries,
        )
    raise RuntimeError(f"Unknown Vision Provider: {selected}")
