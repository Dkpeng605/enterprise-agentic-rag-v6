"""Immutable configuration and result models for reproducible evaluation runs."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.domain.evaluation import MetricSet

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class EvaluationRunConfig:
    provider: str
    model: str
    prompt_revision: str
    index_revision: str
    commit_sha: str
    max_cases: int = 30
    max_llm_calls: int = 0
    settings: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        for name in ("provider", "model", "prompt_revision", "index_revision", "commit_sha"):
            require_non_empty(getattr(self, name), name)
        if self.max_cases <= 0:
            raise ValueError("max_cases must be positive")
        if self.max_llm_calls < 0:
            raise ValueError("max_llm_calls must not be negative")
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings or {})))


@dataclass(frozen=True, slots=True)
class EvaluationUsage:
    llm_calls: int = 0
    embedding_calls: int = 0
    rerank_calls: int = 0
    tokens: int = 0

    def __post_init__(self) -> None:
        if min(self.llm_calls, self.embedding_calls, self.rerank_calls, self.tokens) < 0:
            raise ValueError("evaluation usage must not be negative")

    def __add__(self, other: "EvaluationUsage") -> "EvaluationUsage":
        return EvaluationUsage(
            self.llm_calls + other.llm_calls,
            self.embedding_calls + other.embedding_calls,
            self.rerank_calls + other.rerank_calls,
            self.tokens + other.tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "llm_calls": self.llm_calls,
            "embedding_calls": self.embedding_calls,
            "rerank_calls": self.rerank_calls,
            "tokens": self.tokens,
        }


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    case_id: str
    request_hash: str
    metrics: MetricSet
    usage: EvaluationUsage
    from_cache: bool
    judge_metrics: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.case_id, "case_id")
        require_non_empty(self.request_hash, "request_hash")
        if self.judge_metrics is not None:
            object.__setattr__(
                self,
                "judge_metrics",
                MappingProxyType(dict(self.judge_metrics)),
            )


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    schema_version: str
    run_id: str
    dataset_revision: str
    config_hash: str
    config_snapshot: Mapping[str, JsonValue]
    subject_snapshot: Mapping[str, JsonValue]
    evaluator_snapshot: Mapping[str, JsonValue]
    judge_snapshot: Mapping[str, JsonValue] | None
    estimated_llm_calls: int
    cases: tuple[CaseEvaluation, ...]
    aggregate_metrics: Mapping[str, float | None]
    aggregate_judge_metrics: Mapping[str, float | None]
    usage: EvaluationUsage

    def __post_init__(self) -> None:
        for name in ("schema_version", "run_id", "dataset_revision", "config_hash"):
            require_non_empty(getattr(self, name), name)
        for name in (
            "config_snapshot",
            "subject_snapshot",
            "evaluator_snapshot",
            "aggregate_metrics",
            "aggregate_judge_metrics",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        if self.judge_snapshot is not None:
            object.__setattr__(
                self,
                "judge_snapshot",
                MappingProxyType(dict(self.judge_snapshot)),
            )
