"""PostgreSQL authorization, metadata scope, and active Root recovery."""

from collections.abc import Sequence

from sqlalchemy import String, cast, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    DocumentVersionModel,
    LeafModel,
    RootModel,
)
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryScope
from enterprise_rag.ports.context import (
    ResolvedQueryScope,
    ScopeAuthorization,
    StoredLeafEvidence,
    StoredRootEvidence,
)


class ScopeConflictError(AppError):
    """Explicit query filters cannot be satisfied inside the authorized scope."""


class PostgreSQLContextRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve_scope(
        self, authorization: ScopeAuthorization, requested: QueryScope
    ) -> ResolvedQueryScope:
        statement = (
            select(DocumentModel.id, DocumentModel.collection_id)
            .join(CollectionModel, CollectionModel.id == DocumentModel.collection_id)
            .join(
                DocumentVersionModel,
                DocumentVersionModel.id == DocumentModel.active_version_id,
            )
            .where(
                DocumentModel.tenant_id == authorization.tenant_id,
                DocumentModel.status == "ready",
                DocumentVersionModel.status == "indexed",
                CollectionModel.tenant_id == authorization.tenant_id,
                CollectionModel.status == "active",
            )
        )
        if not authorization.full_tenant_access:
            permission_clauses = []
            if authorization.collection_ids:
                permission_clauses.append(
                    DocumentModel.collection_id.in_(authorization.collection_ids)
                )
            if authorization.document_ids:
                permission_clauses.append(DocumentModel.id.in_(authorization.document_ids))
            statement = statement.where(or_(*permission_clauses) if permission_clauses else false())
        if requested.collection_ids:
            statement = statement.where(DocumentModel.collection_id.in_(requested.collection_ids))
        if requested.document_ids:
            statement = statement.where(DocumentModel.id.in_(requested.document_ids))
        if requested.titles:
            statement = statement.where(
                func.lower(DocumentModel.title).in_(_casefolded(requested.titles))
            )
        if requested.organizations:
            statement = statement.where(
                func.lower(DocumentModel.organization).in_(_casefolded(requested.organizations))
            )
        if requested.doc_types:
            statement = statement.where(
                func.lower(DocumentVersionModel.media_type).in_(_casefolded(requested.doc_types))
            )
        if requested.versions:
            statement = statement.where(
                func.lower(cast(DocumentVersionModel.id, String)).in_(
                    _casefolded(requested.versions)
                )
            )

        rows = (await self.session.execute(statement.order_by(DocumentModel.id))).all()
        document_ids = tuple(row.id for row in rows)
        if _has_explicit_scope(requested) and not document_ids:
            raise _scope_conflict("The explicit query scope has no authorized ready documents.")
        if requested.document_ids and not set(requested.document_ids).issubset(document_ids):
            raise _scope_conflict("The explicit query scope contains an unavailable document.")
        if requested.collection_ids and not set(requested.collection_ids).issubset(
            row.collection_id for row in rows
        ):
            raise _scope_conflict("The explicit query scope contains an unavailable collection.")
        sections = tuple(value.casefold() for value in requested.sections)
        if sections:
            section_exists = await self.session.scalar(
                select(RootModel.id)
                .join(DocumentModel, DocumentModel.id == RootModel.document_id)
                .where(
                    RootModel.tenant_id == authorization.tenant_id,
                    RootModel.document_id.in_(document_ids),
                    RootModel.version_id == DocumentModel.active_version_id,
                    DocumentModel.status == "ready",
                    func.lower(RootModel.source_locator["section"].as_string()).in_(sections),
                )
                .limit(1)
            )
            if section_exists is None:
                raise _scope_conflict("The explicit section scope has no authorized content.")
        return ResolvedQueryScope(authorization.tenant_id, document_ids, sections)

    async def load_leaves(
        self, scope: ResolvedQueryScope, leaf_ids: Sequence[str]
    ) -> tuple[StoredLeafEvidence, ...]:
        if not scope.document_ids or not leaf_ids:
            return ()
        statement = (
            select(LeafModel)
            .join(RootModel, RootModel.id == LeafModel.root_id)
            .join(DocumentModel, DocumentModel.id == LeafModel.document_id)
            .join(DocumentVersionModel, DocumentVersionModel.id == LeafModel.version_id)
            .join(CollectionModel, CollectionModel.id == DocumentModel.collection_id)
            .where(
                LeafModel.id.in_(tuple(leaf_ids)),
                LeafModel.tenant_id == scope.tenant_id,
                LeafModel.document_id.in_(scope.document_ids),
                RootModel.tenant_id == scope.tenant_id,
                RootModel.version_id == LeafModel.version_id,
                RootModel.document_id == LeafModel.document_id,
                DocumentModel.tenant_id == scope.tenant_id,
                DocumentModel.status == "ready",
                DocumentModel.active_version_id == LeafModel.version_id,
                DocumentVersionModel.status == "indexed",
                CollectionModel.tenant_id == scope.tenant_id,
                CollectionModel.status == "active",
            )
        )
        if scope.sections:
            statement = statement.where(
                func.lower(RootModel.source_locator["section"].as_string()).in_(scope.sections)
            )
        rows = (await self.session.scalars(statement)).all()
        return tuple(
            StoredLeafEvidence(
                row.id,
                row.root_id,
                row.document_id,
                row.version_id,
                row.retrieval_text,
            )
            for row in rows
        )

    async def load_roots(
        self, scope: ResolvedQueryScope, root_ids: Sequence[str]
    ) -> tuple[StoredRootEvidence, ...]:
        if not scope.document_ids or not root_ids:
            return ()
        statement = (
            select(RootModel, DocumentModel, DocumentVersionModel)
            .join(DocumentModel, DocumentModel.id == RootModel.document_id)
            .join(DocumentVersionModel, DocumentVersionModel.id == RootModel.version_id)
            .join(CollectionModel, CollectionModel.id == DocumentModel.collection_id)
            .where(
                RootModel.id.in_(tuple(root_ids)),
                RootModel.tenant_id == scope.tenant_id,
                RootModel.document_id.in_(scope.document_ids),
                DocumentModel.tenant_id == scope.tenant_id,
                DocumentModel.status == "ready",
                DocumentModel.active_version_id == RootModel.version_id,
                DocumentVersionModel.document_id == DocumentModel.id,
                DocumentVersionModel.status == "indexed",
                CollectionModel.tenant_id == scope.tenant_id,
                CollectionModel.status == "active",
            )
        )
        if scope.sections:
            statement = statement.where(
                func.lower(RootModel.source_locator["section"].as_string()).in_(scope.sections)
            )
        rows = (await self.session.execute(statement)).all()
        return tuple(
            StoredRootEvidence(
                root.id,
                document.id,
                version.id,
                version.source_name,
                document.title,
                document.organization,
                version.media_type,
                root.clean_text,
                root.source_locator,
            )
            for root, document, version in rows
        )


def _casefolded(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(value.casefold() for value in values)


def _has_explicit_scope(scope: QueryScope) -> bool:
    return any(
        (
            scope.collection_ids,
            scope.document_ids,
            scope.titles,
            scope.organizations,
            scope.doc_types,
            scope.versions,
            scope.sections,
        )
    )


def _scope_conflict(message: str) -> ScopeConflictError:
    return ScopeConflictError(ErrorCode.QUERY_SCOPE_CONFLICT, message)
