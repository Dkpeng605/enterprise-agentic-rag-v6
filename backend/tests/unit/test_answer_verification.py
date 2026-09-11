from uuid import UUID

import pytest

from enterprise_rag.domain import QueryIntent, QueryMode, QueryPlan, QueryScope
from enterprise_rag.services import (
    AnswerDraft,
    AnswerStatus,
    AnswerVerificationService,
    DraftCitation,
    DraftParagraph,
    RepairRequest,
    RootContext,
    VerificationIssue,
)

DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000001801")
VERSION_ID = UUID("01900000-0000-7000-8000-000000001802")
ROOT_ID = "root_" + "a" * 64
LEAF_ID = "leaf_" + "b" * 64


def plan() -> QueryPlan:
    return QueryPlan(
        "政策是什么？",
        "政策是什么？",
        QueryIntent.FACTUAL,
        ("政策是什么？",),
        ("定义", "期限"),
        QueryScope(),
        "zh",
        QueryMode.DEEP,
    )


def root() -> RootContext:
    return RootContext(
        ROOT_ID,
        DOCUMENT_ID,
        VERSION_ID,
        "policy.pdf",
        "Policy",
        "Acme",
        "application/pdf",
        {"page": 2, "section": "期限"},
        "政策定义明确。有效期限为三年。",
        (LEAF_ID,),
        0.9,
        False,
    )


def valid_draft() -> AnswerDraft:
    return AnswerDraft(
        (DraftParagraph("政策定义明确，有效期三年。[1]", (1,)),),
        (DraftCitation(1, ROOT_ID, (LEAF_ID,), "有效期限为三年"),),
        ("定义", "期限"),
    )


class FakeRepairer:
    def __init__(self, result: AnswerDraft | Exception) -> None:
        self.result = result
        self.requests: list[RepairRequest] = []

    async def repair(self, request: RepairRequest) -> AnswerDraft:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.anyio
async def test_valid_answer_builds_traceable_domain_citation() -> None:
    repairer = FakeRepairer(RuntimeError("must not run"))

    outcome = await AnswerVerificationService(repairer).finalize(
        plan=plan(), roots=(root(),), draft=valid_draft()
    )

    assert outcome.status is AnswerStatus.ANSWERED
    assert outcome.repair_count == 0 and repairer.requests == []
    assert outcome.citations[0].document_id == DOCUMENT_ID
    assert outcome.citations[0].page == 2
    assert outcome.citations[0].section == "期限"
    assert outcome.citations[0].quote == "有效期限为三年"


@pytest.mark.anyio
async def test_bad_quote_is_repaired_once_using_the_same_roots() -> None:
    bad = AnswerDraft(
        (DraftParagraph("错误引用。[1]", (1,)),),
        (DraftCitation(1, ROOT_ID, (LEAF_ID,), "不存在的原文"),),
        ("定义", "期限"),
    )
    repairer = FakeRepairer(valid_draft())

    outcome = await AnswerVerificationService(repairer).finalize(
        plan=plan(), roots=(root(),), draft=bad
    )

    assert outcome.status is AnswerStatus.REPAIRED
    assert outcome.repair_count == 1 and len(repairer.requests) == 1
    assert repairer.requests[0].roots == (root(),)
    assert VerificationIssue.QUOTE_NOT_FOUND in repairer.requests[0].issues


@pytest.mark.anyio
async def test_missing_requirement_after_one_repair_abstains() -> None:
    incomplete = AnswerDraft(
        (DraftParagraph("只有定义。[1]", (1,)),),
        (DraftCitation(1, ROOT_ID, (LEAF_ID,), "政策定义明确"),),
        ("定义",),
    )
    repairer = FakeRepairer(incomplete)

    outcome = await AnswerVerificationService(repairer).finalize(
        plan=plan(), roots=(root(),), draft=incomplete
    )

    assert outcome.status is AnswerStatus.ABSTAINED
    assert outcome.citations == () and outcome.repair_count == 1
    assert outcome.missing_requirements == ("期限",)
    assert VerificationIssue.MISSING_REQUIREMENT in outcome.issues
    assert len(repairer.requests) == 1


@pytest.mark.anyio
async def test_evidence_conflict_abstains_without_attempting_answer_repair() -> None:
    repairer = FakeRepairer(valid_draft())

    outcome = await AnswerVerificationService(repairer).finalize(
        plan=plan(),
        roots=(root(),),
        draft=valid_draft(),
        evidence_conflicts=("期限互相矛盾",),
    )

    assert outcome.status is AnswerStatus.ABSTAINED
    assert outcome.issues == (VerificationIssue.EVIDENCE_CONFLICT,)
    assert outcome.repair_count == 0 and repairer.requests == []


@pytest.mark.anyio
async def test_repair_cannot_introduce_a_new_root_or_leak_repair_failure() -> None:
    bad = AnswerDraft(
        (DraftParagraph("无引用事实", ()),),
        (),
        ("定义", "期限"),
    )
    invented_root = "root_" + "f" * 64
    invented = AnswerDraft(
        (DraftParagraph("虚构证据。[1]", (1,)),),
        (DraftCitation(1, invented_root, (LEAF_ID,), "虚构"),),
        ("定义", "期限"),
    )
    outcome = await AnswerVerificationService(FakeRepairer(invented)).finalize(
        plan=plan(), roots=(root(),), draft=bad
    )
    failed = await AnswerVerificationService(
        FakeRepairer(RuntimeError("secret repair failure"))
    ).finalize(plan=plan(), roots=(root(),), draft=bad)

    assert outcome.status is AnswerStatus.ABSTAINED
    assert VerificationIssue.UNKNOWN_ROOT in outcome.issues
    assert failed.status is AnswerStatus.ABSTAINED
    assert "secret" not in repr(failed)
