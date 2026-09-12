"""Sanitized liveness, readiness, and provider diagnostics."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import Protocol

from sqlalchemy import text

from enterprise_rag.adapters.database import Database
from enterprise_rag.ports import ProviderHealth, ProviderRegistry


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    kind: str
    required: bool
    status: HealthStatus
    latency_ms: float
    code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "required": self.required,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: HealthStatus
    ready: bool
    checks: tuple[ComponentHealth, ...]
    providers: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
            "providers": list(self.providers),
        }


class HealthProbe(Protocol):
    name: str
    kind: str
    required: bool

    async def check(self) -> ComponentHealth: ...


class ConfigurationHealthProbe:
    name = "configuration"
    kind = "application"
    required = True

    def __init__(self, ready: bool) -> None:
        self._ready = ready

    async def check(self) -> ComponentHealth:
        return ComponentHealth(
            self.name,
            self.kind,
            self.required,
            HealthStatus.HEALTHY if self._ready else HealthStatus.UNAVAILABLE,
            0.0,
            None if self._ready else "INCOMPLETE",
        )


class PostgreSQLHealthProbe:
    name = "postgresql"
    kind = "database"
    required = True

    def __init__(self, database: Database | None, *, timeout_seconds: float = 2.0) -> None:
        self._database = database
        self._timeout = timeout_seconds

    async def check(self) -> ComponentHealth:
        started = perf_counter()
        if self._database is None:
            return self._result(started, HealthStatus.UNAVAILABLE, "NOT_CONFIGURED")
        try:
            await asyncio.wait_for(self._ping(), timeout=self._timeout)
        except Exception:
            return self._result(started, HealthStatus.UNAVAILABLE, "UNREACHABLE")
        return self._result(started, HealthStatus.HEALTHY, None)

    async def _ping(self) -> None:
        if self._database is None:
            return
        async with self._database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    def _result(
        self, started: float, status: HealthStatus, code: str | None
    ) -> ComponentHealth:
        return ComponentHealth(
            self.name,
            self.kind,
            self.required,
            status,
            round((perf_counter() - started) * 1_000, 3),
            code,
        )


class HealthService:
    def __init__(
        self,
        *,
        database: Database | None,
        providers: ProviderRegistry | None = None,
        probes: Sequence[HealthProbe] = (),
        database_probe: HealthProbe | None = None,
        configuration_ready: bool = True,
    ) -> None:
        self._probes = (
            ConfigurationHealthProbe(configuration_ready),
            database_probe or PostgreSQLHealthProbe(database),
            *probes,
        )
        self._providers = providers

    async def report(self, *, include_providers: bool = False) -> HealthReport:
        checks = list(
            await asyncio.gather(*(_safe_probe(probe) for probe in self._probes))
        )
        provider_payload: list[dict[str, object]] = []
        if self._providers is not None:
            try:
                provider_infos = self._providers.list_info()
            except Exception:
                provider_infos = ()
                checks.append(
                    ComponentHealth(
                        "provider_registry",
                        "provider",
                        True,
                        HealthStatus.UNAVAILABLE,
                        0.0,
                        "CHECK_FAILED",
                    )
                )
            for info in provider_infos:
                status = _provider_status(info.health)
                checks.append(
                    ComponentHealth(
                        f"{info.kind.value}:{info.name}",
                        "provider",
                        True,
                        status,
                        0.0,
                        "UNAVAILABLE" if status is HealthStatus.UNAVAILABLE else None,
                    )
                )
                if include_providers:
                    provider_payload.append(info.to_dict())
        ready = all(
            not check.required or check.status is not HealthStatus.UNAVAILABLE
            for check in checks
        )
        if not ready:
            status = HealthStatus.UNAVAILABLE
        elif any(check.status is HealthStatus.DEGRADED for check in checks):
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.HEALTHY
        return HealthReport(status, ready, tuple(checks), tuple(provider_payload))


async def _safe_probe(probe: HealthProbe) -> ComponentHealth:
    started = perf_counter()
    try:
        return await probe.check()
    except Exception:
        return ComponentHealth(
            probe.name,
            probe.kind,
            probe.required,
            HealthStatus.UNAVAILABLE,
            round((perf_counter() - started) * 1_000, 3),
            "CHECK_FAILED",
        )


def _provider_status(status: ProviderHealth) -> HealthStatus:
    if status is ProviderHealth.UNAVAILABLE:
        return HealthStatus.UNAVAILABLE
    if status in {ProviderHealth.DEGRADED, ProviderHealth.UNKNOWN}:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY
