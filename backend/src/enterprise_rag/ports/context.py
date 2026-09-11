"""Authorized PostgreSQL fact-source contract for Leaf and Root recovery."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from enterprise_rag.domain.common import freeze_mapping, require_non_empty, require_uuid7
from enterprise_rag.domain.retrieval import QueryScope


@dataclass(frozen=True, slots=True)
class ScopeAuthorization:
    tenant_id: UUID
    full_tenant_access: bool
    collection_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        require_uuid7(self.tenant_id, "tenant_id")
        for name, values in (
            ("collection_ids", self.collection_ids),
            ("document_ids", self.document_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
            for value in values:
                require_uuid7(value, name)


@dataclass(frozen=True, slots=True)
class ResolvedQueryScope:
    tenant_id: UUID
    document_ids: tuple[UUID, ...]
    sections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_uuid7(self.tenant_id, "tenant_id")
        if len(self.document_ids) != len(set(self.document_ids)):
            raise ValueError("document_ids must not contain duplicates")
        for value in self.document_ids:
            require_uuid7(value, "document_ids")
        if any(not value.strip() for value in self.sections):
            raise ValueError("sections must not contain blank values")


@dataclass(frozen=True, slots=True)
class StoredLeafEvidence:
    leaf_id: str
    root_id: str
    document_id: UUID
    version_id: UUID
    retrieval_text: str

    def __post_init__(self) -> None:
        if not self.leaf_id.startswith("leaf_") or not self.root_id.startswith("root_"):
            raise ValueError("evidence IDs must use leaf_ and root_ prefixes")
        require_uuid7(self.document_id, "document_id")
        require_uuid7(self.version_id, "version_id")
        require_non_empty(self.retrieval_text, "retrieval_text")


@dataclass(frozen=True, slots=True)
class StoredRootEvidence:
    root_id: str
    document_id: UUID
    version_id: UUID
    source_name: str
    title: str
    organization: str | None
    media_type: str
    text: str
    source_locator: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.root_id.startswith("root_"):
            raise ValueError("root_id must use the root_ prefix")
        require_uuid7(self.document_id, "document_id")
        require_uuid7(self.version_id, "version_id")
        for name in ("source_name", "title", "media_type", "text"):
            require_non_empty(getattr(self, name), name)
        if self.organization is not None:
            require_non_empty(self.organization, "organization")
        object.__setattr__(self, "source_locator", freeze_mapping(self.source_locator))


class ContextRepository(Protocol):
    async def resolve_scope(
        self, authorization: ScopeAuthorization, requested: QueryScope
    ) -> ResolvedQueryScope: ...

    async def load_leaves(
        self, scope: ResolvedQueryScope, leaf_ids: Sequence[str]
    ) -> tuple[StoredLeafEvidence, ...]: ...

    async def load_roots(
        self, scope: ResolvedQueryScope, root_ids: Sequence[str]
    ) -> tuple[StoredRootEvidence, ...]: ...
