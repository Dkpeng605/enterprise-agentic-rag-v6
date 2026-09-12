"""PostgreSQL and HTTP acceptance tests for tenant-scoped trace timelines."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from enterprise_rag.adapters.database import Database, PostgreSQLTraceStore
from enterprise_rag.adapters.database.models import TraceRunModel, TraceSpanModel
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.api import create_app
from enterprise_rag.domain import AppError, ErrorCode
from enterprise_rag.ports import StoredSpan, TraceCompletion
from enterprise_rag.services import TraceService
from enterprise_rag.services.auth import DEMO_ACTOR_ID

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
SESSION_SECRET = "trace-integration-session-secret-with-more-than-32-bytes"
NOW = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
TENANT_A = UUID("01900000-0000-7000-8000-000000006101")
TENANT_B = UUID("01900000-0000-7000-8000-000000006102")
ACTOR_A = UUID("01900000-0000-7000-8000-000000006103")
ACTOR_B = UUID("01900000-0000-7000-8000-000000006104")
QUERY_A = UUID("01900000-0000-7000-8000-000000006105")
QUERY_A_2 = UUID("01900000-0000-7000-8000-000000006106")
QUERY_B = UUID("01900000-0000-7000-8000-000000006107")
TRACE_A = "1" * 32
TRACE_A_2 = "2" * 32
TRACE_B = "3" * 32


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


@pytest.mark.anyio
async def test_trace_store_is_idempotent_paginated_and_tenant_scoped() -> None:
    database = Database(DATABASE_URL)
    store = PostgreSQLTraceStore(database)
    try:
        await _seed_tenants(database)
        first = _completion(
            TRACE_A,
            TENANT_A,
            ACTOR_A,
            QUERY_A,
            NOW,
            attributes={"planner_degraded": True, "planner_provider": "fixture"},
        )
        spans = _ranked_spans(TRACE_A, NOW)
        await store.persist(first, spans)
        await store.persist(first, spans)
        await store.persist(
            _completion(TRACE_A_2, TENANT_A, ACTOR_A, QUERY_A_2, NOW + timedelta(minutes=1)),
            (),
        )
        await store.persist(
            _completion(TRACE_B, TENANT_B, ACTOR_B, QUERY_B, NOW + timedelta(minutes=2)),
            (),
        )

        async with database.session() as session:
            run_count = await session.scalar(select(func.count()).select_from(TraceRunModel))
            span_count = await session.scalar(select(func.count()).select_from(TraceSpanModel))
        assert run_count == 3
        assert span_count == 3

        newest = await store.list(TENANT_A, trace_type="query", cursor=None, limit=1)
        assert [item.trace_id for item in newest.items] == [TRACE_A_2]
        assert newest.next_cursor is not None
        older = await store.list(
            TENANT_A,
            trace_type="query",
            cursor=newest.next_cursor,
            limit=1,
        )
        assert [item.trace_id for item in older.items] == [TRACE_A]
        assert {item.trace_id for item in newest.items + older.items} == {
            TRACE_A,
            TRACE_A_2,
        }

        detail = await store.get(TENANT_A, TRACE_A)
        assert detail is not None
        assert detail.summary.degraded is True
        assert [span.name for span in detail.spans] == [
            "rag.dense_retrieval",
            "rag.rrf_fusion",
            "rag.rerank",
        ]
        assert detail.spans[0].events[0]["attributes"] == {
            "rag.method": "dense",
            "rag.rank": 1,
            "rag.leaf_id": "leaf_01",
            "rag.root_id": "root_01",
            "rag.score": 0.91,
        }
        assert await store.get(TENANT_B, TRACE_A) is None
        assert [item.trace_id for item in (
            await store.list(TENANT_B, trace_type=None, cursor=None, limit=20)
        ).items] == [TRACE_B]

        with pytest.raises(AppError) as invalid_cursor:
            await store.list(TENANT_A, trace_type=None, cursor="not-a-cursor", limit=20)
        assert invalid_cursor.value.code is ErrorCode.VALIDATION_ERROR
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_anonymous_trace_api_lists_own_tenant_and_hides_other_tenant(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    trace_store = PostgreSQLTraceStore(database)
    trace_service = TraceService(trace_store)
    try:
        async with database.session() as session:
            await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        application = create_app(
            database=database,
            object_store=LocalObjectStore(tmp_path / "objects"),
            session_secret=SESSION_SECRET,
            trace_service=trace_service,
            clock=lambda: NOW,
        )
        transport = httpx2.ASGITransport(app=application)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            auth_response = await client.get("/api/v1/auth/me")
            assert auth_response.status_code == 200
            demo_tenant = UUID(auth_response.json()["tenant"]["id"])
            await trace_store.persist(
                _completion(TRACE_A, demo_tenant, DEMO_ACTOR_ID, QUERY_A, NOW),
                _ranked_spans(TRACE_A, NOW),
            )
            await _seed_other_trace(database)

            listed = await client.get("/api/v1/traces?type=query&limit=1")
            assert listed.status_code == 200
            assert [item["trace_id"] for item in listed.json()["items"]] == [TRACE_A]
            query_list = await client.get("/api/v1/traces/query?limit=1")
            assert query_list.status_code == 200
            assert query_list.json() == listed.json()
            ingestion_list = await client.get("/api/v1/traces/ingestion")
            assert ingestion_list.status_code == 200
            assert ingestion_list.json()["items"] == []
            detail = await client.get(f"/api/v1/traces/{TRACE_A}")
            assert detail.status_code == 200
            assert detail.json()["summary"]["subject_id"] == str(QUERY_A)
            assert detail.json()["spans"][2]["events"][0]["attributes"]["rag.rank"] == 1
            hidden = await client.get(f"/api/v1/traces/{TRACE_B}")
            assert hidden.status_code == 404
            assert hidden.json()["error"]["code"] == "NOT_FOUND"
            invalid = await client.get("/api/v1/traces/not-a-trace")
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await database.dispose()


async def _seed_tenants(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) VALUES "
                "(:tenant_a, 'Trace A', 'trace-a'), (:tenant_b, 'Trace B', 'trace-b')"
            ),
            {"tenant_a": TENANT_A, "tenant_b": TENANT_B},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES "
                "(:actor_a, 'trace-a@example.test', 'unused'), "
                "(:actor_b, 'trace-b@example.test', 'unused')"
            ),
            {"actor_a": ACTOR_A, "actor_b": ACTOR_B},
        )


async def _seed_other_trace(database: Database) -> None:
    async with database.session() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Other', 'trace-other')"),
            {"id": TENANT_B},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (:id, 'trace-other@example.test', 'unused')"
            ),
            {"id": ACTOR_B},
        )
    await PostgreSQLTraceStore(database).persist(
        _completion(TRACE_B, TENANT_B, ACTOR_B, QUERY_B, NOW), ()
    )


def _completion(
    trace_id: str,
    tenant_id: UUID,
    actor_id: UUID,
    query_id: UUID,
    started_at: datetime,
    *,
    attributes: dict[str, object] | None = None,
) -> TraceCompletion:
    return TraceCompletion(
        trace_id,
        "query",
        tenant_id,
        actor_id,
        "anonymous",
        query_id,
        None,
        "standard",
        "answered",
        started_at,
        started_at + timedelta(milliseconds=250),
        {"llm_calls": 1, "input_tokens": 12, "output_tokens": 4},
        attributes or {},
    )


def _ranked_spans(trace_id: str, started_at: datetime) -> tuple[StoredSpan, ...]:
    names = ("rag.dense_retrieval", "rag.rrf_fusion", "rag.rerank")
    event_names = (
        "rag.retrieval.candidate",
        "rag.fusion.candidate",
        "rag.rerank.candidate",
    )
    event_attributes = (
        {
            "rag.method": "dense",
            "rag.rank": 1,
            "rag.leaf_id": "leaf_01",
            "rag.root_id": "root_01",
            "rag.score": 0.91,
        },
        {
            "rag.rank": 1,
            "rag.leaf_id": "leaf_01",
            "rag.root_id": "root_01",
            "rag.dense_rank": 1,
            "rag.fused_score": 0.032,
        },
        {
            "rag.rank": 1,
            "rag.leaf_id": "leaf_01",
            "rag.root_id": "root_01",
            "rag.fused_score": 0.032,
            "rag.rerank_score": 0.97,
        },
    )
    return tuple(
        StoredSpan(
            trace_id,
            f"{index:016x}",
            None,
            name,
            started_at + timedelta(milliseconds=index * 10),
            started_at + timedelta(milliseconds=index * 10 + 5),
            "UNSET",
            {"rag.candidate_count": 1},
            (
                {
                    "name": event_name,
                    "timestamp": started_at.isoformat(),
                    "attributes": attributes,
                },
            ),
        )
        for index, (name, event_name, attributes) in enumerate(
            zip(names, event_names, event_attributes, strict=True), start=1
        )
    )
