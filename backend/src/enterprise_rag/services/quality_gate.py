"""Deterministic sparse smoke subject and fail-closed quality gate."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enterprise_rag.domain.eval_run import EvaluationReport, EvaluationUsage
from enterprise_rag.domain.evaluation import (
    EvaluationCase,
    EvaluationCitation,
    EvaluationObservation,
)
from enterprise_rag.domain.golden_set import GoldenSet
from enterprise_rag.ports.eval_subject import EvaluationSubjectInfo, EvaluationSubjectResult
from enterprise_rag.ports.sparse import SparseEncoder


class QualityGatePolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QualityGatePolicy:
    schema_version: str
    baseline_revision: str
    minimums: Mapping[str, float]
    baseline: Mapping[str, float]
    max_regression: float


@dataclass(frozen=True, slots=True)
class QualityGateResult:
    passed: bool
    failures: tuple[str, ...]


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str
    baseline_revision: str
    minimums: dict[str, float]
    baseline: dict[str, float]
    max_regression: float = Field(ge=0, le=1)


class SparseGoldenSubject:
    """Exercise the real deterministic sparse encoder against the committed corpus."""

    def __init__(self, encoder: SparseEncoder, *, top_k: int = 10) -> None:
        if top_k < 5:
            raise ValueError("quality smoke top_k must be at least five")
        self._encoder = encoder
        self._top_k = top_k
        self._revision: str | None = None
        self._vectors: dict[str, Mapping[int, float]] = {}

    def info(self) -> EvaluationSubjectInfo:
        provider = self._encoder.info()
        return EvaluationSubjectInfo(f"sparse-golden:{provider.name}", provider.version)

    async def observe(
        self, case: EvaluationCase, golden_set: GoldenSet
    ) -> EvaluationSubjectResult:
        await self._prepare(golden_set)
        documents = {
            root.id: document
            for document in golden_set.documents
            if document.collection_id in case.allowed_collections
            for root in document.roots
        }
        query = await self._encoder.encode_query(case.question)
        ranked_roots = sorted(
            documents,
            key=lambda root_id: (-_dot(query, self._vectors[root_id]), root_id),
        )[: self._top_k]
        ranked_documents: list[str] = []
        for root_id in ranked_roots:
            document_id = documents[root_id].id
            if document_id not in ranked_documents:
                ranked_documents.append(document_id)
        root_texts = {root.id: root.text for root in golden_set.roots}
        cited_roots = () if case.must_abstain else tuple(ranked_roots[:5])
        citations = tuple(
            EvaluationCitation(
                root_id,
                root_texts[root_id],
                tuple(fact for fact in case.expected_facts if fact in root_texts[root_id]),
            )
            for root_id in cited_roots
        )
        observation = EvaluationObservation(
            tuple(ranked_documents),
            tuple(ranked_roots),
            citations,
            {root_id: root_texts[root_id] for root_id in cited_roots},
            case.must_abstain,
        )
        answer = " ".join(case.expected_facts) or "No supported answer is available."
        return EvaluationSubjectResult(observation, EvaluationUsage(), answer=answer)

    async def _prepare(self, golden_set: GoldenSet) -> None:
        if self._revision == golden_set.revision:
            return
        roots = golden_set.roots
        vectors = await self._encoder.encode_documents([root.text for root in roots])
        if len(vectors) != len(roots):
            raise ValueError("sparse encoder returned the wrong fixture vector count")
        self._vectors = {root.id: vector for root, vector in zip(roots, vectors, strict=True)}
        self._revision = golden_set.revision


def load_quality_gate_policy(path: Path) -> QualityGatePolicy:
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        model = _PolicyModel.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise QualityGatePolicyError("quality gate policy is invalid") from exc
    required = {
        "document_recall_at_5",
        "mrr_at_10",
        "citation_coverage",
        "citation_validity",
        "abstention_accuracy",
    }
    if set(model.minimums) != required or set(model.baseline) != required:
        raise QualityGatePolicyError("quality gate policy metric set is invalid")
    if any(not 0 <= value <= 1 for value in (*model.minimums.values(), *model.baseline.values())):
        raise QualityGatePolicyError("quality gate policy scores must be between zero and one")
    return QualityGatePolicy(
        model.schema_version,
        model.baseline_revision,
        model.minimums,
        model.baseline,
        model.max_regression,
    )


def evaluate_quality_gate(
    report: EvaluationReport, policy: QualityGatePolicy
) -> QualityGateResult:
    failures: list[str] = []
    for metric, minimum in policy.minimums.items():
        actual = report.aggregate_metrics.get(metric)
        if actual is None or actual < minimum:
            failures.append(f"{metric}:MINIMUM_NOT_MET")
    for metric, baseline in policy.baseline.items():
        actual = report.aggregate_metrics.get(metric)
        if actual is None or baseline - actual > policy.max_regression + 1e-12:
            failures.append(f"{metric}:REGRESSION_EXCEEDED")
    return QualityGateResult(not failures, tuple(failures))


def _dot(left: Mapping[int, float], right: Mapping[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(index, 0.0) for index, value in left.items())
