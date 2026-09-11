"""Validate an untrusted structured plan with deterministic fallback."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryIntent, QueryPlan, QueryScope
from enterprise_rag.ports.planner import ConversationRole, PlannerRequest, QueryPlannerProvider

_CJK = re.compile(r"[\u3400-\u9fff]")
_PRONOUN = re.compile(r"(?:它|这(?:个|些)?|该|前者|后者|\b(?:it|this|that|they|those)\b)", re.I)
_COMPARISON = re.compile(r"(?:比较|对比|区别|差异|\bvs\.?\b|\bversus\b)", re.I)
_PROCEDURAL = re.compile(r"(?:如何|怎么|步骤|流程|\bhow\b)", re.I)
_SUMMARY = re.compile(r"(?:总结|概括|摘要|\bsummar(?:y|ize)\b)", re.I)
_MULTI_SPLIT = re.compile(r"(?:；|;|并且|同时|以及|\band\b)", re.I)


@dataclass(frozen=True, slots=True)
class PlannerOutcome:
    plan: QueryPlan
    provider: str
    degraded: bool
    error_code: ErrorCode | None = None


class QueryPlanningService:
    def __init__(self, provider: QueryPlannerProvider, *, max_sub_queries: int = 4) -> None:
        if not 1 <= max_sub_queries <= 8:
            raise ValueError("max_sub_queries must be between 1 and 8")
        self._provider = provider
        self._max_sub_queries = max_sub_queries

    async def plan(self, request: PlannerRequest) -> PlannerOutcome:
        provider_name = self._provider.info().name
        try:
            payload = await self._provider.plan(request)
            plan = self._parse_plan(payload, request)
        except Exception as error:
            code = (
                error.code
                if isinstance(error, AppError)
                and error.code
                in {ErrorCode.PLANNER_UNAVAILABLE, ErrorCode.PLANNER_INVALID_RESPONSE}
                else ErrorCode.PLANNER_INVALID_RESPONSE
            )
            return PlannerOutcome(self._deterministic_plan(request), provider_name, True, code)
        return PlannerOutcome(plan, provider_name, False)

    def _parse_plan(self, payload: Mapping[str, object], request: PlannerRequest) -> QueryPlan:
        required_keys = {
            "rewritten_query",
            "intent",
            "sub_queries",
            "requirements",
            "scope",
            "language",
        }
        if set(payload) != required_keys:
            raise ValueError("planner response fields are invalid")
        rewritten = _required_text(payload["rewritten_query"])
        intent = QueryIntent(_required_text(payload["intent"]))
        sub_queries = _text_tuple(payload["sub_queries"], 1, self._max_sub_queries)
        requirements = _text_tuple(payload["requirements"], 0, 8)
        language = _required_text(payload["language"])
        scope_payload = payload["scope"]
        if not isinstance(scope_payload, Mapping):
            raise ValueError("planner scope must be an object")
        scope = _parse_scope(scope_payload, request.requested_scope)
        return QueryPlan(
            request.query,
            rewritten,
            intent,
            sub_queries,
            requirements,
            scope,
            language,
            request.mode,
        )

    def _deterministic_plan(self, request: PlannerRequest) -> QueryPlan:
        query = request.query.strip()
        rewritten = query
        if _PRONOUN.search(query):
            previous = next(
                (
                    turn.content.strip()
                    for turn in reversed(request.history)
                    if turn.role is ConversationRole.USER
                ),
                None,
            )
            if previous:
                rewritten = f"{previous}；后续问题：{query}"
        intent = _intent(query)
        segments = tuple(
            dict.fromkeys(
                value.strip(" ，,。?")
                for value in _MULTI_SPLIT.split(rewritten)
                if value.strip(" ，,。?")
            )
        )
        sub_queries = segments[: self._max_sub_queries] if len(segments) > 1 else (rewritten,)
        requirements = segments[:8] if len(segments) > 1 else (query,)
        return QueryPlan(
            request.query,
            rewritten,
            intent,
            sub_queries,
            requirements,
            request.requested_scope,
            "zh" if _CJK.search(query) else "en",
            request.mode,
        )


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("planner text field is invalid")
    return value.strip()


def _text_tuple(value: object, minimum: int, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError("planner list field has an invalid size")
    result = tuple(_required_text(item) for item in value)
    if len(result) != len(set(result)):
        raise ValueError("planner list field contains duplicates")
    return result


def _parse_scope(payload: Mapping[object, object], base: QueryScope) -> QueryScope:
    expected = {
        "collection_ids",
        "document_ids",
        "titles",
        "organizations",
        "doc_types",
        "versions",
        "sections",
    }
    if set(payload) != expected:
        raise ValueError("planner scope fields are invalid")
    collection_ids = _uuid_tuple(payload["collection_ids"])
    document_ids = _uuid_tuple(payload["document_ids"])
    _require_narrower_ids(collection_ids, base.collection_ids)
    _require_narrower_ids(document_ids, base.document_ids)
    return QueryScope(
        collection_ids=collection_ids or base.collection_ids,
        document_ids=document_ids or base.document_ids,
        titles=_merge_text_scope(payload["titles"], base.titles),
        organizations=_merge_text_scope(payload["organizations"], base.organizations),
        doc_types=_merge_text_scope(payload["doc_types"], base.doc_types),
        versions=_merge_text_scope(payload["versions"], base.versions),
        sections=_merge_text_scope(payload["sections"], base.sections),
    )


def _uuid_tuple(value: object) -> tuple[UUID, ...]:
    values = _text_tuple(value, 0, 100)
    try:
        result = tuple(UUID(item) for item in values)
    except ValueError as error:
        raise ValueError("planner scope UUID is invalid") from error
    if len(result) != len(set(result)):
        raise ValueError("planner scope UUIDs contain duplicates")
    return result


def _require_narrower_ids(candidate: tuple[UUID, ...], base: tuple[UUID, ...]) -> None:
    if candidate and (not base or not set(candidate).issubset(base)):
        raise ValueError("planner cannot expand an ID scope")


def _merge_text_scope(value: object, base: tuple[str, ...]) -> tuple[str, ...]:
    candidate = _text_tuple(value, 0, 100)
    if (
        base
        and candidate
        and {item.casefold() for item in candidate} != {item.casefold() for item in base}
    ):
        raise ValueError("planner cannot replace an explicit metadata scope")
    return candidate or base


def _intent(query: str) -> QueryIntent:
    checks: Sequence[tuple[re.Pattern[str], QueryIntent]] = (
        (_COMPARISON, QueryIntent.COMPARISON),
        (_PROCEDURAL, QueryIntent.PROCEDURAL),
        (_SUMMARY, QueryIntent.SUMMARY),
    )
    return next(
        (intent for pattern, intent in checks if pattern.search(query)), QueryIntent.FACTUAL
    )
