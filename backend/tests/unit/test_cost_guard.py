import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from enterprise_rag.adapters.usage import InMemoryUsageStore
from enterprise_rag.domain import AppError, ErrorCode, QueryMode, QueryScope
from enterprise_rag.ports import (
    CompletionRequest,
    CompletionResult,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    UsageAmounts,
    UsageLimits,
    UsageSnapshot,
)
from enterprise_rag.services import (
    BoundedLanguageModel,
    BudgetedQueryRunner,
    CostGuard,
    QueryBudget,
    QueryCommand,
    QueryExecution,
    QueryRunStatus,
)
from enterprise_rag.services.query_api import ProgressSink

QUERY_ID = UUID("01900000-0000-7000-8000-000000001a01")
TENANT_ID = UUID("01900000-0000-7000-8000-000000001a02")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000001a03")
SESSION_A = UUID("01900000-0000-7000-8000-000000001a08")
SESSION_B = UUID("01900000-0000-7000-8000-000000001a09")
NOW = datetime(2026, 9, 12, 1, 2, 3, tzinfo=UTC)
DEFAULT_LIMITS = UsageLimits(1, 6, 1_000, 100)
DEFAULT_BUDGET = UsageAmounts(6, 1_000, 100)


def command(
    query_id: UUID = QUERY_ID,
    mode: QueryMode = QueryMode.STANDARD,
    session_id: UUID | None = None,
) -> QueryCommand:
    return QueryCommand(
        query_id, TENANT_ID, ACTOR_ID, "test", mode, QueryScope(), (), session_id
    )


class FakeRunner:
    def __init__(
        self,
        *,
        usage: Mapping[str, object] | None = None,
        delay: float = 0,
        error: Exception | None = None,
    ) -> None:
        self.calls = 0
        self.usage = usage or {"llm_calls": 2, "input_tokens": 100, "output_tokens": 20}
        self.delay = delay
        self.error = error

    async def run(self, query: QueryCommand, *, emit: ProgressSink | None = None) -> QueryExecution:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return QueryExecution(query.query_id, QueryRunStatus.ANSWERED, "answer", (), {}, self.usage)


def guarded(
    runner: FakeRunner,
    store: InMemoryUsageStore,
    *,
    limits: UsageLimits = DEFAULT_LIMITS,
    budget: UsageAmounts = DEFAULT_BUDGET,
    timeout: float = 1,
) -> BudgetedQueryRunner:
    guard = CostGuard(
        store,
        limits=limits,
        budgets=QueryBudget(budget, budget),
        clock=lambda: NOW,
    )
    return BudgetedQueryRunner(runner, guard, timeout_seconds=timeout)


@pytest.mark.anyio
async def test_rate_and_daily_budget_reject_before_runner_provider_work() -> None:
    store = InMemoryUsageStore()
    runner = FakeRunner()
    service = guarded(runner, store)

    await service.run(command())
    with pytest.raises(AppError) as raised:
        await service.run(command(UUID("01900000-0000-7000-8000-000000001a04")))

    assert raised.value.code is ErrorCode.RATE_LIMITED
    assert raised.value.details["budget"] == "queries_per_minute"
    assert runner.calls == 1
    assert await store.snapshot(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, rate_limit_id=ACTOR_ID, now=NOW
    ) == UsageSnapshot(1, 2, 100, 20)


@pytest.mark.anyio
async def test_concurrent_reservations_cannot_overspend_daily_budget() -> None:
    store = InMemoryUsageStore()
    runner = FakeRunner()
    service = guarded(
        runner,
        store,
        limits=UsageLimits(10, 6, 1_000, 100),
    )
    commands = (
        command(UUID("01900000-0000-7000-8000-000000001a05")),
        command(UUID("01900000-0000-7000-8000-000000001a06")),
    )

    results = await asyncio.gather(
        *(service.run(item) for item in commands), return_exceptions=True
    )

    assert sum(isinstance(item, QueryExecution) for item in results) == 1
    assert sum(isinstance(item, AppError) for item in results) == 1
    assert runner.calls == 1


@pytest.mark.anyio
async def test_minute_limit_is_per_session_while_daily_budget_is_shared() -> None:
    store = InMemoryUsageStore()
    runner = FakeRunner()
    service = guarded(runner, store, limits=UsageLimits(1, 12, 2_000, 200))

    await service.run(command(QUERY_ID, session_id=SESSION_A))
    await service.run(
        command(UUID("01900000-0000-7000-8000-000000001a0a"), session_id=SESSION_B)
    )
    with pytest.raises(AppError):
        await service.run(
            command(UUID("01900000-0000-7000-8000-000000001a0b"), session_id=SESSION_A)
        )

    first = await store.snapshot(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, rate_limit_id=SESSION_A, now=NOW
    )
    second = await store.snapshot(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, rate_limit_id=SESSION_B, now=NOW
    )
    assert first == UsageSnapshot(1, 4, 200, 40)
    assert second == UsageSnapshot(1, 4, 200, 40)
    assert runner.calls == 2


@pytest.mark.anyio
async def test_invalid_or_excess_usage_keeps_full_reservation_charged() -> None:
    for usage in (
        {"llm_calls": 2, "input_tokens": 1},
        {"llm_calls": 7, "input_tokens": 1, "output_tokens": 1},
    ):
        store = InMemoryUsageStore()
        service = guarded(FakeRunner(usage=usage), store)

        with pytest.raises(AppError) as raised:
            await service.run(command())

        assert raised.value.code is ErrorCode.LLM_INVALID_RESPONSE
        snapshot = await store.snapshot(
            tenant_id=TENANT_ID, actor_id=ACTOR_ID, rate_limit_id=ACTOR_ID, now=NOW
        )
        assert (snapshot.llm_calls, snapshot.input_tokens, snapshot.output_tokens) == (
            6,
            1_000,
            100,
        )


@pytest.mark.anyio
async def test_query_timeout_is_bounded_and_forfeits_reservation() -> None:
    store = InMemoryUsageStore()
    service = guarded(FakeRunner(delay=1), store, timeout=0.001)

    with pytest.raises(AppError) as raised:
        await service.run(command())

    assert raised.value.code is ErrorCode.SERVICE_UNAVAILABLE
    snapshot = await store.snapshot(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, rate_limit_id=ACTOR_ID, now=NOW
    )
    assert snapshot.llm_calls == 6


@pytest.mark.anyio
async def test_query_cancellation_forfeits_reservation_and_propagates() -> None:
    store = InMemoryUsageStore()
    service = guarded(FakeRunner(delay=1), store)
    task = asyncio.create_task(service.run(command()))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = await store.snapshot(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, rate_limit_id=ACTOR_ID, now=NOW
    )
    assert snapshot.llm_calls == 6


@pytest.mark.anyio
async def test_minute_and_day_windows_reset_independently() -> None:
    store = InMemoryUsageStore()
    limits = UsageLimits(1, 10, 2_000, 200)
    requested = UsageAmounts(1, 10, 2)
    first = await store.reserve(
        query_id=QUERY_ID,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        rate_limit_id=ACTOR_ID,
        now=NOW,
        requested=requested,
        limits=limits,
    )
    await store.settle(first, requested)
    later = NOW + timedelta(minutes=1)
    second = await store.reserve(
        query_id=UUID("01900000-0000-7000-8000-000000001a07"),
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        rate_limit_id=ACTOR_ID,
        now=later,
        requested=requested,
        limits=limits,
    )
    await store.settle(second, requested)

    snapshot = await store.snapshot(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, rate_limit_id=ACTOR_ID, now=later
    )
    assert snapshot.queries == 1
    assert snapshot.llm_calls == 2


class FakeLanguageModel:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.LLM, "fake", "1", frozenset(), False, ProviderHealth.HEALTHY
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "timeout":
            await asyncio.sleep(1)
        assert isinstance(outcome, CompletionResult)
        return outcome

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_language_model_retries_only_transient_failures_and_reports_retries() -> None:
    delegate = FakeLanguageModel(
        [
            AppError(ErrorCode.LLM_UNAVAILABLE, "secret"),
            CompletionResult("ok", 10, 2),
        ]
    )
    model = BoundedLanguageModel(
        delegate, timeout_seconds=1, max_retries=2, retry_backoff_seconds=0
    )

    result = await model.complete(CompletionRequest("system", "user", 10))

    assert result.retry_count == 1
    assert delegate.calls == 2
    assert model.info().name == "fake"
    await model.aclose()
    assert delegate.closed is True


@pytest.mark.anyio
async def test_language_model_timeout_and_error_are_bounded_and_sanitized() -> None:
    delegate = FakeLanguageModel(["timeout", "timeout"])
    model = BoundedLanguageModel(
        delegate, timeout_seconds=0.001, max_retries=1, retry_backoff_seconds=0
    )

    with pytest.raises(AppError) as raised:
        await model.complete(CompletionRequest("system", "user", 10))

    assert raised.value.code is ErrorCode.LLM_UNAVAILABLE
    assert "timeout" not in raised.value.message.lower()
    assert delegate.calls == 2


@pytest.mark.anyio
async def test_language_model_does_not_retry_deterministic_error() -> None:
    delegate = FakeLanguageModel(
        [AppError(ErrorCode.LLM_INVALID_RESPONSE, "invalid provider payload")]
    )
    model = BoundedLanguageModel(
        delegate, timeout_seconds=1, max_retries=2, retry_backoff_seconds=0
    )

    with pytest.raises(AppError) as raised:
        await model.complete(CompletionRequest("system", "user", 10))

    assert raised.value.code is ErrorCode.LLM_INVALID_RESPONSE
    assert delegate.calls == 1
