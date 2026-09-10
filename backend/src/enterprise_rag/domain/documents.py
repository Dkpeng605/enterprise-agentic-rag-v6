"""Immutable document, version, root, and leaf domain models."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from enterprise_rag.domain.common import (
    freeze_mapping,
    require_non_empty,
    require_sha256,
    require_utc,
    require_uuid7,
    sha256_text,
    stable_content_id,
    to_json_value,
)

CONTENT_ID_PATTERN = re.compile(r"^(root|leaf)_[0-9a-f]{64}$")


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class DocumentVisibility(StrEnum):
    PRIVATE = "private"
    TENANT = "tenant"
    PUBLIC = "public"


class DocumentVersionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class RootKind(StrEnum):
    PAGE = "page"
    SECTION = "section"
    SHEET_ROWS = "sheet_rows"
    TEXT_BLOCK = "text_block"


@dataclass(frozen=True, slots=True)
class Document:
    id: UUID
    tenant_id: UUID
    collection_id: UUID
    logical_name: str
    title: str
    status: DocumentStatus
    active_version_id: UUID | None
    visibility: DocumentVisibility
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "tenant_id", "collection_id", "created_by"):
            require_uuid7(getattr(self, name), name)
        if self.active_version_id is not None:
            require_uuid7(self.active_version_id, "active_version_id")
        require_non_empty(self.logical_name, "logical_name")
        require_non_empty(self.title, "title")
        require_utc(self.created_at, "created_at")
        require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "tenant_id": str(self.tenant_id),
            "collection_id": str(self.collection_id),
            "logical_name": self.logical_name,
            "title": self.title,
            "status": self.status.value,
            "active_version_id": (
                str(self.active_version_id) if self.active_version_id is not None else None
            ),
            "visibility": self.visibility.value,
            "created_by": str(self.created_by),
            "created_at": to_json_value(self.created_at),
            "updated_at": to_json_value(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    id: UUID
    document_id: UUID
    sha256: str
    source_name: str
    media_type: str
    size_bytes: int
    object_key: str
    parser_provider: str
    parser_version: str
    status: DocumentVersionStatus
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, "id")
        require_uuid7(self.document_id, "document_id")
        require_sha256(self.sha256, "sha256")
        for name in (
            "source_name",
            "media_type",
            "object_key",
            "parser_provider",
            "parser_version",
        ):
            require_non_empty(getattr(self, name), name)
        if self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        if self.status is DocumentVersionStatus.FAILED and not self.error_code:
            raise ValueError("failed versions require error_code")
        if self.status is not DocumentVersionStatus.FAILED and (
            self.error_code is not None or self.error_message is not None
        ):
            raise ValueError("non-failed versions must not contain error fields")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "document_id": str(self.document_id),
            "sha256": self.sha256,
            "source_name": self.source_name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "object_key": self.object_key,
            "parser_provider": self.parser_provider,
            "parser_version": self.parser_version,
            "status": self.status.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class RootChunk:
    id: str
    tenant_id: UUID
    document_id: UUID
    version_id: UUID
    index_revision: str
    ordinal: int
    kind: RootKind
    source_locator: Mapping[str, object]
    raw_text: str
    clean_text: str
    metadata: Mapping[str, object]
    content_hash: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "document_id", "version_id"):
            require_uuid7(getattr(self, name), name)
        require_non_empty(self.index_revision, "index_revision")
        require_non_empty(self.clean_text, "clean_text")
        require_sha256(self.content_hash, "content_hash")
        if self.ordinal < 0:
            raise ValueError("ordinal must not be negative")
        if not self.source_locator:
            raise ValueError("source_locator must not be empty")
        expected_id = self.derive_id(
            version_id=self.version_id,
            index_revision=self.index_revision,
            ordinal=self.ordinal,
            kind=self.kind,
            content_hash=self.content_hash,
        )
        if self.id != expected_id or CONTENT_ID_PATTERN.fullmatch(self.id) is None:
            raise ValueError("root id does not match its immutable identity fields")
        object.__setattr__(self, "source_locator", freeze_mapping(self.source_locator))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @staticmethod
    def derive_id(
        *,
        version_id: UUID,
        index_revision: str,
        ordinal: int,
        kind: RootKind,
        content_hash: str,
    ) -> str:
        return stable_content_id(
            "root",
            str(version_id),
            index_revision,
            str(ordinal),
            kind.value,
            content_hash,
        )

    @classmethod
    def create(
        cls,
        *,
        tenant_id: UUID,
        document_id: UUID,
        version_id: UUID,
        index_revision: str,
        ordinal: int,
        kind: RootKind,
        source_locator: Mapping[str, object],
        raw_text: str,
        clean_text: str,
        metadata: Mapping[str, object] | None = None,
    ) -> "RootChunk":
        content_hash = sha256_text(clean_text)
        return cls(
            id=cls.derive_id(
                version_id=version_id,
                index_revision=index_revision,
                ordinal=ordinal,
                kind=kind,
                content_hash=content_hash,
            ),
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            index_revision=index_revision,
            ordinal=ordinal,
            kind=kind,
            source_locator=source_locator,
            raw_text=raw_text,
            clean_text=clean_text,
            metadata=metadata or {},
            content_hash=content_hash,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "tenant_id": str(self.tenant_id),
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "index_revision": self.index_revision,
            "ordinal": self.ordinal,
            "kind": self.kind.value,
            "source_locator": to_json_value(self.source_locator),
            "raw_text": self.raw_text,
            "clean_text": self.clean_text,
            "metadata": to_json_value(self.metadata),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class LeafChunk:
    id: str
    root_id: str
    tenant_id: UUID
    document_id: UUID
    version_id: UUID
    ordinal: int
    text: str
    retrieval_text: str
    start_offset: int | None
    end_offset: int | None
    token_count: int
    metadata: Mapping[str, object]
    content_hash: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "document_id", "version_id"):
            require_uuid7(getattr(self, name), name)
        if (
            CONTENT_ID_PATTERN.fullmatch(self.root_id) is None
            or not self.root_id.startswith("root_")
        ):
            raise ValueError("root_id must be a stable root content ID")
        require_non_empty(self.text, "text")
        require_non_empty(self.retrieval_text, "retrieval_text")
        require_sha256(self.content_hash, "content_hash")
        if self.ordinal < 0:
            raise ValueError("ordinal must not be negative")
        if self.token_count <= 0:
            raise ValueError("token_count must be positive")
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("start_offset and end_offset must be provided together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and (self.start_offset < 0 or self.end_offset <= self.start_offset)
        ):
            raise ValueError("leaf offsets must form a positive range")
        expected_id = self.derive_id(
            root_id=self.root_id,
            ordinal=self.ordinal,
            content_hash=self.content_hash,
        )
        if self.id != expected_id or CONTENT_ID_PATTERN.fullmatch(self.id) is None:
            raise ValueError("leaf id does not match its immutable identity fields")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @staticmethod
    def derive_id(*, root_id: str, ordinal: int, content_hash: str) -> str:
        return stable_content_id("leaf", root_id, str(ordinal), content_hash)

    @classmethod
    def create(
        cls,
        *,
        root_id: str,
        tenant_id: UUID,
        document_id: UUID,
        version_id: UUID,
        ordinal: int,
        text: str,
        retrieval_text: str,
        start_offset: int | None,
        end_offset: int | None,
        token_count: int,
        metadata: Mapping[str, object] | None = None,
    ) -> "LeafChunk":
        content_hash = sha256_text(text)
        return cls(
            id=cls.derive_id(root_id=root_id, ordinal=ordinal, content_hash=content_hash),
            root_id=root_id,
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            ordinal=ordinal,
            text=text,
            retrieval_text=retrieval_text,
            start_offset=start_offset,
            end_offset=end_offset,
            token_count=token_count,
            metadata=metadata or {},
            content_hash=content_hash,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "root_id": self.root_id,
            "tenant_id": str(self.tenant_id),
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "ordinal": self.ordinal,
            "text": self.text,
            "retrieval_text": self.retrieval_text,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "token_count": self.token_count,
            "metadata": to_json_value(self.metadata),
            "content_hash": self.content_hash,
        }
