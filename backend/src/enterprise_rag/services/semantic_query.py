"""Real-provider query runner for the local Mac application composition."""

import asyncio
from collections.abc import Mapping

from enterprise_rag.adapters.database import Database, PostgreSQLContextRepository
from enterprise_rag.domain.retrieval import Citation, QueryMode, QueryScope
from enterprise_rag.observability import start_span
from enterprise_rag.ports.context import ScopeAuthorization
from enterprise_rag.ports.embedding import EmbeddingProvider
from enterprise_rag.ports.llm import CompletionRequest, LanguageModel
from enterprise_rag.ports.planner import PlannerRequest
from enterprise_rag.ports.reranker import Reranker
from enterprise_rag.ports.sparse import SparseEncoder
from enterprise_rag.ports.vector_store import IndexSchema, VectorStore
from enterprise_rag.services.fusion import ReciprocalRankFusion
from enterprise_rag.services.query_api import (
    ProgressSink,
    QueryCommand,
    QueryExecution,
    QueryProgress,
    QueryProgressStage,
    QueryRunStatus,
)
from enterprise_rag.services.query_planner import QueryPlanningService
from enterprise_rag.services.reranking import RerankingService
from enterprise_rag.services.retrieval import DualSearchResult, DualSearchService
from enterprise_rag.services.scope_root import RootContext, ScopeRootService


class SemanticQueryRunner:
    """Run semantic hybrid retrieval, cross-encoder reranking, and grounded generation."""

    def __init__(
        self,
        *,
        database: Database,
        embedding: EmbeddingProvider,
        sparse: SparseEncoder,
        vector_store: VectorStore,
        reranker: Reranker,
        language_model: LanguageModel,
        index_revision: str,
        dense_top_k: int = 40,
        sparse_top_k: int = 40,
        fused_top_k: int = 30,
        rerank_candidates: int = 20,
        selected_leaf_k: int = 8,
        rrf_k: int = 60,
        max_parent_chars: int = 18_000,
        max_output_tokens: int = 1_000,
        planner: QueryPlanningService | None = None,
    ) -> None:
        if not index_revision.strip():
            raise ValueError("index_revision must not be blank")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self._database = database
        self._embedding = embedding
        self._sparse = sparse
        self._vector_store = vector_store
        self._reranker = reranker
        self._language_model = language_model
        self._index_revision = index_revision
        self._search = DualSearchService(
            embedding=embedding,
            sparse=sparse,
            vector_store=vector_store,
            dense_top_k=dense_top_k,
            sparse_top_k=sparse_top_k,
        )
        self._fusion = ReciprocalRankFusion(
            rrf_k=rrf_k,
            top_k=fused_top_k,
            max_leaves_per_root=3,
        )
        self._reranking = RerankingService(
            reranker,
            rerank_candidates=rerank_candidates,
            selected_leaf_k=selected_leaf_k,
        )
        self._max_parent_chars = max_parent_chars
        self._max_output_tokens = max_output_tokens
        self._planner = planner or QueryPlanningService()

    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        await self._progress(emit, QueryProgressStage.PLANNING, "执行查询改写与子查询规划")
        with start_span("rag.query_planning") as span:
            planned = await self._planner.plan(
                PlannerRequest(command.query, command.history, command.scope, command.mode)
            )
            plan = planned.plan
            authorization = ScopeAuthorization(command.tenant_id, True)
            span.set_attribute("provider.name", planned.provider)
            span.set_attribute("rag.query.mode", command.mode.value)
            span.set_attribute("rag.plan.original", plan.original_query)
            span.set_attribute("rag.plan.rewritten", plan.rewritten_query)
            span.set_attribute("rag.plan.intent", plan.intent.value)
            span.set_attribute("rag.plan.language", plan.language)
            span.set_attribute("rag.plan.sub_queries", plan.sub_queries)
            span.set_attribute("rag.plan.sub_query_count", len(plan.sub_queries))
            span.set_attribute("rag.degraded", planned.degraded)

        await self._progress(
            emit,
            QueryProgressStage.RETRIEVING,
            f"执行 {len(plan.sub_queries)} 条子查询的 Dense / Sparse 检索",
        )
        await self._vector_store.ensure_revision(
            IndexSchema(self._index_revision, self._embedding.dimension)
        )
        with start_span(
            "rag.retrieval", attributes={"rag.branch_count": len(plan.sub_queries)}
        ):
            searched = await asyncio.gather(
                *(
                    self._search_branch(command, plan.scope, query, branch_index)
                    for branch_index, query in enumerate(plan.sub_queries)
                )
            )
        with start_span("rag.rrf_fusion") as span:
            fused = self._fusion.fuse(tuple(searched))
            span.set_attribute("rag.candidate_count", len(fused.hits))
            span.set_attribute(
                "rag.fusion.ranked_list_count", fused.diagnostic.ranked_list_count
            )
            span.set_attribute("rag.fusion.input_hit_count", fused.diagnostic.input_hit_count)
            span.set_attribute(
                "rag.fusion.unique_leaf_count", fused.diagnostic.unique_leaf_count
            )
            span.set_attribute(
                "rag.fusion.root_quota_dropped", fused.diagnostic.root_quota_dropped
            )
            span.set_attribute(
                "rag.fusion.top_k_dropped", fused.diagnostic.top_k_dropped
            )
            for rank, hit in enumerate(fused.hits, start=1):
                attributes: dict[str, str | int | float] = {
                    "rag.rank": rank,
                    "rag.leaf_id": hit.leaf_id,
                    "rag.root_id": hit.root_id,
                    "rag.fused_score": hit.fused_score,
                }
                if hit.dense_rank is not None:
                    attributes["rag.dense_rank"] = hit.dense_rank
                if hit.sparse_rank is not None:
                    attributes["rag.sparse_rank"] = hit.sparse_rank
                span.add_event("rag.fusion.candidate", attributes)

        await self._progress(emit, QueryProgressStage.RERANKING, "使用本地 CrossEncoder 重排")
        async with self._database.session() as session:
            scope_root = ScopeRootService(
                PostgreSQLContextRepository(session),
                max_parent_chars=self._max_parent_chars,
            )
            with start_span("rag.auth_and_scope") as span:
                if fused.hits:
                    prepared = await scope_root.prepare_candidates(
                        authorization, plan.scope, fused.hits
                    )
                    resolved_scope = prepared.scope
                    items = prepared.items
                    rejected_count = prepared.rejected_count
                else:
                    resolved_scope = await scope_root.resolve_scope(
                        authorization, plan.scope
                    )
                    items = ()
                    rejected_count = 0
                span.set_attribute("rag.input_count", len(fused.hits))
                span.set_attribute("rag.output_count", len(items))
                span.set_attribute("rag.rejected_count", rejected_count)
            with start_span("rag.rerank") as span:
                reranked = await self._reranking.rerank(plan.rewritten_query, items)
                span.set_attribute("rag.degraded", reranked.degraded)
                span.set_attribute("rag.input_count", len(items))
                span.set_attribute("rag.candidate_count", reranked.candidate_count)
                span.set_attribute("rag.output_count", len(reranked.hits))
                for rank, hit in enumerate(reranked.hits, start=1):
                    attributes = {
                        "rag.rank": rank,
                        "rag.leaf_id": hit.leaf_id,
                        "rag.root_id": hit.root_id,
                        "rag.fused_score": hit.fused_score,
                    }
                    if hit.rerank_score is not None:
                        attributes["rag.rerank_score"] = hit.rerank_score
                    span.add_event("rag.rerank.candidate", attributes)

            await self._progress(emit, QueryProgressStage.RECOVERING, "恢复授权 Root 原文")
            with start_span("rag.root_restore") as span:
                recovered = await scope_root.recover(resolved_scope, reranked.hits)
                span.set_attribute("rag.input_count", len(reranked.hits))
                span.set_attribute("rag.output_count", len(recovered.roots))
                span.set_attribute("rag.rejected_count", recovered.rejected_count)
                span.set_attribute("rag.truncated_count", recovered.truncated_count)
                span.set_attribute("rag.used_chars", recovered.used_chars)

        await self._progress(emit, QueryProgressStage.ANSWERING, "调用 LLM 生成有依据回答")
        if not recovered.roots:
            return QueryExecution(
                command.query_id,
                QueryRunStatus.NO_RESULTS,
                "当前授权范围内没有检索到可回答该问题的证据。",
                (),
                diagnostics=self._diagnostics(
                    mode=command.mode,
                    candidate_count=fused.diagnostic.unique_leaf_count,
                    citation_count=0,
                    planner_provider=planned.provider,
                    planner_degraded=planned.degraded,
                    reranker_degraded=reranked.degraded,
                ),
                usage={"llm_calls": 0, "input_tokens": 0, "output_tokens": 0},
            )

        root_limit = 5 if command.mode is QueryMode.DEEP else 3
        roots = recovered.roots[:root_limit]
        with start_span(
            "rag.answer_generation",
            attributes={"provider.name": self._language_model.info().name},
        ) as span:
            completion = await self._language_model.complete(
                CompletionRequest(
                    _system_prompt(command.mode),
                    _answer_prompt(command, roots),
                    self._max_output_tokens,
                )
            )
            span.set_attribute("rag.llm_calls", 1 + completion.retry_count)
            span.set_attribute("rag.input_tokens", completion.input_tokens)
            span.set_attribute("rag.output_tokens", completion.output_tokens)
            span.set_attribute("rag.citation_count", len(roots))
        citations = tuple(
            _citation(index, root) for index, root in enumerate(roots, start=1)
        )
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ANSWERED,
            completion.text,
            citations,
            diagnostics=self._diagnostics(
                mode=command.mode,
                candidate_count=fused.diagnostic.unique_leaf_count,
                citation_count=len(citations),
                planner_provider=planned.provider,
                planner_degraded=planned.degraded,
                reranker_degraded=reranked.degraded,
            ),
            usage={
                "llm_calls": 1 + completion.retry_count,
                "input_tokens": completion.input_tokens,
                "output_tokens": completion.output_tokens,
            },
        )

    def _diagnostics(
        self,
        *,
        mode: QueryMode,
        candidate_count: int,
        citation_count: int,
        planner_provider: str,
        planner_degraded: bool,
        reranker_degraded: bool,
    ) -> Mapping[str, object]:
        return {
            "runtime": "mac_semantic",
            "mode": mode.value,
            "embedding_provider": self._embedding.info().name,
            "reranker_provider": self._reranker.info().name,
            "llm_provider": self._language_model.info().name,
            "candidate_count": candidate_count,
            "citation_count": citation_count,
            "planner_provider": planner_provider,
            "planner_degraded": planner_degraded,
            "reranker_degraded": reranker_degraded,
        }

    async def _search_branch(
        self,
        command: QueryCommand,
        scope: QueryScope,
        query: str,
        branch_index: int,
    ) -> DualSearchResult:
        with start_span(
            "rag.retrieval.branch",
            attributes={
                "rag.branch.index": branch_index,
                "rag.branch.query": query,
            },
        ) as span:
            result = await self._search.search(
                query=query,
                tenant_id=command.tenant_id,
                index_revision=self._index_revision,
                scope=scope,
            )
            dense_ids = {hit.leaf_id for hit in result.dense.hits}
            sparse_ids = {hit.leaf_id for hit in result.sparse.hits}
            span.set_attribute(
                "rag.branch.dense_requested", result.dense.diagnostic.requested_top_k
            )
            span.set_attribute("rag.branch.dense_returned", len(dense_ids))
            span.set_attribute(
                "rag.branch.sparse_requested", result.sparse.diagnostic.requested_top_k
            )
            span.set_attribute("rag.branch.sparse_returned", len(sparse_ids))
            span.set_attribute("rag.branch.overlap_count", len(dense_ids & sparse_ids))
            span.set_attribute("rag.branch.unique_count", len(dense_ids | sparse_ids))
            return result

    @staticmethod
    async def _progress(
        emit: ProgressSink | None, stage: QueryProgressStage, detail: str
    ) -> None:
        if emit is not None:
            await emit(QueryProgress(stage, detail))


def _system_prompt(mode: QueryMode) -> str:
    depth = "逐项核对证据并综合回答" if mode is QueryMode.DEEP else "简洁回答"
    return (
        "你是企业知识库问答助手。只能依据用户消息中编号的证据作答，不得使用外部知识。"
        f"请{depth}，在相关句子后使用 [1]、[2] 形式标注证据编号。"
        "证据不足或冲突时必须明确说明，且不要编造引用。使用与问题相同的语言回答。"
    )


def _answer_prompt(command: QueryCommand, roots: tuple[RootContext, ...]) -> str:
    history = "\n".join(
        f"{turn.role.value}: {turn.content}" for turn in command.history[-6:]
    )
    evidence = "\n\n".join(
        f"[证据 {index}] 标题：{root.title}；来源：{root.source_name}\n{root.text}"
        for index, root in enumerate(roots, start=1)
    )
    history_block = f"对话历史：\n{history}\n\n" if history else ""
    return f"{history_block}问题：{command.query}\n\n{evidence}"


def _excerpt(text: str, *, limit: int = 320) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else f"{normalized[:limit].rstrip()}…"


def _citation(identifier: int, root: RootContext) -> Citation:
    page_value = root.source_locator.get("page")
    page = page_value if isinstance(page_value, int) and not isinstance(page_value, bool) else None
    section_value = root.source_locator.get("section") or root.source_locator.get("sheet")
    section = section_value if isinstance(section_value, str) and section_value.strip() else None
    return Citation(
        identifier,
        root.document_id,
        root.root_id,
        root.leaf_ids,
        root.source_name,
        root.title,
        page,
        section,
        _excerpt(root.text),
        root.score,
    )
