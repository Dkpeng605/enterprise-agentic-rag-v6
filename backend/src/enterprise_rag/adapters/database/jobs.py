"""Concurrency-safe PostgreSQL ingestion job state machine."""

from datetime import datetime, timedelta
from typing import NoReturn
from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.adapters.database.models import IngestionJobModel
from enterprise_rag.domain.common import new_uuid7, require_non_empty, require_utc
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.jobs import JobSnapshot, JobStatus


class JobStateError(AppError):
    """A missing job or rejected conditional state transition."""


def _snapshot(model: IngestionJobModel) -> JobSnapshot:
    return JobSnapshot(
        id=model.id,
        tenant_id=model.tenant_id,
        document_id=model.document_id,
        version_id=model.version_id,
        type=model.type,
        status=JobStatus(model.status),
        attempts=model.attempts,
        max_attempts=model.max_attempts,
        available_at=model.available_at,
        lease_owner=model.lease_owner,
        lease_until=model.lease_until,
        heartbeat_at=model.heartbeat_at,
        progress=model.progress,
        stage=model.stage,
        error_code=model.error_code,
        error_message=model.error_message,
        cancel_requested=model.cancel_requested,
    )


class IngestionJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        version_id: UUID,
        available_at: datetime,
        max_attempts: int,
        job_type: str = "ingest",
        job_id: UUID | None = None,
    ) -> JobSnapshot:
        require_utc(available_at, "available_at")
        require_non_empty(job_type, "job_type")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        model = IngestionJobModel(
            id=job_id or new_uuid7(),
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            type=job_type,
            status=JobStatus.QUEUED.value,
            attempts=0,
            max_attempts=max_attempts,
            available_at=available_at,
            progress=0,
            cancel_requested=False,
        )
        self.session.add(model)
        await self.session.flush()
        return _snapshot(model)

    async def get(self, job_id: UUID) -> JobSnapshot | None:
        model = await self.session.get(IngestionJobModel, job_id)
        return None if model is None else _snapshot(model)

    async def get_for_version(
        self, version_id: UUID, *, job_type: str = "ingest"
    ) -> JobSnapshot | None:
        require_non_empty(job_type, "job_type")
        model = await self.session.scalar(
            select(IngestionJobModel)
            .where(
                IngestionJobModel.version_id == version_id,
                IngestionJobModel.type == job_type,
            )
            .order_by(IngestionJobModel.created_at.desc(), IngestionJobModel.id.desc())
            .limit(1)
        )
        return None if model is None else _snapshot(model)

    async def ensure_owned_running(self, job_id: UUID, *, owner: str, now: datetime) -> JobSnapshot:
        """Lock and validate a running job before its worker performs side effects."""

        model = await self._lock_owned_active(job_id, owner=owner, now=now)
        if model.status != JobStatus.RUNNING.value or model.cancel_requested:
            await self._raise_invalid(job_id, "perform running job")
        return _snapshot(model)

    async def lease_next(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        job_type: str | None = None,
    ) -> JobSnapshot | None:
        require_non_empty(owner, "owner")
        require_utc(now, "now")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        if job_type is not None:
            require_non_empty(job_type, "job_type")
        statement = (
            select(IngestionJobModel)
            .where(
                IngestionJobModel.status.in_((JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value)),
                IngestionJobModel.available_at <= now,
                *((IngestionJobModel.type == job_type,) if job_type is not None else ()),
            )
            .order_by(IngestionJobModel.available_at, IngestionJobModel.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        model = (await self.session.execute(statement)).scalar_one_or_none()
        if model is None:
            return None
        model.status = JobStatus.LEASED.value
        model.lease_owner = owner
        model.lease_until = now + lease_for
        model.heartbeat_at = now
        model.attempts += 1
        await self.session.flush()
        return _snapshot(model)

    async def checkpoint(
        self,
        job_id: UUID,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        progress: int,
        stage: str,
    ) -> JobSnapshot:
        """Atomically acknowledge cancellation or renew a running job."""

        if not 0 <= progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        require_non_empty(stage, "stage")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        model = await self._lock_owned_active(job_id, owner=owner, now=now)
        if model.status != JobStatus.RUNNING.value:
            await self._raise_invalid(job_id, "checkpoint")
        if model.cancel_requested:
            model.status = JobStatus.CANCELLED.value
            model.lease_owner = None
            model.lease_until = None
            model.stage = "cancelled"
            await self.session.flush()
            return _snapshot(model)
        if progress < model.progress:
            await self._raise_invalid(job_id, "decrease progress")
        model.heartbeat_at = now
        model.lease_until = now + lease_for
        model.progress = progress
        model.stage = stage
        await self.session.flush()
        return _snapshot(model)

    async def start(self, job_id: UUID, *, owner: str, now: datetime) -> JobSnapshot:
        model = await self._lock_owned_active(job_id, owner=owner, now=now)
        if model.status != JobStatus.LEASED.value:
            await self._raise_invalid(job_id, "start")
        model.status = JobStatus.RUNNING.value
        await self.session.flush()
        return _snapshot(model)

    async def heartbeat(
        self,
        job_id: UUID,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        progress: int,
        stage: str,
    ) -> JobSnapshot:
        if not 0 <= progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        require_non_empty(stage, "stage")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        model = await self._lock_owned_active(job_id, owner=owner, now=now)
        if progress < model.progress:
            await self._raise_invalid(job_id, "decrease progress")
        model.heartbeat_at = now
        model.lease_until = now + lease_for
        model.progress = progress
        model.stage = stage
        await self.session.flush()
        return _snapshot(model)

    async def succeed(self, job_id: UUID, *, owner: str, now: datetime) -> JobSnapshot:
        model = await self._lock_owned_active(job_id, owner=owner, now=now)
        if model.status != JobStatus.RUNNING.value or model.cancel_requested:
            await self._raise_invalid(job_id, "succeed")
        model.status = JobStatus.SUCCEEDED.value
        model.progress = 100
        model.lease_owner = None
        model.lease_until = None
        await self.session.flush()
        return _snapshot(model)

    async def retry(
        self,
        job_id: UUID,
        *,
        owner: str,
        now: datetime,
        delay: timedelta,
        error_code: str,
        error_message: str,
    ) -> JobSnapshot:
        if delay < timedelta(0):
            raise ValueError("retry delay must not be negative")
        require_non_empty(error_code, "error_code")
        require_non_empty(error_message, "error_message")
        model = await self._lock_owned_active(job_id, owner=owner, now=now)
        if model.status != JobStatus.RUNNING.value or model.cancel_requested:
            await self._raise_invalid(job_id, "retry")
        model.error_code = error_code
        model.error_message = error_message
        model.lease_owner = None
        model.lease_until = None
        if model.attempts >= model.max_attempts:
            model.status = JobStatus.FAILED.value
        else:
            model.status = JobStatus.RETRY_WAIT.value
            model.available_at = now + delay
        await self.session.flush()
        return _snapshot(model)

    async def fail(
        self,
        job_id: UUID,
        *,
        owner: str,
        now: datetime,
        error_code: str,
        error_message: str,
    ) -> JobSnapshot:
        require_non_empty(error_code, "error_code")
        require_non_empty(error_message, "error_message")
        model = await self._lock_owned_active(job_id, owner=owner, now=now)
        if model.status != JobStatus.RUNNING.value:
            await self._raise_invalid(job_id, "fail")
        model.status = JobStatus.FAILED.value
        model.error_code = error_code
        model.error_message = error_message
        model.lease_owner = None
        model.lease_until = None
        await self.session.flush()
        return _snapshot(model)

    async def request_cancel(self, job_id: UUID) -> JobSnapshot:
        model = await self._lock(job_id)
        status = JobStatus(model.status)
        if status is JobStatus.CANCELLED:
            return _snapshot(model)
        if status in (JobStatus.QUEUED, JobStatus.RETRY_WAIT):
            model.status = JobStatus.CANCELLED.value
        elif status in (JobStatus.LEASED, JobStatus.RUNNING):
            model.cancel_requested = True
        else:
            await self._raise_invalid(job_id, "cancel")
        await self.session.flush()
        return _snapshot(model)

    async def acknowledge_cancel(
        self,
        job_id: UUID,
        *,
        owner: str,
        now: datetime,
    ) -> JobSnapshot:
        model = await self._lock_owned_active(job_id, owner=owner, now=now)
        if not model.cancel_requested:
            await self._raise_invalid(job_id, "acknowledge cancel")
        model.status = JobStatus.CANCELLED.value
        model.lease_owner = None
        model.lease_until = None
        await self.session.flush()
        return _snapshot(model)

    async def recover_expired(self, *, now: datetime, limit: int = 100) -> tuple[int, int]:
        """Move expired active leases to retry_wait or terminal failed."""

        require_utc(now, "now")
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            select(IngestionJobModel)
            .where(
                IngestionJobModel.status.in_((JobStatus.LEASED.value, JobStatus.RUNNING.value)),
                IngestionJobModel.lease_until <= now,
            )
            .order_by(IngestionJobModel.lease_until)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        models = tuple((await self.session.scalars(statement)).all())
        retry_count = 0
        failed_count = 0
        for model in models:
            model.lease_owner = None
            model.lease_until = None
            model.error_code = "JOB_LEASE_EXPIRED"
            model.error_message = "The worker lease expired."
            if model.cancel_requested:
                model.status = JobStatus.CANCELLED.value
            elif model.attempts >= model.max_attempts:
                model.status = JobStatus.FAILED.value
                failed_count += 1
            else:
                model.status = JobStatus.RETRY_WAIT.value
                model.available_at = now
                retry_count += 1
        await self.session.flush()
        return retry_count, failed_count

    async def _lock(self, job_id: UUID) -> IngestionJobModel:
        statement: Select[tuple[IngestionJobModel]] = (
            select(IngestionJobModel).where(IngestionJobModel.id == job_id).with_for_update()
        )
        model = (await self.session.execute(statement)).scalar_one_or_none()
        if model is None:
            raise JobStateError(
                ErrorCode.JOB_NOT_FOUND,
                "The ingestion job does not exist.",
                {"job_id": str(job_id)},
            )
        return model

    async def _lock_owned_active(
        self,
        job_id: UUID,
        *,
        owner: str,
        now: datetime,
    ) -> IngestionJobModel:
        require_non_empty(owner, "owner")
        require_utc(now, "now")
        statement = (
            select(IngestionJobModel)
            .where(
                IngestionJobModel.id == job_id,
                IngestionJobModel.lease_owner == owner,
                IngestionJobModel.lease_until > now,
                or_(
                    IngestionJobModel.status == JobStatus.LEASED.value,
                    IngestionJobModel.status == JobStatus.RUNNING.value,
                ),
            )
            .with_for_update()
        )
        model = (await self.session.execute(statement)).scalar_one_or_none()
        if model is None:
            await self._raise_invalid(job_id, "owned transition")
        return model

    async def _raise_invalid(self, job_id: UUID, action: str) -> NoReturn:
        exists = await self.session.scalar(
            select(IngestionJobModel.id).where(IngestionJobModel.id == job_id)
        )
        if exists is None:
            raise JobStateError(
                ErrorCode.JOB_NOT_FOUND,
                "The ingestion job does not exist.",
                {"job_id": str(job_id)},
            )
        raise JobStateError(
            ErrorCode.JOB_INVALID_TRANSITION,
            "The ingestion job transition was rejected.",
            {"action": action, "job_id": str(job_id)},
        )
