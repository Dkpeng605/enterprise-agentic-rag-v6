"""PostgreSQL boundaries for asynchronous deletion and reconciliation."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.adapters.database.models import (
    DocumentContentClaimModel,
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    LeafModel,
    RootModel,
)
from enterprise_rag.domain.common import require_utc
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.jobs import JobStatus


class DocumentLifecycleError(AppError):
    """A tenant-scoped lifecycle target is missing or invalid."""


@dataclass(frozen=True, slots=True)
class DeleteRequest:
    document_id: UUID
    job_id: UUID
    status: str
    already_requested: bool


@dataclass(frozen=True, slots=True)
class VersionResource:
    version_id: UUID
    object_key: str


@dataclass(frozen=True, slots=True)
class DeletionContext:
    tenant_id: UUID
    document_id: UUID
    job_id: UUID
    versions: tuple[VersionResource, ...]
    completed: bool


@dataclass(frozen=True, slots=True)
class ReconcileDatabaseSnapshot:
    vector_counts: dict[tuple[UUID, UUID], int]
    object_keys: frozenset[str]
    expired_leases: int


class DocumentLifecycleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def request_delete(
        self, *, tenant_id: UUID, document_id: UUID, now: datetime
    ) -> DeleteRequest:
        require_utc(now, "now")
        document = await self.session.scalar(
            select(DocumentModel)
            .where(DocumentModel.id == document_id, DocumentModel.tenant_id == tenant_id)
            .with_for_update()
        )
        if document is None:
            raise DocumentLifecycleError(
                ErrorCode.NOT_FOUND,
                "The document was not found.",
                {"document_id": str(document_id)},
            )

        existing = await self.session.scalar(
            select(IngestionJobModel)
            .where(
                IngestionJobModel.document_id == document_id,
                IngestionJobModel.tenant_id == tenant_id,
                IngestionJobModel.type == "delete",
            )
            .order_by(IngestionJobModel.created_at.desc())
            .limit(1)
        )
        if document.status in {"deleting", "deleted"} and existing is not None:
            reusable_statuses = {"queued", "leased", "running", "retry_wait", "succeeded"}
            if existing.status in reusable_statuses:
                return DeleteRequest(document.id, existing.id, document.status, True)

        version_id = await self.session.scalar(
            select(DocumentVersionModel.id)
            .where(DocumentVersionModel.document_id == document_id)
            .order_by(DocumentVersionModel.created_at.desc())
            .limit(1)
        )
        if version_id is None:
            raise DocumentLifecycleError(
                ErrorCode.CONFLICT,
                "A document without a version cannot be deleted asynchronously.",
                {"document_id": str(document_id)},
            )

        document.status = "deleting"
        document.active_version_id = None
        await self.session.execute(
            update(IngestionJobModel)
            .where(
                IngestionJobModel.document_id == document_id,
                IngestionJobModel.type != "delete",
                IngestionJobModel.status.in_(("queued", "retry_wait")),
            )
            .values(status="cancelled", cancel_requested=True)
        )
        await self.session.execute(
            update(IngestionJobModel)
            .where(
                IngestionJobModel.document_id == document_id,
                IngestionJobModel.type != "delete",
                IngestionJobModel.status.in_(("leased", "running")),
            )
            .values(cancel_requested=True)
        )
        job = await IngestionJobRepository(self.session).enqueue(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            available_at=now,
            max_attempts=10,
            job_type="delete",
        )
        return DeleteRequest(document.id, job.id, document.status, False)

    async def load_deletion(
        self, *, job_id: UUID, owner: str, now: datetime
    ) -> DeletionContext:
        job_repository = IngestionJobRepository(self.session)
        snapshot = await job_repository.get(job_id)
        if snapshot is None or snapshot.type != "delete":
            raise DocumentLifecycleError(
                ErrorCode.JOB_NOT_FOUND,
                "The deletion job was not found.",
                {"job_id": str(job_id)},
            )
        document = await self.session.get(DocumentModel, snapshot.document_id)
        if document is None or document.tenant_id != snapshot.tenant_id:
            raise DocumentLifecycleError(
                ErrorCode.NOT_FOUND,
                "The deletion target was not found.",
                {"job_id": str(job_id)},
            )
        completed = snapshot.status is JobStatus.SUCCEEDED and document.status == "deleted"
        if not completed:
            await job_repository.ensure_owned_running(job_id, owner=owner, now=now)
        rows = await self.session.execute(
            select(DocumentVersionModel.id, DocumentVersionModel.object_key)
            .where(DocumentVersionModel.document_id == document.id)
            .order_by(DocumentVersionModel.created_at, DocumentVersionModel.id)
        )
        versions = tuple(VersionResource(version_id, object_key) for version_id, object_key in rows)
        return DeletionContext(
            tenant_id=document.tenant_id,
            document_id=document.id,
            job_id=job_id,
            versions=versions,
            completed=completed,
        )

    async def cleanup_content(self, context: DeletionContext) -> None:
        await self.session.execute(
            delete(RootModel).where(RootModel.document_id == context.document_id)
        )
        await self.session.execute(
            delete(DocumentContentClaimModel).where(
                DocumentContentClaimModel.document_id == context.document_id
            )
        )
        await self.session.execute(
            update(DocumentVersionModel)
            .where(DocumentVersionModel.document_id == context.document_id)
            .values(status="deleted", error_code=None, error_message=None)
        )

    async def object_is_referenced(self, object_key: str) -> bool:
        count = await self.session.scalar(
            select(func.count())
            .select_from(DocumentVersionModel)
            .where(
                DocumentVersionModel.object_key == object_key,
                DocumentVersionModel.status != "deleted",
            )
        )
        return bool(count)

    async def finalize(self, context: DeletionContext, *, owner: str, now: datetime) -> None:
        document = await self.session.scalar(
            select(DocumentModel)
            .where(
                DocumentModel.id == context.document_id,
                DocumentModel.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if document is None:
            raise DocumentLifecycleError(
                ErrorCode.NOT_FOUND,
                "The deletion target was not found.",
                {"document_id": str(context.document_id)},
            )
        document.status = "deleted"
        document.active_version_id = None
        await IngestionJobRepository(self.session).succeed(context.job_id, owner=owner, now=now)

    async def reconcile_snapshot(self, *, now: datetime) -> ReconcileDatabaseSnapshot:
        require_utc(now, "now")
        rows = await self.session.execute(
            select(
                DocumentModel.tenant_id,
                DocumentVersionModel.id,
                func.count(LeafModel.id),
            )
            .join(DocumentVersionModel, DocumentVersionModel.document_id == DocumentModel.id)
            .outerjoin(LeafModel, LeafModel.version_id == DocumentVersionModel.id)
            .where(DocumentVersionModel.status != "deleted")
            .group_by(DocumentModel.tenant_id, DocumentVersionModel.id)
        )
        vector_counts = {
            (tenant_id, version_id): int(count)
            for tenant_id, version_id, count in rows
        }
        object_keys = frozenset(
            await self.session.scalars(
                select(DocumentVersionModel.object_key).where(
                    DocumentVersionModel.status != "deleted"
                )
            )
        )
        expired_leases = int(
            await self.session.scalar(
                select(func.count())
                .select_from(IngestionJobModel)
                .where(
                    IngestionJobModel.status.in_(("leased", "running")),
                    IngestionJobModel.lease_until <= now,
                )
            )
            or 0
        )
        return ReconcileDatabaseSnapshot(vector_counts, object_keys, expired_leases)
