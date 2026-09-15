from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOT = REPOSITORY_ROOT / "infra" / "production"


def _compose() -> dict[str, object]:
    payload = yaml.safe_load((PRODUCTION_ROOT / "compose.yml").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_production_compose_keeps_application_services_private() -> None:
    services = _compose()["services"]
    assert isinstance(services, dict)
    assert set(services) == {"postgres", "migrate", "api", "worker", "frontend", "gateway"}

    for name in ("postgres", "migrate", "api", "worker", "frontend"):
        service = services[name]
        assert isinstance(service, dict)
        assert "ports" not in service

    gateway = services["gateway"]
    assert isinstance(gateway, dict)
    assert gateway["ports"] == [
        "${HTTP_BIND:-0.0.0.0}:80:80",
        "${HTTPS_BIND:-0.0.0.0}:443:443",
    ]
    assert gateway["networks"] == ["private"]
    assert services["api"]["expose"] == ["8000"]
    assert services["frontend"]["expose"] == ["8080"]


def test_production_compose_shares_runtime_and_orders_migrations() -> None:
    services = _compose()["services"]
    assert isinstance(services, dict)
    for name in ("migrate", "api", "worker"):
        service = services[name]
        assert isinstance(service, dict)
        assert "runtime-data:/data/runtime" in service["volumes"]
        dependency = "migrate" if name != "migrate" else "postgres"
        expected_condition = (
            "service_completed_successfully" if name != "migrate" else "service_healthy"
        )
        assert service["depends_on"][dependency]["condition"] == expected_condition

    assert services["api"]["command"][:2] == ["uvicorn", "enterprise_rag.main:app"]
    assert services["worker"]["command"] == ["enterprise-rag-worker"]
    assert services["migrate"]["command"][-2:] == ["upgrade", "head"]


def test_gateway_preserves_sse_mcp_and_security_headers() -> None:
    caddyfile = (PRODUCTION_ROOT / "gateway.Caddyfile").read_text(encoding="utf-8")

    assert "path /api/v1/queries/stream /mcp*" in caddyfile
    assert "flush_interval -1" in caddyfile
    assert "reverse_proxy @api api:8000" in caddyfile
    assert "reverse_proxy frontend:8080" in caddyfile
    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
    ):
        assert header in caddyfile
