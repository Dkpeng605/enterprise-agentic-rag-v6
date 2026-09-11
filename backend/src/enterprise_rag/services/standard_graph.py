"""Explicit Standard RAG state graph with a hard two-LLM-call ceiling."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryMode, QueryPlan, QueryScope, RetrievalHit
from enterprise_rag.ports.context import ResolvedQueryScope, ScopeAuthorization
from enterprise_rag.ports.llm import CompletionRequest, LanguageModel
from enterprise_rag.ports.planner import ConversationTurn, PlannerRequest
from enterprise_rag.services.fusion import FusionResult
from enterprise_rag.services.query_planner import PlannerOutcome
from enterprise_rag.services.reranking import RerankItem, RerankOutcome
from enterprise_rag.services.retrieval import DualSearchResult
from enterprise_rag.services.scope_root import (
    PreparedRerankCandidates,
    RecoveredContext,
    RootContext,
)


class QueryGraphStage(StrEnum):
    PLAN = "plan"
    SEARCH = "search"
    FUSE = "fuse"
    AUTHORIZE = "authorize"
    RERANK = "rerank"
    RECOVER = "recover"
    ANSWER = "answer"
    COMPLETE = "complete"
    NO_RESULTS = "no_results"
    FAILED = "failed"


class QueryGraphStatus(StrEnum):
    COMPLETE = "complete"
    NO_RESULTS = "no_results"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StageTransition:
    stage: QueryGraphStage


@dataclass(frozen=True, slots=True)
class StandardQueryRequest:
    query: str
    history: tuple[ConversationTurn, ...]
    requested_scope: QueryScope
    authorization: ScopeAuthorization
    index_revision: str

    def __post_init__(self) -> None:
        require_non_empty(self.query, "query")
        require_non_empty(self.index_revision, "index_revision")


@dataclass(frozen=True, slots=True)
class StandardQueryResult:
    status: QueryGraphStatus
    answer: str | None
    plan: QueryPlan | None
    roots: tuple[RootContext, ...]
    transitions: tuple[StageTransition, ...]
    llm_calls: int
    planner_degraded: bool
    reranker_degraded: bool
    error_code: ErrorCode | None = None


class _Planning(Protocol):
    async def plan(self, request: PlannerRequest) -> PlannerOutcome: ...


class _Searching(Protocol):
    async def search(
        self,
        *,
        query: str,
        tenant_id: UUID,
        index_revision: str,
        scope: QueryScope | None = None,
    ) -> DualSearchResult: ...


class _Fusion(Protocol):
    def fuse(self, results: tuple[DualSearchResult, ...]) -> FusionResult: ...


class _ScopeRoot(Protocol):
    async def resolve_scope(
        self, authorization: ScopeAuthorization, requested_scope: QueryScope
    ) -> ResolvedQueryScope: ...

    async def prepare_candidates(
        self,
        authorization: ScopeAuthorization,
        requested_scope: QueryScope,
        hits: Sequence[RetrievalHit],
    ) -> PreparedRerankCandidates: ...

    async def recover(
        self, scope: ResolvedQueryScope, selected_hits: Sequence[RetrievalHit]
    ) -> RecoveredContext: ...


class _Reranking(Protocol):
    async def rerank(self, query: str, items: Sequence[RerankItem]) -> RerankOutcome: ...


class StandardQueryGraph:
    def __init__(
        self,
        *,
        planner: _Planning,
        search: _Searching,
        fusion: _Fusion,
        scope_root: _ScopeRoot,
        reranking: _Reranking,
        language_model: LanguageModel,
        max_output_tokens: int = 1_000,
    ) -> None:
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self._planner = planner
        self._search = search
        self._fusion = fusion
        self._scope_root = scope_root
        self._reranking = reranking
        self._language_model = language_model
        self._max_output_tokens = max_output_tokens

    async def run(self, request: StandardQueryRequest) -> StandardQueryResult:
        transitions: list[StageTransition] = []
        plan: QueryPlan | None = None
        planner_degraded = False
        reranker_degraded = False
        llm_calls = 0
        roots: tuple[RootContext, ...] = ()
        try:
            transitions.append(StageTransition(QueryGraphStage.PLAN))
            llm_calls += 1
            planned = await self._planner.plan(
                PlannerRequest(
                    request.query,
                    request.history,
                    request.requested_scope,
                    QueryMode.STANDARD,
                )
            )
            plan = planned.plan
            planner_degraded = planned.degraded

            transitions.append(StageTransition(QueryGraphStage.SEARCH))
            searched = await asyncio.gather(
                *(
                    self._search.search(
                        query=query,
                        tenant_id=request.authorization.tenant_id,
                        index_revision=request.index_revision,
                        scope=plan.scope,
                    )
                    for query in plan.sub_queries
                )
            )
            transitions.append(StageTransition(QueryGraphStage.FUSE))
            fused = self._fusion.fuse(tuple(searched))

            transitions.append(StageTransition(QueryGraphStage.AUTHORIZE))
            if fused.hits:
                prepared = await self._scope_root.prepare_candidates(
                    request.authorization, plan.scope, fused.hits
                )
                scope = prepared.scope
                items = prepared.items
            else:
                scope = await self._scope_root.resolve_scope(request.authorization, plan.scope)
                items = ()
            if not items:
                transitions.append(StageTransition(QueryGraphStage.NO_RESULTS))
                return _result(
                    QueryGraphStatus.NO_RESULTS,
                    None,
                    plan,
                    (),
                    transitions,
                    llm_calls,
                    planner_degraded,
                    False,
                )

            transitions.append(StageTransition(QueryGraphStage.RERANK))
            reranked = await self._reranking.rerank(plan.rewritten_query, items)
            reranker_degraded = reranked.degraded
            transitions.append(StageTransition(QueryGraphStage.RECOVER))
            recovered = await self._scope_root.recover(scope, reranked.hits)
            roots = recovered.roots
            if not roots:
                transitions.append(StageTransition(QueryGraphStage.NO_RESULTS))
                return _result(
                    QueryGraphStatus.NO_RESULTS,
                    None,
                    plan,
                    (),
                    transitions,
                    llm_calls,
                    planner_degraded,
                    reranker_degraded,
                )

            transitions.append(StageTransition(QueryGraphStage.ANSWER))
            llm_calls += 1
            if llm_calls > 2:
                raise RuntimeError("Standard graph exceeded its LLM call ceiling")
            completion = await self._language_model.complete(
                CompletionRequest(
                    "Answer only from the supplied evidence. State uncertainty explicitly.",
                    _answer_prompt(plan, roots),
                    self._max_output_tokens,
                )
            )
            transitions.append(StageTransition(QueryGraphStage.COMPLETE))
            return _result(
                QueryGraphStatus.COMPLETE,
                completion.text,
                plan,
                roots,
                transitions,
                llm_calls,
                planner_degraded,
                reranker_degraded,
            )
        except Exception as error:
            transitions.append(StageTransition(QueryGraphStage.FAILED))
            return _result(
                QueryGraphStatus.FAILED,
                None,
                plan,
                roots,
                transitions,
                llm_calls,
                planner_degraded,
                reranker_degraded,
                error.code if isinstance(error, AppError) else ErrorCode.INTERNAL_ERROR,
            )


def _answer_prompt(plan: QueryPlan, roots: tuple[RootContext, ...]) -> str:
    evidence = "\n\n".join(
        f"[ROOT {index}] {root.title} | {root.source_name}\n{root.text}"
        for index, root in enumerate(roots, start=1)
    )
    return f"Question: {plan.original_query}\nRequirements: {list(plan.requirements)}\n\n{evidence}"


def _result(
    status: QueryGraphStatus,
    answer: str | None,
    plan: QueryPlan | None,
    roots: tuple[RootContext, ...],
    transitions: list[StageTransition],
    llm_calls: int,
    planner_degraded: bool,
    reranker_degraded: bool,
    error_code: ErrorCode | None = None,
) -> StandardQueryResult:
    return StandardQueryResult(
        status,
        answer,
        plan,
        roots,
        tuple(transitions),
        llm_calls,
        planner_degraded,
        reranker_degraded,
        error_code,
    )
