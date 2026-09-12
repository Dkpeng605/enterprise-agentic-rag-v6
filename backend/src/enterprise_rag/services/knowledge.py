"""Transport-neutral knowledge-query application boundary."""

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from time import perf_counter
from uuid import UUID

from opentelemetry.trace import TracerProvider

from enterprise_rag.domain.common import new_uuid7, require_non_empty, require_uuid7, utc_now
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryMode, QueryScope
from enterprise_rag.observability import (
    ApplicationMetrics,
    bind_context,
    bind_metrics,
    current_context,
    start_span,
)
from enterprise_rag.ports.planner import ConversationTurn
from enterprise_rag.ports.traces import TraceCompletion, TraceRecorder
from enterprise_rag.services.auth import Principal
from enterprise_rag.services.query_api import (
    DisconnectCheck,
    QueryApiService,
    QueryCommand,
    QueryExecution,
    QueryStreamEvent,
)

QueryIdFactory = Callable[[], UUID]
Clock = Callable[[], datetime]
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    question: str
    mode: QueryMode = QueryMode.STANDARD
    scope: QueryScope = QueryScope()
    history: tuple[ConversationTurn, ...] = ()

    def __post_init__(self) -> None:
        require_non_empty(self.question, "question")
        if len(self.question) > 2_000:
            raise ValueError("question must not exceed 2000 characters")


class KnowledgeApplication:
    """The single query use case shared by HTTP, MCP, and later CLI adapters."""

    def __init__(
        self,
        query_api: QueryApiService,
        *,
        query_id_factory: QueryIdFactory = new_uuid7,
        tracer_provider: TracerProvider | None = None,
        trace_recorder: TraceRecorder | None = None,
        metrics: ApplicationMetrics | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._query_api = query_api
        self._query_id_factory = query_id_factory
        self._tracer_provider = tracer_provider
        self._trace_recorder = trace_recorder
        self._metrics = metrics
        self._clock = clock

    def command(self, principal: Principal, request: KnowledgeQuery) -> QueryCommand:
        try:
            query_id = self._query_id_factory()
            require_uuid7(query_id, "query_id")
            return QueryCommand(
                query_id,
                principal.tenant_id,
                principal.actor_id,
                request.question.strip(),
                request.mode,
                request.scope,
                request.history,
                principal.session_id,
            )
        except ValueError as error:
            raise AppError(ErrorCode.VALIDATION_ERROR, "The query request is invalid.") from error

    async def execute(self, principal: Principal, request: KnowledgeQuery) -> QueryExecution:
        command = self.command(principal, request)
        request_id = _context_uuid(current_context().request_id)
        started_at = self._clock()
        started_monotonic = perf_counter()
        trace_id: str | None = None
        status = "error"
        usage: Mapping[str, object] = {}
        attributes: Mapping[str, object] = {}
        try:
            with bind_metrics(self._metrics), bind_context(
                tenant_id=command.tenant_id,
                actor_id=command.actor_id,
                query_id=command.query_id,
            ), start_span(
                "rag.query",
                attributes={"rag.query.mode": command.mode.value},
                tracer_provider=self._tracer_provider,
            ) as span:
                trace_id = _span_trace_id(span)
                result = await self._query_api.execute(command)
                if trace_id is not None:
                    result = replace(result, trace_id=trace_id)
                status = result.status.value
                usage = _numeric_usage(result.usage)
                attributes = _diagnostic_attributes(result.diagnostics)
                span.set_attribute("rag.query.status", status)
                span.set_attribute("rag.query.citation_count", len(result.citations))
                LOGGER.info(
                    "rag.query.completed",
                    extra={"event_code": "QUERY_COMPLETED", "outcome": status},
                )
        finally:
            try:
                await self._record_trace(
                    trace_id=trace_id,
                    principal=principal,
                    command=command,
                    request_id=request_id,
                    status=status,
                    started_at=started_at,
                    usage=usage,
                    attributes=attributes,
                )
            finally:
                if self._metrics is not None:
                    self._metrics.observe_query(
                        mode=command.mode.value,
                        status=status,
                        duration_seconds=perf_counter() - started_monotonic,
                    )
        return result

    def stream(
        self,
        principal: Principal,
        request: KnowledgeQuery,
        *,
        disconnected: DisconnectCheck,
    ) -> AsyncIterator[QueryStreamEvent]:
        command = self.command(principal, request)
        return self._observed_stream(principal, command, disconnected=disconnected)

    async def _observed_stream(
        self,
        principal: Principal,
        command: QueryCommand,
        *,
        disconnected: DisconnectCheck,
    ) -> AsyncIterator[QueryStreamEvent]:
        request_id = _context_uuid(current_context().request_id)
        started_at = self._clock()
        started_monotonic = perf_counter()
        trace_id: str | None = None
        trace_status = "cancelled"
        usage: Mapping[str, object] = {}
        attributes: Mapping[str, object] = {}
        try:
            with bind_metrics(self._metrics), bind_context(
                tenant_id=command.tenant_id,
                actor_id=command.actor_id,
                query_id=command.query_id,
            ), start_span(
                "rag.query.stream",
                attributes={"rag.query.mode": command.mode.value},
                tracer_provider=self._tracer_provider,
            ) as span:
                trace_id = _span_trace_id(span)
                async for event in self._query_api.stream(
                    command, disconnected=disconnected
                ):
                    output_event = _stream_trace_event(event, trace_id, command.mode)
                    if event.event in {"completed", "error"}:
                        trace_status = _stream_status(event)
                        if event.event == "completed":
                            usage = _numeric_usage(_mapping(event.data.get("usage")))
                            attributes = _diagnostic_attributes(
                                _mapping(event.data.get("diagnostics"))
                            )
                        span.set_attribute("rag.query.status", trace_status)
                        LOGGER.info(
                            "rag.query.stream.terminal",
                            extra={
                                "event_code": "QUERY_STREAM_TERMINAL",
                                "outcome": trace_status,
                            },
                        )
                    yield output_event
        finally:
            try:
                await self._record_trace(
                    trace_id=trace_id,
                    principal=principal,
                    command=command,
                    request_id=request_id,
                    status=trace_status,
                    started_at=started_at,
                    usage=usage,
                    attributes=attributes,
                )
            finally:
                if self._metrics is not None:
                    self._metrics.observe_query(
                        mode=command.mode.value,
                        status=trace_status,
                        duration_seconds=perf_counter() - started_monotonic,
                    )

    async def _record_trace(
        self,
        *,
        trace_id: str | None,
        principal: Principal,
        command: QueryCommand,
        request_id: UUID | None,
        status: str,
        started_at: datetime,
        usage: Mapping[str, object],
        attributes: Mapping[str, object],
    ) -> None:
        if self._trace_recorder is None or trace_id is None:
            return
        completion = TraceCompletion(
            trace_id,
            "query",
            command.tenant_id,
            command.actor_id,
            principal.actor_type,
            command.query_id,
            request_id,
            command.mode.value,
            status,
            started_at,
            self._clock(),
            usage,
            attributes,
        )
        try:
            await self._trace_recorder.record(completion)
        except Exception:
            LOGGER.warning(
                "rag.trace.persist_failed",
                extra={"event_code": "TRACE_PERSIST_FAILED", "outcome": "degraded"},
            )


def _span_trace_id(span: object) -> str | None:
    get_context = getattr(span, "get_span_context", None)
    if not callable(get_context):
        return None
    context = get_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}"


def _context_uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        result = UUID(value)
        require_uuid7(result, "request_id")
        return result
    except ValueError:
        return None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _numeric_usage(values: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in values.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def _diagnostic_attributes(values: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in values.items()
        if (key.endswith("_degraded") and isinstance(value, bool))
        or (key.endswith("_provider") and isinstance(value, str))
    }


def _stream_status(event: QueryStreamEvent) -> str:
    if event.event == "completed":
        value = event.data.get("status")
        return value if isinstance(value, str) else "completed"
    return "error"


def _stream_trace_event(
    event: QueryStreamEvent,
    trace_id: str | None,
    mode: QueryMode,
) -> QueryStreamEvent:
    if trace_id is None or event.event not in {"accepted", "completed"}:
        return event
    data = dict(event.data)
    data["trace_id"] = trace_id
    if event.event == "accepted":
        data["mode"] = mode.value
    return QueryStreamEvent(event.sequence, event.event, data)


class McpApplicationService:
    """Protocol-free MCP facade; transport adapters only decode and encode messages."""

    def __init__(self, knowledge: KnowledgeApplication) -> None:
        self._knowledge = knowledge

    async def query_knowledge_base(
        self, principal: Principal, request: KnowledgeQuery
    ) -> QueryExecution:
        return await self._knowledge.execute(principal, request)
