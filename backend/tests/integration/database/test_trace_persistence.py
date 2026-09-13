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
JOB_OK = UUID("01900000-0000-7000-8000-000000006108")
JOB_FAILED = UUID("01900000-0000-7000-8000-000000006109")
TRACE_A = "1" * 32
TRACE_A_2 = "2" * 32
TRACE_B = "3" * 32
TRACE_INGEST_OK = "4" * 32
TRACE_INGEST_FAILED = "5" * 32


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
            _completion(
                TRACE_A_2,
                TENANT_A,
                ACTOR_A,
                QUERY_A_2,
                NOW + timedelta(minutes=1),
                mode="deep",
                status="abstained",
            ),
            _deep_spans(TRACE_A_2, NOW + timedelta(minutes=1)),
        )
        await store.persist(
            _completion(TRACE_B, TENANT_B, ACTOR_B, QUERY_B, NOW + timedelta(minutes=2)),
            (),
        )

        async with database.session() as session:
            run_count = await session.scalar(select(func.count()).select_from(TraceRunModel))
            span_count = await session.scalar(select(func.count()).select_from(TraceSpanModel))
        assert run_count == 3
        assert span_count == 6

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
            "rag.sparse_retrieval",
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
                _completion(
                    TRACE_A,
                    demo_tenant,
                    DEMO_ACTOR_ID,
                    QUERY_A,
                    NOW,
                    attributes={
                        "planner_degraded": True,
                        "planner_provider": "fixture-planner",
                    },
                ),
                _ranked_spans(TRACE_A, NOW),
            )
            await trace_store.persist(
                _completion(
                    TRACE_A_2,
                    demo_tenant,
                    DEMO_ACTOR_ID,
                    QUERY_A_2,
                    NOW + timedelta(minutes=1),
                    mode="deep",
                    status="abstained",
                ),
                _deep_spans(TRACE_A_2, NOW + timedelta(minutes=1)),
            )
            await trace_store.persist(
                _ingestion_completion(
                    TRACE_INGEST_OK,
                    demo_tenant,
                    JOB_OK,
                    NOW + timedelta(minutes=2),
                    status="succeeded",
                    progress=100,
                    completed=True,
                ),
                _ingestion_spans(TRACE_INGEST_OK, NOW + timedelta(minutes=2)),
            )
            await trace_store.persist(
                _ingestion_completion(
                    TRACE_INGEST_FAILED,
                    demo_tenant,
                    JOB_FAILED,
                    NOW + timedelta(minutes=3),
                    status="failed",
                    progress=40,
                    completed=False,
                    error_code="INTERNAL_ERROR",
                ),
                _failed_ingestion_spans(
                    TRACE_INGEST_FAILED, NOW + timedelta(minutes=3)
                ),
            )
            await _seed_other_trace(database)

            listed = await client.get("/api/v1/traces?type=query&limit=2")
            assert listed.status_code == 200
            assert [item["trace_id"] for item in listed.json()["items"]] == [
                TRACE_A_2,
                TRACE_A,
            ]
            query_list = await client.get("/api/v1/traces/query?limit=2")
            assert query_list.status_code == 200
            assert query_list.json() == listed.json()
            deep_list = await client.get(
                "/api/v1/traces/query?mode=deep&status=abstained&degraded=false"
            )
            assert [item["trace_id"] for item in deep_list.json()["items"]] == [
                TRACE_A_2
            ]
            degraded_list = await client.get(
                "/api/v1/traces/query?mode=standard&degraded=true"
            )
            assert [item["trace_id"] for item in degraded_list.json()["items"]] == [
                TRACE_A
            ]
            ingestion_list = await client.get("/api/v1/traces/ingestion")
            assert ingestion_list.status_code == 200
            assert [item["trace_id"] for item in ingestion_list.json()["items"]] == [
                TRACE_INGEST_FAILED,
                TRACE_INGEST_OK,
            ]
            failed_ingestion = await client.get(
                "/api/v1/traces/ingestion?status=failed"
            )
            assert [
                item["trace_id"] for item in failed_ingestion.json()["items"]
            ] == [TRACE_INGEST_FAILED]
            detail = await client.get(f"/api/v1/traces/{TRACE_A}")
            assert detail.status_code == 200
            assert detail.json()["summary"]["subject_id"] == str(QUERY_A)
            assert detail.json()["spans"][3]["events"][0]["attributes"]["rag.rank"] == 1
            query_view = await client.get(f"/api/v1/traces/query/{TRACE_A}")
            assert query_view.status_code == 200
            assert query_view.json()["rankings"][0] == {
                "leaf_id": "leaf_01",
                "root_id": "root_01",
                "dense_rank": 1,
                "sparse_rank": 2,
                "rrf_rank": 1,
                "rerank_rank": 1,
                "dense_score": 0.91,
                "sparse_score": 0.81,
                "rrf_score": 0.032,
                "rerank_score": 0.97,
            }
            assert query_view.json()["degradations"] == [
                {"component": "planner", "provider": "fixture-planner"}
            ]
            deep_view = await client.get(f"/api/v1/traces/query/{TRACE_A_2}")
            assert deep_view.json()["recovery_rounds"][0]["route"] == "hyde_dense"
            ingestion_view = await client.get(
                f"/api/v1/traces/ingestion/{TRACE_INGEST_OK}"
            )
            assert ingestion_view.status_code == 200
            assert ingestion_view.json()["completed"] is True
            assert ingestion_view.json()["stages"][1]["root_count"] == 2
            assert ingestion_view.json()["stages"][3]["verified_count"] == 5
            assert ingestion_view.json()["batches"][0]["phase"] == "staging"
            assert ingestion_view.json()["batches"][1]["phase"] == "activation"
            failed_view = await client.get(
                f"/api/v1/traces/ingestion/{TRACE_INGEST_FAILED}"
            )
            assert failed_view.json()["error_code"] == "INTERNAL_ERROR"
            assert failed_view.json()["stages"][2]["status"] == "ERROR"
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
    mode: str = "standard",
    status: str = "answered",
) -> TraceCompletion:
    return TraceCompletion(
        trace_id,
        "query",
        tenant_id,
        actor_id,
        "anonymous",
        query_id,
        None,
        mode,
        status,
        started_at,
        started_at + timedelta(milliseconds=250),
        {"llm_calls": 1, "input_tokens": 12, "output_tokens": 4},
        attributes or {},
    )


def _ingestion_completion(
    trace_id: str,
    tenant_id: UUID,
    job_id: UUID,
    started_at: datetime,
    *,
    status: str,
    progress: int,
    completed: bool,
    error_code: str | None = None,
) -> TraceCompletion:
    return TraceCompletion(
        trace_id,
        "ingestion",
        tenant_id,
        None,
        "worker",
        job_id,
        None,
        None,
        status,
        started_at,
        started_at + timedelta(milliseconds=500),
        {},
        {
            "attempt": 1,
            "progress": progress,
            "completed": completed,
            "error_code": error_code,
        },
    )


def _ranked_spans(trace_id: str, started_at: datetime) -> tuple[StoredSpan, ...]:
    names = (
        "rag.dense_retrieval",
        "rag.sparse_retrieval",
        "rag.rrf_fusion",
        "rag.rerank",
    )
    event_names = (
        "rag.retrieval.candidate",
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
            "rag.method": "sparse",
            "rag.rank": 2,
            "rag.leaf_id": "leaf_01",
            "rag.root_id": "root_01",
            "rag.score": 0.81,
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


def _deep_spans(trace_id: str, started_at: datetime) -> tuple[StoredSpan, ...]:
    return (
        StoredSpan(
            trace_id,
            "00000000000000a1",
            None,
            "rag.deep_recovery",
            started_at + timedelta(milliseconds=30),
            started_at + timedelta(milliseconds=80),
            "UNSET",
            {"rag.recovery.round_count": 1, "rag.recovery.decision": "abstain"},
        ),
        StoredSpan(
            trace_id,
            "00000000000000a2",
            "00000000000000a1",
            "rag.deep_recovery.round",
            started_at + timedelta(milliseconds=40),
            started_at + timedelta(milliseconds=70),
            "UNSET",
            {
                "rag.recovery.round": 1,
                "rag.recovery.route": "hyde_dense",
                "rag.recovery.retrieval_mode": "dense_only",
                "rag.recovery.target_count": 1,
                "rag.recovery.returned_count": 2,
                "rag.recovery.added_count": 1,
                "rag.recovery.duplicate_count": 1,
            },
        ),
    )


def _ingestion_spans(
    trace_id: str, started_at: datetime
) -> tuple[StoredSpan, ...]:
    root_span_id = "00000000000000b0"
    project_span_id = "00000000000000b3"
    return (
        StoredSpan(
            trace_id,
            root_span_id,
            None,
            "rag.ingestion",
            started_at,
            started_at + timedelta(milliseconds=500),
            "UNSET",
            {"rag.ingestion.status": "succeeded"},
        ),
        StoredSpan(
            trace_id,
            "00000000000000b1",
            root_span_id,
            "rag.ingestion.load",
            started_at + timedelta(milliseconds=10),
            started_at + timedelta(milliseconds=70),
            "UNSET",
            {"rag.ingestion.root_count": 2},
        ),
        StoredSpan(
            trace_id,
            "00000000000000b2",
            root_span_id,
            "rag.ingestion.split",
            started_at + timedelta(milliseconds=80),
            started_at + timedelta(milliseconds=140),
            "UNSET",
            {"rag.ingestion.leaf_count": 5},
        ),
        StoredSpan(
            trace_id,
            project_span_id,
            root_span_id,
            "rag.ingestion.project",
            started_at + timedelta(milliseconds=150),
            started_at + timedelta(milliseconds=350),
            "UNSET",
            {
                "rag.ingestion.expected_count": 5,
                "rag.ingestion.verified_count": 5,
                "rag.ingestion.batch_count": 3,
            },
        ),
        _batch_span(
            trace_id,
            "00000000000000b4",
            project_span_id,
            started_at,
            offset=170,
            phase="staging",
        ),
        _batch_span(
            trace_id,
            "00000000000000b5",
            project_span_id,
            started_at,
            offset=230,
            phase="activation",
        ),
        StoredSpan(
            trace_id,
            "00000000000000b6",
            root_span_id,
            "rag.ingestion.finalize",
            started_at + timedelta(milliseconds=360),
            started_at + timedelta(milliseconds=430),
            "UNSET",
        ),
    )


def _batch_span(
    trace_id: str,
    span_id: str,
    parent_span_id: str,
    started_at: datetime,
    *,
    offset: int,
    phase: str,
) -> StoredSpan:
    return StoredSpan(
        trace_id,
        span_id,
        parent_span_id,
        "rag.ingestion.projection.batch",
        started_at + timedelta(milliseconds=offset),
        started_at + timedelta(milliseconds=offset + 20),
        "UNSET",
        {
            "rag.ingestion.batch.phase": phase,
            "rag.ingestion.batch.index": 1,
            "rag.ingestion.batch.count": 3,
            "rag.ingestion.batch.item_count": 2,
            "rag.ingestion.batch.written_count": 2,
        },
    )


def _failed_ingestion_spans(
    trace_id: str, started_at: datetime
) -> tuple[StoredSpan, ...]:
    root_span_id = "00000000000000c0"
    return (
        StoredSpan(
            trace_id,
            root_span_id,
            None,
            "rag.ingestion",
            started_at,
            started_at + timedelta(milliseconds=500),
            "UNSET",
            {"rag.ingestion.status": "failed"},
        ),
        StoredSpan(
            trace_id,
            "00000000000000c1",
            root_span_id,
            "rag.ingestion.load",
            started_at + timedelta(milliseconds=10),
            started_at + timedelta(milliseconds=70),
            "UNSET",
            {"rag.ingestion.root_count": 1},
        ),
        StoredSpan(
            trace_id,
            "00000000000000c2",
            root_span_id,
            "rag.ingestion.split",
            started_at + timedelta(milliseconds=80),
            started_at + timedelta(milliseconds=110),
            "ERROR",
        ),
    )
