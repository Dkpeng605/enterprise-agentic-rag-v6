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
        "status": "skeleton-ready",
    }


@pytest.mark.anyio
async def test_openapi_document_is_available() -> None:
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Enterprise Agentic RAG v6"
