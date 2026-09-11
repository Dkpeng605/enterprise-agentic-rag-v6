"""Pre-provider query budgets plus bounded language-model timeout and retry."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryMode
from enterprise_rag.ports.llm import CompletionRequest, CompletionResult, LanguageModel
from enterprise_rag.ports.provider import ProviderInfo
from enterprise_rag.ports.usage import (
    UsageAmounts,
    UsageLimits,
    UsageReservation,
    UsageStore,
)
from enterprise_rag.services.query_api import (
    ProgressSink,
    QueryCommand,
    QueryExecution,
    QueryRunner,
)

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class QueryBudget:
    standard: UsageAmounts
    deep: UsageAmounts

    def for_mode(self, mode: QueryMode) -> UsageAmounts:
        return self.deep if mode is QueryMode.DEEP else self.standard


class CostGuard:
    def __init__(
        self,
        store: UsageStore,
        *,
        limits: UsageLimits,
        budgets: QueryBudget,
        clock: Clock,
    ) -> None:
        self._store = store
        self._limits = limits
        self._budgets = budgets
        self._clock = clock

    async def reserve(self, command: QueryCommand) -> UsageReservation:
        return await self._store.reserve(
            query_id=command.query_id,
            tenant_id=command.tenant_id,
            actor_id=command.actor_id,
            rate_limit_id=command.session_id or command.actor_id,
            now=self._clock(),
            requested=self._budgets.for_mode(command.mode),
            limits=self._limits,
        )

    async def settle(self, reservation: UsageReservation, result: QueryExecution) -> QueryExecution:
        if result.query_id != reservation.query_id:
            await self._store.settle(reservation, reservation.reserved)
            raise AppError(ErrorCode.LLM_INVALID_RESPONSE, "Query usage could not be verified.")
        try:
            actual = _reported_usage(result)
        except ValueError as error:
            await self._store.settle(reservation, reservation.reserved)
            raise AppError(
                ErrorCode.LLM_INVALID_RESPONSE, "Query usage could not be verified."
            ) from error
        if not actual.fits_within(reservation.reserved):
            await self._store.settle(reservation, reservation.reserved)
            raise AppError(ErrorCode.LLM_INVALID_RESPONSE, "Query usage exceeded its reservation.")
        await self._store.settle(reservation, actual)
        return result

    async def forfeit(self, reservation: UsageReservation) -> None:
        await self._store.settle(reservation, reservation.reserved)


class BudgetedQueryRunner:
    """Reserve the whole query ceiling before delegate code can call a Provider."""

    def __init__(self, delegate: QueryRunner, guard: CostGuard, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("query timeout must be positive")
        self._delegate = delegate
        self._guard = guard
        self._timeout = timeout_seconds

    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        reservation = await self._guard.reserve(command)
        try:
            result = await asyncio.wait_for(
                self._delegate.run(command, emit=emit), timeout=self._timeout
            )
        except asyncio.CancelledError:
            await self._guard.forfeit(reservation)
            raise
        except TimeoutError as error:
            await self._guard.forfeit(reservation)
            raise AppError(
                ErrorCode.SERVICE_UNAVAILABLE, "The query exceeded its execution timeout."
            ) from error
        except Exception:
            await self._guard.forfeit(reservation)
            raise
        return await self._guard.settle(reservation, result)


class BoundedLanguageModel:
    """Apply one timeout and a bounded retry policy around a pluggable LLM."""

    def __init__(
        self,
        delegate: LanguageModel,
        *,
        timeout_seconds: float,
        max_retries: int,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        if timeout_seconds <= 0 or not 0 <= max_retries <= 10:
            raise ValueError("language-model timeout and retries are invalid")
        if not 0 <= retry_backoff_seconds <= 10:
            raise ValueError("language-model retry backoff is invalid")
        self._delegate = delegate
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds

    def info(self) -> ProviderInfo:
        return self._delegate.info()

    async def aclose(self) -> None:
        await self._delegate.aclose()

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        for retry_count in range(self._max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self._delegate.complete(request), timeout=self._timeout
                )
                return CompletionResult(
                    result.text,
                    result.input_tokens,
                    result.output_tokens,
                    result.retry_count + retry_count,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if retry_count >= self._max_retries or not _retryable(error):
                    if isinstance(error, AppError) and error.code is not ErrorCode.LLM_UNAVAILABLE:
                        raise
                    raise AppError(
                        ErrorCode.LLM_UNAVAILABLE,
                        "The language model is temporarily unavailable.",
                    ) from error
                if self._backoff:
                    await asyncio.sleep(min(5.0, self._backoff * (2**retry_count)))
        raise AssertionError("language-model retry loop did not terminate")


def _reported_usage(result: QueryExecution) -> UsageAmounts:
    values: list[int] = []
    for key in ("llm_calls", "input_tokens", "output_tokens"):
        value = result.usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"query usage field {key} is invalid")
        values.append(value)
    return UsageAmounts(*values)


def _retryable(error: Exception) -> bool:
    return isinstance(error, TimeoutError) or (
        isinstance(error, AppError) and error.code is ErrorCode.LLM_UNAVAILABLE
    )
