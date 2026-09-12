"""Immutable, transport-neutral inputs and outputs for deterministic evaluation."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.domain.retrieval import QueryMode


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    language: str
    category: str
    question: str
    mode: QueryMode
    allowed_collections: tuple[str, ...]
    expected_document_ids: tuple[str, ...]
    expected_root_ids: tuple[str, ...]
    expected_facts: tuple[str, ...]
    must_abstain: bool
    max_recovery_rounds: int
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "category", "question"):
            require_non_empty(getattr(self, name), name)
        if self.language not in {"zh", "en"}:
            raise ValueError("evaluation language must be zh or en")
        if not 0 <= self.max_recovery_rounds <= 10:
            raise ValueError("max_recovery_rounds must be between zero and ten")
        for name in (
            "allowed_collections",
            "expected_document_ids",
            "expected_root_ids",
            "expected_facts",
            "tags",
        ):
            _require_unique_non_empty(getattr(self, name), name)
        if self.must_abstain and self.expected_facts:
            raise ValueError("abstention cases must not declare expected facts")
        if not self.must_abstain and not self.expected_facts:
            raise ValueError("answerable cases must declare expected facts")


@dataclass(frozen=True, slots=True)
class EvaluationCitation:
    root_id: str
    quote: str
    facts: tuple[str, ...]

    def __post_init__(self) -> None:
        require_non_empty(self.root_id, "citation root_id")
        require_non_empty(self.quote, "citation quote")
        _require_unique_non_empty(self.facts, "citation facts")


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    ranked_document_ids: tuple[str, ...]
    ranked_root_ids: tuple[str, ...]
    citations: tuple[EvaluationCitation, ...]
    authorized_roots: Mapping[str, str]
    abstained: bool

    def __post_init__(self) -> None:
        _require_unique_non_empty(self.ranked_document_ids, "ranked_document_ids")
        _require_unique_non_empty(self.ranked_root_ids, "ranked_root_ids")
        for root_id, text in self.authorized_roots.items():
            require_non_empty(root_id, "authorized root ID")
            require_non_empty(text, "authorized root text")
        object.__setattr__(
            self,
            "authorized_roots",
            MappingProxyType(dict(self.authorized_roots)),
        )


@dataclass(frozen=True, slots=True)
class MetricSet:
    document_recall_at_5: float | None
    root_recall_at_5: float | None
    mrr_at_10: float | None
    citation_coverage: float | None
    citation_validity: float | None
    abstention_accuracy: float

    def __post_init__(self) -> None:
        for name in (
            "document_recall_at_5",
            "root_recall_at_5",
            "mrr_at_10",
            "citation_coverage",
            "citation_validity",
            "abstention_accuracy",
        ):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")

    def to_dict(self) -> dict[str, float | None]:
        return {
            "document_recall_at_5": self.document_recall_at_5,
            "root_recall_at_5": self.root_recall_at_5,
            "mrr_at_10": self.mrr_at_10,
            "citation_coverage": self.citation_coverage,
            "citation_validity": self.citation_validity,
            "abstention_accuracy": self.abstention_accuracy,
        }


def _require_unique_non_empty(values: tuple[str, ...], name: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError(f"{name} must not contain blank values")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")
