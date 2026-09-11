"""PostgreSQL atomic usage windows and durable query reservations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.models import (
    QueryBudgetReservationModel,
    QueryUsageWindowModel,
)
from enterprise_rag.domain.common import new_uuid7, require_utc
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.usage import (
    UsageAmounts,
    UsageLimits,
    UsageReservation,
    UsageSnapshot,
)


class PostgreSQLUsageStore:
    def __init__(self, database: Database) -> None:
        self._database = database

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
        async with self._database.session() as session:
            minute = await session.execute(
                text(
                    """
                    INSERT INTO query_usage_windows
                        (tenant_id, subject_id, window_kind, window_start, query_count,
                         llm_calls, input_tokens, output_tokens, created_at, updated_at)
                    VALUES (:tenant_id, :subject_id, 'minute', :window_start, 1, 0, 0, 0,
                            :now, :now)
                    ON CONFLICT (tenant_id, subject_id, window_kind, window_start)
                    DO UPDATE SET
                        query_count = query_usage_windows.query_count + 1,
                        updated_at = EXCLUDED.updated_at
                    WHERE query_usage_windows.query_count + 1 <= :query_limit
                    RETURNING query_count
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "subject_id": rate_limit_id,
                    "window_start": minute_start,
                    "now": now,
                    "query_limit": limits.queries_per_minute,
                },
            )
            if minute.scalar_one_or_none() is None:
                raise _minute_limit(limits, now)
            day = await session.execute(
                text(
                    """
                    INSERT INTO query_usage_windows
                        (tenant_id, subject_id, window_kind, window_start, query_count,
                         llm_calls, input_tokens, output_tokens, created_at, updated_at)
                    VALUES (:tenant_id, :subject_id, 'day', :window_start, 0,
                            :llm_calls, :input_tokens, :output_tokens, :now, :now)
                    ON CONFLICT (tenant_id, subject_id, window_kind, window_start)
                    DO UPDATE SET
                        llm_calls = query_usage_windows.llm_calls + EXCLUDED.llm_calls,
                        input_tokens = query_usage_windows.input_tokens + EXCLUDED.input_tokens,
                        output_tokens = query_usage_windows.output_tokens + EXCLUDED.output_tokens,
                        updated_at = EXCLUDED.updated_at
                    WHERE
                        query_usage_windows.llm_calls + EXCLUDED.llm_calls <= :call_limit
                        AND query_usage_windows.input_tokens + EXCLUDED.input_tokens
                            <= :input_limit
                        AND query_usage_windows.output_tokens + EXCLUDED.output_tokens
                            <= :output_limit
                    RETURNING llm_calls, input_tokens, output_tokens
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "subject_id": actor_id,
                    "window_start": day_start,
                    "llm_calls": requested.llm_calls,
                    "input_tokens": requested.input_tokens,
                    "output_tokens": requested.output_tokens,
                    "now": now,
                    "call_limit": limits.daily_llm_calls,
                    "input_limit": limits.daily_input_tokens,
                    "output_limit": limits.daily_output_tokens,
                },
            )
            if day.one_or_none() is None:
                raise _daily_limit(limits, now)
            session.add(
                QueryBudgetReservationModel(
                    id=reservation.id,
                    query_id=query_id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    rate_limit_id=rate_limit_id,
                    minute_start=minute_start,
                    day_start=day_start,
                    reserved_llm_calls=requested.llm_calls,
                    reserved_input_tokens=requested.input_tokens,
                    reserved_output_tokens=requested.output_tokens,
                    actual_llm_calls=0,
                    actual_input_tokens=0,
                    actual_output_tokens=0,
                    settled=False,
                )
            )
        return reservation

    async def settle(self, reservation: UsageReservation, actual: UsageAmounts) -> UsageSnapshot:
        if not actual.fits_within(reservation.reserved):
            raise ValueError("actual usage exceeds the reservation")
        async with self._database.session() as session:
            stored = await session.scalar(
                select(QueryBudgetReservationModel)
                .where(QueryBudgetReservationModel.id == reservation.id)
                .with_for_update()
            )
            if stored is None or not _matches(stored, reservation):
                raise ValueError("usage reservation is unknown")
            if not stored.settled:
                refund = UsageAmounts(
                    reservation.reserved.llm_calls - actual.llm_calls,
                    reservation.reserved.input_tokens - actual.input_tokens,
                    reservation.reserved.output_tokens - actual.output_tokens,
                )
                await session.execute(
                    update(QueryUsageWindowModel)
                    .where(
                        QueryUsageWindowModel.tenant_id == reservation.tenant_id,
                        QueryUsageWindowModel.subject_id == reservation.actor_id,
                        QueryUsageWindowModel.window_kind == "day",
                        QueryUsageWindowModel.window_start == reservation.day_start,
                    )
                    .values(
                        llm_calls=QueryUsageWindowModel.llm_calls - refund.llm_calls,
                        input_tokens=QueryUsageWindowModel.input_tokens - refund.input_tokens,
                        output_tokens=QueryUsageWindowModel.output_tokens - refund.output_tokens,
                    )
                )
                stored.actual_llm_calls = actual.llm_calls
                stored.actual_input_tokens = actual.input_tokens
                stored.actual_output_tokens = actual.output_tokens
                stored.settled = True
            return await _snapshot(
                session,
                reservation.tenant_id,
                reservation.actor_id,
                reservation.rate_limit_id,
                reservation.minute_start,
                reservation.day_start,
            )

    async def snapshot(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        rate_limit_id: UUID,
        now: datetime,
    ) -> UsageSnapshot:
        minute_start, day_start = _windows(now)
        async with self._database.session() as session:
            return await _snapshot(
                session, tenant_id, actor_id, rate_limit_id, minute_start, day_start
            )


async def _snapshot(
    session: AsyncSession,
    tenant_id: UUID,
    actor_id: UUID,
    rate_limit_id: UUID,
    minute_start: datetime,
    day_start: datetime,
) -> UsageSnapshot:
    rows = (
        await session.execute(
            select(QueryUsageWindowModel).where(
                QueryUsageWindowModel.tenant_id == tenant_id,
                or_(
                    and_(
                        QueryUsageWindowModel.window_kind == "minute",
                        QueryUsageWindowModel.subject_id == rate_limit_id,
                        QueryUsageWindowModel.window_start == minute_start,
                    ),
                    and_(
                        QueryUsageWindowModel.window_kind == "day",
                        QueryUsageWindowModel.subject_id == actor_id,
                        QueryUsageWindowModel.window_start == day_start,
                    ),
                ),
            )
        )
    ).scalars()
    queries = 0
    usage = UsageAmounts()
    for row in rows:
        if row.window_kind == "minute":
            queries = row.query_count
        elif row.window_kind == "day":
            usage = UsageAmounts(row.llm_calls, row.input_tokens, row.output_tokens)
    return UsageSnapshot(queries, usage.llm_calls, usage.input_tokens, usage.output_tokens)


def _matches(model: QueryBudgetReservationModel, reservation: UsageReservation) -> bool:
    return (
        model.query_id == reservation.query_id
        and model.tenant_id == reservation.tenant_id
        and model.actor_id == reservation.actor_id
        and model.rate_limit_id == reservation.rate_limit_id
        and model.minute_start == reservation.minute_start
        and model.day_start == reservation.day_start
        and model.reserved_llm_calls == reservation.reserved.llm_calls
        and model.reserved_input_tokens == reservation.reserved.input_tokens
        and model.reserved_output_tokens == reservation.reserved.output_tokens
    )


def _windows(now: datetime) -> tuple[datetime, datetime]:
    require_utc(now, "now")
    return now.replace(second=0, microsecond=0), now.replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _minute_limit(limits: UsageLimits, now: datetime) -> AppError:
    return AppError(
        ErrorCode.RATE_LIMITED,
        "The query rate limit has been reached.",
        {
            "budget": "queries_per_minute",
            "limit": limits.queries_per_minute,
            "retry_after_seconds": max(1, 60 - now.second),
        },
    )


def _daily_limit(limits: UsageLimits, now: datetime) -> AppError:
    next_day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    next_day += timedelta(days=1)
    return AppError(
        ErrorCode.RATE_LIMITED,
        "The daily language-model budget has been reached.",
        {
            "budget": "daily_llm_budget",
            "limits": {
                "llm_calls": limits.daily_llm_calls,
                "input_tokens": limits.daily_input_tokens,
                "output_tokens": limits.daily_output_tokens,
            },
            "retry_after_seconds": max(1, int((next_day - now).total_seconds())),
        },
    )
