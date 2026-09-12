"""Exact deterministic retrieval, citation, and abstention metrics."""

from collections.abc import Sequence

from enterprise_rag.domain.evaluation import EvaluationCase, EvaluationObservation, MetricSet
from enterprise_rag.ports.evaluator import EvaluatorInfo

SUPPORTED_METRICS = frozenset(
    {
        "document_recall_at_5",
        "root_recall_at_5",
        "mrr_at_10",
        "citation_coverage",
        "citation_validity",
        "abstention_accuracy",
    }
)


class DeterministicEvaluator:
    def info(self) -> EvaluatorInfo:
        return EvaluatorInfo("deterministic", "1", False, 0, SUPPORTED_METRICS)

    async def evaluate(
        self, case: EvaluationCase, observation: EvaluationObservation
    ) -> MetricSet:
        return MetricSet(
            _recall(case.expected_document_ids, observation.ranked_document_ids, 5),
            _recall(case.expected_root_ids, observation.ranked_root_ids, 5),
            _mrr(case.expected_root_ids, observation.ranked_root_ids, 10),
            _citation_coverage(case, observation),
            _citation_validity(observation),
            float(observation.abstained == case.must_abstain),
        )


def _recall(gold: Sequence[str], ranked: Sequence[str], top_k: int) -> float | None:
    if not gold:
        return None
    return len(set(gold).intersection(ranked[:top_k])) / len(gold)


def _mrr(gold: Sequence[str], ranked: Sequence[str], top_k: int) -> float | None:
    if not gold:
        return None
    gold_set = set(gold)
    for rank, value in enumerate(ranked[:top_k], start=1):
        if value in gold_set:
            return 1 / rank
    return 0.0


def _citation_coverage(
    case: EvaluationCase, observation: EvaluationObservation
) -> float | None:
    if not case.expected_facts:
        return None
    cited = {fact for citation in observation.citations for fact in citation.facts}
    return len(set(case.expected_facts).intersection(cited)) / len(case.expected_facts)


def _citation_validity(observation: EvaluationObservation) -> float | None:
    if not observation.citations:
        return None if observation.abstained else 0.0
    valid = sum(
        citation.quote in root_text
        for citation in observation.citations
        if (root_text := observation.authorized_roots.get(citation.root_id)) is not None
    )
    return valid / len(observation.citations)
