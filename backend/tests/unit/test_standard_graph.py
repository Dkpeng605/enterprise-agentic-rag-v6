from collections.abc import Sequence
from uuid import UUID

import pytest

from enterprise_rag.domain import QueryIntent, QueryMode, QueryPlan, QueryScope, RetrievalHit
from enterprise_rag.ports import (
    CompletionRequest,
    CompletionResult,
    PlannerRequest,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    ResolvedQueryScope,
    ScopeAuthorization,
)
from enterprise_rag.services import (
    DualSearchResult,
    FusionDiagnostic,
    FusionResult,
    PlannerOutcome,
    PreparedRerankCandidates,
    QueryGraphStage,
    QueryGraphStatus,
    RecoveredContext,
    RerankItem,
    RerankOutcome,
    RootContext,
    SearchBranchResult,
    SearchDiagnostic,
    SearchMethod,
    StandardQueryGraph,
    StandardQueryRequest,
)

TENANT_ID = UUID("01900000-0000-7000-8000-000000001701")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000001702")
VERSION_ID = UUID("01900000-0000-7000-8000-000000001703")
LEAF_ID = "leaf_" + "a" * 64
ROOT_ID = "root_" + "b" * 64


def query_plan() -> QueryPlan:
    return QueryPlan(
        "问题",
        "改写问题",
        QueryIntent.FACTUAL,
        ("子问题一", "子问题二"),
        ("回答问题",),
        QueryScope(),
        "zh",
        QueryMode.STANDARD,
    )


def retrieval_hit(*, selected: bool = False) -> RetrievalHit:
    return RetrievalHit(
        LEAF_ID,
        ROOT_ID,
        1,
        1,
        0.5,
        0.9 if selected else None,
        selected,
    )


def dual(query: str) -> DualSearchResult:
    dense = SearchBranchResult(
        SearchMethod.DENSE,
        (),
        SearchDiagnostic(SearchMethod.DENSE, 40, 0, 0, 0),
    )
    sparse = SearchBranchResult(
        SearchMethod.SPARSE,
        (),
        SearchDiagnostic(SearchMethod.SPARSE, 40, 0, 0, 0),
    )
    return DualSearchResult(query, dense, sparse)


class FakePlannerService:
    def __init__(self, *, degraded: bool = False) -> None:
        self.degraded = degraded
        self.calls = 0

    async def plan(self, request: PlannerRequest) -> PlannerOutcome:
        self.calls += 1
        assert request.mode is QueryMode.STANDARD
        return PlannerOutcome(query_plan(), "planner", self.degraded)


class FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(
        self,
        *,
        query: str,
        tenant_id: UUID,
        index_revision: str,
        scope: QueryScope | None = None,
    ) -> DualSearchResult:
        assert tenant_id == TENANT_ID
        assert index_revision == "test-v1"
        assert scope == QueryScope()
        self.queries.append(query)
        return dual(query)


class FakeFusion:
    def __init__(self, hits: tuple[RetrievalHit, ...]) -> None:
        self.hits = hits

    def fuse(self, results: tuple[DualSearchResult, ...]) -> FusionResult:
        assert len(results) == 2
        return FusionResult(self.hits, FusionDiagnostic(4, 0, len(self.hits), 0, 0))


class FakeScopeRoot:
    def __init__(self, *, roots: tuple[RootContext, ...]) -> None:
        self.scope = ResolvedQueryScope(TENANT_ID, (DOCUMENT_ID,))
        self.roots = roots
        self.resolve_calls = 0
        self.prepare_calls = 0

    async def resolve_scope(
        self, authorization: ScopeAuthorization, requested_scope: QueryScope
    ) -> ResolvedQueryScope:
        self.resolve_calls += 1
        assert authorization.tenant_id == TENANT_ID
        assert requested_scope == QueryScope()
        return self.scope

    async def prepare_candidates(
        self,
        authorization: ScopeAuthorization,
        requested_scope: QueryScope,
        hits: Sequence[RetrievalHit],
    ) -> PreparedRerankCandidates:
        self.prepare_calls += 1
        assert tuple(hits) == (retrieval_hit(),)
        return PreparedRerankCandidates(
            self.scope, (RerankItem(retrieval_hit(), "retrieval evidence"),), 0
        )

    async def recover(
        self, scope: ResolvedQueryScope, selected_hits: Sequence[RetrievalHit]
    ) -> RecoveredContext:
        assert scope == self.scope
        assert tuple(selected_hits) == (retrieval_hit(selected=True),)
        return RecoveredContext(self.roots, sum(len(root.text) for root in self.roots), 0, 0)


class FakeReranking:
    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, query: str, items: Sequence[RerankItem]) -> RerankOutcome:
        self.calls += 1
        assert query == "改写问题"
        assert len(items) == 1
        return RerankOutcome((retrieval_hit(selected=True),), "reranker", 1, False)


class FakeLanguageModel:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[CompletionRequest] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.LLM,
            "fake_llm",
            "1",
            frozenset({"text"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return CompletionResult("有证据的答案", 20, 8)

    async def aclose(self) -> None:
        return None


def root_context() -> RootContext:
    return RootContext(
        ROOT_ID,
        DOCUMENT_ID,
        VERSION_ID,
        "policy.txt",
        "Policy",
        "Acme",
        "text/plain",
        {"section": "intro"},
        "root evidence",
        (LEAF_ID,),
        0.9,
        False,
    )


def graph(
    *, hits: tuple[RetrievalHit, ...], roots: tuple[RootContext, ...], degraded: bool = False
) -> tuple[
    StandardQueryGraph,
    FakePlannerService,
    FakeSearch,
    FakeScopeRoot,
    FakeReranking,
    FakeLanguageModel,
]:
    planner = FakePlannerService(degraded=degraded)
    search = FakeSearch()
    scope_root = FakeScopeRoot(roots=roots)
    reranking = FakeReranking()
    llm = FakeLanguageModel()
    return (
        StandardQueryGraph(
            planner=planner,
            search=search,
            fusion=FakeFusion(hits),
            scope_root=scope_root,
            reranking=reranking,
            language_model=llm,
        ),
        planner,
        search,
        scope_root,
        reranking,
        llm,
    )


def request() -> StandardQueryRequest:
    return StandardQueryRequest(
        "问题",
        (),
        QueryScope(),
        ScopeAuthorization(TENANT_ID, True),
        "test-v1",
    )


@pytest.mark.anyio
async def test_normal_path_has_explicit_order_and_exact_two_llm_calls() -> None:
    runner, planner, search, scope_root, reranking, llm = graph(
        hits=(retrieval_hit(),), roots=(root_context(),)
    )

    result = await runner.run(request())

    assert result.status is QueryGraphStatus.COMPLETE
    assert result.answer == "有证据的答案"
    assert result.llm_calls == 2
    assert [transition.stage for transition in result.transitions] == list(QueryGraphStage)[:8]
    assert planner.calls == 1
    assert search.queries == ["子问题一", "子问题二"]
    assert scope_root.prepare_calls == 1 and scope_root.resolve_calls == 0
    assert reranking.calls == 1 and len(llm.requests) == 1
    assert "root evidence" in llm.requests[0].user_prompt


@pytest.mark.anyio
async def test_no_results_resolves_scope_but_skips_reranker_and_answer_llm() -> None:
    runner, _, _, scope_root, reranking, llm = graph(hits=(), roots=())

    result = await runner.run(request())

    assert result.status is QueryGraphStatus.NO_RESULTS
    assert result.answer is None and result.llm_calls == 1
    assert [item.stage for item in result.transitions] == [
        QueryGraphStage.PLAN,
        QueryGraphStage.SEARCH,
        QueryGraphStage.FUSE,
        QueryGraphStage.AUTHORIZE,
        QueryGraphStage.NO_RESULTS,
    ]
    assert scope_root.resolve_calls == 1 and scope_root.prepare_calls == 0
    assert reranking.calls == 0 and llm.requests == []


@pytest.mark.anyio
async def test_planner_degradation_is_visible_without_adding_an_llm_call() -> None:
    runner, *_ = graph(hits=(retrieval_hit(),), roots=(root_context(),), degraded=True)

    result = await runner.run(request())

    assert result.status is QueryGraphStatus.COMPLETE
    assert result.planner_degraded is True
    assert result.llm_calls == 2
