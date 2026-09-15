import json
from collections.abc import Mapping

import pytest

from enterprise_rag.adapters.planners import LanguageModelQueryPlanner
from enterprise_rag.domain import AppError, ErrorCode, QueryMode, QueryScope
from enterprise_rag.ports import (
    CompletionRequest,
    CompletionResult,
    ConversationRole,
    ConversationTurn,
    PlannerRequest,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
)
from enterprise_rag.services import QueryPlanningService


class FakeLanguageModel:
    def __init__(self, result: CompletionResult | Exception) -> None:
        self.result = result
        self.requests: list[CompletionRequest] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.LLM,
            "fake_llm",
            "model-v1",
            frozenset({"chat-completions"}),
            True,
            ProviderHealth.HEALTHY,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        return None


def planner_payload() -> Mapping[str, object]:
    return {
        "rewritten_query": "计算机学院辅导员的联系电话是什么？",
        "intent": "factual",
        "use_sub_queries": True,
        "sub_queries": [
            "计算机学院辅导员联系电话",
            "计算机学院学生工作办公室联系方式",
        ],
        "requirements": ["辅导员联系电话"],
        "scope": {
            "collection_ids": [],
            "document_ids": [],
            "titles": [],
            "organizations": [],
            "doc_types": [],
            "versions": [],
            "sections": [],
        },
        "language": "zh",
    }


@pytest.mark.anyio
async def test_language_model_planner_sends_bounded_context_and_parses_json() -> None:
    model = FakeLanguageModel(
        CompletionResult(json.dumps(planner_payload(), ensure_ascii=False), 120, 80)
    )
    provider = LanguageModelQueryPlanner(model, max_output_tokens=900)
    request = PlannerRequest(
        "他的电话是多少？",
        (
            ConversationTurn(ConversationRole.USER, "计算机学院的辅导员是谁？"),
            ConversationTurn(ConversationRole.ASSISTANT, "现有资料提到了张老师。"),
        ),
        QueryScope(titles=("选课指南",)),
        QueryMode.STANDARD,
    )

    result = await provider.plan(request)

    assert result.payload == planner_payload()
    assert (result.input_tokens, result.output_tokens, result.retry_count) == (120, 80, 0)
    assert provider.info().name == "fake_llm_query_planner"
    assert provider.info().capabilities >= frozenset({"query-rewrite", "query-decomposition"})
    assert len(model.requests) == 1
    completion = model.requests[0]
    assert completion.max_output_tokens == 900
    assert completion.json_mode is True
    prompt = json.loads(completion.user_prompt)
    assert prompt["query"] == request.query
    assert prompt["history"][0] == {
        "role": "user",
        "content": "计算机学院的辅导员是谁？",
    }
    assert prompt["requested_scope"]["titles"] == ["选课指南"]
    assert "exactly one JSON object" in completion.system_prompt
    assert "Set" in completion.system_prompt
    assert "use_sub_queries to false by default" in completion.system_prompt
    assert "default" in completion.system_prompt
    assert "not a mandate to split" in completion.system_prompt
    assert "Do not turn each sub-query" in completion.system_prompt
    assert "separate requirement" in completion.system_prompt


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["not-json", "[]", '{"rewritten_query":"x"} trailing'])
async def test_language_model_planner_rejects_non_object_or_malformed_json(text: str) -> None:
    provider = LanguageModelQueryPlanner(FakeLanguageModel(CompletionResult(text, 1, 1)))

    with pytest.raises(AppError) as raised:
        await provider.plan(PlannerRequest("问题", (), QueryScope(), QueryMode.STANDARD))

    assert raised.value.code is ErrorCode.PLANNER_INVALID_RESPONSE
    assert text not in str(raised.value)
    assert raised.value.details == {
        "llm_calls": 1,
        "input_tokens": 1,
        "output_tokens": 1,
    }


@pytest.mark.anyio
async def test_invalid_planner_json_fallback_still_reports_consumed_usage() -> None:
    provider = LanguageModelQueryPlanner(
        FakeLanguageModel(CompletionResult("not-json", 41, 7, retry_count=1))
    )

    outcome = await QueryPlanningService(provider).plan(
        PlannerRequest("问题", (), QueryScope(), QueryMode.STANDARD)
    )

    assert outcome.degraded is True
    assert outcome.error_code is ErrorCode.PLANNER_INVALID_RESPONSE
    assert (outcome.llm_calls, outcome.input_tokens, outcome.output_tokens) == (2, 41, 7)


@pytest.mark.anyio
async def test_language_model_planner_maps_llm_outage_without_leaking_details() -> None:
    provider = LanguageModelQueryPlanner(
        FakeLanguageModel(AppError(ErrorCode.LLM_UNAVAILABLE, "secret upstream detail"))
    )

    with pytest.raises(AppError) as raised:
        await provider.plan(PlannerRequest("问题", (), QueryScope(), QueryMode.STANDARD))

    assert raised.value.code is ErrorCode.PLANNER_UNAVAILABLE
    assert "secret" not in str(raised.value)
