"""Persistent, lease-aware ingestion Worker loop."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import monotonic
from typing import Protocol

from enterprise_rag.domain.common import utc_now
from enterprise_rag.services.ingestion import PipelineRunResult

LOGGER = logging.getLogger(__name__)
Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
RecoverExpired = Callable[[datetime, int], Awaitable[int]]


class IngestionRunner(Protocol):
    """The part of the ingestion Pipeline required by a polling Worker."""

    async def run_once(self, *, owner: str) -> PipelineRunResult | None: ...


class IngestionWorker:
    """Run one serial Pipeline lease at a time with persistent-job recovery.

    The current Mac and offline compositions own one instance inside their API
    process. PostgreSQL lease semantics remain explicit so a future production
    runner can reuse this loop after the cross-process Milvus boundary is solved;
    this class alone does not make Milvus Lite safe for multiple processes.
    """

    def __init__(
        self,
        pipeline: IngestionRunner,
        recover_expired: RecoverExpired,
        *,
        owner: str,
        poll_interval_seconds: float = 0.5,
        recovery_interval_seconds: float = 5.0,
        recovery_limit: int = 10,
        clock: Clock = utc_now,
        monotonic_clock: MonotonicClock = monotonic,
    ) -> None:
        if not owner.strip():
            raise ValueError("worker owner must not be blank")
        if poll_interval_seconds <= 0 or recovery_interval_seconds <= 0:
            raise ValueError("worker intervals must be positive")
        if recovery_limit <= 0:
            raise ValueError("worker recovery limit must be positive")
        self._pipeline = pipeline
        self._recover_expired = recover_expired
        self._owner = owner
        self._poll_interval_seconds = poll_interval_seconds
        self._recovery_interval_seconds = recovery_interval_seconds
        self._recovery_limit = recovery_limit
        self._clock = clock
        self._monotonic = monotonic_clock
        self._next_recovery_at = float("-inf")

    @property
    def owner(self) -> str:
        return self._owner

    async def poll_once(self) -> PipelineRunResult | None:
        """Recover expired persistent jobs when due, then claim one new job."""

        now = self._clock()
        monotonic_now = self._monotonic()
        if monotonic_now >= self._next_recovery_at:
            recovered = await self._recover_expired(now, self._recovery_limit)
            self._next_recovery_at = monotonic_now + self._recovery_interval_seconds
            if recovered:
                LOGGER.info(
                    "rag.worker.expired_jobs_recovered",
                    extra={
                        "event_code": "WORKER_EXPIRED_JOBS_RECOVERED",
                        "outcome": "ok",
                        "recovered_count": recovered,
                    },
                )
        return await self._pipeline.run_once(owner=self._owner)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Poll until the owning composition sets ``stop_event``.

        A polling or Pipeline exception is isolated to the Worker loop.  The
        persistent lease and retry state remain in PostgreSQL, so a later poll can
        recover the job. Cancellation is always propagated
        for a prompt, orderly shutdown.
        """

        while not stop_event.is_set():
            try:
                result = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "rag.worker.poll_failed",
                    extra={
                        "event_code": "WORKER_POLL_FAILED",
                        "outcome": "error",
                    },
                )
                result = None
            if result is None:
                await self._wait_for_stop(stop_event)

    async def _wait_for_stop(self, stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=self._poll_interval_seconds
            )
        except TimeoutError:
            return


__all__ = ["IngestionWorker"]
