"""EDD checks for bounded, safe, best-effort trace recording."""

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from enterprise_rag.domain.jobs import JobSnapshot, JobStatus
from enterprise_rag.observability import BufferedSpanExporter, start_span
from enterprise_rag.ports import TraceCompletion
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

SESSION_ID = UUID("01900000-0000-7000-8000-000000005402")
TENANT_ID = UUID("01900000-0000-7000-8000-000000005403")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000005404")
QUERY_ID = UUID("01900000-0000-7000-8000-000000005405")
JOB_ID = UUID("01900000-0000-7000-8000-000000005406")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000005407")
VERSION_ID = UUID("01900000-0000-7000-8000-000000005408")
NOW = datetime(2026, 9, 12, tzinfo=UTC)


class DiagnosticRunner:
    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        del emit
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ANSWERED,
            "safe answer",
            (),
            {
                "planner_degraded": True,
                "reranker_provider": "fixture-reranker",
                "private_prompt": "must-not-persist",
            },
            {
                "llm_calls": 1,
                "input_tokens": 8,
                "provider": "must-not-persist",
            },
        )


class CapturingRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.completions: list[TraceCompletion] = []

    async def record(self, completion: TraceCompletion) -> None:
        self.completions.append(completion)
        if self.fail:
            raise RuntimeError("database secret must not reach logs")


class TraceOnlyPipeline(IngestionPipeline):
    def __init__(self, provider: TracerProvider, recorder: CapturingRecorder) -> None:
        self._tracer_provider = provider
        self._trace_recorder = recorder
        self._clock = lambda: NOW

    async def _execute_traced(self, job: JobSnapshot, *, owner: str) -> PipelineRunResult:
        del owner
        return PipelineRunResult(job, job.status is JobStatus.SUCCEEDED)

    async def execute_fixture(self, job: JobSnapshot) -> PipelineRunResult:
        return await self._execute(job, owner="fixture-worker")


def _telemetry() -> tuple[TracerProvider, BufferedSpanExporter]:
    exporter = BufferedSpanExporter(max_traces=2, max_spans_per_trace=10)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_buffered_exporter_keeps_rankings_and_drops_sensitive_values() -> None:
    provider, exporter = _telemetry()
    with start_span("rag.query", tracer_provider=provider) as span:
        trace_id = f"{span.get_span_context().trace_id:032x}"
        span.set_attribute("rag.candidate_count", 1)
        span.set_attribute("rag.query.text", "private question")
        span.set_attribute("authorization", "Bearer private-token")
        span.add_event(
            "rag.rerank.candidate",
            {
                "rag.rank": 1,
                "rag.leaf_id": "leaf_01",
                "rag.rerank_score": 0.93,
                "rag.query.text": "private question",
                "error.message": "database secret",
            },
        )

    spans = exporter.take(trace_id)

    assert len(spans) == 1
    assert spans[0].attributes["rag.candidate_count"] == 1
    event_attributes = _event_attributes(spans[0].events[0])
    assert event_attributes == {
        "rag.rank": 1,
        "rag.leaf_id": "leaf_01",
        "rag.rerank_score": 0.93,
    }
    serialized = repr(spans)
    assert "private question" not in serialized
    assert "private-token" not in serialized
    assert "database secret" not in serialized


@pytest.mark.anyio
async def test_query_completion_records_safe_summary_after_span_finishes() -> None:
    provider, _ = _telemetry()
    recorder = CapturingRecorder()
    application = KnowledgeApplication(
        QueryApiService(DiagnosticRunner()),
        query_id_factory=lambda: QUERY_ID,
        tracer_provider=provider,
        trace_recorder=recorder,
    )

    result = await application.execute(
        Principal(SESSION_ID, TENANT_ID, ACTOR_ID),
        KnowledgeQuery("private question"),
    )

    assert result.status is QueryRunStatus.ANSWERED
    assert len(recorder.completions) == 1
    completion = recorder.completions[0]
    assert completion.subject_id == QUERY_ID
    assert completion.status == "answered"
    assert completion.usage == {"llm_calls": 1, "input_tokens": 8}
    assert completion.attributes == {
        "planner_degraded": True,
        "reranker_provider": "fixture-reranker",
    }
    assert completion.finished_at >= completion.started_at
    assert "private question" not in repr(completion)


@pytest.mark.anyio
async def test_trace_write_failure_does_not_fail_query_or_leak_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = _telemetry()
    recorder = CapturingRecorder(fail=True)
    application = KnowledgeApplication(
        QueryApiService(DiagnosticRunner()),
        query_id_factory=lambda: QUERY_ID,
        tracer_provider=provider,
        trace_recorder=recorder,
    )

    warnings: list[tuple[str, Mapping[str, object]]] = []

    def capture_warning(message: str, *, extra: Mapping[str, object]) -> None:
        warnings.append((message, extra))

    monkeypatch.setattr(
        "enterprise_rag.services.knowledge.LOGGER.warning", capture_warning
    )
    result = await application.execute(
        Principal(SESSION_ID, TENANT_ID, ACTOR_ID),
        KnowledgeQuery("private question"),
    )

    assert result.status is QueryRunStatus.ANSWERED
    assert warnings == [
        (
            "rag.trace.persist_failed",
            {"event_code": "TRACE_PERSIST_FAILED", "outcome": "degraded"},
        )
    ]
    assert "database secret" not in repr(warnings)


@pytest.mark.anyio
async def test_ingestion_completion_records_worker_trace() -> None:
    provider, _ = _telemetry()
    recorder = CapturingRecorder()
    job = JobSnapshot(
        JOB_ID,
        TENANT_ID,
        DOCUMENT_ID,
        VERSION_ID,
        "ingest",
        JobStatus.SUCCEEDED,
        1,
        3,
        NOW,
        None,
        None,
        None,
        100,
        "finalizing",
        None,
        None,
        False,
    )

    result = await TraceOnlyPipeline(provider, recorder).execute_fixture(job)

    assert result.completed is True
    assert len(recorder.completions) == 1
    completion = recorder.completions[0]
    assert completion.trace_type == "ingestion"
    assert completion.actor_type == "worker"
    assert completion.subject_id == JOB_ID
    assert completion.status == "succeeded"
    assert completion.attributes == {
        "attempt": 1,
        "progress": 100,
        "completed": True,
        "error_code": None,
    }


def _event_attributes(event: Mapping[str, object]) -> Mapping[str, object]:
    attributes = event.get("attributes")
    assert isinstance(attributes, Mapping)
    return attributes
