import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from enterprise_rag.adapters.database import Database, IngestionJobRepository, JobStateError
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    DocumentVersionModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.domain import ErrorCode, JobStatus

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
TENANT_ID = UUID("01900000-0000-7000-8000-000000000201")
USER_ID = UUID("01900000-0000-7000-8000-000000000202")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000000203")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000000204")
VERSION_ID = UUID("01900000-0000-7000-8000-000000000205")
JOB_ID = UUID("01900000-0000-7000-8000-000000000206")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


async def reset_and_seed(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add_all(
            [
                TenantModel(id=TENANT_ID, name="Demo", slug="job-demo"),
                UserModel(
                    id=USER_ID,
                    email="job@example.invalid",
                    password_hash="not-a-real-hash",
                ),
            ]
        )
        await session.flush()
        session.add(
            CollectionModel(id=COLLECTION_ID, tenant_id=TENANT_ID, name="Job Tests")
        )
        await session.flush()
        session.add(
            DocumentModel(
                id=DOCUMENT_ID,
                tenant_id=TENANT_ID,
                collection_id=COLLECTION_ID,
                logical_name="job-test",
                title="Job Test",
                status="pending",
                visibility="tenant",
                created_by=USER_ID,
            )
        )
        await session.flush()
        session.add(
            DocumentVersionModel(
                id=VERSION_ID,
                document_id=DOCUMENT_ID,
                sha256="a" * 64,
                source_name="job.txt",
                media_type="text/plain",
                size_bytes=3,
                object_key="objects/job",
                parser_provider="text",
                parser_version="1",
                status="pending",
            )
        )


async def enqueue(database: Database, *, job_id: UUID = JOB_ID, max_attempts: int = 3) -> None:
    async with database.session() as session:
        repository = IngestionJobRepository(session)
        await repository.enqueue(
            tenant_id=TENANT_ID,
            document_id=DOCUMENT_ID,
            version_id=VERSION_ID,
            available_at=NOW,
            max_attempts=max_attempts,
            job_id=job_id,
        )


@pytest.mark.anyio
async def test_two_workers_can_lease_a_job_only_once() -> None:
    database = Database(DATABASE_URL)
    try:
        await reset_and_seed(database)
        await enqueue(database)

        async def claim(owner: str) -> UUID | None:
            async with database.session() as session:
                snapshot = await IngestionJobRepository(session).lease_next(
                    owner=owner,
                    now=NOW,
                    lease_for=timedelta(seconds=30),
                )
                return None if snapshot is None else snapshot.id

        claimed = await asyncio.gather(claim("worker-a"), claim("worker-b"))

        assert claimed.count(JOB_ID) == 1
        assert claimed.count(None) == 1
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_start_heartbeat_and_success_require_owner_and_monotonic_progress() -> None:
    database = Database(DATABASE_URL)
    try:
        await reset_and_seed(database)
        await enqueue(database)
        async with database.session() as session:
            repository = IngestionJobRepository(session)
            leased = await repository.lease_next(
                owner="worker",
                now=NOW,
                lease_for=timedelta(seconds=30),
            )
            assert leased is not None
            with pytest.raises(JobStateError) as wrong_owner:
                await repository.start(JOB_ID, owner="other", now=NOW)
            assert wrong_owner.value.code is ErrorCode.JOB_INVALID_TRANSITION

            running = await repository.start(JOB_ID, owner="worker", now=NOW)
            assert running.status is JobStatus.RUNNING
            heartbeat = await repository.heartbeat(
                JOB_ID,
                owner="worker",
                now=NOW + timedelta(seconds=10),
                lease_for=timedelta(seconds=30),
                progress=50,
                stage="embedding",
            )
            assert heartbeat.progress == 50
            assert heartbeat.stage == "embedding"
            with pytest.raises(JobStateError):
                await repository.heartbeat(
                    JOB_ID,
                    owner="worker",
                    now=NOW + timedelta(seconds=11),
                    lease_for=timedelta(seconds=30),
                    progress=49,
                    stage="embedding",
                )
            succeeded = await repository.succeed(
                JOB_ID,
                owner="worker",
                now=NOW + timedelta(seconds=12),
            )
            assert succeeded.status is JobStatus.SUCCEEDED
            assert succeeded.progress == 100
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_retry_wait_releases_lease_and_max_attempts_fail_terminally() -> None:
    database = Database(DATABASE_URL)
    try:
        await reset_and_seed(database)
        await enqueue(database, max_attempts=2)
        async with database.session() as session:
            repository = IngestionJobRepository(session)
            assert await repository.lease_next(
                owner="worker",
                now=NOW,
                lease_for=timedelta(seconds=30),
            )
            await repository.start(JOB_ID, owner="worker", now=NOW)
            waiting = await repository.retry(
                JOB_ID,
                owner="worker",
                now=NOW,
                delay=timedelta(seconds=10),
                error_code="TEMPORARY",
                error_message="Temporary failure.",
            )
            assert waiting.status is JobStatus.RETRY_WAIT
            assert waiting.lease_owner is None
            assert await repository.lease_next(
                owner="early",
                now=NOW + timedelta(seconds=9),
                lease_for=timedelta(seconds=30),
            ) is None
            assert await repository.lease_next(
                owner="worker",
                now=NOW + timedelta(seconds=10),
                lease_for=timedelta(seconds=30),
            )
            await repository.start(
                JOB_ID,
                owner="worker",
                now=NOW + timedelta(seconds=10),
            )
            failed = await repository.retry(
                JOB_ID,
                owner="worker",
                now=NOW + timedelta(seconds=10),
                delay=timedelta(0),
                error_code="STILL_FAILING",
                error_message="Still failing.",
            )
            assert failed.status is JobStatus.FAILED
            assert failed.attempts == 2
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_cancel_is_immediate_before_lease_and_cooperative_while_running() -> None:
    second_job_id = UUID("01900000-0000-7000-8000-000000000207")
    database = Database(DATABASE_URL)
    try:
        await reset_and_seed(database)
        await enqueue(database)
        async with database.session() as session:
            repository = IngestionJobRepository(session)
            cancelled = await repository.request_cancel(JOB_ID)
            assert cancelled.status is JobStatus.CANCELLED
            assert (await repository.request_cancel(JOB_ID)).status is JobStatus.CANCELLED

        await enqueue(database, job_id=second_job_id)
        async with database.session() as session:
            repository = IngestionJobRepository(session)
            assert await repository.lease_next(
                owner="worker",
                now=NOW,
                lease_for=timedelta(seconds=30),
            )
            await repository.start(second_job_id, owner="worker", now=NOW)
            requested = await repository.request_cancel(second_job_id)
            assert requested.status is JobStatus.RUNNING
            assert requested.cancel_requested is True
            acknowledged = await repository.acknowledge_cancel(
                second_job_id,
                owner="worker",
                now=NOW,
            )
            assert acknowledged.status is JobStatus.CANCELLED
            assert acknowledged.lease_owner is None
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_expired_leases_are_recovered_to_retry_or_failed() -> None:
    terminal_job_id = UUID("01900000-0000-7000-8000-000000000208")
    database = Database(DATABASE_URL)
    try:
        await reset_and_seed(database)
        await enqueue(database, max_attempts=2)
        await enqueue(database, job_id=terminal_job_id, max_attempts=1)
        async with database.session() as session:
            repository = IngestionJobRepository(session)
            assert await repository.lease_next(
                owner="worker-a", now=NOW, lease_for=timedelta(seconds=1)
            )
            assert await repository.lease_next(
                owner="worker-b", now=NOW, lease_for=timedelta(seconds=1)
            )

        async with database.session() as session:
            repository = IngestionJobRepository(session)
            recovered, failed = await repository.recover_expired(
                now=NOW + timedelta(seconds=2)
            )
            assert (recovered, failed) == (1, 1)
            retry_snapshot = await repository.get(JOB_ID)
            failed_snapshot = await repository.get(terminal_job_id)
            assert retry_snapshot is not None
            assert failed_snapshot is not None
            assert retry_snapshot.status is JobStatus.RETRY_WAIT
            assert failed_snapshot.status is JobStatus.FAILED
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_lease_is_expired_at_its_exact_deadline() -> None:
    database = Database(DATABASE_URL)
    try:
        await reset_and_seed(database)
        await enqueue(database)
        async with database.session() as session:
            repository = IngestionJobRepository(session)
            assert await repository.lease_next(
                owner="worker", now=NOW, lease_for=timedelta(seconds=1)
            )

        async with database.session() as session:
            repository = IngestionJobRepository(session)
            with pytest.raises(JobStateError):
                await repository.start(
                    JOB_ID,
                    owner="worker",
                    now=NOW + timedelta(seconds=1),
                )
            recovered, failed = await repository.recover_expired(
                now=NOW + timedelta(seconds=1)
            )
            assert (recovered, failed) == (1, 0)
    finally:
        await database.dispose()
