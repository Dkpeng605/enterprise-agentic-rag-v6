"""Budgeted tenant evaluation runs, history, reports, and comparability."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from enterprise_rag.adapters.database.evaluations import (
    EvaluationRunPage,
    PersistedEvaluationRun,
    PostgreSQLEvaluationRunStore,
)
from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.domain.common import JsonSerializable, new_uuid7
from enterprise_rag.domain.eval_run import EvaluationRunConfig
from enterprise_rag.domain.evaluation import EvaluationCase
from enterprise_rag.domain.golden_set import GoldenSet
from enterprise_rag.domain.retrieval import QueryMode
from enterprise_rag.observability.metrics import current_metrics
from enterprise_rag.services.eval_runner import EvaluationRunner, evaluation_report_dict
from enterprise_rag.services.evaluation import DeterministicEvaluator
from enterprise_rag.services.golden_set import GoldenSetLoader
from enterprise_rag.services.quality_gate import SparseGoldenSubject

Clock = Callable[[], datetime]
PROFILE_ID = "deterministic-sparse-v1"
PROMPT_REVISION = "none"


@dataclass(frozen=True, slots=True)
class EvaluationProfile(JsonSerializable):
    id: str
    label: str
    provider: str
    model: str
    prompt_revision: str
    requires_remote: bool
    estimated_llm_calls_per_case: int

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
            "prompt_revision": self.prompt_revision,
            "requires_remote": self.requires_remote,
            "estimated_llm_calls_per_case": self.estimated_llm_calls_per_case,
        }


@dataclass(frozen=True, slots=True)
class EvaluationCatalog(JsonSerializable):
    dataset_revision: str
    dataset_label: str
    case_counts: Mapping[str, int]
    profiles: tuple[EvaluationProfile, ...]
    max_cases: int
    max_llm_calls: int

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_revision": self.dataset_revision,
            "dataset_label": self.dataset_label,
            "case_counts": dict(self.case_counts),
            "profiles": [profile.to_dict() for profile in self.profiles],
            "max_cases": self.max_cases,
            "max_llm_calls": self.max_llm_calls,
        }


@dataclass(frozen=True, slots=True)
class EvaluationComparison(JsonSerializable):
    base_run_id: UUID
    candidate_run_id: UUID
    comparable: bool
    reasons: tuple[str, ...]
    base_metrics: Mapping[str, float | None]
    candidate_metrics: Mapping[str, float | None]
    deltas: Mapping[str, float | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "base_run_id": self.base_run_id,
            "candidate_run_id": self.candidate_run_id,
            "comparable": self.comparable,
            "reasons": list(self.reasons),
            "base_metrics": dict(self.base_metrics),
            "candidate_metrics": dict(self.candidate_metrics),
            "deltas": dict(self.deltas),
        }


class EvaluationWorkspaceService:
    def __init__(
        self,
        store: PostgreSQLEvaluationRunStore,
        *,
        manifest: Path,
        max_cases: int,
        max_llm_calls: int,
        commit_sha: str,
        clock: Clock,
    ) -> None:
        self._store = store
        self._golden = GoldenSetLoader().load(_resolve_manifest(manifest))
        self._max_cases = max_cases
        self._max_llm_calls = max_llm_calls
        self._commit_sha = commit_sha
        self._clock = clock
        encoder = HashingSparseEncoder()
        self._profile = EvaluationProfile(
            PROFILE_ID,
            "Deterministic sparse smoke",
            encoder.info().name,
            encoder.info().version,
            PROMPT_REVISION,
            False,
            0,
        )

    def catalog(self) -> EvaluationCatalog:
        counts = {
            "all": len(self._golden.cases),
            "standard": sum(case.mode is QueryMode.STANDARD for case in self._golden.cases),
            "deep": sum(case.mode is QueryMode.DEEP for case in self._golden.cases),
        }
        return EvaluationCatalog(
            self._golden.revision,
            "Golden Set v1 · local deterministic smoke",
            counts,
            (self._profile,),
            self._max_cases,
            self._max_llm_calls,
        )

    async def create_run(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        dataset_revision: str,
        mode: str,
        provider_profile: str,
        max_cases: int,
        max_llm_calls: int,
    ) -> PersistedEvaluationRun:
        if dataset_revision != self._golden.revision:
            raise ValueError("unknown evaluation dataset revision")
        if provider_profile != self._profile.id:
            raise ValueError("unknown evaluation provider profile")
        if mode not in {"all", "standard", "deep"}:
            raise ValueError("unknown evaluation mode")
        if not 1 <= max_cases <= self._max_cases:
            raise ValueError("evaluation case budget is outside the configured limit")
        if not 0 <= max_llm_calls <= self._max_llm_calls:
            raise ValueError("evaluation LLM budget is outside the configured limit")
        selected = self._select_cases(mode, max_cases)
        estimated_llm_calls = len(selected) * self._profile.estimated_llm_calls_per_case
        if estimated_llm_calls > max_llm_calls:
            raise ValueError("estimated evaluation calls exceed the requested budget")
        return await self._store.create(
            run_id=new_uuid7(),
            tenant_id=tenant_id,
            actor_id=actor_id,
            dataset_revision=self._golden.revision,
            mode=mode,
            provider_profile=self._profile.id,
            provider=self._profile.provider,
            model=self._profile.model,
            prompt_revision=self._profile.prompt_revision,
            index_revision=self._golden.revision,
            commit_sha=self._commit_sha,
            max_cases=max_cases,
            max_llm_calls=max_llm_calls,
            estimated_llm_calls=estimated_llm_calls,
            case_ids=tuple(case.id for case in selected),
            now=self._clock(),
        )

    async def execute_run(self, tenant_id: UUID, run_id: UUID) -> None:
        try:
            run = await self.get_run(tenant_id, run_id)
            await self._store.mark_running(tenant_id, run_id, now=self._clock())
            case_ids = frozenset(run.case_ids)
            selected = GoldenSet(
                self._golden.schema_version,
                self._golden.revision,
                tuple(case for case in self._golden.cases if case.id in case_ids),
                self._golden.documents,
            )
            runner = EvaluationRunner(
                SparseGoldenSubject(HashingSparseEncoder()), DeterministicEvaluator()
            )

            async def progress(completed: int, _: int) -> None:
                await self._store.update_progress(
                    tenant_id, run_id, completed=completed
                )

            report = await runner.run(
                selected,
                EvaluationRunConfig(
                    provider=run.provider or "unknown",
                    model=run.model or "unknown",
                    prompt_revision=run.prompt_revision or "unknown",
                    index_revision=run.index_revision or "unknown",
                    commit_sha=run.commit_sha or "unknown",
                    max_cases=len(selected.cases),
                    max_llm_calls=run.max_llm_calls,
                    settings={
                        "mode": run.mode,
                        "profile": run.provider_profile,
                        "purpose": "local-deterministic-smoke",
                    },
                ),
                progress=progress,
            )
            payload = cast(dict[str, object], evaluation_report_dict(report))
            await self._store.succeed(
                tenant_id, run_id, report=payload, now=self._clock()
            )
            if metrics := current_metrics():
                metrics.observe_evaluation(status="succeeded")
        except Exception:
            await self._store.fail(
                tenant_id,
                run_id,
                error_code="EVALUATION_FAILED",
                now=self._clock(),
            )
            if metrics := current_metrics():
                metrics.observe_evaluation(status="failed")

    async def list_runs(
        self,
        tenant_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> EvaluationRunPage:
        return await self._store.list(
            tenant_id, status=status, cursor=cursor, limit=limit
        )

    async def get_run(
        self, tenant_id: UUID, run_id: UUID
    ) -> PersistedEvaluationRun:
        run = await self._store.get(tenant_id, run_id)
        if run is None:
            from enterprise_rag.domain.errors import AppError, ErrorCode

            raise AppError(ErrorCode.NOT_FOUND, "The evaluation run was not found.")
        return run

    async def compare(
        self, tenant_id: UUID, base_id: UUID, candidate_id: UUID
    ) -> EvaluationComparison:
        base = await self.get_run(tenant_id, base_id)
        candidate = await self.get_run(tenant_id, candidate_id)
        reasons = _comparison_reasons(base, candidate)
        base_metrics = _metrics(base.report)
        candidate_metrics = _metrics(candidate.report)
        deltas: dict[str, float | None] = {}
        for name in sorted(set(base_metrics) | set(candidate_metrics)):
            base_value = base_metrics.get(name)
            candidate_value = candidate_metrics.get(name)
            deltas[name] = (
                candidate_value - base_value
                if candidate_value is not None and base_value is not None
                else None
            )
        return EvaluationComparison(
            base.id,
            candidate.id,
            not reasons,
            reasons,
            base_metrics,
            candidate_metrics,
            deltas if not reasons else {},
        )

    def _select_cases(self, mode: str, max_cases: int) -> tuple[EvaluationCase, ...]:
        cases = self._golden.cases
        if mode != "all":
            cases = tuple(case for case in cases if case.mode.value == mode)
        return cases[:max_cases]


def _comparison_reasons(
    base: PersistedEvaluationRun, candidate: PersistedEvaluationRun
) -> tuple[str, ...]:
    reasons: list[str] = []
    if base.id == candidate.id:
        reasons.append("SAME_RUN")
    if base.status != "succeeded" or candidate.status != "succeeded":
        reasons.append("RUN_NOT_SUCCEEDED")
    if base.report is None or candidate.report is None:
        reasons.append("REPORT_MISSING")
    required = (
        "dataset_revision",
        "provider_profile",
        "provider",
        "model",
        "prompt_revision",
        "index_revision",
    )
    if any(not getattr(run, field) for run in (base, candidate) for field in required):
        reasons.append("METADATA_INCOMPLETE")
    for field, code in (
        ("dataset_revision", "DATASET_MISMATCH"),
        ("mode", "MODE_MISMATCH"),
        ("provider_profile", "PROVIDER_MISMATCH"),
        ("provider", "PROVIDER_MISMATCH"),
        ("model", "MODEL_MISMATCH"),
        ("prompt_revision", "PROMPT_MISMATCH"),
        ("index_revision", "INDEX_MISMATCH"),
        ("case_ids", "CASE_SET_MISMATCH"),
    ):
        if getattr(base, field) != getattr(candidate, field):
            reasons.append(code)
    return tuple(dict.fromkeys(reasons))


def _metrics(report: dict[str, object] | None) -> dict[str, float | None]:
    if report is None or not isinstance(report.get("aggregate_metrics"), dict):
        return {}
    values = cast(dict[str, object], report["aggregate_metrics"])
    return {
        name: float(value) if isinstance(value, (int, float)) else None
        for name, value in values.items()
        if isinstance(name, str) and (value is None or isinstance(value, (int, float)))
    }


def _resolve_manifest(path: Path) -> Path:
    if path.is_absolute() or path.exists():
        return path
    repository_path = Path(__file__).resolve().parents[4] / path
    return repository_path if repository_path.exists() else path
