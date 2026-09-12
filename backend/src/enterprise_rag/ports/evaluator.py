"""Replaceable evaluator contract and explicit cost/capability declaration."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from enterprise_rag.domain.evaluation import EvaluationCase, EvaluationObservation, MetricSet


@dataclass(frozen=True, slots=True)
class EvaluatorInfo:
    name: str
    version: str
    requires_llm: bool
    estimated_llm_calls_per_case: int
    supported_metrics: frozenset[str]

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("evaluator name and version must not be blank")
        if self.estimated_llm_calls_per_case < 0:
            raise ValueError("estimated LLM calls must not be negative")
        if not self.requires_llm and self.estimated_llm_calls_per_case != 0:
            raise ValueError("a deterministic evaluator cannot estimate LLM calls")
        if not self.supported_metrics or any(
            not metric.strip() for metric in self.supported_metrics
        ):
            raise ValueError("supported_metrics must contain non-empty names")


@runtime_checkable
class Evaluator(Protocol):
    def info(self) -> EvaluatorInfo: ...

    async def evaluate(
        self, case: EvaluationCase, observation: EvaluationObservation
    ) -> MetricSet: ...
