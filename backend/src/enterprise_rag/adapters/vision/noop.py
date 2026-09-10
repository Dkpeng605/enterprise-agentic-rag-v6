"""Explicit no-caption adapter used by default in local and demo environments."""

from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.vision import VisionImage


class NoopVisionProvider:
    def __init__(self) -> None:
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.VISION,
            name="none",
            version="1",
            capabilities=frozenset({"skip_caption"}),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def caption(self, image: VisionImage) -> str | None:
        del image
        if self._closed:
            raise RuntimeError("Vision Provider is closed")
        return None

    async def aclose(self) -> None:
        self._closed = True
