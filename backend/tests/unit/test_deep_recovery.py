from collections.abc import Sequence

import pytest

from enterprise_rag.domain import QueryScope
from enterprise_rag.services import (
    DeepRecoveryController,
    DeepRecoveryRequest,
    EvidenceAssessment,
    EvidenceDecision,
    EvidenceItem,
    EvidenceLedger,
    RecoveryAction,
    RecoveryPlanner,
    RecoveryRoute,
    RetrievalMode,
)


def evidence(
    letter: str,
    *,
    confidence: float,
    covered: tuple[str, ...] = (),
    round_number: int = 0,
    route: RecoveryRoute | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        f"leaf_{letter * 64}",
        f"root_{letter * 64}",
        confidence,
        covered,
        round_number,
        route,
    )


def recover_assessment(missing: str) -> EvidenceAssessment:
    return EvidenceAssessment(
        0.1,
        (),
        (missing,),
        (),
        EvidenceDecision.RECOVER,
        "gap",
    )


def test_ledger_deduplicates_across_rounds_and_reserves_recovery_slots() -> None:
    initial = tuple(
        evidence(letter, confidence=0.9 - index / 10) for index, letter in enumerate("abcd")
    )
    ledger = EvidenceLedger(initial)
    added = ledger.add(
        (
            evidence(
                "a",
                confidence=1.0,
                round_number=1,
                route=RecoveryRoute.QUERY_REWRITE_HYBRID,
            ),
            evidence(
                "e",
                confidence=0.2,
                round_number=1,
                route=RecoveryRoute.QUERY_REWRITE_HYBRID,
            ),
            evidence(
                "f",
                confidence=0.1,
                round_number=1,
                route=RecoveryRoute.QUERY_REWRITE_HYBRID,
            ),
        )
    )

    selected = ledger.selected(top_k=4, recovery_reserve=2)

    assert added == 2 and ledger.duplicate_count == 1
    assert {item.leaf_id for item in selected if item.round_number > 0} == {
        f"leaf_{'e' * 64}",
        f"leaf_{'f' * 64}",
    }


@pytest.mark.parametrize(
    ("requirement", "repairable", "route", "mode"),
    [
        ("召回不足，需要同义表达", (), RecoveryRoute.QUERY_REWRITE_HYBRID, RetrievalMode.HYBRID),
        ("这个概念是什么", (), RecoveryRoute.HYDE_DENSE, RetrievalMode.DENSE_ONLY),
        ("查找型号 AB-120", (), RecoveryRoute.EXACT_TERM_SPARSE, RetrievalMode.SPARSE_ONLY),
        ("文档范围可能过窄", ("titles",), RecoveryRoute.SCOPE_REPAIR, RetrievalMode.HYBRID),
    ],
)
def test_all_four_recovery_routes_have_distinct_execution_contracts(
    requirement: str,
    repairable: tuple[str, ...],
    route: RecoveryRoute,
    mode: RetrievalMode,
) -> None:
    request = DeepRecoveryRequest(
        "原始问题",
        (requirement,),
        QueryScope(titles=("Policy",)),
        (),
        repairable,
    )

    action = RecoveryPlanner().plan(request, recover_assessment(requirement), round_number=1)

    assert action.route is route and action.retrieval_mode is mode
    if route is RecoveryRoute.SCOPE_REPAIR:
        assert action.scope.titles == ()
        assert action.repaired_scope_fields == ("titles",)
    elif route is RecoveryRoute.EXACT_TERM_SPARSE:
        assert action.query == request.query
    else:
        assert action.query != request.query


class FakeAssessor:
    def __init__(self, decision: EvidenceDecision = EvidenceDecision.ANSWER) -> None:
        self.decision = decision
        self.calls = 0

    async def assess(
        self, requirements: tuple[str, ...], evidence: tuple[EvidenceItem, ...], score: float
    ) -> EvidenceAssessment:
        self.calls += 1
        return EvidenceAssessment(score, requirements, (), (), self.decision, "assessed")


class FakeExecutor:
    def __init__(self, *, always_duplicate: EvidenceItem | None = None) -> None:
        self.always_duplicate = always_duplicate
        self.actions: list[RecoveryAction] = []

    async def execute(self, action: RecoveryAction) -> Sequence[EvidenceItem]:
        self.actions.append(action)
        values: list[EvidenceItem] = []
        if self.always_duplicate is not None:
            values.append(
                evidence(
                    self.always_duplicate.leaf_id.removeprefix("leaf_")[0],
                    confidence=self.always_duplicate.confidence,
                    covered=self.always_duplicate.covered_requirements,
                    round_number=action.round_number,
                    route=action.route,
                )
            )
        values.append(
            evidence(
                chr(ord("m") + action.round_number),
                confidence=0.1,
                round_number=action.round_number,
                route=action.route,
            )
        )
        return values


@pytest.mark.anyio
async def test_high_threshold_answers_without_assessor_or_recovery() -> None:
    assessor = FakeAssessor()
    executor = FakeExecutor()
    request = DeepRecoveryRequest(
        "问题",
        ("需求",),
        QueryScope(),
        (evidence("a", confidence=1.0, covered=("需求",)),),
    )

    result = await DeepRecoveryController(assessor=assessor, executor=executor).run(request)

    assert result.decision is EvidenceDecision.ANSWER
    assert result.assessment.score == 1.0
    assert result.recovery_rounds == 0 and result.assessor_calls == 0
    assert assessor.calls == 0 and executor.actions == []


@pytest.mark.anyio
async def test_middle_band_uses_assessor_once() -> None:
    assessor = FakeAssessor()
    executor = FakeExecutor()
    request = DeepRecoveryRequest(
        "问题",
        ("需求一", "需求二"),
        QueryScope(),
        (evidence("a", confidence=0.5, covered=("需求一",)),),
    )

    result = await DeepRecoveryController(assessor=assessor, executor=executor).run(request)

    assert 0.45 <= result.assessment.score < 0.8
    assert result.decision is EvidenceDecision.ANSWER
    assert result.assessor_calls == 1 and assessor.calls == 1


@pytest.mark.anyio
async def test_low_evidence_stops_after_two_rounds_and_deduplicates() -> None:
    duplicate = evidence("a", confidence=0.1)
    assessor = FakeAssessor()
    executor = FakeExecutor(always_duplicate=duplicate)
    request = DeepRecoveryRequest(
        "问题",
        ("召回不足",),
        QueryScope(),
        (duplicate,),
    )

    result = await DeepRecoveryController(assessor=assessor, executor=executor, max_rounds=2).run(
        request
    )

    assert result.decision is EvidenceDecision.ABSTAIN
    assert result.recovery_rounds == 2
    assert [action.round_number for action in result.actions] == [1, 2]
    assert result.duplicate_count == 2
    assert len({item.leaf_id for item in result.evidence}) == len(result.evidence)
    assert result.assessor_calls == 0
