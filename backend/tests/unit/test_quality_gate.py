"""Acceptance tests for the deterministic required-CI quality gate."""

from dataclasses import replace
from pathlib import Path

import pytest

from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.domain import EvaluationReport, EvaluationRunConfig
from enterprise_rag.services import (
    DeterministicEvaluator,
    EvaluationRunner,
    GoldenSetLoader,
    SparseGoldenSubject,
    evaluate_quality_gate,
    load_quality_gate_policy,
)

REPOSITORY_ROOT = Path(__file__).parents[3]
MANIFEST = REPOSITORY_ROOT / "evals/golden/v1/manifest.yaml"
POLICY = REPOSITORY_ROOT / "evals/quality-gate-v1.yaml"


async def smoke_report() -> EvaluationReport:
    golden_set = GoldenSetLoader().load(MANIFEST)
    encoder = HashingSparseEncoder()
    try:
        return await EvaluationRunner(
            SparseGoldenSubject(encoder),
            DeterministicEvaluator(),
        ).run(
            golden_set,
            EvaluationRunConfig(
                "hashing_lexical",
                encoder.info().version,
                "none",
                golden_set.revision,
                "test-sha",
                max_cases=30,
                max_llm_calls=0,
            ),
        )
    finally:
        await encoder.aclose()


@pytest.mark.anyio
async def test_committed_sparse_smoke_report_passes_exact_policy() -> None:
    report = await smoke_report()
    result = evaluate_quality_gate(report, load_quality_gate_policy(POLICY))

    assert result.passed is True
    assert result.failures == ()
    assert report.aggregate_metrics == {
        "document_recall_at_5": 1.0,
        "root_recall_at_5": 1.0,
        "mrr_at_10": 0.9615384615384616,
        "citation_coverage": 1.0,
        "citation_validity": 1.0,
        "abstention_accuracy": 1.0,
    }


@pytest.mark.anyio
async def test_metric_regression_fails_gate_and_therefore_required_job() -> None:
    report = await smoke_report()
    regressed_metrics = dict(report.aggregate_metrics)
    regressed_metrics["mrr_at_10"] = 0.93
    regressed = replace(report, aggregate_metrics=regressed_metrics)

    result = evaluate_quality_gate(regressed, load_quality_gate_policy(POLICY))

    assert result.passed is False
    assert result.failures == ("mrr_at_10:REGRESSION_EXCEEDED",)
