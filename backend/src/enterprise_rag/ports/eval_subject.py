"""Replaceable system-under-evaluation and successful-result cache contracts."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from enterprise_rag.domain.eval_run import CaseEvaluation, EvaluationUsage
from enterprise_rag.domain.evaluation import EvaluationCase, EvaluationObservation
from enterprise_rag.domain.golden_set import GoldenSet


@dataclass(frozen=True, slots=True)
class EvaluationSubjectInfo:
    name: str
    version: str
    estimated_llm_calls_per_case: int = 0
    estimated_embedding_calls_per_case: int = 0
    estimated_rerank_calls_per_case: int = 0

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("evaluation subject name and version must not be blank")
        estimates = (
            self.estimated_llm_calls_per_case,
            self.estimated_embedding_calls_per_case,
            self.estimated_rerank_calls_per_case,
        )
        if min(estimates) < 0:
            raise ValueError("estimated evaluation calls must not be negative")


@dataclass(frozen=True, slots=True)
class EvaluationSubjectResult:
    observation: EvaluationObservation
    usage: EvaluationUsage
    cacheable: bool = True
    answer: str | None = None


@runtime_checkable
class EvaluationSubject(Protocol):
    def info(self) -> EvaluationSubjectInfo: ...

    async def observe(
        self, case: EvaluationCase, golden_set: GoldenSet
    ) -> EvaluationSubjectResult: ...


class EvaluationResultCache(Protocol):
    def get(self, request_hash: str) -> CaseEvaluation | None: ...

    def put(self, result: CaseEvaluation) -> None: ...
