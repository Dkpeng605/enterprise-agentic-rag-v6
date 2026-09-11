"""Atomic query and LLM usage-budget persistence contract."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from enterprise_rag.domain.common import require_utc, require_uuid7


@dataclass(frozen=True, slots=True)
class UsageAmounts:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if min(self.llm_calls, self.input_tokens, self.output_tokens) < 0:
            raise ValueError("usage amounts must not be negative")

    def fits_within(self, other: "UsageAmounts") -> bool:
        return (
            self.llm_calls <= other.llm_calls
            and self.input_tokens <= other.input_tokens
            and self.output_tokens <= other.output_tokens
        )


@dataclass(frozen=True, slots=True)
class UsageLimits:
    queries_per_minute: int
    daily_llm_calls: int
    daily_input_tokens: int
    daily_output_tokens: int

    def __post_init__(self) -> None:
        if (
            min(
                self.queries_per_minute,
                self.daily_llm_calls,
                self.daily_input_tokens,
                self.daily_output_tokens,
            )
            <= 0
        ):
            raise ValueError("usage limits must be positive")


@dataclass(frozen=True, slots=True)
class UsageReservation:
    id: UUID
    query_id: UUID
    tenant_id: UUID
    actor_id: UUID
    rate_limit_id: UUID
    minute_start: datetime
    day_start: datetime
    reserved: UsageAmounts

    def __post_init__(self) -> None:
        for name in ("id", "query_id", "tenant_id", "actor_id", "rate_limit_id"):
            require_uuid7(getattr(self, name), name)
        require_utc(self.minute_start, "minute_start")
        require_utc(self.day_start, "day_start")


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    queries: int
    llm_calls: int
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if min(self.queries, self.llm_calls, self.input_tokens, self.output_tokens) < 0:
            raise ValueError("usage snapshot values must not be negative")


class UsageStore(Protocol):
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
    ) -> UsageReservation: ...

    async def settle(
        self, reservation: UsageReservation, actual: UsageAmounts
    ) -> UsageSnapshot: ...

    async def snapshot(
        self, *, tenant_id: UUID, actor_id: UUID, rate_limit_id: UUID, now: datetime
    ) -> UsageSnapshot: ...
