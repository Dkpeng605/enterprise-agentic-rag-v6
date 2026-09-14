"""Safe, revision-aware reindexing after an Embedding Provider change."""

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, select

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.database.models import (
    DocumentModel,
    DocumentVersionModel,
    LeafModel,
    RootModel,
)
from enterprise_rag.domain.common import to_json_value
from enterprise_rag.domain.documents import LeafChunk, RootChunk, RootKind
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.cleaner import CleanRoot
from enterprise_rag.ports.loader import IngestionContext
from enterprise_rag.ports.splitter import Splitter
from enterprise_rag.ports.vector_store import VectorStore
from enterprise_rag.services.projection import ProjectionRequest, ProjectionService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderIndexDocument:
    document_id: UUID
    title: str
    version_id: UUID
    stored_revisions: tuple[str, ...]
    active_revision: str
    compatible: bool
    root_count: int
    leaf_count: int
    vector_count: int


@dataclass(frozen=True, slots=True)
class ProviderIndexStatus:
    active_revision: str
    embedding_model: str
    embedding_dimension: int
    total_documents: int
    compatible_documents: int
    incompatible_documents: int
    documents: tuple[ProviderIndexDocument, ...]


@dataclass(frozen=True, slots=True)
class ProviderReindexItem:
    document_id: UUID
    title: str
    status: str
    old_revisions: tuple[str, ...]
    leaf_count: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderReindexResult:
    active_revision: str
    requested_count: int
    rebuilt_count: int
    skipped_count: int
    failed_count: int
    cleanup_failed_count: int
    items: tuple[ProviderReindexItem, ...]


@dataclass(frozen=True, slots=True)
class _DocumentSnapshot:
    tenant_id: UUID
    collection_id: UUID
    document_id: UUID
    title: str
    version_id: UUID
    roots: tuple[RootChunk, ...]


class ProviderReindexService:
    """Stage new vectors, atomically swap PG Root/Leaf rows, then retire old vectors."""

    def __init__(
        self,
        *,
        database: Database,
        splitter: Splitter,
        projection: ProjectionService,
        vector_store: VectorStore,
        active_revision: str,
        embedding_model: str,
        embedding_dimension: int,
        temporary_root: Path,
        max_documents: int = 100,
    ) -> None:
        if not active_revision.strip() or not embedding_model.strip():
            raise ValueError("active Provider revision and model are required")
        if embedding_dimension <= 1 or max_documents <= 0:
            raise ValueError("Provider reindex limits are invalid")
        self._database = database
        self._splitter = splitter
        self._projection = projection
        self._vector_store = vector_store
        self._active_revision = active_revision
        self._embedding_model = embedding_model
        self._embedding_dimension = embedding_dimension
        self._temporary_root = temporary_root
        self._temporary_root.mkdir(parents=True, exist_ok=True)
        self._max_documents = max_documents
        self._lock = asyncio.Lock()

    async def status(self, tenant_id: UUID | None = None) -> ProviderIndexStatus:
        snapshots = await self._load_snapshots(tenant_id)
        documents: list[ProviderIndexDocument] = []
        for snapshot in snapshots:
            revisions = tuple(sorted({root.index_revision for root in snapshot.roots}))
            leaf_count, vector_count, compatible = await self._compatibility(snapshot)
            documents.append(
                ProviderIndexDocument(
                    snapshot.document_id,
                    snapshot.title,
                    snapshot.version_id,
                    revisions,
                    self._active_revision,
                    compatible,
                    len(snapshot.roots),
                    leaf_count,
                    vector_count,
                )
            )
        compatible_count = sum(item.compatible for item in documents)
        return ProviderIndexStatus(
            self._active_revision,
            self._embedding_model,
            self._embedding_dimension,
            len(documents),
            compatible_count,
            len(documents) - compatible_count,
            tuple(documents),
        )

    async def reindex(self, tenant_id: UUID | None = None) -> ProviderReindexResult:
        if self._lock.locked():
            raise AppError(ErrorCode.CONFLICT, "A Provider reindex is already running.")
        async with self._lock:
            snapshots = await self._load_snapshots(tenant_id)
            if len(snapshots) > self._max_documents:
                raise AppError(
                    ErrorCode.CONFLICT,
                    "The number of documents exceeds the Provider reindex safety limit.",
                    {"max_documents": self._max_documents},
                )
            item_values: list[ProviderReindexItem] = []
            for snapshot in snapshots:
                leaf_count, _, compatible = await self._compatibility(snapshot)
                item_values.append(
                    ProviderReindexItem(
                        snapshot.document_id,
                        snapshot.title,
                        "skipped",
                        tuple(sorted({root.index_revision for root in snapshot.roots})),
                        leaf_count,
                    )
                    if compatible
                    else await self._reindex_one(snapshot)
                )
            items = tuple(item_values)
            rebuilt = sum(item.status == "rebuilt" for item in items)
            cleanup_failed = sum(item.status == "rebuilt_cleanup_degraded" for item in items)
            return ProviderReindexResult(
                self._active_revision,
                len(snapshots),
                rebuilt + cleanup_failed,
                sum(item.status == "skipped" for item in items),
                sum(item.status == "failed" for item in items),
                cleanup_failed,
                items,
            )

    async def _compatibility(self, snapshot: _DocumentSnapshot) -> tuple[int, int, bool]:
        revisions = tuple(sorted({root.index_revision for root in snapshot.roots}))
        leaf_count = await self._leaf_count(snapshot.version_id)
        vector_count = await self._vector_store.count_by_version_revision(
            snapshot.tenant_id, snapshot.version_id, self._active_revision
        )
        compatible = (
            revisions == (self._active_revision,)
            and leaf_count > 0
            and vector_count == leaf_count
        )
        return leaf_count, vector_count, compatible

    async def _reindex_one(self, snapshot: _DocumentSnapshot) -> ProviderReindexItem:
        old_revisions = tuple(sorted({root.index_revision for root in snapshot.roots}))
        context_dir = self._temporary_root / str(snapshot.version_id)
        context_dir.mkdir(parents=True, exist_ok=True)
        context = IngestionContext(
            snapshot.tenant_id,
            snapshot.document_id,
            snapshot.version_id,
            context_dir,
            self._active_revision,
        )
        try:
            results = [
                await self._splitter.split(
                    CleanRoot(
                        ordinal=root.ordinal,
                        kind=root.kind,
                        source_locator=root.source_locator,
                        raw_text=root.raw_text,
                        clean_text=root.clean_text,
                        metadata=root.metadata,
                    ),
                    context,
                )
                for root in snapshot.roots
            ]
            roots = tuple(result.root for result in results)
            leaves = tuple(leaf for result in results for leaf in result.leaves)
            await self._projection.project(
                ProjectionRequest(
                    snapshot.tenant_id,
                    snapshot.collection_id,
                    snapshot.document_id,
                    snapshot.version_id,
                    self._active_revision,
                    leaves,
                )
            )
        except BaseException as error:
            LOGGER.error(
                "Provider reindex projection failed",
                extra={"event_code": "PROVIDER_REINDEX_PROJECTION_FAILED", "outcome": "error"},
            )
            await self._cleanup_target(snapshot)
            if isinstance(error, asyncio.CancelledError):
                raise
            return ProviderReindexItem(
                snapshot.document_id,
                snapshot.title,
                "failed",
                old_revisions,
                0,
                "Provider reindex projection failed; the old index was preserved.",
            )
        try:
            await self._swap_content(snapshot, roots, leaves)
        except BaseException as error:
            LOGGER.error(
                "Provider reindex database swap failed",
                extra={"event_code": "PROVIDER_REINDEX_SWAP_FAILED", "outcome": "error"},
            )
            await self._cleanup_target(snapshot)
            if isinstance(error, asyncio.CancelledError):
                raise
            return ProviderReindexItem(
                snapshot.document_id,
                snapshot.title,
                "failed",
                old_revisions,
                0,
                "Provider reindex database swap failed; the old index was preserved.",
            )
        cleanup_failed = False
        for revision in old_revisions:
            if revision == self._active_revision:
                continue
            try:
                await self._vector_store.delete_by_version_revision(
                    snapshot.tenant_id, snapshot.version_id, revision
                )
            except Exception:
                cleanup_failed = True
        return ProviderReindexItem(
            snapshot.document_id,
            snapshot.title,
            "rebuilt_cleanup_degraded" if cleanup_failed else "rebuilt",
            old_revisions,
            len(leaves),
            "Old revision cleanup failed; the new index is active." if cleanup_failed else None,
        )

    async def _swap_content(
        self,
        snapshot: _DocumentSnapshot,
        roots: tuple[RootChunk, ...],
        leaves: tuple[LeafChunk, ...],
    ) -> None:
        async with self._database.session() as session:
            await session.execute(
                delete(RootModel).where(RootModel.version_id == snapshot.version_id)
            )
            await session.flush()
            session.add_all(
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
            )
            await session.flush()
            session.add_all(
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
            )
            await session.flush()

    async def _cleanup_target(self, snapshot: _DocumentSnapshot) -> None:
        with suppress(Exception):
            await self._vector_store.delete_by_version_revision(
                snapshot.tenant_id, snapshot.version_id, self._active_revision
            )
        # The original revision remains usable; status will expose any partial target.

    async def _load_snapshots(self, tenant_id: UUID | None) -> tuple[_DocumentSnapshot, ...]:
        async with self._database.session() as session:
            filters = [DocumentModel.status == "ready", DocumentVersionModel.status == "indexed"]
            if tenant_id is not None:
                filters.append(DocumentModel.tenant_id == tenant_id)
            rows = (
                await session.execute(
                    select(DocumentModel, DocumentVersionModel)
                    .join(
                        DocumentVersionModel,
                        DocumentVersionModel.id == DocumentModel.active_version_id,
                    )
                    .where(*filters)
                    .order_by(DocumentModel.title, DocumentModel.id)
                )
            ).all()
            version_ids = [version.id for _, version in rows]
            grouped: dict[UUID, list[RootModel]] = defaultdict(list)
            if version_ids:
                root_rows = await session.scalars(
                    select(RootModel).where(RootModel.version_id.in_(version_ids))
                )
                for root in root_rows:
                    grouped[root.version_id].append(root)
            return tuple(
                _DocumentSnapshot(
                    document.tenant_id,
                    document.collection_id,
                    document.id,
                    document.title,
                    version.id,
                    tuple(
                        _root_chunk(root)
                        for root in sorted(
                            grouped.get(version.id, ()), key=lambda item: item.ordinal
                        )
                    ),
                )
                for document, version in rows
            )

    async def _leaf_count(self, version_id: UUID) -> int:
        async with self._database.session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(LeafModel)
                    .where(LeafModel.version_id == version_id)
                )
                or 0
            )


def _root_chunk(row: RootModel) -> RootChunk:
    return RootChunk(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        version_id=row.version_id,
        index_revision=row.index_revision,
        ordinal=row.ordinal,
        kind=RootKind(row.kind),
        source_locator=row.source_locator,
        raw_text=row.raw_text,
        clean_text=row.clean_text,
        metadata=row.metadata_json,
        content_hash=row.content_hash,
    )
