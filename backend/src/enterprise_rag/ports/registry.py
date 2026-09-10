"""Provider registration, capability resolution, and lifecycle ownership."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from enterprise_rag.ports.provider import Provider, ProviderInfo, ProviderKind


class RegistryErrorCode(StrEnum):
    DUPLICATE = "PROVIDER_DUPLICATE"
    UNKNOWN = "PROVIDER_UNKNOWN"
    CAPABILITY_MISMATCH = "PROVIDER_CAPABILITY_MISMATCH"
    REGISTRY_CLOSED = "PROVIDER_REGISTRY_CLOSED"
    CLOSE_FAILED = "PROVIDER_CLOSE_FAILED"


@dataclass(frozen=True, slots=True)
class RegistryError(Exception):
    code: RegistryErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ProviderRegistry:
    """Own providers from successful registration until application shutdown."""

    def __init__(self) -> None:
        self._providers: dict[tuple[ProviderKind, str], Provider] = {}
        self._closed = False

    def register(self, provider: Provider) -> None:
        """Register one initialized provider; ownership transfers only on success."""

        if self._closed:
            raise RegistryError(
                RegistryErrorCode.REGISTRY_CLOSED,
                "The provider registry is already closed.",
            )
        info = provider.info()
        if info.key in self._providers:
            raise RegistryError(
                RegistryErrorCode.DUPLICATE,
                "A provider with the same kind and name is already registered.",
                {"kind": info.kind.value, "name": info.name},
            )
        self._providers[info.key] = provider

    def resolve(
        self,
        kind: ProviderKind,
        name: str,
        *,
        required_capabilities: Iterable[str] = (),
    ) -> Provider:
        """Resolve a provider and prove it supplies every requested capability."""

        provider = self._providers.get((kind, name))
        if provider is None:
            raise RegistryError(
                RegistryErrorCode.UNKNOWN,
                "The requested provider is not registered.",
                {"kind": kind.value, "name": name},
            )
        required = frozenset(required_capabilities)
        missing = required - provider.info().capabilities
        if missing:
            raise RegistryError(
                RegistryErrorCode.CAPABILITY_MISMATCH,
                "The provider does not support every required capability.",
                {
                    "kind": kind.value,
                    "name": name,
                    "missing_capabilities": sorted(missing),
                },
            )
        return provider

    def list_info(self, kind: ProviderKind | None = None) -> tuple[ProviderInfo, ...]:
        """Return metadata in stable kind/name order for diagnostics and admin APIs."""

        values = (
            provider.info()
            for provider in self._providers.values()
            if kind is None or provider.info().kind is kind
        )
        return tuple(sorted(values, key=lambda item: (item.kind.value, item.name)))

    async def aclose(self) -> None:
        """Close all owned providers once, in reverse registration order."""

        if self._closed:
            return
        self._closed = True
        failures: list[dict[str, str]] = []
        for provider in reversed(tuple(self._providers.values())):
            info = provider.info()
            try:
                await provider.aclose()
            except Exception:
                failures.append({"kind": info.kind.value, "name": info.name})
        self._providers.clear()
        if failures:
            raise RegistryError(
                RegistryErrorCode.CLOSE_FAILED,
                "One or more providers failed to close.",
                {"providers": failures},
            )

    async def __aenter__(self) -> "ProviderRegistry":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
