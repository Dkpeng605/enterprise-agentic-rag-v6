"""Immutable query planning, retrieval hit, and citation models."""

import math
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from enterprise_rag.domain.common import require_non_empty, require_uuid7


class QueryIntent(StrEnum):
    FACTUAL = "factual"
    COMPARISON = "comparison"
    PROCEDURAL = "procedural"
    SUMMARY = "summary"
    EXPLORATORY = "exploratory"


class QueryMode(StrEnum):
    STANDARD = "standard"
    DEEP = "deep"


def _validate_text_tuple(values: tuple[str, ...], field_name: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} must not contain empty values")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


@dataclass(frozen=True, slots=True)
class QueryScope:
    collection_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    titles: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    doc_types: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        uuid_fields = {
            "collection_ids": self.collection_ids,
            "document_ids": self.document_ids,
        }
        for field_name, values in uuid_fields.items():
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicates")
            for value in values:
                require_uuid7(value, field_name)
        text_fields = {
            "titles": self.titles,
            "organizations": self.organizations,
            "doc_types": self.doc_types,
            "versions": self.versions,
            "sections": self.sections,
        }
        for field_name, text_values in text_fields.items():
            _validate_text_tuple(text_values, field_name)

    def to_dict(self) -> dict[str, object]:
        return {
            "collection_ids": [str(value) for value in self.collection_ids],
            "document_ids": [str(value) for value in self.document_ids],
            "titles": list(self.titles),
            "organizations": list(self.organizations),
            "doc_types": list(self.doc_types),
            "versions": list(self.versions),
            "sections": list(self.sections),
        }


@dataclass(frozen=True, slots=True)
class QueryPlan:
    original_query: str
    rewritten_query: str
    intent: QueryIntent
    sub_queries: tuple[str, ...]
    requirements: tuple[str, ...]
    scope: QueryScope
    language: str
    mode: QueryMode

    def __post_init__(self) -> None:
        require_non_empty(self.original_query, "original_query")
        require_non_empty(self.rewritten_query, "rewritten_query")
        require_non_empty(self.language, "language")
        _validate_text_tuple(self.sub_queries, "sub_queries")
        _validate_text_tuple(self.requirements, "requirements")

    def to_dict(self) -> dict[str, object]:
        return {
            "original_query": self.original_query,
            "rewritten_query": self.rewritten_query,
            "intent": self.intent.value,
            "sub_queries": list(self.sub_queries),
            "requirements": list(self.requirements),
            "scope": self.scope.to_dict(),
            "language": self.language,
            "mode": self.mode.value,
        }


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    leaf_id: str
    root_id: str
    dense_rank: int | None
    sparse_rank: int | None
    fused_score: float
    rerank_score: float | None
    selected: bool

    def __post_init__(self) -> None:
        if not self.leaf_id.startswith("leaf_") or not self.root_id.startswith("root_"):
            raise ValueError("hit IDs must use leaf_ and root_ prefixes")
        if self.dense_rank is None and self.sparse_rank is None:
            raise ValueError("a retrieval hit requires at least one source rank")
        for name in ("dense_rank", "sparse_rank"):
            rank = getattr(self, name)
            if rank is not None and rank <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("fused_score", "rerank_score"):
            score = getattr(self, name)
            if score is not None and not math.isfinite(score):
                raise ValueError(f"{name} must be finite")

    def to_dict(self) -> dict[str, object]:
        return {
            "leaf_id": self.leaf_id,
            "root_id": self.root_id,
            "dense_rank": self.dense_rank,
            "sparse_rank": self.sparse_rank,
            "fused_score": self.fused_score,
            "rerank_score": self.rerank_score,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class Citation:
    id: int
    document_id: UUID
    root_id: str
    chunk_ids: tuple[str, ...]
    source_name: str
    title: str
    page: int | None
    section: str | None
    quote: str
    score: float | None

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("citation id must be positive")
        require_uuid7(self.document_id, "document_id")
        if not self.root_id.startswith("root_"):
            raise ValueError("root_id must use the root_ prefix")
        if not self.chunk_ids or any(not value.startswith("leaf_") for value in self.chunk_ids):
            raise ValueError("chunk_ids must contain at least one leaf ID")
        if len(self.chunk_ids) != len(set(self.chunk_ids)):
            raise ValueError("chunk_ids must not contain duplicates")
        for name in ("source_name", "title", "quote"):
            require_non_empty(getattr(self, name), name)
        if self.page is not None and self.page <= 0:
            raise ValueError("page must be positive")
        if self.section is not None:
            require_non_empty(self.section, "section")
        if self.score is not None and not math.isfinite(self.score):
            raise ValueError("score must be finite")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "document_id": str(self.document_id),
            "root_id": self.root_id,
            "chunk_ids": list(self.chunk_ids),
            "source_name": self.source_name,
            "title": self.title,
            "page": self.page,
            "section": self.section,
            "quote": self.quote,
            "score": self.score,
        }
