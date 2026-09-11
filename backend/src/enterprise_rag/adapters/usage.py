"""Concurrency-safe in-memory usage store for isolated tests and local composition."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from enterprise_rag.domain.common import new_uuid7, require_utc
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.usage import (
    UsageAmounts,
    UsageLimits,
    UsageReservation,
    UsageSnapshot,
)


class InMemoryUsageStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._minutes: dict[tuple[UUID, UUID, datetime], int] = {}
        self._days: dict[tuple[UUID, UUID, datetime], UsageAmounts] = {}
        self._reservations: dict[UUID, tuple[UsageReservation, bool]] = {}

    async def reserve(
        self,
        *,
        query_id: UUID,
        tenant_id: UUID,
        actor_id: UUID,
        rate_limit_id: UUID,
        now: datetime,
        requested: UsageAmounts,
        limits: UsageLimits,
    ) -> UsageReservation:
        minute_start, day_start = _windows(now)
        minute_key = (tenant_id, rate_limit_id, minute_start)
        day_key = (tenant_id, actor_id, day_start)
        async with self._lock:
            queries = self._minutes.get(minute_key, 0)
            current = self._days.get(day_key, UsageAmounts())
            _check_limits(queries, current, requested, limits, now)
            reservation = UsageReservation(
                new_uuid7(),
                query_id,
                tenant_id,
                actor_id,
                rate_limit_id,
                minute_start,
                day_start,
                requested,
            )
            self._minutes[minute_key] = queries + 1
            self._days[day_key] = _add(current, requested)
            self._reservations[reservation.id] = (reservation, False)
            return reservation

    async def settle(self, reservation: UsageReservation, actual: UsageAmounts) -> UsageSnapshot:
        if not actual.fits_within(reservation.reserved):
            raise ValueError("actual usage exceeds the reservation")
        day_key = (reservation.tenant_id, reservation.actor_id, reservation.day_start)
        minute_key = (
            reservation.tenant_id,
            reservation.rate_limit_id,
            reservation.minute_start,
        )
        async with self._lock:
            stored = self._reservations.get(reservation.id)
            if stored is None or stored[0] != reservation:
                raise ValueError("usage reservation is unknown")
            if not stored[1]:
                current = self._days[day_key]
                self._days[day_key] = _subtract(current, _subtract(reservation.reserved, actual))
                self._reservations[reservation.id] = (reservation, True)
            return _snapshot(self._minutes.get(minute_key, 0), self._days[day_key])

    async def snapshot(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        rate_limit_id: UUID,
        now: datetime,
    ) -> UsageSnapshot:
        minute_start, day_start = _windows(now)
        async with self._lock:
            queries = self._minutes.get((tenant_id, rate_limit_id, minute_start), 0)
            usage = self._days.get((tenant_id, actor_id, day_start), UsageAmounts())
            return _snapshot(queries, usage)


def _windows(now: datetime) -> tuple[datetime, datetime]:
    require_utc(now, "now")
    utc = now.astimezone(UTC)
    return utc.replace(second=0, microsecond=0), utc.replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _check_limits(
    queries: int,
    current: UsageAmounts,
    requested: UsageAmounts,
    limits: UsageLimits,
    now: datetime,
) -> None:
    if queries + 1 > limits.queries_per_minute:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "The query rate limit has been reached.",
            {
                "budget": "queries_per_minute",
                "limit": limits.queries_per_minute,
                "retry_after_seconds": max(1, 60 - now.second),
            },
        )
    values = (
        ("daily_llm_calls", current.llm_calls + requested.llm_calls, limits.daily_llm_calls),
        (
            "daily_input_tokens",
            current.input_tokens + requested.input_tokens,
            limits.daily_input_tokens,
        ),
        (
            "daily_output_tokens",
            current.output_tokens + requested.output_tokens,
            limits.daily_output_tokens,
        ),
    )
    for budget, value, limit in values:
        if value > limit:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                "The daily language-model budget has been reached.",
                {"budget": budget, "limit": limit, "retry_after_seconds": _until_utc_day(now)},
            )


def _until_utc_day(now: datetime) -> int:
    next_day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    next_day += timedelta(days=1)
    return max(1, int((next_day - now).total_seconds()))


def _add(left: UsageAmounts, right: UsageAmounts) -> UsageAmounts:
    return UsageAmounts(
        left.llm_calls + right.llm_calls,
        left.input_tokens + right.input_tokens,
        left.output_tokens + right.output_tokens,
    )


def _subtract(left: UsageAmounts, right: UsageAmounts) -> UsageAmounts:
    return UsageAmounts(
        left.llm_calls - right.llm_calls,
        left.input_tokens - right.input_tokens,
        left.output_tokens - right.output_tokens,
    )


def _snapshot(queries: int, usage: UsageAmounts) -> UsageSnapshot:
    return UsageSnapshot(queries, usage.llm_calls, usage.input_tokens, usage.output_tokens)
