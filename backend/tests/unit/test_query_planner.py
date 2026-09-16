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
        "use_sub_queries": True,
        "sub_queries": [
            "比较甲和乙的成本与性能",
            "甲乙方案成本与性能对比",
        ],
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
async def test_deterministic_fallback_keeps_decomposition_opt_in() -> None:
    request = PlannerRequest(
        "如何部署；同时如何回滚",
        (),
        QueryScope(collection_ids=(COLLECTION_A,)),
        QueryMode.STANDARD,
    )

    outcome = await QueryPlanningService().plan(request)

    assert outcome.provider == "deterministic"
    assert outcome.degraded is False
    assert outcome.plan.rewritten_query == request.query
    assert outcome.plan.sub_queries == (outcome.plan.rewritten_query,)
    assert outcome.plan.requirements == (request.query,)
    assert outcome.plan.scope == request.requested_scope


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
    assert outcome.llm_calls == 1


@pytest.mark.anyio
async def test_simple_factual_plan_does_not_add_unasked_requirements() -> None:
    payload = valid_payload()
    payload.update(
        {
            "rewritten_query": "系统支持哪些文档格式？",
            "intent": "factual",
            "use_sub_queries": False,
            "sub_queries": ["系统支持哪些文档格式？"],
            "requirements": ["如有区分，需说明支持导入与导出的格式"],
            "scope": {
                "collection_ids": [],
                "document_ids": [],
                "titles": [],
                "organizations": [],
                "doc_types": [],
                "versions": [],
                "sections": [],
            },
        }
    )
    request = PlannerRequest("系统支持哪些文档格式？", (), QueryScope(), QueryMode.STANDARD)

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is False
    assert outcome.plan.rewritten_query == "系统支持哪些文档格式？"
    assert outcome.plan.sub_queries == ("系统支持哪些文档格式？",)
    assert outcome.plan.requirements == ("系统支持哪些文档格式？",)


@pytest.mark.anyio
async def test_llm_can_explicitly_opt_in_to_alternative_routes() -> None:
    payload = valid_payload()
    payload.update(
        {
            "rewritten_query": "系统支持哪些文档格式？",
            "intent": "factual",
            "use_sub_queries": True,
            "sub_queries": [
                "系统支持哪些文档格式？",
                "哪些文件格式被系统支持？",
                "系统支持的文档格式有哪些？",
            ],
            "requirements": ["系统支持哪些文档格式？"],
            "scope": {
                "collection_ids": [],
                "document_ids": [],
                "titles": [],
                "organizations": [],
                "doc_types": [],
                "versions": [],
                "sections": [],
            },
        }
    )
    request = PlannerRequest("系统支持哪些文档格式？", (), QueryScope(), QueryMode.STANDARD)

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is False
    assert outcome.plan.sub_queries == (
        "系统支持哪些文档格式？",
        "哪些文件格式被系统支持？",
        "系统支持的文档格式有哪些？",
    )
    assert outcome.plan.requirements == (request.query,)
    assert outcome.plan.use_sub_queries is True


@pytest.mark.anyio
async def test_explicit_opt_out_never_activates_model_extra_routes() -> None:
    payload = valid_payload()
    payload.update(
        {
            "rewritten_query": "系统支持哪些文档格式？",
            "intent": "factual",
            "use_sub_queries": False,
            # Treat this as malformed provider content but keep the safe opt-out.
            "sub_queries": ["导入格式", "导出格式", "兼容格式"],
            "requirements": ["系统支持哪些文档格式？"],
            "scope": {
                "collection_ids": [],
                "document_ids": [],
                "titles": [],
                "organizations": [],
                "doc_types": [],
                "versions": [],
                "sections": [],
            },
        }
    )
    request = PlannerRequest("系统支持哪些文档格式？", (), QueryScope(), QueryMode.STANDARD)

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is False
    assert outcome.plan.use_sub_queries is False
    assert outcome.plan.sub_queries == (outcome.plan.rewritten_query,)


@pytest.mark.anyio
async def test_explicit_opt_out_with_empty_routes_keeps_the_rewritten_single_route() -> None:
    payload = valid_payload()
    payload.update(
        {
            "rewritten_query": "如何配置本地索引？",
            "intent": "procedural",
            "use_sub_queries": False,
            "sub_queries": [],
            "requirements": ["如何配置本地索引？"],
            "scope": {
                "collection_ids": [],
                "document_ids": [],
                "titles": [],
                "organizations": [],
                "doc_types": [],
                "versions": [],
                "sections": [],
            },
        }
    )
    request = PlannerRequest("如何配置本地索引？", (), QueryScope(), QueryMode.STANDARD)

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is False
    assert outcome.plan.use_sub_queries is False
    assert outcome.plan.sub_queries == ("如何配置本地索引？",)
    assert outcome.plan.requirements == (request.query,)


@pytest.mark.anyio
async def test_simple_factual_plan_overrides_model_misclassified_intent() -> None:
    payload = valid_payload()
    payload.update(
        {
            "rewritten_query": "当前知识库中的访问控制要求是什么？包括认证和授权。",
            "intent": "summary",
            "use_sub_queries": False,
            "sub_queries": ["当前知识库中的访问控制要求是什么？包括认证和授权。"],
            "requirements": [],
            "scope": {
                "collection_ids": [],
                "document_ids": [],
                "titles": [],
                "organizations": [],
                "doc_types": [],
                "versions": [],
                "sections": [],
            },
        }
    )
    request = PlannerRequest(
        "当前知识库中的访问控制要求是什么？", (), QueryScope(), QueryMode.STANDARD
    )

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is False
    assert outcome.plan.sub_queries == (outcome.plan.rewritten_query,)
    assert outcome.plan.use_sub_queries is False
    assert outcome.plan.requirements == (request.query,)


@pytest.mark.anyio
async def test_empty_provider_requirements_still_use_the_original_question() -> None:
    payload = valid_payload()
    payload.update(
        {
            "rewritten_query": "如何部署；同时如何回滚",
            "intent": "procedural",
            "use_sub_queries": False,
            "sub_queries": ["如何部署；同时如何回滚"],
            "requirements": [],
            "scope": {
                "collection_ids": [],
                "document_ids": [],
                "titles": [],
                "organizations": [],
                "doc_types": [],
                "versions": [],
                "sections": [],
            },
        }
    )
    request = PlannerRequest("如何部署；同时如何回滚", (), QueryScope(), QueryMode.STANDARD)

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is False
    assert outcome.plan.requirements == (request.query,)


@pytest.mark.anyio
async def test_multi_part_question_is_not_split_without_explicit_opt_in() -> None:
    payload = valid_payload()
    payload.update(
        {
            "rewritten_query": "系统支持哪些文档格式；并且哪些格式可导出？",
            "intent": "factual",
            "use_sub_queries": False,
            "sub_queries": ["系统支持哪些文档格式；并且哪些格式可导出？"],
            "requirements": ["支持哪些文档格式", "哪些格式可导出"],
            "scope": {
                "collection_ids": [],
                "document_ids": [],
                "titles": [],
                "organizations": [],
                "doc_types": [],
                "versions": [],
                "sections": [],
            },
        }
    )
    request = PlannerRequest(
        "系统支持哪些文档格式；并且哪些格式可导出？", (), QueryScope(), QueryMode.STANDARD
    )

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is False
    assert outcome.plan.sub_queries == (outcome.plan.rewritten_query,)
    assert outcome.plan.use_sub_queries is False
    assert outcome.plan.requirements == (request.query,)


@pytest.mark.anyio
async def test_missing_opt_in_field_falls_back_to_one_route() -> None:
    payload = valid_payload()
    del payload["use_sub_queries"]
    request = PlannerRequest("比较甲和乙", (), QueryScope(), QueryMode.STANDARD)

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is True
    assert outcome.plan.use_sub_queries is False
    assert outcome.plan.sub_queries == (request.query,)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid", ["expanded_scope", "bad_uuid", "unknown_field", "duplicate", "bad_switch"]
)
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
    elif invalid == "bad_switch":
        payload["use_sub_queries"] = "true"
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
async def test_planner_has_a_hard_four_route_limit() -> None:
    payload = valid_payload()
    payload["sub_queries"] = [f"检索路径 {index}" for index in range(5)]
    request = PlannerRequest("原始问题", (), QueryScope(), QueryMode.STANDARD)

    outcome = await QueryPlanningService(FakePlanner(payload)).plan(request)

    assert outcome.degraded is True
    assert outcome.plan.sub_queries == (request.query,)
    assert outcome.plan.requirements == (request.query,)
    with pytest.raises(ValueError, match="between 1 and 4"):
        QueryPlanningService(max_sub_queries=5)


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
    assert outcome.plan.sub_queries == (outcome.plan.rewritten_query,)
    assert outcome.plan.requirements == (request.query,)
    assert outcome.plan.language == "zh"
    assert outcome.plan.mode is QueryMode.DEEP
    assert outcome.plan.scope.titles == ("方案",)
