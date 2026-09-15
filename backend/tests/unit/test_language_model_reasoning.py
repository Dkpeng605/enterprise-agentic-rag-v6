import json
from uuid import UUID

import pytest

from enterprise_rag.domain import QueryIntent, QueryMode, QueryPlan, QueryScope
from enterprise_rag.ports import CompletionRequest, CompletionResult, ProviderHealth, ProviderInfo
from enterprise_rag.ports.provider import ProviderKind
from enterprise_rag.services import (
    AnswerStatus,
    AnswerVerificationService,
    EvidenceDecision,
    EvidenceItem,
    LanguageModelAnswerAuthor,
    LanguageModelEvidenceAssessor,
    RootContext,
)
from enterprise_rag.services.language_model_reasoning import _bounded_evidence

DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000001801")
VERSION_ID = UUID("01900000-0000-7000-8000-000000001802")
ROOT_ID = "root_" + "a" * 64
LEAF_ID = "leaf_" + "b" * 64


class ScriptedLanguageModel:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.requests: list[CompletionRequest] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.LLM,
            "scripted_llm",
            "test",
            frozenset({"chat-completions"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return CompletionResult(response, 31, 17, 1)

    async def aclose(self) -> None:
        return None


def plan() -> QueryPlan:
    return QueryPlan(
        "政策是什么？",
        "政策是什么？",
        QueryIntent.FACTUAL,
        ("政策是什么？",),
        ("政策是什么？",),
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
        {"page": 2},
        "政策定义明确。有效期限为三年。",
        (LEAF_ID,),
        0.9,
        False,
    )


@pytest.mark.anyio
async def test_llm_evidence_assessor_reads_bounded_evidence_and_reports_usage() -> None:
    model = ScriptedLanguageModel(
        [
            json.dumps(
                {
                    "covered_requirements": ["政策是什么？"],
                    "missing_requirements": [],
                    "conflicts": [],
                    "decision": "answer",
                    "reason": "证据完整",
                },
                ensure_ascii=False,
            )
        ]
    )
    assessor = LanguageModelEvidenceAssessor(model, max_evidence_chars=100)

    result = await assessor.assess(
        ("政策是什么？",),
        (EvidenceItem(LEAF_ID, ROOT_ID, 0.9, (), 0, None, "政策定义与期限证据"),),
        0.27,
    )

    assert result.decision is EvidenceDecision.ANSWER
    assert result.score == pytest.approx(0.97)
    assert result.llm_calls == 2
    assert result.input_tokens == 31 and result.output_tokens == 17
    assert "政策定义与期限证据" in model.requests[0].user_prompt
    assert model.requests[0].json_mode is True
    assert model.requests[0].max_output_tokens == 2_000


def test_bounded_assessor_evidence_prioritizes_the_best_alternative_route() -> None:
    items = tuple(
        EvidenceItem(
            f"leaf_{letter * 64}",
            f"root_{letter * 64}",
            confidence,
            (),
            0,
            None,
            text,
        )
        for letter, confidence, text in (
            ("a", 0.10, "低置信度噪声证据"),
            ("b", 0.20, "另一条低置信度噪声证据"),
            ("c", 0.95, "唯一可靠替代路径证据"),
        )
    )

    bounded = _bounded_evidence(items, limit=len("唯一可靠替代路径证据"))

    assert bounded == [
        {
            "leaf_id": "leaf_" + "c" * 64,
            "root_id": "root_" + "c" * 64,
            "confidence": 0.95,
            "text": "唯一可靠替代路径证据",
        }
    ]


@pytest.mark.anyio
async def test_structured_answer_is_verified_and_invalid_first_draft_is_repaired_once() -> None:
    invalid = {
        "paragraphs": [{"text": "政策有效三年。[1]", "citation_ids": [1]}],
        "citations": [
            {"id": 1, "root_id": ROOT_ID, "leaf_ids": [LEAF_ID], "quote": "并不存在"}
        ],
        "covered_requirements": ["政策是什么？"],
    }
    valid = {
        "paragraphs": [{"text": "政策定义明确，有效期限为三年。[1]", "citation_ids": [1]}],
        "citations": [
            {"id": 1, "root_id": ROOT_ID, "leaf_ids": [LEAF_ID], "quote": "有效期限为三年"}
        ],
        "covered_requirements": ["政策是什么？"],
    }
    author = LanguageModelAnswerAuthor(
        ScriptedLanguageModel([json.dumps(invalid), json.dumps(valid)])
    )

    authored = await author.draft(plan=plan(), roots=(root(),))
    outcome = await AnswerVerificationService(author).finalize(
        plan=plan(), roots=(root(),), draft=authored.draft
    )

    assert outcome.status is AnswerStatus.REPAIRED
    assert outcome.citations[0].quote == "有效期限为三年"
    assert authored.llm_calls == 2
    assert author.repair_usage.llm_calls == 2
    assert len(author.model_requests) == 2
    assert "chain of thought" in author.model_requests[0].system_prompt
    assert "at most 4 short paragraphs" in author.model_requests[0].system_prompt


@pytest.mark.anyio
async def test_answer_author_receives_selected_leaf_evidence_not_full_root() -> None:
    valid = {
        "paragraphs": [{"text": "有效期限为三年。[1]", "citation_ids": [1]}],
        "citations": [
            {"id": 1, "root_id": ROOT_ID, "leaf_ids": [LEAF_ID], "quote": "有效期限为三年"}
        ],
        "covered_requirements": ["政策是什么？"],
    }
    selected_root = RootContext(
        ROOT_ID,
        DOCUMENT_ID,
        VERSION_ID,
        "policy.pdf",
        "Policy",
        "Acme",
        "application/pdf",
        {"page": 2},
        "完整 Root 原文，不应重复发送给 Answer Author。",
        (LEAF_ID,),
        0.9,
        False,
        "有效期限为三年",
    )
    model = ScriptedLanguageModel([json.dumps(valid, ensure_ascii=False)])
    author = LanguageModelAnswerAuthor(model)

    await author.draft(plan=plan(), roots=(selected_root,))

    assert "有效期限为三年" in author.model_requests[0].user_prompt
    assert "完整 Root 原文" not in author.model_requests[0].user_prompt


@pytest.mark.anyio
async def test_answer_author_receives_leaf_subquery_requirement_hints() -> None:
    coverage_plan = QueryPlan(
        "定义和期限是什么？",
        "定义和期限是什么？",
        QueryIntent.FACTUAL,
        ("定义和期限是什么？",),
        ("定义和期限是什么？",),
        QueryScope(),
        "zh",
        QueryMode.STANDARD,
    )
    selected_root = RootContext(
        ROOT_ID,
        DOCUMENT_ID,
        VERSION_ID,
        "policy.pdf",
        "Policy",
        "Acme",
        "application/pdf",
        {"page": 2},
        "政策定义明确。有效期限为三年。",
        (LEAF_ID,),
        0.9,
        False,
        leaf_matched_queries={LEAF_ID: ("定义", "期限")},
    )
    response = {
        "paragraphs": [{"text": "已找到相关证据。", "citation_ids": [], "factual": False}],
        "citations": [],
        "covered_requirements": [],
    }
    model = ScriptedLanguageModel([json.dumps(response, ensure_ascii=False)])
    author = LanguageModelAnswerAuthor(model)

    await author.draft(plan=coverage_plan, roots=(selected_root,))

    payload = json.loads(model.requests[0].user_prompt)
    assert payload["use_sub_queries"] is False
    assert payload["requirements"] == ["定义和期限是什么？"]
    assert payload["roots"][0]["leaf_evidence"] == [
        {
            "leaf_id": LEAF_ID,
            "matched_queries": ["定义", "期限"],
            "requirement_hints": ["定义和期限是什么？"],
        }
    ]


@pytest.mark.anyio
async def test_invalid_json_draft_gets_one_schema_retry_with_aggregated_usage() -> None:
    valid = {
        "paragraphs": [{"text": "政策定义明确。[1]", "citation_ids": [1]}],
        "citations": [
            {"id": 1, "root_id": ROOT_ID, "leaf_ids": [LEAF_ID], "quote": "政策定义明确"}
        ],
        "covered_requirements": ["政策是什么？"],
    }
    author = LanguageModelAnswerAuthor(
        ScriptedLanguageModel(["not-json", json.dumps(valid)])
    )

    authored = await author.draft(plan=plan(), roots=(root(),))

    assert authored.llm_calls == 4
    assert authored.input_tokens == 62 and authored.output_tokens == 34
    assert len(author.model_requests) == 2
    assert "previous draft failed" in author.model_requests[1].system_prompt
    assert all(request.json_mode for request in author.model_requests)
