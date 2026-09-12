"""Readiness acceptance test against a real PostgreSQL dependency."""

import os
from pathlib import Path

import httpx2
import pytest

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.api import create_app

DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
SESSION_SECRET = "health-integration-session-secret-with-more-than-32-bytes"


@pytest.mark.anyio
async def test_real_postgresql_and_workspace_make_readiness_healthy(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    try:
        application = create_app(
            database=database,
            object_store=LocalObjectStore(tmp_path / "objects"),
            session_secret=SESSION_SECRET,
        )
        transport = httpx2.ASGITransport(app=application)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            ready = await client.get("/health/ready")
            doctor = await client.get("/health/doctor")

        assert ready.status_code == 200
        assert ready.json()["status"] == "healthy"
        assert ready.json()["ready"] is True
        checks = {item["name"]: item for item in ready.json()["checks"]}
        assert checks["configuration"]["status"] == "healthy"
        assert checks["postgresql"]["status"] == "healthy"
        assert doctor.status_code == 200
        serialized = repr(doctor.json())
        assert DATABASE_URL not in serialized
        assert "enterprise_rag:enterprise_rag" not in serialized
    finally:
        await database.dispose()
