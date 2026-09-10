"""Document upload registration orchestration."""

from collections.abc import AsyncIterable
from dataclasses import dataclass
from uuid import UUID

from enterprise_rag.adapters.database.documents import (
    DocumentRegistration,
    DocumentRegistrationRepository,
)
from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.domain.common import require_non_empty, require_uuid7
from enterprise_rag.domain.documents import DocumentVisibility
from enterprise_rag.ports.object_store import ObjectStore, validate_sha256


@dataclass(frozen=True, slots=True)
class RegisterDocument:
    tenant_id: UUID
    collection_id: UUID
    created_by: UUID
    logical_name: str
    title: str
    source_name: str
    media_type: str
    visibility: DocumentVisibility = DocumentVisibility.TENANT
    parser_provider: str = "unassigned"
    parser_version: str = "unassigned"
    expected_sha256: str | None = None
    max_bytes: int = 100 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("tenant_id", "collection_id", "created_by"):
            require_uuid7(getattr(self, name), name)
        limits = {
            "logical_name": 300,
            "title": 500,
            "source_name": 500,
            "media_type": 200,
            "parser_provider": 100,
            "parser_version": 100,
        }
        for name, limit in limits.items():
            value = getattr(self, name)
            require_non_empty(value, name)
            if len(value) > limit:
                raise ValueError(f"{name} must not exceed {limit} characters")
        if self.expected_sha256 is not None:
            validate_sha256(self.expected_sha256)
        if self.max_bytes < 0:
            raise ValueError("max_bytes must not be negative")


class DocumentRegistrationService:
    def __init__(self, database: Database, object_store: ObjectStore) -> None:
        self._database = database
        self._object_store = object_store

    async def register(
        self, command: RegisterDocument, chunks: AsyncIterable[bytes]
    ) -> DocumentRegistration:
        stored_object = await self._object_store.put(
            chunks,
            expected_sha256=command.expected_sha256,
            max_bytes=command.max_bytes,
        )
        async with self._database.session() as session:
            return await DocumentRegistrationRepository(session).register(
                tenant_id=command.tenant_id,
                collection_id=command.collection_id,
                created_by=command.created_by,
                logical_name=command.logical_name,
                title=command.title,
                source_name=command.source_name,
                media_type=command.media_type,
                visibility=command.visibility,
                parser_provider=command.parser_provider,
                parser_version=command.parser_version,
                stored_object=stored_object,
            )
