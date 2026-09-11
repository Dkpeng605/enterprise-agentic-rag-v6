"""Tenant-scoped collection and document application services."""

import base64
import binascii
import json
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.adapters.database.lifecycle import DocumentLifecycleRepository
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    LeafModel,
    RootModel,
)
from enterprise_rag.domain.common import new_uuid7, require_utc
from enterprise_rag.domain.documents import DocumentVisibility
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.jobs import JobSnapshot
from enterprise_rag.ports.object_store import ObjectStore
from enterprise_rag.services.documents import DocumentRegistrationService, RegisterDocument


@dataclass(frozen=True, slots=True)
class CollectionSnapshot:
    id: UUID
    name: str
    description: str | None
    visibility: str
    is_seed: bool
    document_count: int
    ready_document_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CollectionDeleteSnapshot:
    id: UUID
    status: str
    job_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    id: UUID
    collection_id: UUID
    title: str
    organization: str | None
    status: str
    visibility: str
    version_id: UUID
    source_name: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentPage:
    items: tuple[DocumentSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class DocumentDetail:
    summary: DocumentSummary
    root_count: int
    leaf_count: int
    recent_job: JobSnapshot | None
    version_error_code: str | None
    version_error_message: str | None


@dataclass(frozen=True, slots=True)
class UploadSnapshot:
    document_id: UUID
    version_id: UUID
    job_id: UUID
    deduplicated: bool
    status: str


class WorkspaceService:
    def __init__(
        self,
        database: Database,
        object_store: ObjectStore,
        *,
        max_upload_bytes: int,
        max_documents: int,
        max_attempts: int,
    ) -> None:
        self._database = database
        self._object_store = object_store
        self._max_upload_bytes = max_upload_bytes
        self._max_documents = max_documents
        self._max_attempts = max_attempts

    async def list_collections(self, tenant_id: UUID) -> tuple[CollectionSnapshot, ...]:
        async with self._database.session() as session:
            rows = await session.execute(
                select(
                    CollectionModel,
                    func.count(DocumentModel.id).filter(DocumentModel.status != "deleted"),
                    func.count(DocumentModel.id).filter(DocumentModel.status == "ready"),
                )
                .outerjoin(DocumentModel, DocumentModel.collection_id == CollectionModel.id)
                .where(
                    CollectionModel.tenant_id == tenant_id,
                    CollectionModel.status == "active",
                )
                .group_by(CollectionModel.id)
                .order_by(CollectionModel.created_at, CollectionModel.id)
            )
            return tuple(self._collection(row[0], int(row[1]), int(row[2])) for row in rows)

    async def get_collection(self, tenant_id: UUID, collection_id: UUID) -> CollectionSnapshot:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(
                        CollectionModel,
                        func.count(DocumentModel.id).filter(DocumentModel.status != "deleted"),
                        func.count(DocumentModel.id).filter(DocumentModel.status == "ready"),
                    )
                    .outerjoin(DocumentModel, DocumentModel.collection_id == CollectionModel.id)
                    .where(
                        CollectionModel.id == collection_id,
                        CollectionModel.tenant_id == tenant_id,
                        CollectionModel.status == "active",
                    )
                    .group_by(CollectionModel.id)
                )
            ).one_or_none()
            if row is None:
                raise self._not_found("collection", collection_id)
            return self._collection(row[0], int(row[1]), int(row[2]))

    async def create_collection(
        self,
        tenant_id: UUID,
        *,
        name: str,
        description: str | None,
        visibility: DocumentVisibility,
    ) -> CollectionSnapshot:
        async with self._database.session() as session:
            existing = await session.scalar(
                select(CollectionModel.id).where(
                    CollectionModel.tenant_id == tenant_id,
                    func.lower(CollectionModel.name) == name.casefold(),
                )
            )
            if existing is not None:
                raise AppError(ErrorCode.CONFLICT, "A collection with this name already exists.")
            model = CollectionModel(
                id=new_uuid7(),
                tenant_id=tenant_id,
                name=name,
                description=description,
                visibility=visibility.value,
            )
            try:
                async with session.begin_nested():
                    session.add(model)
                    await session.flush()
            except IntegrityError as error:
                raise AppError(
                    ErrorCode.CONFLICT, "A collection with this name already exists."
                ) from error
            return self._collection(model, 0, 0)

    async def update_collection(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        *,
        name: str | None,
        description: str | None,
        visibility: DocumentVisibility | None,
        description_set: bool,
    ) -> CollectionSnapshot:
        async with self._database.session() as session:
            model = await session.scalar(
                select(CollectionModel)
                .where(
                    CollectionModel.id == collection_id,
                    CollectionModel.tenant_id == tenant_id,
                    CollectionModel.status == "active",
                )
                .with_for_update()
            )
            if model is None:
                raise self._not_found("collection", collection_id)
            if name is not None:
                duplicate = await session.scalar(
                    select(CollectionModel.id).where(
                        CollectionModel.tenant_id == tenant_id,
                        CollectionModel.id != collection_id,
                        func.lower(CollectionModel.name) == name.casefold(),
                    )
                )
                if duplicate is not None:
                    raise AppError(
                        ErrorCode.CONFLICT, "A collection with this name already exists."
                    )
                model.name = name
            if description_set:
                model.description = description
            if visibility is not None:
                model.visibility = visibility.value
            await session.flush()
        return await self.get_collection(tenant_id, collection_id)

    async def delete_collection(
        self, tenant_id: UUID, collection_id: UUID, *, confirm_name: str, now: datetime
    ) -> CollectionDeleteSnapshot:
        require_utc(now, "now")
        async with self._database.session() as session:
            model = await session.scalar(
                select(CollectionModel)
                .where(
                    CollectionModel.id == collection_id,
                    CollectionModel.tenant_id == tenant_id,
                    CollectionModel.status == "active",
                )
                .with_for_update()
            )
            if model is None:
                raise self._not_found("collection", collection_id)
            if model.name != confirm_name:
                raise AppError(ErrorCode.CONFLICT, "Collection confirmation does not match.")
            if model.is_seed:
                raise AppError(ErrorCode.CONFLICT, "The seed collection cannot be deleted.")
            document_ids = tuple(
                await session.scalars(
                    select(DocumentModel.id).where(
                        DocumentModel.collection_id == collection_id,
                        DocumentModel.tenant_id == tenant_id,
                        DocumentModel.status != "deleted",
                    )
                )
            )
            if not document_ids:
                await session.delete(model)
                return CollectionDeleteSnapshot(collection_id, "deleted", ())
            model.status = "deleting"
            lifecycle = DocumentLifecycleRepository(session)
            requests = [
                await lifecycle.request_delete(
                    tenant_id=tenant_id, document_id=document_id, now=now
                )
                for document_id in document_ids
            ]
            return CollectionDeleteSnapshot(
                collection_id, "deleting", tuple(item.job_id for item in requests)
            )

    async def upload_document(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        collection_id: UUID,
        title: str,
        organization: str | None,
        visibility: DocumentVisibility,
        source_name: str,
        media_type: str,
        chunks: AsyncIterable[bytes],
        now: datetime,
    ) -> UploadSnapshot:
        async with self._database.session() as session:
            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(DocumentModel)
                    .where(
                        DocumentModel.tenant_id == tenant_id,
                        DocumentModel.status.not_in(("deleting", "deleted")),
                    )
                )
                or 0
            )
        if count >= self._max_documents:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                "The demo workspace document limit has been reached.",
                {"limit": self._max_documents},
            )
        logical_name = f"{organization or ''}::{title}"
        registration = await DocumentRegistrationService(
            self._database,
            self._object_store,
            max_attempts=self._max_attempts,
        ).register(
            RegisterDocument(
                tenant_id=tenant_id,
                collection_id=collection_id,
                created_by=actor_id,
                logical_name=logical_name,
                title=title,
                source_name=source_name,
                media_type=media_type,
                organization=organization,
                visibility=visibility,
                max_bytes=self._max_upload_bytes,
            ),
            chunks,
            now=now,
        )
        if registration.job_id is None:
            raise RuntimeError("document registration did not create an ingestion job")
        async with self._database.session() as session:
            status = await session.scalar(
                select(DocumentModel.status).where(DocumentModel.id == registration.document_id)
            )
        if status is None:
            raise RuntimeError("registered document disappeared")
        return UploadSnapshot(
            registration.document_id,
            registration.version_id,
            registration.job_id,
            registration.deduplicated,
            status,
        )

    async def list_documents(
        self,
        tenant_id: UUID,
        *,
        collection_id: UUID | None,
        status: str | None,
        media_type: str | None,
        keyword: str | None,
        cursor: str | None,
        limit: int,
    ) -> DocumentPage:
        cursor_value = self._decode_cursor(cursor) if cursor else None
        latest_version_id = (
            select(DocumentVersionModel.id)
            .where(DocumentVersionModel.document_id == DocumentModel.id)
            .order_by(DocumentVersionModel.created_at.desc(), DocumentVersionModel.id.desc())
            .limit(1)
            .correlate(DocumentModel)
            .scalar_subquery()
        )
        statement = (
            select(DocumentModel, DocumentVersionModel)
            .join(DocumentVersionModel, DocumentVersionModel.id == latest_version_id)
            .join(CollectionModel, CollectionModel.id == DocumentModel.collection_id)
            .where(
                DocumentModel.tenant_id == tenant_id,
                DocumentModel.status != "deleted",
                CollectionModel.status == "active",
            )
        )
        if collection_id is not None:
            statement = statement.where(DocumentModel.collection_id == collection_id)
        if status is not None:
            statement = statement.where(DocumentModel.status == status)
        if media_type is not None:
            statement = statement.where(DocumentVersionModel.media_type == media_type)
        if keyword:
            pattern = f"%{keyword}%"
            statement = statement.where(
                or_(
                    DocumentModel.title.ilike(pattern),
                    DocumentModel.logical_name.ilike(pattern),
                    DocumentVersionModel.source_name.ilike(pattern),
                )
            )
        if cursor_value is not None:
            created_at, document_id = cursor_value
            statement = statement.where(
                or_(
                    DocumentModel.created_at < created_at,
                    and_(
                        DocumentModel.created_at == created_at,
                        DocumentModel.id < document_id,
                    ),
                )
            )
        statement = statement.order_by(
            DocumentModel.created_at.desc(), DocumentModel.id.desc()
        ).limit(limit + 1)
        async with self._database.session() as session:
            rows = list((await session.execute(statement)).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = tuple(self._document(document, version) for document, version in rows)
        next_cursor = None
        if has_more and rows:
            next_cursor = self._encode_cursor(rows[-1][0].created_at, rows[-1][0].id)
        return DocumentPage(items, next_cursor)

    async def get_document(self, tenant_id: UUID, document_id: UUID) -> DocumentDetail:
        async with self._database.session() as session:
            document = await session.scalar(
                select(DocumentModel).where(
                    DocumentModel.id == document_id,
                    DocumentModel.tenant_id == tenant_id,
                    DocumentModel.status != "deleted",
                )
            )
            if document is None:
                raise self._not_found("document", document_id)
            version = await session.scalar(
                select(DocumentVersionModel)
                .where(DocumentVersionModel.document_id == document.id)
                .order_by(DocumentVersionModel.created_at.desc(), DocumentVersionModel.id.desc())
                .limit(1)
            )
            if version is None:
                raise self._not_found("document", document_id)
            root_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(RootModel)
                    .where(RootModel.version_id == version.id)
                )
                or 0
            )
            leaf_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(LeafModel)
                    .where(LeafModel.version_id == version.id)
                )
                or 0
            )
            job = await IngestionJobRepository(session).get_for_version(version.id)
            return DocumentDetail(
                self._document(document, version),
                root_count,
                leaf_count,
                job,
                version.error_code,
                version.error_message,
            )

    async def get_job(self, tenant_id: UUID, job_id: UUID) -> JobSnapshot:
        async with self._database.session() as session:
            model = await session.scalar(
                select(IngestionJobModel).where(
                    IngestionJobModel.id == job_id,
                    IngestionJobModel.tenant_id == tenant_id,
                )
            )
            if model is None:
                raise self._not_found("ingestion job", job_id)
            snapshot = await IngestionJobRepository(session).get(model.id)
            if snapshot is None:
                raise self._not_found("ingestion job", job_id)
            return snapshot

    async def delete_document(
        self, tenant_id: UUID, document_id: UUID, *, now: datetime
    ) -> tuple[UUID, str, bool]:
        async with self._database.session() as session:
            result = await DocumentLifecycleRepository(session).request_delete(
                tenant_id=tenant_id, document_id=document_id, now=now
            )
            return result.job_id, result.status, result.already_requested

    @staticmethod
    def _collection(
        model: CollectionModel, document_count: int, ready_count: int
    ) -> CollectionSnapshot:
        return CollectionSnapshot(
            model.id,
            model.name,
            model.description,
            model.visibility,
            model.is_seed,
            document_count,
            ready_count,
            model.created_at,
            model.updated_at,
        )

    @staticmethod
    def _document(model: DocumentModel, version: DocumentVersionModel) -> DocumentSummary:
        return DocumentSummary(
            model.id,
            model.collection_id,
            model.title,
            model.organization,
            model.status,
            model.visibility,
            version.id,
            version.source_name,
            version.media_type,
            version.size_bytes,
            version.sha256,
            model.created_at,
            model.updated_at,
        )

    @staticmethod
    def _encode_cursor(created_at: datetime, document_id: UUID) -> str:
        value = json.dumps([created_at.isoformat(), str(document_id)], separators=(",", ":"))
        return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
        try:
            padding = "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(cursor + padding))
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError
            created_at = datetime.fromisoformat(value[0])
            document_id = UUID(value[1])
            require_utc(created_at, "cursor timestamp")
            return created_at, document_id
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as error:
            raise AppError(ErrorCode.VALIDATION_ERROR, "The cursor is invalid.") from error

    @staticmethod
    def _not_found(kind: str, identity: UUID) -> AppError:
        return AppError(
            ErrorCode.NOT_FOUND,
            f"The {kind} was not found.",
            {"id": str(identity)},
        )
