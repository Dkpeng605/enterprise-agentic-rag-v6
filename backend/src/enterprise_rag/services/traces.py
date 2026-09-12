"""Best-effort trace recording and tenant-scoped query service."""

from uuid import UUID

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.traces import PostgreSQLTraceStore
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.observability.exporter import BufferedSpanExporter
from enterprise_rag.ports.traces import TraceCompletion, TraceDetail, TracePage, TraceStore


class TraceService:
    def __init__(self, store: TraceStore, exporter: BufferedSpanExporter | None = None) -> None:
        self._store = store
        self._exporter = exporter

    async def record(self, completion: TraceCompletion) -> None:
        spans = self._exporter.take(completion.trace_id) if self._exporter else ()
        await self._store.persist(completion, spans)

    async def list_traces(
        self,
        tenant_id: UUID,
        *,
        trace_type: str | None,
        cursor: str | None,
        limit: int,
    ) -> TracePage:
        return await self._store.list(
            tenant_id, trace_type=trace_type, cursor=cursor, limit=limit
        )

    async def get_trace(self, tenant_id: UUID, trace_id: str) -> TraceDetail:
        invalid_character = any(
            character not in "0123456789abcdef" for character in trace_id
        )
        if len(trace_id) != 32 or invalid_character:
            raise AppError(ErrorCode.VALIDATION_ERROR, "The trace ID is invalid.")
        detail = await self._store.get(tenant_id, trace_id)
        if detail is None:
            raise AppError(ErrorCode.NOT_FOUND, "The trace was not found.")
        return detail


def build_persistent_tracing(
    database: Database,
    *,
    max_buffered_traces: int = 1_000,
    max_spans_per_trace: int = 200,
) -> tuple[TracerProvider, TraceService]:
    exporter = BufferedSpanExporter(
        max_traces=max_buffered_traces, max_spans_per_trace=max_spans_per_trace
    )
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, TraceService(PostgreSQLTraceStore(database), exporter)
