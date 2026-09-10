"""PostgreSQL document registration and content-deduplication repository."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentContentClaimModel,
    DocumentModel,
    DocumentVersionModel,
)
from enterprise_rag.domain.common import new_uuid7
from enterprise_rag.domain.documents import DocumentVisibility
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.object_store import StoredObject


class DocumentRegistrationError(AppError):
    """A registration target is missing or not in the requested tenant boundary."""


@dataclass(frozen=True, slots=True)
class DocumentRegistration:
    document_id: UUID
    version_id: UUID
    sha256: str
    object_key: str
    deduplicated: bool
    new_document: bool


class DocumentRegistrationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(
        self,
        *,
        tenant_id: UUID,
        collection_id: UUID,
        created_by: UUID,
        logical_name: str,
        title: str,
        source_name: str,
        media_type: str,
        visibility: DocumentVisibility,
        parser_provider: str,
        parser_version: str,
        stored_object: StoredObject,
    ) -> DocumentRegistration:
        await self._require_owned_collection(tenant_id, collection_id)
        await self._lock_registration_keys(
            tenant_id=tenant_id,
            collection_id=collection_id,
            logical_name=logical_name,
            sha256=stored_object.sha256,
        )

        existing = await self._find_claim(tenant_id, collection_id, stored_object.sha256)
        if existing is not None:
            return DocumentRegistration(
                document_id=existing.document_id,
                version_id=existing.version_id,
                sha256=stored_object.sha256,
                object_key=stored_object.key,
                deduplicated=True,
                new_document=False,
            )

        document = await self._find_document(tenant_id, collection_id, logical_name)
        new_document = document is None
        if document is None:
            document = DocumentModel(
                id=new_uuid7(),
                tenant_id=tenant_id,
                collection_id=collection_id,
                logical_name=logical_name,
                title=title,
                status="pending",
                visibility=visibility.value,
                created_by=created_by,
            )
            self.session.add(document)
            await self.session.flush()
        elif document.status in {"deleting", "deleted"}:
            raise DocumentRegistrationError(
                code=ErrorCode.CONFLICT,
                message="The document is being deleted or has been deleted.",
                details={"document_id": str(document.id)},
            )
        else:
            document.title = title
            document.status = "processing"

        version = DocumentVersionModel(
            id=new_uuid7(),
            document_id=document.id,
            sha256=stored_object.sha256,
            source_name=source_name,
            media_type=media_type,
            size_bytes=stored_object.size_bytes,
            object_key=stored_object.key,
            parser_provider=parser_provider,
            parser_version=parser_version,
            status="pending",
        )
        self.session.add(version)
        await self.session.flush()
        self.session.add(
            DocumentContentClaimModel(
                tenant_id=tenant_id,
                collection_id=collection_id,
                sha256=stored_object.sha256,
                document_id=document.id,
                version_id=version.id,
            )
        )
        await self.session.flush()
        return DocumentRegistration(
            document_id=document.id,
            version_id=version.id,
            sha256=stored_object.sha256,
            object_key=stored_object.key,
            deduplicated=False,
            new_document=new_document,
        )

    async def _require_owned_collection(self, tenant_id: UUID, collection_id: UUID) -> None:
        statement = select(CollectionModel.id).where(
            CollectionModel.id == collection_id,
            CollectionModel.tenant_id == tenant_id,
        )
        if (await self.session.scalar(statement)) is None:
            raise DocumentRegistrationError(
                code=ErrorCode.NOT_FOUND,
                message="The collection was not found.",
                details={"collection_id": str(collection_id)},
            )

    async def _lock_registration_keys(
        self,
        *,
        tenant_id: UUID,
        collection_id: UUID,
        logical_name: str,
        sha256: str,
    ) -> None:
        keys = sorted(
            (
                f"content:{tenant_id}:{collection_id}:{sha256}",
                f"document:{tenant_id}:{collection_id}:{logical_name}",
            )
        )
        for key in keys:
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": key},
            )

    async def _find_claim(
        self, tenant_id: UUID, collection_id: UUID, sha256: str
    ) -> DocumentContentClaimModel | None:
        result = await self.session.execute(
            select(DocumentContentClaimModel).where(
                DocumentContentClaimModel.tenant_id == tenant_id,
                DocumentContentClaimModel.collection_id == collection_id,
                DocumentContentClaimModel.sha256 == sha256,
            )
        )
        return result.scalar_one_or_none()

    async def _find_document(
        self, tenant_id: UUID, collection_id: UUID, logical_name: str
    ) -> DocumentModel | None:
        result = await self.session.execute(
            select(DocumentModel).where(
                DocumentModel.tenant_id == tenant_id,
                DocumentModel.collection_id == collection_id,
                DocumentModel.logical_name == logical_name,
            )
        )
        return result.scalar_one_or_none()
