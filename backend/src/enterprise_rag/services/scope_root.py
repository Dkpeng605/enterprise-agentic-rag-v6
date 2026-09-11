"""Prepare authorized Leaf candidates and recover bounded Root context."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from enterprise_rag.domain.retrieval import QueryScope, RetrievalHit
from enterprise_rag.ports.context import ContextRepository, ResolvedQueryScope, ScopeAuthorization
from enterprise_rag.services.reranking import RerankItem


@dataclass(frozen=True, slots=True)
class PreparedRerankCandidates:
    scope: ResolvedQueryScope
    items: tuple[RerankItem, ...]
    rejected_count: int


@dataclass(frozen=True, slots=True)
class RootContext:
    root_id: str
    document_id: UUID
    version_id: UUID
    source_name: str
    title: str
    organization: str | None
    media_type: str
    source_locator: Mapping[str, object]
    text: str
    leaf_ids: tuple[str, ...]
    score: float
    truncated: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_locator", MappingProxyType(dict(self.source_locator)))


@dataclass(frozen=True, slots=True)
class RecoveredContext:
    roots: tuple[RootContext, ...]
    used_chars: int
    rejected_count: int
    truncated_count: int


class ScopeRootService:
    def __init__(self, repository: ContextRepository, *, max_parent_chars: int = 18_000) -> None:
        if max_parent_chars <= 0:
            raise ValueError("max_parent_chars must be positive")
        self._repository = repository
        self._max_parent_chars = max_parent_chars

    async def resolve_scope(
        self, authorization: ScopeAuthorization, requested_scope: QueryScope
    ) -> ResolvedQueryScope:
        return await self._repository.resolve_scope(authorization, requested_scope)

    async def prepare_candidates(
        self,
        authorization: ScopeAuthorization,
        requested_scope: QueryScope,
        hits: Sequence[RetrievalHit],
    ) -> PreparedRerankCandidates:
        _validate_hits(hits)
        scope = await self.resolve_scope(authorization, requested_scope)
        stored = await self._repository.load_leaves(scope, [hit.leaf_id for hit in hits])
        by_id = {item.leaf_id: item for item in stored}
        items = tuple(
            RerankItem(hit, by_id[hit.leaf_id].retrieval_text)
            for hit in hits
            if hit.leaf_id in by_id and by_id[hit.leaf_id].root_id == hit.root_id
        )
        return PreparedRerankCandidates(scope, items, len(hits) - len(items))

    async def recover(
        self,
        scope: ResolvedQueryScope,
        selected_hits: Sequence[RetrievalHit],
    ) -> RecoveredContext:
        _validate_hits(selected_hits)
        if any(not hit.selected for hit in selected_hits):
            raise ValueError("Root recovery accepts only selected hits")
        unique_root_ids = tuple(dict.fromkeys(hit.root_id for hit in selected_hits))
        stored = await self._repository.load_roots(scope, unique_root_ids)
        by_id = {item.root_id: item for item in stored}
        leaf_ids: dict[str, list[str]] = {}
        scores: dict[str, float] = {}
        for hit in selected_hits:
            leaf_ids.setdefault(hit.root_id, []).append(hit.leaf_id)
            score = hit.rerank_score if hit.rerank_score is not None else hit.fused_score
            scores[hit.root_id] = max(scores.get(hit.root_id, score), score)

        roots: list[RootContext] = []
        used_chars = 0
        truncated_count = 0
        for root_id in unique_root_ids:
            root = by_id.get(root_id)
            if root is None:
                continue
            remaining = self._max_parent_chars - used_chars
            if remaining <= 0:
                break
            text = root.text[:remaining]
            truncated = len(text) < len(root.text)
            if truncated:
                truncated_count += 1
            roots.append(
                RootContext(
                    root.root_id,
                    root.document_id,
                    root.version_id,
                    root.source_name,
                    root.title,
                    root.organization,
                    root.media_type,
                    root.source_locator,
                    text,
                    tuple(leaf_ids[root_id]),
                    scores[root_id],
                    truncated,
                )
            )
            used_chars += len(text)
        return RecoveredContext(
            tuple(roots),
            used_chars,
            len(unique_root_ids) - len(roots),
            truncated_count,
        )


def _validate_hits(hits: Sequence[RetrievalHit]) -> None:
    ids = [hit.leaf_id for hit in hits]
    if len(ids) != len(set(ids)):
        raise ValueError("retrieval hits must have unique Leaf IDs")
