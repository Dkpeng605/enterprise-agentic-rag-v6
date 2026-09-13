"""Replaceable evaluator contract and explicit cost/capability declaration."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from enterprise_rag.domain.evaluation import EvaluationCase, EvaluationObservation, MetricSet
from enterprise_rag.ports.provider import ProviderHealth, ProviderKind


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

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.EVALUATOR

    @property
    def key(self) -> tuple[ProviderKind, str]:
        return (self.kind, self.name)

    @property
    def capabilities(self) -> frozenset[str]:
        return self.supported_metrics

    @property
    def is_remote(self) -> bool:
        return self.requires_llm

    @property
    def health(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
            "is_remote": self.is_remote,
            "health": self.health.value,
        }


@runtime_checkable
class Evaluator(Protocol):
    def info(self) -> EvaluatorInfo: ...

    async def evaluate(
        self, case: EvaluationCase, observation: EvaluationObservation
    ) -> MetricSet: ...
