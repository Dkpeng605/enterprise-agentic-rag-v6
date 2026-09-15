"""Deep evidence ledger, dual-threshold routing, and bounded Recovery."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.domain.retrieval import QueryScope
from enterprise_rag.observability import current_metrics, start_span


class EvidenceDecision(StrEnum):
    ANSWER = "answer"
    RECOVER = "recover"
    ABSTAIN = "abstain"


class RecoveryRoute(StrEnum):
    QUERY_REWRITE_HYBRID = "query_rewrite_hybrid"
    HYDE_DENSE = "hyde_dense"
    EXACT_TERM_SPARSE = "exact_term_sparse"
    SCOPE_REPAIR = "scope_repair"


class RetrievalMode(StrEnum):
    HYBRID = "hybrid"
    DENSE_ONLY = "dense_only"
    SPARSE_ONLY = "sparse_only"


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    leaf_id: str
    root_id: str
    confidence: float
    covered_requirements: tuple[str, ...]
    round_number: int
    route: RecoveryRoute | None
    text: str

    def __post_init__(self) -> None:
        if not self.leaf_id.startswith("leaf_") or not self.root_id.startswith("root_"):
            raise ValueError("evidence IDs must use Leaf and Root prefixes")
        if not 0 <= self.confidence <= 1:
            raise ValueError("evidence confidence must be between zero and one")
        if self.round_number < 0:
            raise ValueError("round_number must not be negative")
        if self.round_number == 0 and self.route is not None:
            raise ValueError("initial evidence must not have a Recovery route")
        if self.round_number > 0 and self.route is None:
            raise ValueError("Recovery evidence must identify its route")
        if any(not value.strip() for value in self.covered_requirements):
            raise ValueError("covered requirements must not be blank")
        require_non_empty(self.text, "text")


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    score: float
    covered_requirements: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    conflicts: tuple[str, ...]
    decision: EvidenceDecision
    reason: str
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    degraded: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 1:
            raise ValueError("assessment score must be between zero and one")
        require_non_empty(self.reason, "reason")
        if min(self.llm_calls, self.input_tokens, self.output_tokens) < 0:
            raise ValueError("assessment usage must not be negative")
        for name, values in (
            ("covered_requirements", self.covered_requirements),
            ("missing_requirements", self.missing_requirements),
            ("conflicts", self.conflicts),
        ):
            if any(not value.strip() for value in values) or len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique non-empty values")
        if set(self.covered_requirements) & set(self.missing_requirements):
            raise ValueError("covered and missing requirements must not overlap")


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    round_number: int
    route: RecoveryRoute
    retrieval_mode: RetrievalMode
    query: str
    scope: QueryScope
    target_requirements: tuple[str, ...]
    repaired_scope_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.round_number <= 0:
            raise ValueError("Recovery round_number must be positive")
        require_non_empty(self.query, "query")
        if not self.target_requirements:
            raise ValueError("Recovery must target at least one requirement")


@dataclass(frozen=True, slots=True)
class DeepRecoveryRequest:
    query: str
    requirements: tuple[str, ...]
    scope: QueryScope
    initial_evidence: tuple[EvidenceItem, ...]
    repairable_scope_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_non_empty(self.query, "query")
        if not self.requirements or any(not value.strip() for value in self.requirements):
            raise ValueError("Deep mode requires non-empty requirements")
        allowed = {
            "collection_ids",
            "document_ids",
            "titles",
            "organizations",
            "doc_types",
            "versions",
            "sections",
        }
        if not set(self.repairable_scope_fields).issubset(allowed):
            raise ValueError("repairable_scope_fields contains an unknown field")


@dataclass(frozen=True, slots=True)
class DeepRecoveryOutcome:
    decision: EvidenceDecision
    assessment: EvidenceAssessment
    evidence: tuple[EvidenceItem, ...]
    actions: tuple[RecoveryAction, ...]
    recovery_rounds: int
    duplicate_count: int
    assessor_calls: int
    assessor_input_tokens: int
    assessor_output_tokens: int
    assessor_degraded: bool


class EvidenceAssessor(Protocol):
    async def assess(
        self, requirements: tuple[str, ...], evidence: tuple[EvidenceItem, ...], score: float
    ) -> EvidenceAssessment: ...


class RecoveryExecutor(Protocol):
    async def execute(self, action: RecoveryAction) -> Sequence[EvidenceItem]: ...


class EvidenceLedger:
    def __init__(self, initial: Sequence[EvidenceItem] = ()) -> None:
        self._items: dict[str, EvidenceItem] = {}
        self.duplicate_count = 0
        self.add(initial)

    def add(self, items: Sequence[EvidenceItem]) -> int:
        added = 0
        for item in items:
            if item.leaf_id in self._items:
                self.duplicate_count += 1
                continue
            self._items[item.leaf_id] = item
            added += 1
        return added

    def all(self) -> tuple[EvidenceItem, ...]:
        return tuple(self._items.values())

    def selected(self, *, top_k: int, recovery_reserve: int = 2) -> tuple[EvidenceItem, ...]:
        if top_k <= 0 or recovery_reserve < 0:
            raise ValueError("evidence selection limits are invalid")
        indexed = list(enumerate(self._items.values()))
        ranked = sorted(indexed, key=lambda item: (-item[1].confidence, item[0]))
        recovery = [item for item in ranked if item[1].round_number > 0][:recovery_reserve]
        reserved_ids = {item.leaf_id for _, item in recovery}
        remaining = [item for item in ranked if item[1].leaf_id not in reserved_ids]
        selected = recovery + remaining[: max(0, top_k - len(recovery))]
        selected.sort(key=lambda item: (-item[1].confidence, item[0]))
        return tuple(item for _, item in selected[:top_k])


class RecoveryPlanner:
    def plan(
        self,
        request: DeepRecoveryRequest,
        assessment: EvidenceAssessment,
        *,
        round_number: int,
    ) -> RecoveryAction:
        missing = assessment.missing_requirements or request.requirements
        route = _classify_route(missing[0], request.repairable_scope_fields)
        if route is RecoveryRoute.SCOPE_REPAIR:
            repaired_scope, fields = _repair_scope(request.scope, request.repairable_scope_fields)
            return RecoveryAction(
                round_number,
                route,
                RetrievalMode.HYBRID,
                f"{request.query}；范围修复：{missing[0]}",
                repaired_scope,
                missing,
                fields,
            )
        if route is RecoveryRoute.HYDE_DENSE:
            return RecoveryAction(
                round_number,
                route,
                RetrievalMode.DENSE_ONLY,
                f"可能回答“{missing[0]}”的文档会说明：{request.query}",
                request.scope,
                missing,
            )
        if route is RecoveryRoute.EXACT_TERM_SPARSE:
            return RecoveryAction(
                round_number,
                route,
                RetrievalMode.SPARSE_ONLY,
                request.query,
                request.scope,
                missing,
            )
        return RecoveryAction(
            round_number,
            route,
            RetrievalMode.HYBRID,
            f"{request.query}；换一种表达检索：{missing[0]}",
            request.scope,
            missing,
        )


class DeepRecoveryController:
    def __init__(
        self,
        *,
        assessor: EvidenceAssessor,
        executor: RecoveryExecutor,
        planner: RecoveryPlanner | None = None,
        low_threshold: float = 0.45,
        high_threshold: float = 0.80,
        max_rounds: int = 2,
        always_assess: bool = False,
    ) -> None:
        if not 0 <= low_threshold < high_threshold <= 1:
            raise ValueError("Deep thresholds are invalid")
        if not 0 <= max_rounds <= 10:
            raise ValueError("max_rounds must be between zero and ten")
        self._assessor = assessor
        self._executor = executor
        self._planner = planner or RecoveryPlanner()
        self._low = low_threshold
        self._high = high_threshold
        self._max_rounds = max_rounds
        self._always_assess = always_assess

    async def run(self, request: DeepRecoveryRequest) -> DeepRecoveryOutcome:
        with start_span(
            "rag.deep_recovery",
            attributes={
                "rag.recovery.max_rounds": self._max_rounds,
                "rag.recovery.initial_evidence_count": len(request.initial_evidence),
            },
        ) as span:
            outcome = await self._run_observed(request)
            span.set_attribute("rag.recovery.round_count", outcome.recovery_rounds)
            span.set_attribute("rag.recovery.decision", outcome.decision.value)
            span.set_attribute("rag.recovery.duplicate_count", outcome.duplicate_count)
            return outcome

    async def _run_observed(self, request: DeepRecoveryRequest) -> DeepRecoveryOutcome:
        ledger = EvidenceLedger(request.initial_evidence)
        actions: list[RecoveryAction] = []
        assessor_calls = 0
        assessor_input_tokens = 0
        assessor_output_tokens = 0
        assessor_degraded = False
        for round_number in range(self._max_rounds + 1):
            with start_span(
                "rag.deep_recovery.assess",
                attributes={
                    "rag.recovery.round": round_number,
                    "rag.recovery.evidence_count": len(ledger.all()),
                },
            ) as assessment_span:
                provider_name = getattr(self._assessor, "provider_name", None)
                if isinstance(provider_name, str) and provider_name:
                    assessment_span.set_attribute("provider.name", provider_name)
                assessment, used_assessor = await self._assess(
                    request.requirements, ledger.all()
                )
                if (
                    assessment.decision is EvidenceDecision.ABSTAIN
                    and assessment.missing_requirements
                    and not assessment.conflicts
                    and round_number < self._max_rounds
                ):
                    assessment = replace(
                        assessment,
                        decision=EvidenceDecision.RECOVER,
                        reason=(
                            "Evidence is incomplete without a conflict; scheduling bounded "
                            "Recovery before final abstention."
                        ),
                    )
                assessment_span.set_attribute("rag.recovery.score", assessment.score)
                assessment_span.set_attribute(
                    "rag.recovery.decision", assessment.decision.value
                )
                assessment_span.set_attribute(
                    "rag.recovery.missing_count", len(assessment.missing_requirements)
                )
                assessment_span.set_attribute(
                    "rag.recovery.covered_count", len(assessment.covered_requirements)
                )
                assessment_span.set_attribute(
                    "rag.recovery.covered_requirements", assessment.covered_requirements
                )
                assessment_span.set_attribute(
                    "rag.recovery.missing_requirements", assessment.missing_requirements
                )
                assessment_span.set_attribute("rag.llm_calls", assessment.llm_calls)
                assessment_span.set_attribute("rag.input_tokens", assessment.input_tokens)
                assessment_span.set_attribute("rag.output_tokens", assessment.output_tokens)
                assessment_span.set_attribute("rag.degraded", assessment.degraded)
            if used_assessor:
                assessor_calls += max(1, assessment.llm_calls)
                assessor_input_tokens += assessment.input_tokens
                assessor_output_tokens += assessment.output_tokens
                assessor_degraded = assessor_degraded or assessment.degraded
            if assessment.decision is not EvidenceDecision.RECOVER:
                return _outcome(
                    assessment,
                    ledger,
                    actions,
                    assessor_calls,
                    assessor_input_tokens,
                    assessor_output_tokens,
                    assessor_degraded,
                )
            if round_number >= self._max_rounds:
                assessment = replace(
                    assessment,
                    decision=EvidenceDecision.ABSTAIN,
                    reason="Maximum Recovery rounds reached with unresolved evidence gaps.",
                )
                return _outcome(
                    assessment,
                    ledger,
                    actions,
                    assessor_calls,
                    assessor_input_tokens,
                    assessor_output_tokens,
                    assessor_degraded,
                )
            action = self._planner.plan(request, assessment, round_number=round_number + 1)
            actions.append(action)
            if (metrics := current_metrics()) is not None:
                metrics.observe_recovery(route=action.route.value)
            with start_span(
                "rag.deep_recovery.round",
                attributes={
                    "rag.recovery.round": action.round_number,
                    "rag.recovery.route": action.route.value,
                    "rag.recovery.retrieval_mode": action.retrieval_mode.value,
                    "rag.recovery.target_count": len(action.target_requirements),
                    "rag.recovery.repaired_scope_count": len(
                        action.repaired_scope_fields
                    ),
                },
            ) as recovery_span:
                recovered = tuple(await self._executor.execute(action))
                recovery_span.set_attribute(
                    "rag.recovery.returned_count", len(recovered)
                )
                if any(
                    item.round_number != action.round_number
                    or item.route is not action.route
                    for item in recovered
                ):
                    raise ValueError(
                        "Recovery evidence provenance does not match its action"
                    )
                added = ledger.add(recovered)
                recovery_span.set_attribute("rag.recovery.added_count", added)
                recovery_span.set_attribute(
                    "rag.recovery.duplicate_count", len(recovered) - added
                )
        raise AssertionError("Deep Recovery loop did not terminate")

    async def _assess(
        self, requirements: tuple[str, ...], evidence: tuple[EvidenceItem, ...]
    ) -> tuple[EvidenceAssessment, bool]:
        covered = tuple(
            requirement
            for requirement in requirements
            if any(requirement in item.covered_requirements for item in evidence)
        )
        missing = tuple(item for item in requirements if item not in covered)
        coverage = len(covered) / len(requirements)
        confidence = max((item.confidence for item in evidence), default=0.0)
        score = 0.7 * coverage + 0.3 * confidence
        should_use_assessor = bool(evidence) and (
            self._always_assess or self._low <= score < self._high
        )
        if should_use_assessor:
            try:
                assessed = await self._assessor.assess(requirements, evidence, score)
                _validate_assessment(assessed, requirements)
            except Exception as error:
                llm_calls, input_tokens, output_tokens = _error_usage(error)
                decision = (
                    EvidenceDecision.ANSWER
                    if score >= self._high
                    else EvidenceDecision.RECOVER
                )
                reason = (
                    "Evidence assessor unavailable; deterministic coverage and confidence "
                    "met the high threshold."
                    if decision is EvidenceDecision.ANSWER
                    else "Evidence assessor unavailable; continuing with bounded recovery."
                )
                return (
                    EvidenceAssessment(
                        score,
                        covered,
                        missing,
                        (),
                        decision,
                        reason,
                        llm_calls=llm_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        degraded=True,
                    ),
                    True,
                )
            return assessed, True
        if score >= self._high:
            return (
                EvidenceAssessment(
                    score, covered, missing, (), EvidenceDecision.ANSWER, "High threshold met."
                ),
                False,
            )
        if score < self._low:
            return (
                EvidenceAssessment(
                    score,
                    covered,
                    missing,
                    (),
                    EvidenceDecision.RECOVER,
                    "Evidence score is below the low threshold.",
                ),
                False,
            )
        raise AssertionError("middle-band evidence must be assessed")


def _validate_assessment(
    assessment: EvidenceAssessment, requirements: tuple[str, ...]
) -> None:
    """Keep assessor output inside the one-user-requirement boundary.

    Assessors are Providers and therefore untrusted. In particular, a model must
    not be able to turn retrieval-route labels into new answer obligations or
    claim ``answer`` while its partition still has an uncovered requirement.
    Invalid output is handled by the caller's deterministic fallback.
    """

    if not isinstance(assessment, EvidenceAssessment):
        raise ValueError("Evidence Assessor returned an invalid assessment")
    covered = set(assessment.covered_requirements)
    missing = set(assessment.missing_requirements)
    requirement_set = set(requirements)
    if covered - requirement_set:
        raise ValueError("Evidence Assessor returned unknown covered requirements")
    if missing - requirement_set:
        raise ValueError("Evidence Assessor returned unknown missing requirements")
    if covered & missing:
        raise ValueError("Evidence Assessor returned overlapping requirements")
    if covered | missing != requirement_set:
        raise ValueError("Evidence Assessor returned an incomplete requirement partition")
    if missing and assessment.decision is EvidenceDecision.ANSWER:
        raise ValueError("Evidence Assessor answered with missing requirements")
    if assessment.conflicts and assessment.decision is EvidenceDecision.ANSWER:
        raise ValueError("Evidence Assessor ignored an evidence conflict")


_EXACT_TERM = re.compile(r"(?:[A-Z]{2,}[\w-]*|\w*\d[\w.-]*|型号|编号|版本号)")
_DESCRIPTION = re.compile(r"(?:是什么|定义|概念|介绍|\bwhat is\b|\bdescribe\b)", re.I)
_SCOPE = re.compile(r"(?:范围|文档|集合|scope|collection|document)", re.I)


def _classify_route(requirement: str, repairable_scope_fields: tuple[str, ...]) -> RecoveryRoute:
    if repairable_scope_fields and _SCOPE.search(requirement):
        return RecoveryRoute.SCOPE_REPAIR
    if _EXACT_TERM.search(requirement):
        return RecoveryRoute.EXACT_TERM_SPARSE
    if _DESCRIPTION.search(requirement):
        return RecoveryRoute.HYDE_DENSE
    return RecoveryRoute.QUERY_REWRITE_HYBRID


def _repair_scope(scope: QueryScope, fields: tuple[str, ...]) -> tuple[QueryScope, tuple[str, ...]]:
    if not fields:
        raise ValueError("Scope repair requires a proven repairable field")
    field = fields[0]
    if field == "collection_ids":
        repaired = replace(scope, collection_ids=())
    elif field == "document_ids":
        repaired = replace(scope, document_ids=())
    elif field == "titles":
        repaired = replace(scope, titles=())
    elif field == "organizations":
        repaired = replace(scope, organizations=())
    elif field == "doc_types":
        repaired = replace(scope, doc_types=())
    elif field == "versions":
        repaired = replace(scope, versions=())
    else:
        repaired = replace(scope, sections=())
    return repaired, (field,)


def _outcome(
    assessment: EvidenceAssessment,
    ledger: EvidenceLedger,
    actions: list[RecoveryAction],
    assessor_calls: int,
    assessor_input_tokens: int,
    assessor_output_tokens: int,
    assessor_degraded: bool,
) -> DeepRecoveryOutcome:
    return DeepRecoveryOutcome(
        assessment.decision,
        assessment,
        ledger.all(),
        tuple(actions),
        len(actions),
        ledger.duplicate_count,
        assessor_calls,
        assessor_input_tokens,
        assessor_output_tokens,
        assessor_degraded,
    )


def _error_usage(error: Exception) -> tuple[int, int, int]:
    details = getattr(error, "details", {})
    values: list[int] = []
    for name in ("llm_calls", "input_tokens", "output_tokens"):
        item = details.get(name) if isinstance(details, dict | Mapping) else None
        values.append(
            item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else 0
        )
    calls, input_tokens, output_tokens = values
    return max(1, calls), input_tokens, output_tokens
