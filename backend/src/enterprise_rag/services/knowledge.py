"""Transport-neutral knowledge-query application boundary."""

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from uuid import UUID

from opentelemetry.trace import TracerProvider

from enterprise_rag.domain.common import new_uuid7, require_non_empty, require_uuid7
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryMode, QueryScope
from enterprise_rag.observability import bind_context, start_span
from enterprise_rag.ports.planner import ConversationTurn
from enterprise_rag.services.auth import Principal
from enterprise_rag.services.query_api import (
    DisconnectCheck,
    QueryApiService,
    QueryCommand,
    QueryExecution,
    QueryStreamEvent,
)

QueryIdFactory = Callable[[], UUID]
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
    ) -> None:
        self._query_api = query_api
        self._query_id_factory = query_id_factory
        self._tracer_provider = tracer_provider

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
        with bind_context(
            tenant_id=command.tenant_id,
            actor_id=command.actor_id,
            query_id=command.query_id,
        ), start_span(
            "rag.query",
            attributes={"rag.query.mode": command.mode.value},
            tracer_provider=self._tracer_provider,
        ) as span:
            result = await self._query_api.execute(command)
            span.set_attribute("rag.query.status", result.status.value)
            span.set_attribute("rag.query.citation_count", len(result.citations))
            LOGGER.info(
                "rag.query.completed",
                extra={"event_code": "QUERY_COMPLETED", "outcome": result.status.value},
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
        return self._observed_stream(command, disconnected=disconnected)

    async def _observed_stream(
        self,
        command: QueryCommand,
        *,
        disconnected: DisconnectCheck,
    ) -> AsyncIterator[QueryStreamEvent]:
        with bind_context(
            tenant_id=command.tenant_id,
            actor_id=command.actor_id,
            query_id=command.query_id,
        ), start_span(
            "rag.query.stream",
            attributes={"rag.query.mode": command.mode.value},
            tracer_provider=self._tracer_provider,
        ) as span:
            async for event in self._query_api.stream(command, disconnected=disconnected):
                if event.event in {"completed", "error"}:
                    span.set_attribute("rag.query.status", event.event)
                    LOGGER.info(
                        "rag.query.stream.terminal",
                        extra={"event_code": "QUERY_STREAM_TERMINAL", "outcome": event.event},
                    )
                yield event


class McpApplicationService:
    """Protocol-free MCP facade; transport adapters only decode and encode messages."""

    def __init__(self, knowledge: KnowledgeApplication) -> None:
        self._knowledge = knowledge

    async def query_knowledge_base(
        self, principal: Principal, request: KnowledgeQuery
    ) -> QueryExecution:
        return await self._knowledge.execute(principal, request)
