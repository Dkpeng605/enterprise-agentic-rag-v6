"""Build, verify, activate, and compensate Leaf vector projections."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from enterprise_rag.domain.common import require_non_empty, require_uuid7
from enterprise_rag.domain.documents import LeafChunk
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.embedding import EmbeddingProvider
from enterprise_rag.ports.sparse import SparseEncoder
from enterprise_rag.ports.vector_store import IndexSchema, VectorRecord, VectorStore

Sleeper = Callable[[float], Awaitable[None]]


class ProjectionError(AppError):
    """A stable client-safe projection transaction failure."""


@dataclass(frozen=True, slots=True)
class ProjectionRequest:
    tenant_id: UUID
    collection_id: UUID
    document_id: UUID
    version_id: UUID
    index_revision: str
    leaves: tuple[LeafChunk, ...]

    def __post_init__(self) -> None:
        for name in ("tenant_id", "collection_id", "document_id", "version_id"):
            require_uuid7(getattr(self, name), name)
        require_non_empty(self.index_revision, "index_revision")
        if not self.leaves:
            raise ValueError("projection requires at least one Leaf")
        if len({leaf.id for leaf in self.leaves}) != len(self.leaves):
            raise ValueError("projection Leaves must have unique IDs")
        if any(
            leaf.tenant_id != self.tenant_id
            or leaf.document_id != self.document_id
            or leaf.version_id != self.version_id
            for leaf in self.leaves
        ):
            raise ValueError("projection Leaf ownership does not match the request")


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    expected_count: int
    staged_count: int
    activated_count: int
    verified_count: int
    batches: int

    def __post_init__(self) -> None:
        if not (
            self.expected_count == self.staged_count == self.activated_count == self.verified_count
        ):
            raise ValueError("projection result counts must all match")
        if self.expected_count <= 0 or self.batches <= 0:
            raise ValueError("projection result counts must be positive")


class ProjectionService:
    def __init__(
        self,
        *,
        embedding: EmbeddingProvider,
        sparse: SparseEncoder,
        vector_store: VectorStore,
        batch_size: int = 128,
        cleanup_retries: int = 2,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if batch_size <= 0 or not 0 <= cleanup_retries <= 10:
            raise ValueError("projection batch and retry settings are invalid")
        self._embedding = embedding
        self._sparse = sparse
        self._vector_store = vector_store
        self._batch_size = batch_size
        self._cleanup_retries = cleanup_retries
        self._sleeper = sleeper

    async def project(self, request: ProjectionRequest) -> ProjectionResult:
        expected = len(request.leaves)
        await self._vector_store.ensure_revision(
            IndexSchema(request.index_revision, self._embedding.dimension)
        )
        texts = tuple(leaf.retrieval_text for leaf in request.leaves)
        dense_vectors = await self._embedding.embed_documents(texts)
        sparse_vectors = await self._sparse.encode_documents(texts)
        if len(dense_vectors) != expected or len(sparse_vectors) != expected:
            raise ProjectionError(
                ErrorCode.PROJECTION_UPSERT_FAILED,
                "Vector encoders returned an unexpected result count.",
            )
        staged = self._records(request, dense_vectors, sparse_vectors, status="processing")
        batches = (expected + self._batch_size - 1) // self._batch_size
        try:
            staged_count = await self._upsert_all(staged)
            await self._verify_count(request, expected)
            ready = tuple(
                VectorRecord(
                    index_revision=record.index_revision,
                    leaf_id=record.leaf_id,
                    root_id=record.root_id,
                    tenant_id=record.tenant_id,
                    collection_id=record.collection_id,
                    document_id=record.document_id,
                    version_id=record.version_id,
                    status="ready",
                    dense_vector=record.dense_vector,
                    sparse_vector=record.sparse_vector,
                    metadata=record.metadata,
                )
                for record in staged
            )
            activated_count = await self._upsert_all(ready)
            verified = await self._verify_count(request, expected)
            return ProjectionResult(expected, staged_count, activated_count, verified, batches)
        except BaseException as primary:
            try:
                await self._cleanup(request)
            except Exception as cleanup_error:
                raise ProjectionError(
                    ErrorCode.PROJECTION_CLEANUP_FAILED,
                    "A failed projection could not be fully cleaned up.",
                ) from cleanup_error
            if isinstance(primary, asyncio.CancelledError | AppError):
                raise primary
            raise ProjectionError(
                ErrorCode.PROJECTION_UPSERT_FAILED,
                "Vector projection failed and was compensated.",
            ) from primary

    async def _upsert_all(self, records: Sequence[VectorRecord]) -> int:
        count = 0
        for start in range(0, len(records), self._batch_size):
            batch = records[start : start + self._batch_size]
            result = await self._vector_store.upsert(batch)
            if result.count != len(batch):
                raise ProjectionError(
                    ErrorCode.PROJECTION_UPSERT_FAILED,
                    "VectorStore reported a partial projection upsert.",
                    {"expected_count": len(batch), "actual_count": result.count},
                )
            count += result.count
        return count

    async def _verify_count(self, request: ProjectionRequest, expected: int) -> int:
        actual = await self._vector_store.count_by_version(request.tenant_id, request.version_id)
        if actual != expected:
            raise ProjectionError(
                ErrorCode.PROJECTION_COUNT_MISMATCH,
                "Vector projection count verification failed.",
                {"expected_count": expected, "actual_count": actual},
            )
        return actual

    async def _cleanup(self, request: ProjectionRequest) -> None:
        last_error: Exception | None = None
        for attempt in range(self._cleanup_retries + 1):
            try:
                await self._vector_store.delete_by_version(request.tenant_id, request.version_id)
                if (
                    await self._vector_store.count_by_version(request.tenant_id, request.version_id)
                    == 0
                ):
                    return
                last_error = RuntimeError("projection rows remain after delete")
            except Exception as error:
                last_error = error
            if attempt < self._cleanup_retries:
                await self._sleeper(0.25 * (2**attempt))
        raise RuntimeError("projection cleanup retries exhausted") from last_error

    @staticmethod
    def _records(
        request: ProjectionRequest,
        dense_vectors: Sequence[Sequence[float]],
        sparse_vectors: Sequence[Mapping[int, float]],
        *,
        status: str,
    ) -> tuple[VectorRecord, ...]:
        records: list[VectorRecord] = []
        for leaf, dense, sparse in zip(request.leaves, dense_vectors, sparse_vectors, strict=True):
            records.append(
                VectorRecord(
                    index_revision=request.index_revision,
                    leaf_id=leaf.id,
                    root_id=leaf.root_id,
                    tenant_id=request.tenant_id,
                    collection_id=request.collection_id,
                    document_id=request.document_id,
                    version_id=request.version_id,
                    status=status,
                    dense_vector=tuple(dense),
                    sparse_vector=sparse,
                    metadata={"token_count": leaf.token_count},
                )
            )
        return tuple(records)
