"""Hand-calculated acceptance fixtures for deterministic evaluation metrics."""

import pytest

from enterprise_rag.domain import (
    EvaluationCase,
    EvaluationCitation,
    EvaluationObservation,
    QueryMode,
)
from enterprise_rag.ports import Evaluator
from enterprise_rag.services import DeterministicEvaluator


def answerable_case() -> EvaluationCase:
    return EvaluationCase(
        "golden_compare_001",
        "zh",
        "comparison",
        "比较 A 与 B 的保留期限。",
        QueryMode.DEEP,
        ("demo-policy",),
        ("doc_a", "doc_b"),
        ("root_a", "root_b"),
        ("A 保留 30 天", "B 保留 90 天"),
        False,
        2,
        ("multi_doc", "bilingual"),
    )


@pytest.mark.anyio
async def test_hand_calculated_recall_mrr_citation_and_abstention() -> None:
    evaluator = DeterministicEvaluator()
    observation = EvaluationObservation(
        ("doc_a", "irrelevant_1", "irrelevant_2", "irrelevant_3", "irrelevant_4"),
        ("irrelevant_root", "root_b", "other_root", "root_a"),
        (
            EvaluationCitation("root_a", "A 保留 30 天", ("A 保留 30 天",)),
            EvaluationCitation("root_b", "B 保留 60 天", ("unknown fact",)),
        ),
        {
            "root_a": "政策明确规定 A 保留 30 天。",
            "root_b": "政策明确规定 B 保留 90 天。",
        },
        False,
    )

    metrics = await evaluator.evaluate(answerable_case(), observation)

    assert metrics.to_dict() == {
        "document_recall_at_5": 0.5,
        "root_recall_at_5": 1.0,
        "mrr_at_10": 0.5,
        "citation_coverage": 0.5,
        "citation_validity": 0.5,
        "abstention_accuracy": 1.0,
    }


@pytest.mark.anyio
async def test_missing_gold_and_uncited_answer_have_explicit_semantics() -> None:
    evaluator = DeterministicEvaluator()
    unanswerable = EvaluationCase(
        "golden_abstain_001",
        "en",
        "unanswerable",
        "What is not present?",
        QueryMode.STANDARD,
        (),
        (),
        (),
        (),
        True,
        0,
        ("abstention",),
    )

    correct_abstention = await evaluator.evaluate(
        unanswerable,
        EvaluationObservation((), (), (), {}, True),
    )
    uncited_answer = await evaluator.evaluate(
        answerable_case(),
        EvaluationObservation(("doc_a",), ("root_a",), (), {"root_a": "text"}, False),
    )

    assert correct_abstention.document_recall_at_5 is None
    assert correct_abstention.root_recall_at_5 is None
    assert correct_abstention.mrr_at_10 is None
    assert correct_abstention.citation_coverage is None
    assert correct_abstention.citation_validity is None
    assert correct_abstention.abstention_accuracy == 1.0
    assert uncited_answer.citation_coverage == 0.0
    assert uncited_answer.citation_validity == 0.0


def test_evaluator_declares_zero_llm_cost_and_rejects_ambiguous_cases() -> None:
    evaluator = DeterministicEvaluator()

    assert isinstance(evaluator, Evaluator)
    assert evaluator.info().requires_llm is False
    assert evaluator.info().estimated_llm_calls_per_case == 0
    assert evaluator.info().supported_metrics == frozenset(
        {
            "document_recall_at_5",
            "root_recall_at_5",
            "mrr_at_10",
            "citation_coverage",
            "citation_validity",
            "abstention_accuracy",
        }
    )
    with pytest.raises(ValueError, match="duplicates"):
        EvaluationCase(
            "duplicate",
            "zh",
            "keyword",
            "question",
            QueryMode.STANDARD,
            (),
            ("doc", "doc"),
            ("root",),
            ("fact",),
            False,
            0,
        )
    with pytest.raises(ValueError, match="abstention"):
        EvaluationCase(
            "invalid-abstention",
            "en",
            "unanswerable",
            "question",
            QueryMode.STANDARD,
            (),
            (),
            (),
            ("fact",),
            True,
            0,
        )
