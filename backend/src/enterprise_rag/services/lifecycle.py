"""Idempotent cross-store deletion Saga and reconciliation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.adapters.database.lifecycle import (
    DeleteRequest,
    DocumentLifecycleRepository,
)
from enterprise_rag.ports.object_store import ObjectStore
from enterprise_rag.ports.vector_store import VectorStore


class DeletionStep(StrEnum):
    VECTORS_DELETED = "vectors_deleted"
    POSTGRES_CONTENT_DELETED = "postgres_content_deleted"
    OBJECTS_DELETED = "objects_deleted"


Checkpoint = Callable[[DeletionStep], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DeletionResult:
    document_id: UUID
    job_id: UUID
    vector_count: int
    object_count: int
    already_completed: bool


class DocumentDeletionService:
    def __init__(
        self,
        database: Database,
        vector_store: VectorStore,
        object_store: ObjectStore,
    ) -> None:
        self._database = database
        self._vector_store = vector_store
        self._object_store = object_store

    async def request_delete(
        self, *, tenant_id: UUID, document_id: UUID, now: datetime
    ) -> DeleteRequest:
        async with self._database.session() as session:
            return await DocumentLifecycleRepository(session).request_delete(
                tenant_id=tenant_id, document_id=document_id, now=now
            )

    async def execute(
        self,
        *,
        job_id: UUID,
        owner: str,
        now: datetime,
        checkpoint: Checkpoint | None = None,
    ) -> DeletionResult:
        async with self._database.session() as session:
            context = await DocumentLifecycleRepository(session).load_deletion(
                job_id=job_id, owner=owner, now=now
            )
        if context.completed:
            return DeletionResult(context.document_id, job_id, 0, 0, True)

        vector_count = 0
        for version in context.versions:
            vector_count += await self._vector_store.delete_by_version(
                context.tenant_id, version.version_id
            )
        await self._checkpoint(checkpoint, DeletionStep.VECTORS_DELETED)

        async with self._database.session() as session:
            await DocumentLifecycleRepository(session).cleanup_content(context)
        await self._checkpoint(checkpoint, DeletionStep.POSTGRES_CONTENT_DELETED)

        object_count = 0
        async with self._object_store.mutation_guard():
            for object_key in sorted({version.object_key for version in context.versions}):
                async with self._database.session() as session:
                    referenced = await DocumentLifecycleRepository(session).object_is_referenced(
                        object_key
                    )
                if not referenced and await self._object_store.delete(object_key):
                    object_count += 1
        await self._checkpoint(checkpoint, DeletionStep.OBJECTS_DELETED)

        async with self._database.session() as session:
            await DocumentLifecycleRepository(session).finalize(context, owner=owner, now=now)
        return DeletionResult(context.document_id, job_id, vector_count, object_count, False)

    @staticmethod
    async def _checkpoint(checkpoint: Checkpoint | None, step: DeletionStep) -> None:
        if checkpoint is not None:
            await checkpoint(step)


class ReconcileIssueKind(StrEnum):
    ORPHAN_VECTOR = "orphan_vector"
    VECTOR_COUNT_MISMATCH = "vector_count_mismatch"
    MISSING_OBJECT = "missing_object"
    ORPHAN_OBJECT = "orphan_object"
    EXPIRED_LEASE = "expired_lease"


@dataclass(frozen=True, slots=True)
class ReconcileIssue:
    kind: ReconcileIssueKind
    identity: str
    expected: int | None
    actual: int | None
    repaired: bool


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    issues: tuple[ReconcileIssue, ...]

    @property
    def repaired_count(self) -> int:
        return sum(issue.repaired for issue in self.issues)

    @property
    def unresolved_count(self) -> int:
        return sum(not issue.repaired for issue in self.issues)


class ReconcileService:
    def __init__(
        self,
        database: Database,
        vector_store: VectorStore,
        object_store: ObjectStore,
    ) -> None:
        self._database = database
        self._vector_store = vector_store
        self._object_store = object_store

    async def run(self, *, now: datetime, apply: bool = False) -> ReconcileReport:
        projections = {
            (item.tenant_id, item.version_id): item.count
            for item in await self._vector_store.list_version_projections()
        }
        async with self._database.session() as session:
            snapshot = await DocumentLifecycleRepository(session).reconcile_snapshot(now=now)
        issues: list[ReconcileIssue] = []

        for vector_key, actual in sorted(projections.items(), key=lambda item: str(item[0])):
            if vector_key in snapshot.vector_counts:
                continue
            repaired = False
            if apply:
                await self._vector_store.delete_by_version(*vector_key)
                repaired = True
            issues.append(
                ReconcileIssue(
                    ReconcileIssueKind.ORPHAN_VECTOR,
                    f"{vector_key[0]}:{vector_key[1]}",
                    0,
                    actual,
                    repaired,
                )
            )

        for vector_key, expected in sorted(
            snapshot.vector_counts.items(), key=lambda item: str(item[0])
        ):
            actual = projections.get(vector_key, 0)
            if actual != expected:
                issues.append(
                    ReconcileIssue(
                        ReconcileIssueKind.VECTOR_COUNT_MISMATCH,
                        f"{vector_key[0]}:{vector_key[1]}",
                        expected,
                        actual,
                        False,
                    )
                )

        async with self._object_store.mutation_guard():
            async with self._database.session() as session:
                object_snapshot = await DocumentLifecycleRepository(
                    session
                ).reconcile_snapshot(now=now)
            actual_keys = frozenset(await self._object_store.list_keys())
            for object_key in sorted(object_snapshot.object_keys - actual_keys):
                issues.append(
                    ReconcileIssue(
                        ReconcileIssueKind.MISSING_OBJECT, object_key, 1, 0, False
                    )
                )
            for object_key in sorted(actual_keys - object_snapshot.object_keys):
                repaired = apply and await self._object_store.delete(object_key)
                issues.append(
                    ReconcileIssue(
                        ReconcileIssueKind.ORPHAN_OBJECT, object_key, 0, 1, repaired
                    )
                )

        if snapshot.expired_leases:
            repaired = False
            if apply:
                async with self._database.session() as session:
                    await IngestionJobRepository(session).recover_expired(now=now)
                async with self._database.session() as session:
                    remaining = await DocumentLifecycleRepository(
                        session
                    ).reconcile_snapshot(now=now)
                repaired = remaining.expired_leases == 0
            issues.append(
                ReconcileIssue(
                    ReconcileIssueKind.EXPIRED_LEASE,
                    "ingestion_jobs",
                    0,
                    snapshot.expired_leases,
                    repaired,
                )
            )
        return ReconcileReport(tuple(issues))
