from collections.abc import Mapping
from uuid import UUID

import pytest

from enterprise_rag.domain import AppError, ErrorCode, QueryIntent, QueryMode, QueryScope
from enterprise_rag.ports import (
    ConversationRole,
    ConversationTurn,
    PlannerRequest,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
)
from enterprise_rag.services import QueryPlanningService

COLLECTION_A = UUID("01900000-0000-7000-8000-000000001601")
COLLECTION_B = UUID("01900000-0000-7000-8000-000000001602")


def valid_payload() -> dict[str, object]:
    return {
        "rewritten_query": "比较甲和乙的成本与性能",
        "intent": "comparison",
        "sub_queries": ["甲的成本与性能", "乙的成本与性能"],
        "requirements": ["成本", "性能"],
        "scope": {
            "collection_ids": [str(COLLECTION_A)],
            "document_ids": [],
            "titles": [],
            "organizations": [],
            "doc_types": ["text/plain"],
            "versions": [],
            "sections": [],
        },
        "language": "zh",
    }


class FakePlanner:
    def __init__(self, response: Mapping[str, object] | Exception) -> None:
        self.response = response
        self.requests: list[PlannerRequest] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.LLM,
            "fake_planner",
            "1",
            frozenset({"structured-output"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def plan(self, request: PlannerRequest) -> Mapping[str, object]:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_valid_structured_plan_is_parsed_and_cannot_override_mode() -> None:
    request = PlannerRequest(
        "比较甲和乙",
        (),
        QueryScope(collection_ids=(COLLECTION_A,)),
        QueryMode.DEEP,
    )
    provider = FakePlanner(valid_payload())

    outcome = await QueryPlanningService(provider).plan(request)

    assert outcome.degraded is False
    assert outcome.plan.intent is QueryIntent.COMPARISON
    assert outcome.plan.mode is QueryMode.DEEP
    assert outcome.plan.scope.collection_ids == (COLLECTION_A,)
    assert outcome.plan.scope.doc_types == ("text/plain",)
    assert provider.requests == [request]


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["expanded_scope", "bad_uuid", "unknown_field", "duplicate"])
async def test_invalid_structured_output_falls_back_without_scope_expansion(invalid: str) -> None:
    payload = valid_payload()
    scope = payload["scope"]
    assert isinstance(scope, dict)
    if invalid == "expanded_scope":
        scope["collection_ids"] = [str(COLLECTION_B)]
    elif invalid == "bad_uuid":
        scope["collection_ids"] = ["invented"]
    elif invalid == "unknown_field":
        payload["extra"] = "unsafe"
    else:
        payload["sub_queries"] = ["same", "same"]
    request = PlannerRequest(
        "比较甲和乙",
        (),
        QueryScope(collection_ids=(COLLECTION_A,)),
        QueryMode.STANDARD,
    )

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is True
    assert outcome.error_code is ErrorCode.PLANNER_INVALID_RESPONSE
    assert outcome.plan.scope == request.requested_scope


@pytest.mark.anyio
async def test_provider_unavailable_uses_sanitized_deterministic_fallback() -> None:
    provider = FakePlanner(AppError(ErrorCode.PLANNER_UNAVAILABLE, "secret upstream diagnostic"))
    request = PlannerRequest("如何部署它", (), QueryScope(), QueryMode.STANDARD)

    outcome = await QueryPlanningService(provider).plan(request)

    assert outcome.degraded is True
    assert outcome.error_code is ErrorCode.PLANNER_UNAVAILABLE
    assert outcome.plan.intent is QueryIntent.PROCEDURAL
    assert "secret" not in repr(outcome)


@pytest.mark.anyio
async def test_fallback_handles_pronoun_comparison_and_multiple_conditions() -> None:
    provider = FakePlanner(RuntimeError("offline"))
    request = PlannerRequest(
        "它们有什么区别；同时比较成本",
        (
            ConversationTurn(ConversationRole.USER, "甲方案和乙方案"),
            ConversationTurn(ConversationRole.ASSISTANT, "请继续"),
        ),
        QueryScope(titles=("方案",)),
        QueryMode.DEEP,
    )

    outcome = await QueryPlanningService(provider, max_sub_queries=4).plan(request)

    assert outcome.plan.rewritten_query.startswith("甲方案和乙方案；后续问题：")
    assert outcome.plan.intent is QueryIntent.COMPARISON
    assert len(outcome.plan.sub_queries) >= 2
    assert len(outcome.plan.requirements) >= 2
    assert outcome.plan.language == "zh"
    assert outcome.plan.mode is QueryMode.DEEP
    assert outcome.plan.scope.titles == ("方案",)
