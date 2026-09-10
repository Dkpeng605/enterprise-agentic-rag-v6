"""PostgreSQL transaction boundaries for the ingestion Pipeline."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.adapters.database.models import (
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    LeafModel,
    RootModel,
)
from enterprise_rag.domain.common import to_json_value
from enterprise_rag.domain.documents import LeafChunk, RootChunk
from enterprise_rag.domain.errors import AppError, ErrorCode


class IngestionPersistenceError(AppError):
    """A missing or inconsistent ingestion persistence target."""


@dataclass(frozen=True, slots=True)
class IngestionWork:
    job_id: UUID
    tenant_id: UUID
    collection_id: UUID
    document_id: UUID
    version_id: UUID
    source_name: str
    media_type: str
    object_key: str


class IngestionContentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def prepare(self, *, job_id: UUID, owner: str, now: datetime) -> IngestionWork:
        await IngestionJobRepository(self.session).ensure_owned_running(
            job_id, owner=owner, now=now
        )
        row = (
            await self.session.execute(
                select(IngestionJobModel, DocumentModel, DocumentVersionModel)
                .join(DocumentModel, DocumentModel.id == IngestionJobModel.document_id)
                .join(
                    DocumentVersionModel,
                    DocumentVersionModel.id == IngestionJobModel.version_id,
                )
                .where(IngestionJobModel.id == job_id, IngestionJobModel.type == "ingest")
            )
        ).one_or_none()
        if row is None:
            raise IngestionPersistenceError(
                ErrorCode.JOB_NOT_FOUND,
                "The ingestion work item was not found.",
                {"job_id": str(job_id)},
            )
        job, document, version = row
        if version.document_id != document.id or document.tenant_id != job.tenant_id:
            raise IngestionPersistenceError(
                ErrorCode.CONFLICT,
                "The ingestion work item has inconsistent ownership.",
            )
        document.status = "processing"
        version.status = "processing"
        version.error_code = None
        version.error_message = None
        await self.session.flush()
        return IngestionWork(
            job.id,
            document.tenant_id,
            document.collection_id,
            document.id,
            version.id,
            version.source_name,
            version.media_type,
            version.object_key,
        )

    async def replace_content(
        self,
        *,
        version_id: UUID,
        roots: Sequence[RootChunk],
        leaves: Sequence[LeafChunk],
    ) -> None:
        if not roots or not leaves:
            raise ValueError("persisted ingestion content must not be empty")
        root_ids = {root.id for root in roots}
        if any(leaf.root_id not in root_ids for leaf in leaves):
            raise ValueError("every persisted Leaf must belong to a persisted Root")
        await self.session.execute(delete(RootModel).where(RootModel.version_id == version_id))
        await self.session.flush()
        self.session.add_all(
            [
                RootModel(
                    id=root.id,
                    tenant_id=root.tenant_id,
                    document_id=root.document_id,
                    version_id=root.version_id,
                    index_revision=root.index_revision,
                    ordinal=root.ordinal,
                    kind=root.kind.value,
                    source_locator=cast(dict[str, Any], to_json_value(root.source_locator)),
                    raw_text=root.raw_text,
                    clean_text=root.clean_text,
                    metadata_json=cast(dict[str, Any], to_json_value(root.metadata)),
                    content_hash=root.content_hash,
                )
                for root in roots
            ]
        )
        await self.session.flush()
        self.session.add_all(
            [
                LeafModel(
                    id=leaf.id,
                    root_id=leaf.root_id,
                    tenant_id=leaf.tenant_id,
                    document_id=leaf.document_id,
                    version_id=leaf.version_id,
                    ordinal=leaf.ordinal,
                    text=leaf.text,
                    retrieval_text=leaf.retrieval_text,
                    start_offset=leaf.start_offset,
                    end_offset=leaf.end_offset,
                    token_count=leaf.token_count,
                    metadata_json=cast(dict[str, Any], to_json_value(leaf.metadata)),
                    content_hash=leaf.content_hash,
                )
                for leaf in leaves
            ]
        )
        await self.session.flush()

    async def finalize(self, work: IngestionWork, *, expected_leaves: int) -> None:
        document = await self.session.scalar(
            select(DocumentModel).where(DocumentModel.id == work.document_id).with_for_update()
        )
        version = await self.session.scalar(
            select(DocumentVersionModel)
            .where(DocumentVersionModel.id == work.version_id)
            .with_for_update()
        )
        actual = int(
            await self.session.scalar(
                select(func.count())
                .select_from(LeafModel)
                .where(LeafModel.version_id == work.version_id)
            )
            or 0
        )
        if document is None or version is None or actual != expected_leaves:
            raise IngestionPersistenceError(
                ErrorCode.PROJECTION_COUNT_MISMATCH,
                "PostgreSQL content count verification failed.",
                {"expected_count": expected_leaves, "actual_count": actual},
            )
        await self.session.execute(
            update(DocumentVersionModel)
            .where(
                DocumentVersionModel.document_id == document.id,
                DocumentVersionModel.id != version.id,
                DocumentVersionModel.status == "indexed",
            )
            .values(status="superseded")
        )
        version.status = "indexed"
        version.error_code = None
        version.error_message = None
        document.status = "ready"
        document.active_version_id = version.id
        await self.session.flush()

    async def reset_content(self, version_id: UUID) -> None:
        await self.session.execute(delete(RootModel).where(RootModel.version_id == version_id))

    async def mark_failed(
        self, work: IngestionWork, *, error_code: str, error_message: str
    ) -> None:
        document = await self.session.get(DocumentModel, work.document_id)
        version = await self.session.get(DocumentVersionModel, work.version_id)
        if document is None or version is None:
            return
        version.status = "failed"
        version.error_code = error_code
        version.error_message = error_message
        document.status = "ready" if document.active_version_id is not None else "failed"
        await self.session.flush()
