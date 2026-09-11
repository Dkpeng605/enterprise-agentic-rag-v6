import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from enterprise_rag.domain import AppError, ErrorCode, QueryMode, QueryScope
from enterprise_rag.services import (
    QueryApiService,
    QueryCommand,
    QueryExecution,
    QueryProgress,
    QueryProgressStage,
    QueryRunStatus,
    QueryStreamEvent,
)
from enterprise_rag.services.query_api import ProgressSink

QUERY_ID = UUID("01900000-0000-7000-8000-000000001901")
TENANT_ID = UUID("01900000-0000-7000-8000-000000001902")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000001903")


def command() -> QueryCommand:
    return QueryCommand(
        QUERY_ID,
        TENANT_ID,
        ACTOR_ID,
        "测试问题",
        QueryMode.STANDARD,
        QueryScope(),
        (),
    )


def execution() -> QueryExecution:
    return QueryExecution(
        QUERY_ID,
        QueryRunStatus.ANSWERED,
        "测试答案",
        (),
        {"planner_degraded": False},
        {"llm_calls": 2},
    )


class FakeRunner:
    def __init__(self, *, delay: float = 0, error: Exception | None = None) -> None:
        self.delay = delay
        self.error = error
        self.cancelled = False

    async def run(self, query: QueryCommand, *, emit: ProgressSink | None = None) -> QueryExecution:
        assert query == command()
        try:
            if emit is not None:
                await emit(QueryProgress(QueryProgressStage.PLANNING))
                await emit(QueryProgress(QueryProgressStage.RETRIEVING, "two sub-queries"))
                await emit(QueryProgress(QueryProgressStage.ANSWERING))
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return execution()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def never_disconnected() -> bool:
    return False


async def collect(stream: AsyncIterator[QueryStreamEvent]) -> list[QueryStreamEvent]:
    return [item async for item in stream]


@pytest.mark.anyio
async def test_sync_execute_and_ordered_sse_progress() -> None:
    service = QueryApiService(FakeRunner(), heartbeat_seconds=1)
    assert await service.execute(command()) == execution()

    events = await collect(service.stream(command(), disconnected=never_disconnected))

    assert [item.event for item in events] == [
        "accepted",
        "progress",
        "progress",
        "progress",
        "completed",
    ]
    assert [item.sequence for item in events] == [1, 2, 3, 4, 5]
    assert events[-1].data["status"] == "answered"


@pytest.mark.anyio
async def test_stream_emits_heartbeat_while_runner_is_quiet() -> None:
    service = QueryApiService(FakeRunner(delay=0.03), heartbeat_seconds=0.005)

    events = await collect(service.stream(command(), disconnected=never_disconnected))

    assert "heartbeat" in [item.event for item in events]
    assert events[-1].event == "completed"


@pytest.mark.anyio
async def test_stream_sanitizes_runner_error() -> None:
    runner = FakeRunner(error=AppError(ErrorCode.LLM_UNAVAILABLE, "secret upstream"))
    service = QueryApiService(runner)

    events = await collect(service.stream(command(), disconnected=never_disconnected))

    assert events[-1].event == "error"
    assert events[-1].data["code"] == "LLM_UNAVAILABLE"
    assert "secret" not in repr(events[-1])


@pytest.mark.anyio
async def test_disconnect_cancels_in_flight_query_without_terminal_event() -> None:
    runner = FakeRunner(delay=1)
    calls = 0

    async def disconnected() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    events = await collect(
        QueryApiService(runner, heartbeat_seconds=0.001).stream(
            command(), disconnected=disconnected
        )
    )

    assert events[0].event == "accepted"
    assert all(item.event not in {"completed", "error"} for item in events)
    assert runner.cancelled is True
