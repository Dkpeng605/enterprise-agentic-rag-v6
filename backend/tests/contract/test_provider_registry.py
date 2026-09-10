import pytest

from enterprise_rag.ports import (
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    ProviderRegistry,
    RegistryError,
    RegistryErrorCode,
)


class FakeProvider:
    def __init__(
        self,
        provider_info: ProviderInfo,
        close_events: list[str] | None = None,
        *,
        fail_close: bool = False,
    ) -> None:
        self._provider_info = provider_info
        self.close_events = close_events if close_events is not None else []
        self.fail_close = fail_close
        self.close_count = 0

    def info(self) -> ProviderInfo:
        return self._provider_info

    async def aclose(self) -> None:
        self.close_count += 1
        self.close_events.append(self._provider_info.name)
        if self.fail_close:
            raise RuntimeError("private provider failure")


def provider_info(
    name: str = "fake",
    *,
    kind: ProviderKind = ProviderKind.EMBEDDING,
    capabilities: frozenset[str] = frozenset({"documents", "query"}),
) -> ProviderInfo:
    return ProviderInfo(
        kind=kind,
        name=name,
        version="1.0.0",
        capabilities=capabilities,
        is_remote=False,
        health=ProviderHealth.HEALTHY,
    )


def test_provider_info_serializes_deterministically() -> None:
    info = provider_info(capabilities=frozenset({"query", "documents"}))

    assert info.to_dict() == {
        "kind": "embedding",
        "name": "fake",
        "version": "1.0.0",
        "capabilities": ["documents", "query"],
        "is_remote": False,
        "health": "healthy",
    }


@pytest.mark.parametrize("name", ["", "   "])
def test_provider_info_rejects_empty_name(name: str) -> None:
    with pytest.raises(ValueError):
        provider_info(name)


def test_provider_info_rejects_empty_version_or_capability() -> None:
    base = provider_info()
    with pytest.raises(ValueError):
        ProviderInfo(
            kind=base.kind,
            name=base.name,
            version="",
            capabilities=base.capabilities,
            is_remote=base.is_remote,
        )
    with pytest.raises(ValueError):
        provider_info(capabilities=frozenset({""}))


def test_duplicate_provider_key_is_rejected_without_taking_ownership() -> None:
    registry = ProviderRegistry()
    first = FakeProvider(provider_info())
    duplicate = FakeProvider(provider_info())
    registry.register(first)

    with pytest.raises(RegistryError) as raised:
        registry.register(duplicate)

    assert raised.value.code is RegistryErrorCode.PROVIDER_DUPLICATE
    assert raised.value.details == {"kind": "embedding", "name": "fake"}
    assert duplicate.close_count == 0


def test_unknown_provider_has_stable_error() -> None:
    registry = ProviderRegistry()

    with pytest.raises(RegistryError) as raised:
        registry.resolve(ProviderKind.LLM, "missing")

    assert raised.value.code is RegistryErrorCode.PROVIDER_UNKNOWN
    assert raised.value.details == {"kind": "llm", "name": "missing"}


def test_capability_mismatch_lists_only_missing_capabilities() -> None:
    registry = ProviderRegistry()
    provider = FakeProvider(provider_info(capabilities=frozenset({"query"})))
    registry.register(provider)

    with pytest.raises(RegistryError) as raised:
        registry.resolve(
            ProviderKind.EMBEDDING,
            "fake",
            required_capabilities={"documents", "query"},
        )

    assert raised.value.code is RegistryErrorCode.PROVIDER_CAPABILITY_MISMATCH
    assert raised.value.details == {
        "kind": "embedding",
        "name": "fake",
        "missing_capabilities": ("documents",),
    }


def test_registry_resolves_matching_provider_and_lists_stably() -> None:
    registry = ProviderRegistry()
    second = FakeProvider(provider_info("zeta", kind=ProviderKind.LLM))
    first = FakeProvider(provider_info("alpha"))
    registry.register(second)
    registry.register(first)

    assert registry.resolve(
        ProviderKind.EMBEDDING,
        "alpha",
        required_capabilities={"query"},
    ) is first
    assert [item.name for item in registry.list_info()] == ["alpha", "zeta"]
    assert registry.list_info(ProviderKind.LLM) == (second.info(),)


@pytest.mark.anyio
async def test_close_is_reverse_order_and_idempotent() -> None:
    events: list[str] = []
    registry = ProviderRegistry()
    first = FakeProvider(provider_info("first"), events)
    second = FakeProvider(provider_info("second", kind=ProviderKind.LLM), events)
    registry.register(first)
    registry.register(second)

    await registry.aclose()
    await registry.aclose()

    assert events == ["second", "first"]
    assert first.close_count == 1
    assert second.close_count == 1


@pytest.mark.anyio
async def test_close_attempts_every_provider_and_sanitizes_failures() -> None:
    events: list[str] = []
    registry = ProviderRegistry()
    healthy = FakeProvider(provider_info("healthy"), events)
    failing = FakeProvider(
        provider_info("failing", kind=ProviderKind.LLM),
        events,
        fail_close=True,
    )
    registry.register(healthy)
    registry.register(failing)

    with pytest.raises(RegistryError) as raised:
        await registry.aclose()

    assert events == ["failing", "healthy"]
    assert raised.value.code is RegistryErrorCode.PROVIDER_CLOSE_FAILED
    assert raised.value.details == {
        "providers": ({"kind": "llm", "name": "failing"},)
    }
    assert "private provider failure" not in str(raised.value)


@pytest.mark.anyio
async def test_register_after_close_is_rejected() -> None:
    registry = ProviderRegistry()
    await registry.aclose()

    with pytest.raises(RegistryError) as raised:
        registry.register(FakeProvider(provider_info()))

    assert raised.value.code is RegistryErrorCode.PROVIDER_REGISTRY_CLOSED
