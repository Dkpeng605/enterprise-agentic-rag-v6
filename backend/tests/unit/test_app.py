import httpx2
import pytest

from enterprise_rag import __version__
from enterprise_rag.api import create_app


@pytest.mark.anyio
async def test_service_descriptor_reports_runnable_skeleton() -> None:
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "enterprise-agentic-rag-v6",
        "version": __version__,
        "status": "configuration-ready",
        "environment": "development",
    }


@pytest.mark.anyio
async def test_openapi_document_is_available() -> None:
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Enterprise Agentic RAG v6"
    assert "/api/v1/documents" in response.json()["paths"]


@pytest.mark.anyio
async def test_unconfigured_workspace_returns_stable_service_unavailable() -> None:
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert response.headers["x-request-id"] == response.json()["error"]["request_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/api/v1/queries", "/api/v1/queries/stream"])
async def test_unconfigured_query_runner_returns_service_unavailable(path: str) -> None:
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path, json={"query": "test"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
