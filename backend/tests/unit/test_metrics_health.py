"""Prometheus cardinality and health/readiness acceptance tests."""

from dataclasses import dataclass

import httpx2
import pytest

from enterprise_rag.api import create_app
from enterprise_rag.config import AppSettings
from enterprise_rag.observability import ApplicationMetrics
from enterprise_rag.ports import (
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    ProviderRegistry,
)
from enterprise_rag.services import ComponentHealth, HealthService, HealthStatus


@dataclass(slots=True)
class StaticProbe:
    status: HealthStatus
    name: str = "fixture"
    kind: str = "runner"
    required: bool = True
    code: str | None = None

    async def check(self) -> ComponentHealth:
        return ComponentHealth(
            self.name,
            self.kind,
            self.required,
            self.status,
            0.1,
            self.code,
        )


class FixtureProvider:
    def __init__(self, health: ProviderHealth) -> None:
        self._health = health

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.LLM,
            "fixture-llm",
            "1",
            frozenset({"complete"}),
            True,
            self._health,
        )

    async def aclose(self) -> None:
        return None


class FailingProbe:
    name = "background_runner"
    kind = "runner"
    required = True

    async def check(self) -> ComponentHealth:
        raise RuntimeError("postgresql://user:secret@example.invalid/private")


def test_metrics_render_required_families_without_high_cardinality_labels() -> None:
    metrics = ApplicationMetrics()
    metrics.observe_http(
        method="GET",
        route="/api/v1/traces/{trace_id}",
        status=200,
        duration_seconds=0.2,
    )
    metrics.observe_query(mode="standard", status="answered", duration_seconds=0.4)
    metrics.observe_candidates(stage="dense", count=8)
    metrics.observe_recovery(route="rewrite_hybrid")
    metrics.observe_provider(
        kind="llm",
        provider="fixture",
        status="success",
        duration_seconds=0.3,
        input_tokens=10,
        output_tokens=4,
    )
    metrics.observe_ingestion_job(status="succeeded", job_type="ingest")
    metrics.observe_ingestion_stage(stage="split", duration_seconds=0.1)
    metrics.observe_milvus(operation="upsert", status="success")
    metrics.observe_evaluation(status="completed")
    metrics.observe_rate_limit(budget="queries_per_minute")

    rendered = metrics.render().decode()

    for name in (
        "http_requests_total",
        "http_request_duration_seconds",
        "rag_queries_total",
        "rag_query_duration_seconds",
        "rag_retrieval_candidates",
        "rag_recovery_rounds_total",
        "provider_requests_total",
        "provider_request_duration_seconds",
        "provider_tokens_total",
        "ingestion_jobs_total",
        "ingestion_stage_duration_seconds",
        "milvus_operations_total",
        "evaluation_runs_total",
        "rate_limit_rejections_total",
    ):
        assert name in rendered
    assert 'route="/api/v1/traces/{trace_id}"' in rendered
    assert not any(
        forbidden in rendered
        for forbidden in ("tenant_id=", "user_id=", "document_id=", "query=")
    )


@pytest.mark.anyio
async def test_http_metrics_use_route_templates_and_hide_unmatched_paths() -> None:
    metrics = ApplicationMetrics()
    transport = httpx2.ASGITransport(app=create_app(metrics=metrics))
    secret_path = "/private/customer-01900000-0000-7000-8000-000000007001"
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        dynamic = await client.get(
            "/api/v1/documents/01900000-0000-7000-8000-000000007001"
        )
        missing = await client.get(secret_path)
        response = await client.get("/metrics")

    assert dynamic.status_code == 503
    assert missing.status_code == 404
    assert response.status_code == 200
    rendered = response.text
    assert 'route="/api/v1/documents/{document_id}"' in rendered
    assert 'route="unmatched"' in rendered
    assert secret_path not in rendered


@pytest.mark.anyio
async def test_liveness_survives_while_unconfigured_readiness_fails_safely() -> None:
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        doctor = await client.get("/health/doctor")

    assert live.status_code == 200
    assert live.json()["status"] == "live"
    assert ready.status_code == 503
    assert ready.json()["ready"] is False
    checks = {item["name"]: item for item in ready.json()["checks"]}
    assert checks["configuration"]["code"] == "INCOMPLETE"
    assert checks["postgresql"]["code"] == "NOT_CONFIGURED"
    assert doctor.status_code == 200
    serialized = repr(doctor.json())
    assert "postgresql+asyncpg" not in serialized
    assert "password" not in serialized.casefold()


@pytest.mark.anyio
async def test_required_provider_failure_changes_readiness_and_doctor_is_sanitized() -> None:
    registry = ProviderRegistry()
    registry.register(FixtureProvider(ProviderHealth.UNAVAILABLE))
    service = HealthService(
        database=None,
        providers=registry,
        database_probe=StaticProbe(HealthStatus.HEALTHY, name="postgresql"),
    )
    transport = httpx2.ASGITransport(app=create_app(health_service=service))
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        ready = await client.get("/health/ready")
        doctor = await client.get("/health/doctor")

    assert ready.status_code == 503
    assert ready.json()["status"] == "unavailable"
    assert doctor.json()["providers"] == [
        {
            "kind": "llm",
            "name": "fixture-llm",
            "version": "1",
            "capabilities": ["complete"],
            "is_remote": True,
            "health": "unavailable",
        }
    ]
    assert "secret" not in repr(doctor.json()).casefold()


@pytest.mark.anyio
async def test_probe_exception_becomes_sanitized_unavailable_readiness() -> None:
    service = HealthService(
        database=None,
        database_probe=StaticProbe(HealthStatus.HEALTHY, name="postgresql"),
        probes=(FailingProbe(),),
    )

    report = await service.report()

    assert report.ready is False
    runner = next(check for check in report.checks if check.name == "background_runner")
    assert runner.status is HealthStatus.UNAVAILABLE
    assert runner.code == "CHECK_FAILED"
    assert "secret" not in repr(report).casefold()


@pytest.mark.anyio
async def test_production_metrics_require_constant_time_bearer_authentication() -> None:
    token = "metrics-only-secret-with-more-than-32-bytes"
    settings = AppSettings.model_validate(
        {
            "app": {"environment": "production", "public_base_url": "https://test"},
            "credentials": {"metrics_token": token},
        }
    )
    transport = httpx2.ASGITransport(app=create_app(settings))
    async with httpx2.AsyncClient(transport=transport, base_url="https://test") as client:
        missing = await client.get("/metrics")
        wrong = await client.get(
            "/metrics", headers={"Authorization": "Bearer wrong-secret"}
        )
        allowed = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {token}"}
        )

    assert missing.status_code == wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert allowed.status_code == 200
    assert token not in allowed.text
    assert "wrong-secret" not in wrong.text
