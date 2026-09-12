"""Immutable, versioned fixtures used by the evaluation runner."""

from dataclasses import dataclass

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.domain.evaluation import EvaluationCase


@dataclass(frozen=True, slots=True)
class GoldenRoot:
    id: str
    text: str

    def __post_init__(self) -> None:
        require_non_empty(self.id, "golden root id")
        require_non_empty(self.text, "golden root text")


@dataclass(frozen=True, slots=True)
class GoldenDocument:
    id: str
    collection_id: str
    title: str
    roots: tuple[GoldenRoot, ...]

    def __post_init__(self) -> None:
        require_non_empty(self.id, "golden document id")
        require_non_empty(self.collection_id, "golden collection id")
        require_non_empty(self.title, "golden document title")
        if not self.roots:
            raise ValueError("a golden document must contain at least one root")
        root_ids = [root.id for root in self.roots]
        if len(root_ids) != len(set(root_ids)):
            raise ValueError("golden root IDs must be unique within a document")


@dataclass(frozen=True, slots=True)
class GoldenSet:
    schema_version: str
    revision: str
    cases: tuple[EvaluationCase, ...]
    documents: tuple[GoldenDocument, ...]

    def __post_init__(self) -> None:
        require_non_empty(self.schema_version, "golden schema version")
        require_non_empty(self.revision, "golden revision")
        if not self.cases or not self.documents:
            raise ValueError("a golden set requires cases and documents")

    @property
    def roots(self) -> tuple[GoldenRoot, ...]:
        return tuple(root for document in self.documents for root in document.roots)
