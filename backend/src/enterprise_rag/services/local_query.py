"""Offline deterministic QueryRunner for Compose and browser acceptance tests."""

from collections.abc import Mapping
from dataclasses import replace

from enterprise_rag.adapters.database import Database, PostgreSQLContextRepository
from enterprise_rag.adapters.rerankers import NoopReranker
from enterprise_rag.domain.retrieval import Citation, QueryScope
from enterprise_rag.observability import start_span, trace_async
from enterprise_rag.ports.context import ScopeAuthorization
from enterprise_rag.ports.embedding import EmbeddingProvider
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
    conversational_answer,
    no_results_answer,
)
from enterprise_rag.services.reranking import RerankingService
from enterprise_rag.services.retrieval import DualSearchService
from enterprise_rag.services.scope_root import RootContext, ScopeRootService


class DeterministicLocalQueryRunner:
    """Exercise the real retrieval boundary without remote models or fabricated answers."""

    def __init__(
        self,
        *,
        database: Database,
        embedding: EmbeddingProvider,
        sparse: SparseEncoder,
        vector_store: VectorStore,
        index_revision: str,
        dense_top_k: int = 40,
        sparse_top_k: int = 40,
        fused_top_k: int = 30,
        rerank_candidates: int = 20,
        selected_leaf_k: int = 8,
        rrf_k: int = 60,
        max_parent_chars: int = 18_000,
    ) -> None:
        if not index_revision.strip():
            raise ValueError("index_revision must not be blank")
        self._database = database
        self._embedding = embedding
        self._sparse = sparse
        self._vector_store = vector_store
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
            NoopReranker(),
            rerank_candidates=rerank_candidates,
            selected_leaf_k=selected_leaf_k,
        )
        self._max_parent_chars = max_parent_chars

    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        if (answer := conversational_answer(command.query)) is not None:
            await self._progress(emit, QueryProgressStage.ANSWERING, "回应日常问候")
            return QueryExecution(
                command.query_id,
                QueryRunStatus.ANSWERED,
                answer,
                (),
                diagnostics={
                    **self._diagnostics(0, 0),
                    "answer_strategy": "conversational_fallback",
                },
                usage=self._usage(),
            )
        await self._progress(emit, QueryProgressStage.PLANNING, "校验查询范围")
        with start_span(
            "rag.query_planning",
            attributes={
                "provider.name": "deterministic_local",
                "rag.query.mode": command.mode.value,
            },
        ):
            authorization = command.authorization or ScopeAuthorization(command.tenant_id, True)

        await self._progress(emit, QueryProgressStage.RETRIEVING, "执行 Dense / Sparse 检索")
        await self._vector_store.ensure_revision(
            IndexSchema(self._index_revision, self._embedding.dimension, self._sparse.mode)
        )
        search_scope = _authorized_search_scope(command.scope, authorization)
        searched = await trace_async(
            "rag.retrieval",
            self._search.search(
                query=command.query,
                tenant_id=command.tenant_id,
                index_revision=self._index_revision,
                scope=search_scope,
            ),
        )
        with start_span("rag.rrf_fusion") as span:
            fused = self._fusion.fuse((searched,))
            span.set_attribute("rag.candidate_count", len(fused.hits))
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

        await self._progress(emit, QueryProgressStage.RERANKING, "按融合顺序确定候选")
        async with self._database.session() as session:
            scope_root = ScopeRootService(
                PostgreSQLContextRepository(session),
                max_parent_chars=self._max_parent_chars,
            )
            with start_span("rag.auth_and_scope"):
                if fused.hits:
                    prepared = await scope_root.prepare_candidates(
                        authorization, command.scope, fused.hits
                    )
                    resolved_scope = prepared.scope
                    items = prepared.items
                else:
                    resolved_scope = await scope_root.resolve_scope(
                        authorization, command.scope
                    )
                    items = ()
            with start_span("rag.rerank") as span:
                reranked = await self._reranking.rerank(command.query, items)
                span.set_attribute("rag.degraded", reranked.degraded)
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
            recovered = await trace_async(
                "rag.root_restore", scope_root.recover(resolved_scope, reranked.hits)
            )

        await self._progress(emit, QueryProgressStage.ANSWERING, "生成可核验摘录式回答")
        with start_span("rag.answer_generation", attributes={"provider.name": "extractive_e2e"}):
            if not recovered.roots:
                return QueryExecution(
                    command.query_id,
                    QueryRunStatus.NO_RESULTS,
                    no_results_answer(),
                    (),
                    diagnostics=self._diagnostics(0, fused.diagnostic.unique_leaf_count),
                    usage=self._usage(),
                )
            roots = recovered.roots[:3]
            answer = "根据当前知识库中检索到的原文：\n\n" + "\n\n".join(
                f"[{index}] {_excerpt(root.text)}" for index, root in enumerate(roots, start=1)
            )
            citations = tuple(
                _citation(index, root) for index, root in enumerate(roots, start=1)
            )
            return QueryExecution(
                command.query_id,
                QueryRunStatus.ANSWERED,
                answer,
                citations,
                diagnostics=self._diagnostics(
                    len(citations), fused.diagnostic.unique_leaf_count
                ),
                usage=self._usage(),
            )

    @staticmethod
    async def _progress(
        emit: ProgressSink | None, stage: QueryProgressStage, detail: str
    ) -> None:
        if emit is not None:
            await emit(QueryProgress(stage, detail))

    @staticmethod
    def _usage() -> Mapping[str, object]:
        return {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0}

    @staticmethod
    def _diagnostics(citation_count: int, candidate_count: int) -> Mapping[str, object]:
        return {
            "runtime": "deterministic_local_e2e",
            "answer_strategy": "extractive",
            "candidate_count": candidate_count,
            "citation_count": citation_count,
            "planner_degraded": False,
            "reranker_degraded": False,
        }


def _authorized_search_scope(
    scope: QueryScope, authorization: ScopeAuthorization
) -> QueryScope:
    """Apply restricted transport authorization before vector retrieval."""

    if authorization.full_tenant_access:
        return scope
    return replace(
        scope,
        collection_ids=scope.collection_ids or authorization.collection_ids,
        document_ids=scope.document_ids or authorization.document_ids,
    )


def _excerpt(text: str, *, limit: int = 520) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else f"{normalized[:limit].rstrip()}…"


def _citation(identifier: int, root: RootContext) -> Citation:
    page_value = root.source_locator.get("page")
    page = page_value if isinstance(page_value, int) and not isinstance(page_value, bool) else None
    section_value = root.source_locator.get("section")
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
        _excerpt(root.text, limit=240),
        root.score,
    )
