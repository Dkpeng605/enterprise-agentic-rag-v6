"""Acceptance tests for reproducible, budgeted evaluation runs."""

import json
from pathlib import Path

import pytest

from enterprise_rag.domain import EvaluationCase, EvaluationRunConfig, GoldenSet
from enterprise_rag.ports import EvaluationSubjectInfo, EvaluationSubjectResult
from enterprise_rag.services import (
    DeterministicEvaluator,
    EvaluationBudgetExceeded,
    EvaluationRunner,
    GoldenFixtureSubject,
    GoldenSetLoader,
    MemoryEvaluationResultCache,
    write_evaluation_report,
)

REPOSITORY_ROOT = Path(__file__).parents[3]
MANIFEST = REPOSITORY_ROOT / "evals/golden/v1/manifest.yaml"


class CountingFixtureSubject(GoldenFixtureSubject):
    def __init__(self, llm_calls_per_case: int = 0) -> None:
        self.calls = 0
        self.llm_calls_per_case = llm_calls_per_case

    def info(self) -> EvaluationSubjectInfo:
        return EvaluationSubjectInfo(
            "counting-fixture",
            "1",
            estimated_llm_calls_per_case=self.llm_calls_per_case,
        )

    async def observe(
        self, case: EvaluationCase, golden_set: GoldenSet
    ) -> EvaluationSubjectResult:
        self.calls += 1
        return await super().observe(case, golden_set)


def config(
    *, prompt_revision: str = "prompt-v1", max_llm_calls: int = 0
) -> EvaluationRunConfig:
    return EvaluationRunConfig(
        provider="fixture",
        model="deterministic",
        prompt_revision=prompt_revision,
        index_revision="index-v1",
        commit_sha="abc123",
        max_cases=3,
        max_llm_calls=max_llm_calls,
        settings={"retrieval_top_k": 5, "mode": "smoke"},
    )


@pytest.mark.anyio
async def test_repeat_run_has_stable_identity_and_uses_success_cache(tmp_path: Path) -> None:
    golden_set = GoldenSetLoader().load(MANIFEST)
    subject = CountingFixtureSubject()
    runner = EvaluationRunner(
        subject,
        DeterministicEvaluator(),
        MemoryEvaluationResultCache(),
    )

    first = await runner.run(golden_set, config())
    second = await runner.run(golden_set, config())
    report_path = tmp_path / "report.json"
    write_evaluation_report(first, report_path)
    first_bytes = report_path.read_bytes()
    write_evaluation_report(first, report_path)

    assert subject.calls == 3
    assert first.run_id == second.run_id
    assert first.config_hash == second.config_hash
    assert first.aggregate_metrics == second.aggregate_metrics
    assert all(not result.from_cache for result in first.cases)
    assert all(result.from_cache for result in second.cases)
    assert first.usage.llm_calls == second.usage.llm_calls == 0
    assert first.aggregate_metrics == {
        "document_recall_at_5": 1.0,
        "root_recall_at_5": 1.0,
        "mrr_at_10": 1.0,
        "citation_coverage": 1.0,
        "citation_validity": 1.0,
        "abstention_accuracy": 1.0,
    }
    assert report_path.read_bytes() == first_bytes
    assert json.loads(first_bytes)["config_snapshot"]["prompt_revision"] == "prompt-v1"


@pytest.mark.anyio
async def test_over_budget_is_rejected_before_any_subject_call() -> None:
    golden_set = GoldenSetLoader().load(MANIFEST)
    subject = CountingFixtureSubject(llm_calls_per_case=1)
    runner = EvaluationRunner(subject, DeterministicEvaluator())

    with pytest.raises(EvaluationBudgetExceeded, match="3.*budget 2"):
        await runner.run(golden_set, config(max_llm_calls=2))

    assert subject.calls == 0


@pytest.mark.anyio
async def test_configuration_change_invalidates_request_hashes() -> None:
    golden_set = GoldenSetLoader().load(MANIFEST)
    subject = CountingFixtureSubject()
    runner = EvaluationRunner(
        subject,
        DeterministicEvaluator(),
        MemoryEvaluationResultCache(),
    )

    first = await runner.run(golden_set, config())
    changed = await runner.run(golden_set, config(prompt_revision="prompt-v2"))

    assert subject.calls == 6
    assert first.run_id != changed.run_id
    assert {case.request_hash for case in first.cases}.isdisjoint(
        case.request_hash for case in changed.cases
    )
