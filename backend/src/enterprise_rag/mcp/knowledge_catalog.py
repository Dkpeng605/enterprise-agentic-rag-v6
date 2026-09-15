"""Tenant-scoped PostgreSQL fact source for MCP tools and resources."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from enterprise_rag.adapters.database import Database, PostgreSQLContextRepository
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryScope
from enterprise_rag.ports.context import ScopeAuthorization, StoredLeafEvidence, StoredRootEvidence
from enterprise_rag.services.auth import Principal
from enterprise_rag.services.fusion import ReciprocalRankFusion
from enterprise_rag.services.retrieval import DualSearchResult, SearchBranchResult
from enterprise_rag.services.workspace import (
    CollectionSnapshot,
    DocumentDetail,
    PipelineRootSummary,
    WorkspaceService,
)

FILTER_FIELDS = frozenset(
    {
        "collection_ids",
        "document_ids",
        "titles",
        "organizations",
        "doc_types",
        "versions",
        "sections",
    }
)


class CatalogSearch(Protocol):
    async def search(
        self,
        *,
        query: str,
        tenant_id: UUID,
        index_revision: str,
        scope: QueryScope,
    ) -> DualSearchResult: ...

    async def search_dense(
        self,
        *,
        query: str,
        tenant_id: UUID,
        index_revision: str,
        scope: QueryScope,
    ) -> SearchBranchResult: ...

    async def search_sparse(
        self,
        *,
        query: str,
        tenant_id: UUID,
        index_revision: str,
        scope: QueryScope,
    ) -> SearchBranchResult: ...


@dataclass(frozen=True, slots=True)
class _RankedLeaf:
    leaf_id: str
    root_id: str
    dense_rank: int | None
    sparse_rank: int | None
    score: float


class McpKnowledgeCatalog:
    """Serve bounded knowledge views after PostgreSQL authorization and stale checks."""

    def __init__(
        self,
        *,
        database: Database,
        workspace: WorkspaceService,
        search: CatalogSearch,
        fusion: ReciprocalRankFusion,
        index_revision: str,
        max_snippet_chars: int = 1_200,
        max_section_chars: int = 8_000,
    ) -> None:
        if not index_revision.strip():
            raise ValueError("index_revision must not be blank")
        if max_snippet_chars <= 0 or max_section_chars <= 0:
            raise ValueError("MCP content bounds must be positive")
        self._database = database
        self._workspace = workspace
        self._search = search
        self._fusion = fusion
        self._index_revision = index_revision
        self._max_snippet_chars = max_snippet_chars
        self._max_section_chars = max_section_chars

    async def search_documents(
        self,
        principal: Principal,
        *,
        query: str,
        strategy: str,
        top_k: int,
        filters: Mapping[str, object],
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        requested = _query_scope(filters)
        if collection_ids is not None and not set(requested.collection_ids).issubset(
            collection_ids
        ):
            raise AppError(ErrorCode.FORBIDDEN, "The MCP collection scope is insufficient.")
        authorization = _authorization(principal, collection_ids)
        async with self._database.session() as session:
            resolved = await PostgreSQLContextRepository(session).resolve_scope(
                authorization, requested
            )
        search_scope = QueryScope(
            document_ids=resolved.document_ids,
            sections=resolved.sections,
        )
        if not resolved.document_ids:
            return {
                "items": [],
                "strategy": strategy,
                "diagnostics": {"candidate_count": 0, "returned_count": 0},
            }

        ranked, diagnostics = await self._search_ranked(
            query=query,
            strategy=strategy,
            top_k=top_k,
            tenant_id=principal.tenant_id,
            scope=search_scope,
        )
        async with self._database.session() as session:
            repository = PostgreSQLContextRepository(session)
            leaves = await repository.load_leaves(
                resolved, tuple(item.leaf_id for item in ranked)
            )
            roots = await repository.load_roots(
                resolved, tuple(dict.fromkeys(item.root_id for item in ranked))
            )
        leaf_by_id = {item.leaf_id: item for item in leaves}
        root_by_id = {item.root_id: item for item in roots}
        items = [
            self._search_item(item, leaf_by_id[item.leaf_id], root_by_id[item.root_id])
            for item in ranked
            if item.leaf_id in leaf_by_id
            and item.root_id in root_by_id
            and _consistent(item, leaf_by_id[item.leaf_id], root_by_id[item.root_id])
        ][:top_k]
        return {
            "items": items,
            "strategy": strategy,
            "diagnostics": {
                **diagnostics,
                "candidate_count": len(ranked),
                "returned_count": len(items),
                "stale_or_unauthorized_dropped": len(ranked) - len(items),
            },
        }

    async def list_collections(
        self, principal: Principal, *, collection_ids: tuple[UUID, ...] | None
    ) -> Sequence[Mapping[str, object]]:
        items = await self._workspace.list_collections(principal.tenant_id)
        allowed = frozenset(collection_ids) if collection_ids is not None else None
        return tuple(
            _collection(item)
            for item in items
            if allowed is None or item.id in allowed
        )

    async def get_document_summary(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        detail = await self._authorized_document(principal, document_id, collection_ids)
        return _document(detail)

    async def list_document_sections(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        await self._authorized_document(principal, document_id, collection_ids)
        parsed_cursor = _cursor(cursor)
        pipeline = await self._workspace.inspect_document_pipeline(
            principal.tenant_id, document_id, cursor=parsed_cursor, limit=limit
        )
        return {
            "document_id": str(document_id),
            "version_id": str(pipeline.version_id),
            "items": [_section_summary(item) for item in pipeline.roots],
            "next_cursor": (
                str(pipeline.next_cursor) if pipeline.next_cursor is not None else None
            ),
        }

    async def verify_answer(
        self,
        principal: Principal,
        *,
        answer: str,
        citations: Sequence[Mapping[str, object]],
        question: str | None,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        # The submitted answer and question are deliberately neither persisted nor echoed.
        del answer, question
        parsed = [_citation(item, index) for index, item in enumerate(citations, start=1)]
        authorization = _authorization(principal, collection_ids)
        async with self._database.session() as session:
            repository = PostgreSQLContextRepository(session)
            resolved = await repository.resolve_scope(authorization, QueryScope())
            roots = await repository.load_roots(
                resolved,
                tuple(item.root_id for item in parsed if item.root_id is not None),
            )
            leaves = await repository.load_leaves(
                resolved,
                tuple(leaf for item in parsed for leaf in item.leaf_ids),
            )
        root_by_id = {item.root_id: item for item in roots}
        leaf_by_id = {item.leaf_id: item for item in leaves}
        issues: list[dict[str, object]] = []
        verified = 0
        seen_ids: set[int] = set()
        for item in parsed:
            before = len(issues)
            if item.invalid:
                _issue(issues, item.index, "invalid_citation")
                continue
            if item.citation_id in seen_ids:
                _issue(issues, item.index, "duplicate_citation")
            seen_ids.add(item.citation_id)
            root = root_by_id.get(item.root_id or "")
            if root is None:
                _issue(issues, item.index, "unknown_root")
            else:
                if item.document_id is not None and item.document_id != root.document_id:
                    _issue(issues, item.index, "document_mismatch")
                if not item.leaf_ids or any(
                    leaf_id not in leaf_by_id
                    or leaf_by_id[leaf_id].root_id != root.root_id
                    for leaf_id in item.leaf_ids
                ):
                    _issue(issues, item.index, "invalid_leaf")
                if item.quote is None or item.quote not in root.text:
                    _issue(issues, item.index, "quote_not_found")
            if len(issues) == before:
                verified += 1
        return {
            "valid": not issues,
            "citation_count": len(parsed),
            "verified_count": verified,
            "issues": issues,
        }

    async def get_collection_resource(
        self,
        principal: Principal,
        collection_id: UUID,
        *,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        _require_collection(collection_id, collection_ids)
        item = await self._workspace.get_collection(principal.tenant_id, collection_id)
        return _collection(item)

    async def get_section_resource(
        self,
        principal: Principal,
        document_id: UUID,
        root_id: str,
        *,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        await self._authorized_document(principal, document_id, collection_ids)
        root = await self._workspace.inspect_pipeline_root(
            principal.tenant_id, document_id, root_id
        )
        text = root.clean_text[: self._max_section_chars]
        return {
            "document_id": str(document_id),
            "root_id": root.summary.id,
            "ordinal": root.summary.ordinal,
            "kind": root.summary.kind,
            "source_locator": dict(root.summary.source_locator),
            "text": text,
            "truncated": len(root.clean_text) > len(text),
        }

    async def _authorized_document(
        self,
        principal: Principal,
        document_id: UUID,
        collection_ids: tuple[UUID, ...] | None,
    ) -> DocumentDetail:
        detail = await self._workspace.get_document(principal.tenant_id, document_id)
        _require_collection(detail.summary.collection_id, collection_ids)
        if detail.summary.status != "ready":
            raise AppError(ErrorCode.NOT_FOUND, "The requested document is unavailable.")
        return detail

    async def _search_ranked(
        self,
        *,
        query: str,
        strategy: str,
        top_k: int,
        tenant_id: UUID,
        scope: QueryScope,
    ) -> tuple[list[_RankedLeaf], dict[str, object]]:
        if strategy == "dense":
            branch = await self._search.search_dense(
                query=query,
                tenant_id=tenant_id,
                index_revision=self._index_revision,
                scope=scope,
            )
            return _rank_branch(branch, top_k), {"ranked_list_count": 1}
        if strategy == "sparse":
            branch = await self._search.search_sparse(
                query=query,
                tenant_id=tenant_id,
                index_revision=self._index_revision,
                scope=scope,
            )
            return _rank_branch(branch, top_k), {"ranked_list_count": 1}
        if strategy != "hybrid":
            raise AppError(ErrorCode.VALIDATION_ERROR, "The search strategy is invalid.")
        result = await self._search.search(
            query=query,
            tenant_id=tenant_id,
            index_revision=self._index_revision,
            scope=scope,
        )
        fused = self._fusion.fuse((result,))
        ranked = [
            _RankedLeaf(
                item.leaf_id,
                item.root_id,
                item.dense_rank,
                item.sparse_rank,
                item.fused_score,
            )
            for item in fused.hits[:top_k]
        ]
        diagnostic = fused.diagnostic
        return ranked, {
            "ranked_list_count": diagnostic.ranked_list_count,
            "input_hit_count": diagnostic.input_hit_count,
            "unique_leaf_count": diagnostic.unique_leaf_count,
            "root_quota_dropped": diagnostic.root_quota_dropped,
            "top_k_dropped": diagnostic.top_k_dropped,
        }

    def _search_item(
        self,
        ranked: _RankedLeaf,
        leaf: StoredLeafEvidence,
        root: StoredRootEvidence,
    ) -> dict[str, object]:
        locator = dict(root.source_locator)
        return {
            "leaf_id": leaf.leaf_id,
            "root_id": root.root_id,
            "document_id": str(root.document_id),
            "version_id": str(root.version_id),
            "title": root.title,
            "organization": root.organization,
            "source_name": root.source_name,
            "media_type": root.media_type,
            "source_locator": locator,
            "snippet": leaf.retrieval_text[: self._max_snippet_chars],
            "snippet_truncated": len(leaf.retrieval_text) > self._max_snippet_chars,
            "dense_rank": ranked.dense_rank,
            "sparse_rank": ranked.sparse_rank,
            "score": ranked.score,
        }


@dataclass(frozen=True, slots=True)
class _SubmittedCitation:
    index: int
    citation_id: int
    document_id: UUID | None
    root_id: str | None
    leaf_ids: tuple[str, ...]
    quote: str | None
    invalid: bool


def _authorization(
    principal: Principal, collection_ids: tuple[UUID, ...] | None
) -> ScopeAuthorization:
    return ScopeAuthorization(
        principal.tenant_id,
        collection_ids is None,
        collection_ids=collection_ids or (),
    )


def _query_scope(filters: Mapping[str, object]) -> QueryScope:
    unknown = set(filters) - FILTER_FIELDS
    if unknown:
        raise AppError(ErrorCode.VALIDATION_ERROR, "The search filters are invalid.")
    try:
        return QueryScope(
            collection_ids=tuple(UUID(value) for value in _strings(filters, "collection_ids")),
            document_ids=tuple(UUID(value) for value in _strings(filters, "document_ids")),
            titles=_strings(filters, "titles"),
            organizations=_strings(filters, "organizations"),
            doc_types=_strings(filters, "doc_types"),
            versions=_strings(filters, "versions"),
            sections=_strings(filters, "sections"),
        )
    except ValueError as error:
        raise AppError(ErrorCode.VALIDATION_ERROR, "The search filters are invalid.") from error


def _strings(filters: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = filters.get(key, ())
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(f"{key} must be an array")
    if len(value) > 100 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 500 for item in value
    ):
        raise ValueError(f"{key} values are invalid")
    return tuple(item.strip() for item in value)


def _rank_branch(branch: SearchBranchResult, top_k: int) -> list[_RankedLeaf]:
    dense = branch.method.value == "dense"
    return [
        _RankedLeaf(
            hit.leaf_id,
            hit.root_id,
            rank if dense else None,
            None if dense else rank,
            hit.score,
        )
        for rank, hit in enumerate(branch.hits[:top_k], start=1)
    ]


def _consistent(
    ranked: _RankedLeaf, leaf: StoredLeafEvidence, root: StoredRootEvidence
) -> bool:
    return (
        leaf.root_id == ranked.root_id
        and leaf.document_id == root.document_id
        and leaf.version_id == root.version_id
    )


def _collection(item: CollectionSnapshot) -> dict[str, object]:
    return {
        "id": str(item.id),
        "name": item.name,
        "description": item.description,
        "visibility": item.visibility,
        "document_count": item.document_count,
        "ready_document_count": item.ready_document_count,
        "updated_at": item.updated_at.isoformat(),
    }


def _document(detail: DocumentDetail) -> dict[str, object]:
    item = detail.summary
    return {
        "id": str(item.id),
        "collection_id": str(item.collection_id),
        "title": item.title,
        "organization": item.organization,
        "status": item.status,
        "visibility": item.visibility,
        "version_id": str(item.version_id),
        "source_name": item.source_name,
        "media_type": item.media_type,
        "size_bytes": item.size_bytes,
        "root_count": detail.root_count,
        "leaf_count": detail.leaf_count,
        "updated_at": item.updated_at.isoformat(),
    }


def _section_summary(item: PipelineRootSummary) -> dict[str, object]:
    return {
        "id": item.id,
        "ordinal": item.ordinal,
        "kind": item.kind,
        "source_locator": dict(item.source_locator),
        "clean_chars": item.clean_chars,
        "changed": item.changed,
        "leaf_count": item.leaf_count,
    }


def _cursor(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise AppError(ErrorCode.VALIDATION_ERROR, "The Root cursor is invalid.") from error
    if parsed < 0 or str(parsed) != value:
        raise AppError(ErrorCode.VALIDATION_ERROR, "The Root cursor is invalid.")
    return parsed


def _require_collection(
    collection_id: UUID, collection_ids: tuple[UUID, ...] | None
) -> None:
    if collection_ids is not None and collection_id not in collection_ids:
        raise AppError(ErrorCode.FORBIDDEN, "The MCP collection scope is insufficient.")


def _citation(value: Mapping[str, object], index: int) -> _SubmittedCitation:
    try:
        citation_id = value.get("id", index)
        if not isinstance(citation_id, int) or isinstance(citation_id, bool) or citation_id <= 0:
            raise ValueError("citation id is invalid")
        raw_document_id = value.get("document_id")
        document_id = UUID(raw_document_id) if isinstance(raw_document_id, str) else None
        root_id = value.get("root_id")
        if not isinstance(root_id, str) or not root_id.startswith("root_"):
            raise ValueError("root id is invalid")
        raw_leaf_ids = value.get("chunk_ids", value.get("leaf_ids", ()))
        if not isinstance(raw_leaf_ids, Sequence) or isinstance(raw_leaf_ids, str | bytes):
            raise ValueError("leaf ids are invalid")
        leaf_ids = tuple(raw_leaf_ids)
        if any(not isinstance(item, str) or not item.startswith("leaf_") for item in leaf_ids):
            raise ValueError("leaf ids are invalid")
        quote = value.get("quote")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 4_000:
            raise ValueError("quote is invalid")
        return _SubmittedCitation(
            index,
            citation_id,
            document_id,
            root_id,
            leaf_ids,
            quote,
            False,
        )
    except (TypeError, ValueError):
        return _SubmittedCitation(index, index, None, None, (), None, True)


def _issue(items: list[dict[str, object]], index: int, code: str) -> None:
    item = {"citation_index": index, "code": code}
    if item not in items:
        items.append(item)
