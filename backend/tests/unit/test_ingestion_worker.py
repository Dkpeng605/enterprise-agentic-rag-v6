import asyncio
from datetime import UTC, datetime

import pytest

from enterprise_rag.services import IngestionWorker

NOW = datetime(2026, 9, 15, tzinfo=UTC)


class FakePipeline:
    def __init__(self) -> None:
        self.owners: list[str] = []
        self.calls = 0

    async def run_once(self, *, owner: str) -> None:
        self.calls += 1
        self.owners.append(owner)
        return None


@pytest.mark.anyio
async def test_worker_recovers_expired_jobs_before_polling_pipeline() -> None:
    pipeline = FakePipeline()
    recovery_calls: list[tuple[datetime, int]] = []

    async def recover_expired(now: datetime, limit: int) -> int:
        recovery_calls.append((now, limit))
        return 2

    worker = IngestionWorker(
        pipeline,
        recover_expired,
        owner="production-worker-a",
        poll_interval_seconds=0.01,
        recovery_interval_seconds=5.0,
        recovery_limit=7,
        clock=lambda: NOW,
        monotonic_clock=lambda: 10.0,
    )

    result = await worker.poll_once()

    assert result is None
    assert recovery_calls == [(NOW, 7)]
    assert pipeline.owners == ["production-worker-a"]


@pytest.mark.anyio
async def test_worker_does_not_repeat_recovery_until_interval_elapses() -> None:
    pipeline = FakePipeline()
    recovery_count = 0

    async def recover_expired(now: datetime, limit: int) -> int:
        nonlocal recovery_count
        del now, limit
        recovery_count += 1
        return 0

    monotonic_now = 100.0
    worker = IngestionWorker(
        pipeline,
        recover_expired,
        owner="production-worker-b",
        poll_interval_seconds=0.01,
        recovery_interval_seconds=5.0,
        clock=lambda: NOW,
        monotonic_clock=lambda: monotonic_now,
    )

    await worker.poll_once()
    await worker.poll_once()
    assert recovery_count == 1

    monotonic_now = 105.0
    await worker.poll_once()
    assert recovery_count == 2


@pytest.mark.anyio
async def test_worker_stops_cooperatively_when_stop_event_is_set() -> None:
    pipeline = FakePipeline()
    stop_event = asyncio.Event()

    async def recover_expired(now: datetime, limit: int) -> int:
        del now, limit
        return 0

    worker = IngestionWorker(
        pipeline,
        recover_expired,
        owner="production-worker-c",
        poll_interval_seconds=0.01,
        recovery_interval_seconds=5.0,
        clock=lambda: NOW,
        monotonic_clock=lambda: 200.0,
    )

    async def stop_after_first_poll() -> None:
        while pipeline.calls == 0:
            await asyncio.sleep(0)
        stop_event.set()

    await asyncio.gather(worker.run_forever(stop_event), stop_after_first_poll())

    assert pipeline.calls == 1


@pytest.mark.anyio
async def test_worker_continues_polling_after_a_transient_pipeline_error() -> None:
    stop_event = asyncio.Event()

    class FlakyPipeline:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self, *, owner: str) -> None:
            del owner
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient failure")
            stop_event.set()
            return None

    async def recover_expired(now: datetime, limit: int) -> int:
        del now, limit
        return 0

    pipeline = FlakyPipeline()
    worker = IngestionWorker(
        pipeline,
        recover_expired,
        owner="production-worker-d",
        poll_interval_seconds=0.01,
        recovery_interval_seconds=5.0,
        clock=lambda: NOW,
        monotonic_clock=lambda: 300.0,
    )

    await worker.run_forever(stop_event)

    assert pipeline.calls == 2
