"""Validate an untrusted structured plan with deterministic fallback."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryIntent, QueryPlan, QueryScope
from enterprise_rag.observability import start_span
from enterprise_rag.ports.planner import (
    ConversationRole,
    PlannerProviderResult,
    PlannerRequest,
    QueryPlannerProvider,
)

_CJK = re.compile(r"[\u3400-\u9fff]")
_PRONOUN = re.compile(r"(?:它|这(?:个|些)?|该|前者|后者|\b(?:it|this|that|they|those)\b)", re.I)
_COMPARISON = re.compile(r"(?:比较|对比|区别|差异|\bvs\.?\b|\bversus\b)", re.I)
_PROCEDURAL = re.compile(r"(?:如何|怎么|步骤|流程|\bhow\b)", re.I)
_SUMMARY = re.compile(r"(?:总结|概括|摘要|\bsummar(?:y|ize)\b)", re.I)
_MAX_SUB_QUERIES = 4


@dataclass(frozen=True, slots=True)
class PlannerOutcome:
    plan: QueryPlan
    provider: str
    degraded: bool
    error_code: ErrorCode | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class QueryPlanningService:
    def __init__(
        self, provider: QueryPlannerProvider | None = None, *, max_sub_queries: int = 4
    ) -> None:
        if not 1 <= max_sub_queries <= _MAX_SUB_QUERIES:
            raise ValueError(f"max_sub_queries must be between 1 and {_MAX_SUB_QUERIES}")
        self._provider = provider
        self._max_sub_queries = max_sub_queries

    async def plan(self, request: PlannerRequest) -> PlannerOutcome:
        if self._provider is None:
            plan = self._deterministic_plan(request)
            return PlannerOutcome(plan, "deterministic", False)
        provider_name = self._provider.info().name
        with start_span(
            "rag.query_planning.provider", attributes={"provider.name": provider_name}
        ) as span:
            try:
                result = await self._provider.plan(request)
                if isinstance(result, PlannerProviderResult):
                    payload = result.payload
                    llm_calls = 1 + result.retry_count
                    input_tokens = result.input_tokens
                    output_tokens = result.output_tokens
                else:
                    payload = result
                    llm_calls = 1
                    input_tokens = 0
                    output_tokens = 0
                plan = self._parse_plan(payload, request)
            except Exception as error:
                code = (
                    error.code
                    if isinstance(error, AppError)
                    and error.code
                    in {ErrorCode.PLANNER_UNAVAILABLE, ErrorCode.PLANNER_INVALID_RESPONSE}
                    else ErrorCode.PLANNER_INVALID_RESPONSE
                )
                span.set_attribute("rag.degraded", True)
                span.set_attribute("error.code", code.value)
                llm_calls, input_tokens, output_tokens = _error_usage(error)
                return PlannerOutcome(
                    self._deterministic_plan(request),
                    provider_name,
                    True,
                    code,
                    llm_calls=llm_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            span.set_attribute("rag.degraded", False)
            span.set_attribute("rag.sub_query_count", len(plan.sub_queries))
            return PlannerOutcome(
                plan,
                provider_name,
                False,
                llm_calls=llm_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    def _parse_plan(self, payload: Mapping[str, object], request: PlannerRequest) -> QueryPlan:
        required_keys = {
            "rewritten_query",
            "intent",
            "sub_queries",
            "use_sub_queries",
            "requirements",
            "scope",
            "language",
        }
        if set(payload) != required_keys:
            raise ValueError("planner response fields are invalid")
        rewritten = _required_text(payload["rewritten_query"])
        intent = QueryIntent(_required_text(payload["intent"]))
        use_sub_queries = _required_bool(payload["use_sub_queries"])
        sub_queries: tuple[str, ...]
        if not use_sub_queries and payload["sub_queries"] == []:
            # ``false`` is an explicit opt-out. Treat an empty optional route
            # list as the canonical single rewritten route instead of turning
            # an otherwise usable rewrite into a planner outage.
            sub_queries = (rewritten,)
        else:
            sub_queries = _text_tuple(
                payload["sub_queries"], 2 if use_sub_queries else 1, self._max_sub_queries
            )
        sub_queries = _normalize_sub_queries(
            rewritten=rewritten,
            sub_queries=sub_queries,
            use_sub_queries=use_sub_queries,
        )
        requirements = _text_tuple(payload["requirements"], 0, 8)
        requirements = _normalize_requirements(
            request,
            provider_requirements=requirements,
        )
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
            use_sub_queries,
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
        # Decomposition is an LLM decision, not a deterministic default.  The
        # fallback must remain a single retrieval route after a planner outage;
        # splitting punctuation or conjunctions here would recreate the same
        # false multi-requirement behavior we are explicitly preventing.
        sub_queries = (rewritten,)
        requirements = (query,)
        return QueryPlan(
            request.query,
            rewritten,
            intent,
            sub_queries,
            requirements,
            request.requested_scope,
            "zh" if _CJK.search(query) else "en",
            request.mode,
            False,
        )


def canonicalize_plan(plan: QueryPlan, *, original_query: str) -> QueryPlan:
    """Bind a planner result to the query received by the server.

    Planner output is provider data, including when a custom query graph injects
    a ``QueryPlan`` object directly.  Retrieval routes may be rewritten or
    decomposed, but the answer obligation must never come from that provider
    data.  Keep exactly one requirement: the current user query.
    """

    requirement = _required_text(original_query)
    if plan.original_query.strip() == requirement and plan.requirements == (requirement,):
        return plan
    return replace(plan, original_query=requirement, requirements=(requirement,))


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("planner text field is invalid")
    return value.strip()


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("planner boolean field is invalid")
    return value


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


def _normalize_requirements(
    request: PlannerRequest,
    *,
    provider_requirements: tuple[str, ...],
) -> tuple[str, ...]:
    """Keep retrieval branches separate from the single user-level obligation.

    The provider's list is still parsed and bounded as untrusted structured output,
    but its semantic contents never become extra answer obligations. A sub-query
    can be an alternative route to evidence for this same original question.
    """

    del provider_requirements
    return (request.query.strip(),)


def _normalize_sub_queries(
    *,
    rewritten: str,
    sub_queries: tuple[str, ...],
    use_sub_queries: bool,
) -> tuple[str, ...]:
    """Apply the LLM's explicit opt-in before any retrieval work starts.

    ``use_sub_queries`` is the only switch that can activate parallel retrieval.
    When it is false, the model-provided list is constrained to the single
    rewritten route. A true decision is reserved for alternative routes to the
    same user question; the model prompt explicitly forbids splitting one
    multi-part question into independent answer obligations. Requirements are
    canonicalized independently to the original user question.
    """

    if not use_sub_queries:
        return (rewritten,)
    return sub_queries


def _error_usage(error: Exception) -> tuple[int, int, int]:
    if not isinstance(error, AppError):
        return (1, 0, 0)
    values: list[int] = []
    for key in ("llm_calls", "input_tokens", "output_tokens"):
        value = error.details.get(key)
        values.append(value if isinstance(value, int) and not isinstance(value, bool) else 0)
    calls, input_tokens, output_tokens = values
    return (max(1, calls), input_tokens, output_tokens)
