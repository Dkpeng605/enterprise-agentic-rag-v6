"""Budgeted, cache-aware evaluation runner with stable comparable reports."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

from enterprise_rag.domain.eval_run import (
    CaseEvaluation,
    EvaluationReport,
    EvaluationRunConfig,
    EvaluationUsage,
    JsonValue,
)
from enterprise_rag.domain.evaluation import (
    EvaluationCase,
    EvaluationCitation,
    EvaluationObservation,
    MetricSet,
)
from enterprise_rag.domain.golden_set import GoldenSet
from enterprise_rag.ports.eval_subject import (
    EvaluationResultCache,
    EvaluationSubject,
    EvaluationSubjectInfo,
    EvaluationSubjectResult,
)
from enterprise_rag.ports.evaluator import Evaluator, EvaluatorInfo


class EvaluationBudgetExceeded(ValueError):
    """Raised before a subject call when the worst-case LLM budget cannot fit."""


class MemoryEvaluationResultCache:
    """Process-local successful-result cache."""

    def __init__(self) -> None:
        self._results: dict[str, CaseEvaluation] = {}

    def get(self, request_hash: str) -> CaseEvaluation | None:
        return self._results.get(request_hash)

    def put(self, result: CaseEvaluation) -> None:
        self._results[result.request_hash] = replace(result, from_cache=False)


class GoldenFixtureSubject:
    """Oracle fixture for runner acceptance tests; never a product-quality claim."""

    def info(self) -> EvaluationSubjectInfo:
        return EvaluationSubjectInfo("golden-fixture-oracle", "1")

    async def observe(
        self, case: EvaluationCase, golden_set: GoldenSet
    ) -> EvaluationSubjectResult:
        root_texts = {root.id: root.text for root in golden_set.roots}
        authorized_roots = {
            root_id: root_texts[root_id] for root_id in case.expected_root_ids
        }
        citations = tuple(
            EvaluationCitation(
                root_id,
                text,
                tuple(fact for fact in case.expected_facts if fact in text),
            )
            for root_id, text in authorized_roots.items()
        )
        observation = EvaluationObservation(
            case.expected_document_ids,
            case.expected_root_ids,
            citations,
            authorized_roots,
            case.must_abstain,
        )
        return EvaluationSubjectResult(observation, EvaluationUsage())


class EvaluationRunner:
    def __init__(
        self,
        subject: EvaluationSubject,
        evaluator: Evaluator,
        cache: EvaluationResultCache | None = None,
    ) -> None:
        self._subject = subject
        self._evaluator = evaluator
        self._cache = cache

    async def run(
        self, golden_set: GoldenSet, config: EvaluationRunConfig
    ) -> EvaluationReport:
        cases = golden_set.cases[: config.max_cases]
        subject_info = self._subject.info()
        evaluator_info = self._evaluator.info()
        estimated_llm_calls = len(cases) * (
            subject_info.estimated_llm_calls_per_case
            + evaluator_info.estimated_llm_calls_per_case
        )
        if estimated_llm_calls > config.max_llm_calls:
            raise EvaluationBudgetExceeded(
                f"estimated LLM calls {estimated_llm_calls} exceed budget "
                f"{config.max_llm_calls}"
            )
        config_snapshot = _config_snapshot(config)
        config_hash = _digest(config_snapshot)
        subject_snapshot = _subject_snapshot(subject_info)
        evaluator_snapshot = _evaluator_snapshot(evaluator_info)
        identity: dict[str, JsonValue] = {
            "dataset_revision": golden_set.revision,
            "config_hash": config_hash,
            "subject": subject_snapshot,
            "evaluator": evaluator_snapshot,
        }
        results: list[CaseEvaluation] = []
        usage = EvaluationUsage()
        for case in cases:
            request_hash = _digest({**identity, "case_id": case.id})
            cached = self._cache.get(request_hash) if self._cache is not None else None
            if cached is not None:
                results.append(replace(cached, from_cache=True))
                continue
            subject_result = await self._subject.observe(case, golden_set)
            metrics = await self._evaluator.evaluate(case, subject_result.observation)
            result = CaseEvaluation(case.id, request_hash, metrics, subject_result.usage, False)
            results.append(result)
            usage += subject_result.usage
            if self._cache is not None and subject_result.cacheable:
                self._cache.put(result)
        run_id = _digest({**identity, "case_ids": [case.id for case in cases]})[:24]
        return EvaluationReport(
            "1.0",
            run_id,
            golden_set.revision,
            config_hash,
            config_snapshot,
            subject_snapshot,
            evaluator_snapshot,
            estimated_llm_calls,
            tuple(results),
            _aggregate(tuple(results)),
            usage,
        )


def write_evaluation_report(report: EvaluationReport, path: Path) -> None:
    """Atomically write a UTF-8 report with deterministic key ordering."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(evaluation_report_dict(report), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def evaluation_report_dict(report: EvaluationReport) -> dict[str, JsonValue]:
    return {
        "schema_version": report.schema_version,
        "run_id": report.run_id,
        "dataset_revision": report.dataset_revision,
        "config_hash": report.config_hash,
        "config_snapshot": dict(report.config_snapshot),
        "subject": dict(report.subject_snapshot),
        "evaluator": dict(report.evaluator_snapshot),
        "estimated_llm_calls": report.estimated_llm_calls,
        "aggregate_metrics": dict(report.aggregate_metrics),
        "usage": cast(JsonValue, report.usage.to_dict()),
        "cases": cast(JsonValue, [
            {
                "case_id": result.case_id,
                "request_hash": result.request_hash,
                "metrics": cast(JsonValue, result.metrics.to_dict()),
                "usage": cast(JsonValue, result.usage.to_dict()),
                "from_cache": result.from_cache,
            }
            for result in report.cases
        ]),
    }


def _config_snapshot(config: EvaluationRunConfig) -> dict[str, JsonValue]:
    snapshot: dict[str, JsonValue] = {
        "provider": config.provider,
        "model": config.model,
        "prompt_revision": config.prompt_revision,
        "index_revision": config.index_revision,
        "commit_sha": config.commit_sha,
        "max_cases": config.max_cases,
        "max_llm_calls": config.max_llm_calls,
        "settings": dict(config.settings or {}),
    }
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    return cast(dict[str, JsonValue], json.loads(serialized))


def _subject_snapshot(info: EvaluationSubjectInfo) -> dict[str, JsonValue]:
    return {
        "name": info.name,
        "version": info.version,
        "estimated_llm_calls_per_case": info.estimated_llm_calls_per_case,
        "estimated_embedding_calls_per_case": info.estimated_embedding_calls_per_case,
        "estimated_rerank_calls_per_case": info.estimated_rerank_calls_per_case,
    }


def _evaluator_snapshot(info: EvaluatorInfo) -> dict[str, JsonValue]:
    return {
        "name": info.name,
        "version": info.version,
        "requires_llm": info.requires_llm,
        "estimated_llm_calls_per_case": info.estimated_llm_calls_per_case,
        "supported_metrics": cast(JsonValue, sorted(info.supported_metrics)),
    }


def _aggregate(results: tuple[CaseEvaluation, ...]) -> dict[str, float | None]:
    names = tuple(MetricSet(None, None, None, None, None, 0).to_dict())
    aggregate: dict[str, float | None] = {}
    for name in names:
        values = [
            value
            for result in results
            if (value := result.metrics.to_dict()[name]) is not None
        ]
        aggregate[name] = sum(values) / len(values) if values else None
    return aggregate


def _digest(value: Mapping[str, JsonValue]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
