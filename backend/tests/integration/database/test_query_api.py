import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.api import create_app
from enterprise_rag.config import AppSettings
from enterprise_rag.domain import QueryMode
from enterprise_rag.services import (
    QueryCommand,
    QueryExecution,
    QueryProgress,
    QueryProgressStage,
    QueryRunStatus,
)
from enterprise_rag.services.query_api import ProgressSink

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
SESSION_SECRET = "query-api-session-secret-with-more-than-32-bytes"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


class FakeQueryRunner:
    def __init__(self) -> None:
        self.commands: list[QueryCommand] = []

    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        self.commands.append(command)
        if emit is not None:
            await emit(QueryProgress(QueryProgressStage.PLANNING))
            await emit(QueryProgress(QueryProgressStage.RETRIEVING))
            await emit(QueryProgress(QueryProgressStage.ANSWERING))
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ANSWERED,
            "来自测试 Runner 的答案",
            (),
            {"mode": command.mode.value},
            {"llm_calls": 2, "input_tokens": 120, "output_tokens": 30},
        )


@pytest.fixture
async def query_api(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx2.AsyncClient, FakeQueryRunner]]:
    database = Database(DATABASE_URL)
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
    runner = FakeQueryRunner()
    application = create_app(
        AppSettings.model_validate({"app": {"public_base_url": "https://testserver"}}),
        database=database,
        object_store=LocalObjectStore(tmp_path / "objects"),
        session_secret=SESSION_SECRET,
        query_runner=runner,
        query_heartbeat_seconds=0.01,
        clock=lambda: NOW,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=application), base_url="https://testserver"
    ) as client:
        auth = await client.get("/api/v1/auth/me")
        assert auth.status_code == 200
        yield client, runner
    await database.dispose()


@pytest.mark.anyio
async def test_query_rest_binds_server_tenant_and_accepts_anonymous_reader(
    query_api: tuple[httpx2.AsyncClient, FakeQueryRunner],
) -> None:
    client, runner = query_api
    response = await client.post(
        "/api/v1/queries",
        json={
            "query": "比较两项政策",
            "mode": "deep",
            "scope": {"titles": ["Policy"]},
            "history": [{"role": "user", "content": "上一条问题"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "answered"
    assert response.json()["usage"] == {
        "llm_calls": 2,
        "input_tokens": 120,
        "output_tokens": 30,
    }
    assert len(response.json()["trace_id"]) == 32
    trace_id = response.json()["trace_id"]
    traces = await client.get("/api/v1/traces/query")
    assert traces.status_code == 200
    assert traces.json()["items"][0]["trace_id"] == trace_id
    trace_detail = await client.get(f"/api/v1/traces/{trace_id}")
    assert trace_detail.status_code == 200
    assert trace_detail.json()["spans"][0]["name"] == "rag.query"
    assert response.headers["x-request-id"]
    assert runner.commands[-1].mode is QueryMode.DEEP
    assert runner.commands[-1].scope.titles == ("Policy",)
    assert (
        str(runner.commands[-1].tenant_id)
        == (await client.get("/api/v1/auth/me")).json()["tenant"]["id"]
    )


@pytest.mark.anyio
async def test_query_sse_has_ordered_ids_events_and_terminal_payload(
    query_api: tuple[httpx2.AsyncClient, FakeQueryRunner],
) -> None:
    client, _ = query_api
    response = await client.post(
        "/api/v1/queries/stream", json={"query": "流式问题", "mode": "standard"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert "event: accepted" in response.text
    assert response.text.count("event: progress") == 3
    assert "event: completed" in response.text
    assert response.text.index("event: accepted") < response.text.index("event: completed")
    assert '"query_id"' in response.text
    assert '"trace_id"' in response.text
    assert '"mode":"standard"' in response.text


@pytest.mark.anyio
async def test_query_validation_and_openapi_contract(
    query_api: tuple[httpx2.AsyncClient, FakeQueryRunner],
) -> None:
    client, _ = query_api
    duplicate_scope = await client.post(
        "/api/v1/queries",
        json={"query": "q", "scope": {"titles": ["same", "same"]}},
    )
    oversized_history = await client.post(
        "/api/v1/queries",
        json={
            "query": "q",
            "history": [
                {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 4000}
                for index in range(4)
            ],
        },
    )
    oversized_query = await client.post(
        "/api/v1/queries",
        json={"query": "x" * 2_001},
    )
    openapi = (await client.get("/openapi.json")).json()

    assert duplicate_scope.status_code == 400
    assert duplicate_scope.json()["error"]["code"] == "VALIDATION_ERROR"
    assert oversized_history.status_code == 400
    assert oversized_query.status_code == 422
    assert "/api/v1/queries" in openapi["paths"]
    assert "/api/v1/queries/stream" in openapi["paths"]
    assert "text/event-stream" in str(openapi["paths"]["/api/v1/queries/stream"])


@pytest.mark.anyio
async def test_query_budget_returns_429_before_sixth_runner_call(
    query_api: tuple[httpx2.AsyncClient, FakeQueryRunner],
) -> None:
    client, runner = query_api

    responses = [
        await client.post("/api/v1/queries", json={"query": f"budget-{index}"})
        for index in range(6)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200, 429]
    assert responses[-1].json()["error"]["code"] == "RATE_LIMITED"
    assert responses[-1].headers["retry-after"] == "55"
    assert len(runner.commands) == 5
