"""Framework-neutral query execution and SSE progress orchestration."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from enterprise_rag.domain.common import require_non_empty, require_uuid7, to_json_value
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import Citation, QueryMode, QueryScope
from enterprise_rag.ports.planner import ConversationTurn

ProgressSink = Callable[["QueryProgress"], Awaitable[None]]
DisconnectCheck = Callable[[], Awaitable[bool]]


class QueryRunStatus(StrEnum):
    ANSWERED = "answered"
    ABSTAINED = "abstained"
    NO_RESULTS = "no_results"


class QueryProgressStage(StrEnum):
    PLANNING = "planning"
    RETRIEVING = "retrieving"
    RERANKING = "reranking"
    RECOVERING = "recovering"
    ANSWERING = "answering"


@dataclass(frozen=True, slots=True)
class QueryCommand:
    query_id: UUID
    tenant_id: UUID
    actor_id: UUID
    query: str
    mode: QueryMode
    scope: QueryScope
    history: tuple[ConversationTurn, ...]
    session_id: UUID | None = None

    def __post_init__(self) -> None:
        for name in ("query_id", "tenant_id", "actor_id"):
            require_uuid7(getattr(self, name), name)
        if self.session_id is not None:
            require_uuid7(self.session_id, "session_id")
        require_non_empty(self.query, "query")
        if len(self.query) > 2_000:
            raise ValueError("query must not exceed 2000 characters")
        if len(self.history) > 12:
            raise ValueError("query history must not exceed 12 turns")
        if sum(len(turn.content) for turn in self.history) > 12_000:
            raise ValueError("query history must not exceed 12000 characters")


@dataclass(frozen=True, slots=True)
class QueryProgress:
    stage: QueryProgressStage
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.detail is not None:
            require_non_empty(self.detail, "detail")


@dataclass(frozen=True, slots=True)
class QueryExecution:
    query_id: UUID
    status: QueryRunStatus
    answer: str
    citations: tuple[Citation, ...]
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    usage: Mapping[str, object] = field(default_factory=dict)
    trace_id: str | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.query_id, "query_id")
        require_non_empty(self.answer, "answer")
        if self.trace_id is not None and (
            len(self.trace_id) != 32
            or any(character not in "0123456789abcdef" for character in self.trace_id)
        ):
            raise ValueError("trace_id has an invalid format")
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))

    def to_dict(self) -> dict[str, object]:
        return {
            "query_id": str(self.query_id),
            "status": self.status.value,
            "answer": self.answer,
            "citations": [citation.to_dict() for citation in self.citations],
            "diagnostics": to_json_value(self.diagnostics),
            "usage": to_json_value(self.usage),
            "trace_id": self.trace_id,
        }


class QueryRunner(Protocol):
    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution: ...


@dataclass(frozen=True, slots=True)
class QueryStreamEvent:
    sequence: int
    event: str
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("SSE sequence must be positive")
        require_non_empty(self.event, "event")
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


class QueryApiService:
    def __init__(self, runner: QueryRunner, *, heartbeat_seconds: float = 15.0) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self._runner = runner
        self._heartbeat_seconds = heartbeat_seconds

    async def execute(self, command: QueryCommand) -> QueryExecution:
        return await self._runner.run(command)

    async def stream(
        self, command: QueryCommand, *, disconnected: DisconnectCheck
    ) -> AsyncIterator[QueryStreamEvent]:
        sequence = 1
        yield QueryStreamEvent(sequence, "accepted", {"query_id": str(command.query_id)})
        queue: asyncio.Queue[QueryProgress] = asyncio.Queue()

        async def emit(progress: QueryProgress) -> None:
            await queue.put(progress)

        run_task = asyncio.create_task(self._runner.run(command, emit=emit))
        progress_task = asyncio.create_task(queue.get())
        last_stage = -1
        try:
            while True:
                if await disconnected():
                    run_task.cancel()
                    await _absorb_cancel(run_task)
                    return
                done, _ = await asyncio.wait(
                    {run_task, progress_task},
                    timeout=self._heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    sequence += 1
                    yield QueryStreamEvent(sequence, "heartbeat", {})
                    continue
                emitted_progress = False
                if progress_task in done:
                    progress = progress_task.result()
                    rank = list(QueryProgressStage).index(progress.stage)
                    if rank <= last_stage:
                        run_task.cancel()
                        await _absorb_cancel(run_task)
                        sequence += 1
                        yield _error_event(sequence, command.query_id, ErrorCode.INTERNAL_ERROR)
                        return
                    last_stage = rank
                    sequence += 1
                    data: dict[str, object] = {"stage": progress.stage.value}
                    if progress.detail is not None:
                        data["detail"] = progress.detail
                    yield QueryStreamEvent(sequence, "progress", data)
                    progress_task = asyncio.create_task(queue.get())
                    emitted_progress = True
                if run_task in done:
                    if emitted_progress:
                        continue
                    progress_task.cancel()
                    await _absorb_cancel(progress_task)
                    try:
                        result = run_task.result()
                    except Exception as error:
                        code = (
                            error.code if isinstance(error, AppError) else ErrorCode.INTERNAL_ERROR
                        )
                        sequence += 1
                        yield _error_event(sequence, command.query_id, code)
                        return
                    sequence += 1
                    yield QueryStreamEvent(sequence, "completed", result.to_dict())
                    return
        finally:
            if not run_task.done():
                run_task.cancel()
                await _absorb_cancel(run_task)
            if not progress_task.done():
                progress_task.cancel()
                await _absorb_cancel(progress_task)


async def _absorb_cancel(task: asyncio.Task[object]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        return


def _error_event(sequence: int, query_id: UUID, code: ErrorCode) -> QueryStreamEvent:
    return QueryStreamEvent(
        sequence,
        "error",
        {"query_id": str(query_id), "code": code.value, "message": "Query execution failed."},
    )
