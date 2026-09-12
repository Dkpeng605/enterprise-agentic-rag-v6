"""Correlation, structured logging, and OpenTelemetry acceptance tests."""

import asyncio
import io
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from enterprise_rag.api import create_app
from enterprise_rag.domain.jobs import JobSnapshot, JobStatus
from enterprise_rag.observability import (
    JsonLogFormatter,
    bind_context,
    current_context,
    safe_attributes,
    start_span,
)
from enterprise_rag.services import (
    IngestionPipeline,
    KnowledgeApplication,
    KnowledgeQuery,
    PipelineRunResult,
    Principal,
    QueryApiService,
    QueryCommand,
    QueryExecution,
    QueryRunStatus,
)
from enterprise_rag.services.query_api import ProgressSink

REQUEST_ID = UUID("01900000-0000-7000-8000-000000005401")
SESSION_ID = UUID("01900000-0000-7000-8000-000000005402")
TENANT_ID = UUID("01900000-0000-7000-8000-000000005403")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000005404")
QUERY_ID = UUID("01900000-0000-7000-8000-000000005405")
JOB_ID = UUID("01900000-0000-7000-8000-000000005406")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000005407")
VERSION_ID = UUID("01900000-0000-7000-8000-000000005408")


class FixtureRunner:
    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ANSWERED,
            "observable answer",
            (),
            {},
            {"llm_calls": 1, "input_tokens": 2, "output_tokens": 3},
        )


class TraceOnlyPipeline(IngestionPipeline):
    def __init__(self, provider: TracerProvider) -> None:
        self._tracer_provider = provider

    async def _execute_traced(self, job: JobSnapshot, *, owner: str) -> PipelineRunResult:
        await asyncio.sleep(0)
        return PipelineRunResult(job, job.status is JobStatus.SUCCEEDED)

    async def execute_fixture(self, job: JobSnapshot) -> PipelineRunResult:
        return await self._execute(job, owner="fixture-worker")


def telemetry() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.mark.anyio
async def test_contextvars_are_nested_and_task_local() -> None:
    async def observe(request_id: UUID) -> str | None:
        with bind_context(request_id=request_id):
            await asyncio.sleep(0)
            return current_context().request_id

    first, second = await asyncio.gather(
        observe(REQUEST_ID),
        observe(UUID("01900000-0000-7000-8000-000000005409")),
    )

    assert first == str(REQUEST_ID)
    assert second != first
    assert current_context().request_id is None


def test_json_log_has_correlation_and_redacts_credentials() -> None:
    provider, _ = telemetry()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("observability-contract")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with bind_context(
        request_id=REQUEST_ID,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        query_id=QUERY_ID,
    ), start_span("fixture.log", tracer_provider=provider):
        logger.info(
            "request failed Authorization: Bearer raw-secret token=also-secret",
            extra={"event_code": "SAFE_EVENT", "authorization": "Bearer ignored-secret"},
            exc_info=(RuntimeError, RuntimeError("exception-secret"), None),
        )

    line = stream.getvalue()
    payload = json.loads(line)
    assert payload["request_id"] == str(REQUEST_ID)
    assert payload["tenant_id"] == str(TENANT_ID)
    assert payload["event"] == "SAFE_EVENT"
    assert payload["level"] == "INFO"
    assert payload["service"] == "enterprise-agentic-rag-v6"
    assert payload["environment"] == "development"
    assert len(payload["trace_id"]) == 32
    assert len(payload["span_id"]) == 16
    assert "raw-secret" not in line
    assert "also-secret" not in line
    assert "ignored-secret" not in line
    assert "exception-secret" not in line
    assert "authorization" not in payload
    assert payload["exception_type"] == "RuntimeError"


@pytest.mark.anyio
async def test_http_query_and_ingestion_emit_correlated_safe_spans() -> None:
    provider, exporter = telemetry()
    knowledge = KnowledgeApplication(
        QueryApiService(FixtureRunner()),
        query_id_factory=lambda: QUERY_ID,
        tracer_provider=provider,
    )
    question = "private query text must not enter telemetry"

    with bind_context(request_id=REQUEST_ID), start_span(
        "transport.fixture", tracer_provider=provider
    ):
        result = await knowledge.execute(
            Principal(SESSION_ID, TENANT_ID, ACTOR_ID), KnowledgeQuery(question)
        )
    assert result.status is QueryRunStatus.ANSWERED

    job = JobSnapshot(
        JOB_ID,
        TENANT_ID,
        DOCUMENT_ID,
        VERSION_ID,
        "ingest",
        JobStatus.SUCCEEDED,
        1,
        3,
        datetime(2026, 9, 12, tzinfo=UTC),
        None,
        None,
        None,
        100,
        "finalizing",
        None,
        None,
        False,
    )
    await TraceOnlyPipeline(provider).execute_fixture(job)

    transport = httpx2.ASGITransport(app=create_app(tracer_provider=provider))
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/?token=must-not-be-captured",
            headers={
                "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
            },
        )
    assert response.status_code == 200

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert {"transport.fixture", "rag.query", "rag.ingestion", "http.request"} <= set(spans)
    query_span = spans["rag.query"]
    assert query_span.parent is not None
    query_attributes = query_span.attributes
    ingestion_attributes = spans["rag.ingestion"].attributes
    http_attributes = spans["http.request"].attributes
    assert query_attributes is not None
    assert ingestion_attributes is not None
    assert http_attributes is not None
    assert query_attributes["app.request_id"] == str(REQUEST_ID)
    assert query_attributes["app.tenant_id"] == str(TENANT_ID)
    assert query_attributes["app.actor_id"] == str(ACTOR_ID)
    assert query_attributes["app.query_id"] == str(QUERY_ID)
    assert query_attributes["rag.query.status"] == "answered"
    assert ingestion_attributes["app.job_id"] == str(JOB_ID)
    assert http_attributes["url.path"] == "/"
    assert f"{spans['http.request'].context.trace_id:032x}" == (
        "0123456789abcdef0123456789abcdef"
    )
    serialized = repr([(span.name, span.attributes) for span in spans.values()])
    assert question not in serialized
    assert "must-not-be-captured" not in serialized


@pytest.mark.parametrize(
    "name",
    ["http.request.header.authorization", "app.api_key", "rag.query.text", "llm.prompt"],
)
def test_sensitive_span_attribute_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="sensitive"):
        safe_attributes({name: "must-not-appear"})
