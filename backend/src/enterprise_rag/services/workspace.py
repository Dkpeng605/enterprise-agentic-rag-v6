"""Tenant-scoped collection and document application services."""

import base64
import binascii
import json
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
class CleaningAuditSnapshot:
    rule: str
    occurrences: int
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True, slots=True)
class PipelineRootSummary:
    id: str
    ordinal: int
    kind: str
    source_locator: Mapping[str, object]
    raw_chars: int
    clean_chars: int
    changed: bool
    leaf_count: int
    cleaning_audit: tuple[CleaningAuditSnapshot, ...]


@dataclass(frozen=True, slots=True)
class PipelineLeafSnapshot:
    id: str
    ordinal: int
    text: str
    retrieval_text: str
    start_offset: int | None
    end_offset: int | None
    token_count: int
    overlap_chars: int
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PipelineRootDetail:
    summary: PipelineRootSummary
    raw_text: str
    clean_text: str
    metadata: Mapping[str, object]
    leaves: tuple[PipelineLeafSnapshot, ...]


@dataclass(frozen=True, slots=True)
class DocumentPipelineSnapshot:
    document_id: UUID
    version_id: UUID
    source_name: str
    parser_provider: str | None
    parser_version: str | None
    cleaner_provider: str | None
    cleaner_version: str | None
    splitter_provider: str | None
    splitter_version: str | None
    splitter_settings: Mapping[str, object]
    llm_cleaning: Mapping[str, object]
    root_count: int
    leaf_count: int
    roots: tuple[PipelineRootSummary, ...]
    next_cursor: int | None


@dataclass(frozen=True, slots=True)
class JobListItem:
    snapshot: JobSnapshot
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JobPage:
    items: tuple[JobListItem, ...]
    next_cursor: str | None


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

    async def inspect_document_pipeline(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        cursor: int | None,
        limit: int,
    ) -> DocumentPipelineSnapshot:
        if cursor is not None and cursor < 0:
            raise AppError(ErrorCode.VALIDATION_ERROR, "The Root cursor is invalid.")
        if not 1 <= limit <= 100:
            raise AppError(ErrorCode.VALIDATION_ERROR, "The Root page size is invalid.")
        async with self._database.session() as session:
            document, version = await self._document_version(
                session, tenant_id, document_id
            )
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
            statement = (
                select(RootModel, func.count(LeafModel.id))
                .outerjoin(LeafModel, LeafModel.root_id == RootModel.id)
                .where(RootModel.version_id == version.id)
                .group_by(RootModel.id)
                .order_by(RootModel.ordinal, RootModel.id)
                .limit(limit + 1)
            )
            if cursor is not None:
                statement = statement.where(RootModel.ordinal > cursor)
            rows = list((await session.execute(statement)).all())
        has_more = len(rows) > limit
        selected = rows[:limit]
        roots = tuple(
            self._pipeline_root(root, int(count)) for root, count in selected
        )
        metadata = selected[0][0].metadata_json if selected else {}
        cleaning = _mapping(metadata.get("cleaning"))
        splitter = _mapping(metadata.get("splitter"))
        llm_cleaning = _mapping(metadata.get("llm_cleaning"))
        return DocumentPipelineSnapshot(
            document.id,
            version.id,
            version.source_name,
            version.parser_provider,
            version.parser_version,
            _text(cleaning.get("provider")),
            _text(cleaning.get("version")),
            _text(splitter.get("provider")),
            _text(splitter.get("version")),
            _mapping(splitter.get("settings")),
            llm_cleaning,
            root_count,
            leaf_count,
            roots,
            roots[-1].ordinal if has_more and roots else None,
        )

    async def inspect_pipeline_root(
        self, tenant_id: UUID, document_id: UUID, root_id: str
    ) -> PipelineRootDetail:
        async with self._database.session() as session:
            _, version = await self._document_version(session, tenant_id, document_id)
            root = await session.scalar(
                select(RootModel).where(
                    RootModel.id == root_id,
                    RootModel.tenant_id == tenant_id,
                    RootModel.document_id == document_id,
                    RootModel.version_id == version.id,
                )
            )
            if root is None:
                raise self._not_found("Root", root_id)
            leaves = tuple(
                await session.scalars(
                    select(LeafModel)
                    .where(
                        LeafModel.root_id == root.id,
                        LeafModel.tenant_id == tenant_id,
                        LeafModel.document_id == document_id,
                        LeafModel.version_id == version.id,
                    )
                    .order_by(LeafModel.ordinal, LeafModel.id)
                )
            )
        previous_end: int | None = None
        projected: list[PipelineLeafSnapshot] = []
        for leaf in leaves:
            overlap = 0
            if previous_end is not None and leaf.start_offset is not None:
                overlap = max(0, previous_end - leaf.start_offset)
            projected.append(
                PipelineLeafSnapshot(
                    leaf.id,
                    leaf.ordinal,
                    leaf.text,
                    leaf.retrieval_text,
                    leaf.start_offset,
                    leaf.end_offset,
                    leaf.token_count,
                    overlap,
                    leaf.metadata_json,
                )
            )
            if leaf.end_offset is not None:
                previous_end = leaf.end_offset
        return PipelineRootDetail(
            self._pipeline_root(root, len(leaves)),
            root.raw_text,
            root.clean_text,
            root.metadata_json,
            tuple(projected),
        )

    async def _document_version(
        self, session: AsyncSession, tenant_id: UUID, document_id: UUID
    ) -> tuple[DocumentModel, DocumentVersionModel]:
        row = (
            await session.execute(
                select(DocumentModel, DocumentVersionModel)
                .join(
                    DocumentVersionModel,
                    DocumentVersionModel.document_id == DocumentModel.id,
                )
                .where(
                    DocumentModel.id == document_id,
                    DocumentModel.tenant_id == tenant_id,
                    DocumentModel.status != "deleted",
                )
                .order_by(
                    DocumentVersionModel.created_at.desc(),
                    DocumentVersionModel.id.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            raise self._not_found("document", document_id)
        return row[0], row[1]

    @staticmethod
    def _pipeline_root(root: RootModel, leaf_count: int) -> PipelineRootSummary:
        return PipelineRootSummary(
            root.id,
            root.ordinal,
            root.kind,
            root.source_locator,
            len(root.raw_text),
            len(root.clean_text),
            root.raw_text != root.clean_text,
            leaf_count,
            _cleaning_audit(root.metadata_json),
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

    async def list_jobs(
        self,
        tenant_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> JobPage:
        cursor_value = self._decode_cursor(cursor) if cursor else None
        statement = select(IngestionJobModel).where(IngestionJobModel.tenant_id == tenant_id)
        if status is not None:
            statement = statement.where(IngestionJobModel.status == status)
        if cursor_value is not None:
            created_at, job_id = cursor_value
            statement = statement.where(
                or_(
                    IngestionJobModel.created_at < created_at,
                    and_(
                        IngestionJobModel.created_at == created_at,
                        IngestionJobModel.id < job_id,
                    ),
                )
            )
        statement = statement.order_by(
            IngestionJobModel.created_at.desc(), IngestionJobModel.id.desc()
        ).limit(limit + 1)
        async with self._database.session() as session:
            models = list((await session.scalars(statement)).all())
            has_more = len(models) > limit
            models = models[:limit]
            repository = IngestionJobRepository(session)
            items: list[JobListItem] = []
            for model in models:
                snapshot = await repository.get(model.id)
                if snapshot is None:
                    raise RuntimeError("listed ingestion job disappeared")
                items.append(JobListItem(snapshot, model.created_at))
        next_cursor = None
        if has_more and models:
            next_cursor = self._encode_cursor(models[-1].created_at, models[-1].id)
        return JobPage(tuple(items), next_cursor)

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
    def _not_found(kind: str, identity: UUID | str) -> AppError:
        return AppError(
            ErrorCode.NOT_FOUND,
            f"The {kind} was not found.",
            {"id": str(identity)},
        )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cleaning_audit(metadata: Mapping[str, object]) -> tuple[CleaningAuditSnapshot, ...]:
    values = _mapping(metadata.get("cleaning")).get("audit")
    if not isinstance(values, list):
        return ()
    result: list[CleaningAuditSnapshot] = []
    for item in values:
        audit = _mapping(item)
        rule = _text(audit.get("rule"))
        occurrences = _integer(audit.get("occurrences"))
        before = _text(audit.get("before_sha256"))
        after = _text(audit.get("after_sha256"))
        if (
            rule is not None
            and occurrences is not None
            and before is not None
            and after is not None
        ):
            result.append(
                CleaningAuditSnapshot(
                    rule, occurrences, before, after
                )
            )
    return tuple(result)
