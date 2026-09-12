"""Optional semantic-judge contract kept separate from deterministic metrics."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.domain.eval_run import EvaluationUsage


@dataclass(frozen=True, slots=True)
class JudgeInfo:
    name: str
    version: str
    estimated_llm_calls_per_case: int
    supported_metrics: frozenset[str]


@dataclass(frozen=True, slots=True)
class JudgeInput:
    question: str
    answer: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        require_non_empty(self.question, "judge question")
        require_non_empty(self.answer, "judge answer")
        if any(not item.strip() for item in self.evidence):
            raise ValueError("judge evidence must not contain blank text")


@dataclass(frozen=True, slots=True)
class JudgeScores:
    faithfulness: float
    relevancy: float

    def __post_init__(self) -> None:
        if not 0 <= self.faithfulness <= 1 or not 0 <= self.relevancy <= 1:
            raise ValueError("judge scores must be between zero and one")


@dataclass(frozen=True, slots=True)
class JudgeResult:
    scores: JudgeScores
    usage: EvaluationUsage


@runtime_checkable
class Judge(Protocol):
    def info(self) -> JudgeInfo: ...

    async def evaluate(self, value: JudgeInput) -> JudgeResult: ...


class JudgeRegistrationStatus(StrEnum):
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class JudgeRegistration:
    status: JudgeRegistrationStatus
    judge: Judge | None
    reason_code: str
