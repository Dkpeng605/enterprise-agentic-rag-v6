"""Real-provider query runner for the local Mac application composition."""

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from enterprise_rag.adapters.database import Database, PostgreSQLContextRepository
from enterprise_rag.domain.errors import AppError
from enterprise_rag.domain.retrieval import QueryMode, QueryScope, RetrievalHit
from enterprise_rag.observability import start_span
from enterprise_rag.ports.context import ScopeAuthorization
from enterprise_rag.ports.embedding import EmbeddingProvider
from enterprise_rag.ports.llm import LanguageModel
from enterprise_rag.ports.planner import PlannerRequest
from enterprise_rag.ports.reranker import Reranker
from enterprise_rag.ports.sparse import SparseEncoder
from enterprise_rag.ports.vector_store import IndexSchema, VectorStore
from enterprise_rag.services.answer_verification import AnswerStatus, AnswerVerificationService
from enterprise_rag.services.deep_recovery import (
    DeepRecoveryController,
    DeepRecoveryRequest,
    EvidenceDecision,
    EvidenceItem,
    EvidenceLedger,
    RecoveryAction,
    RecoveryRoute,
    RetrievalMode,
)
from enterprise_rag.services.fusion import FusionDiagnostic, ReciprocalRankFusion
from enterprise_rag.services.language_model_reasoning import (
    LanguageModelAnswerAuthor,
    LanguageModelEvidenceAssessor,
    ModelUsage,
)
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
from enterprise_rag.services.retrieval import (
    DualSearchResult,
    DualSearchService,
    SearchBranchResult,
)
from enterprise_rag.services.scope_root import RecoveredContext, RootContext, ScopeRootService


@dataclass(frozen=True, slots=True)
class _RetrievalOutcome:
    recovered: RecoveredContext
    selected_hits: tuple[RetrievalHit, ...]
    fusion: FusionDiagnostic
    reranker_degraded: bool


class _SemanticRecoveryExecutor:
    def __init__(self, runner: "SemanticQueryRunner", command: QueryCommand) -> None:
        self._runner = runner
        self._command = command
        self._roots: dict[str, RootContext] = {}

    def remember(self, roots: Sequence[RootContext]) -> None:
        self._roots.update((root.root_id, root) for root in roots)

    async def execute(self, action: RecoveryAction) -> Sequence[EvidenceItem]:
        retrieved = await self._runner._retrieve(
            self._command,
            action.scope,
            (action.query,),
            action.query,
            action.retrieval_mode,
        )
        self.remember(retrieved.recovered.roots)
        return self._runner._evidence(
            retrieved,
            round_number=action.round_number,
            route=action.route,
        )

    def roots_for(self, evidence: Sequence[EvidenceItem], *, limit: int) -> tuple[RootContext, ...]:
        root_ids = tuple(dict.fromkeys(item.root_id for item in evidence))
        return tuple(self._roots[item] for item in root_ids if item in self._roots)[:limit]


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
        max_output_tokens: int = 3_000,
        planner: QueryPlanningService | None = None,
        evidence_assessor: LanguageModelEvidenceAssessor | None = None,
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
        self._evidence_assessor = evidence_assessor or LanguageModelEvidenceAssessor(
            language_model
        )

    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        await self._progress(emit, QueryProgressStage.PLANNING, "执行查询改写与子查询规划")
        with start_span("rag.query_planning") as span:
            planned = await self._planner.plan(
                PlannerRequest(command.query, command.history, command.scope, command.mode)
            )
            plan = planned.plan
            span.set_attribute("provider.name", planned.provider)
            span.set_attribute("rag.query.mode", command.mode.value)
            span.set_attribute("rag.plan.original", plan.original_query)
            span.set_attribute("rag.plan.rewritten", plan.rewritten_query)
            span.set_attribute("rag.plan.intent", plan.intent.value)
            span.set_attribute("rag.plan.language", plan.language)
            span.set_attribute("rag.plan.sub_queries", plan.sub_queries)
            span.set_attribute("rag.plan.sub_query_count", len(plan.sub_queries))
            span.set_attribute("rag.degraded", planned.degraded)
            span.set_attribute("rag.llm_calls", planned.llm_calls)
            span.set_attribute("rag.input_tokens", planned.input_tokens)
            span.set_attribute("rag.output_tokens", planned.output_tokens)

        await self._progress(
            emit,
            QueryProgressStage.RETRIEVING,
            f"执行 {len(plan.sub_queries)} 条子查询的 Dense / Sparse 检索",
        )
        await self._vector_store.ensure_revision(
            IndexSchema(self._index_revision, self._embedding.dimension, self._sparse.mode)
        )
        retrieved = await self._retrieve(
            command,
            plan.scope,
            plan.sub_queries,
            plan.rewritten_query,
            RetrievalMode.HYBRID,
            emit=emit,
        )
        base_usage = ModelUsage(
            planned.llm_calls,
            planned.input_tokens,
            planned.output_tokens,
        )
        effective_plan = (
            plan
            if plan.requirements
            else replace(plan, requirements=(plan.rewritten_query,))
        )
        roots = retrieved.recovered.roots[:3]
        deep_decision: str | None = None
        recovery_rounds = 0
        assessor_degraded = False
        evidence_conflicts: tuple[str, ...] = ()
        if command.mode is QueryMode.DEEP:
            executor = _SemanticRecoveryExecutor(self, command)
            executor.remember(retrieved.recovered.roots)
            recovery = await DeepRecoveryController(
                assessor=self._evidence_assessor,
                executor=executor,
                max_rounds=2,
                always_assess=True,
            ).run(
                DeepRecoveryRequest(
                    effective_plan.rewritten_query,
                    effective_plan.requirements,
                    effective_plan.scope,
                    self._evidence(retrieved),
                    _repairable_scope_fields(command.scope, effective_plan.scope),
                )
            )
            base_usage += ModelUsage(
                recovery.assessor_calls,
                recovery.assessor_input_tokens,
                recovery.assessor_output_tokens,
            )
            deep_decision = recovery.decision.value
            recovery_rounds = recovery.recovery_rounds
            assessor_degraded = recovery.assessor_degraded
            evidence_conflicts = recovery.assessment.conflicts
            selected = EvidenceLedger(recovery.evidence).selected(
                top_k=8, recovery_reserve=2
            )
            roots = executor.roots_for(selected, limit=5)
            if recovery.decision is EvidenceDecision.ABSTAIN:
                await self._progress(
                    emit, QueryProgressStage.ANSWERING, "证据评估未通过，执行安全拒答"
                )
                return QueryExecution(
                    command.query_id,
                    QueryRunStatus.ABSTAINED,
                    "当前证据不足或存在冲突，无法可靠回答。",
                    (),
                    diagnostics=self._diagnostics(
                        mode=command.mode,
                        candidate_count=retrieved.fusion.unique_leaf_count,
                        citation_count=0,
                        planner_provider=planned.provider,
                        planner_degraded=planned.degraded,
                        reranker_degraded=retrieved.reranker_degraded,
                        deep_decision=deep_decision,
                        recovery_rounds=recovery_rounds,
                        assessor_degraded=assessor_degraded,
                        answer_status="not_generated",
                    ),
                    usage=_usage_dict(base_usage),
                )

        await self._progress(emit, QueryProgressStage.ANSWERING, "生成结构化回答并核验引用")
        if not roots:
            return QueryExecution(
                command.query_id,
                QueryRunStatus.NO_RESULTS,
                "当前授权范围内没有检索到可回答该问题的证据。",
                (),
                diagnostics=self._diagnostics(
                    mode=command.mode,
                    candidate_count=retrieved.fusion.unique_leaf_count,
                    citation_count=0,
                    planner_provider=planned.provider,
                    planner_degraded=planned.degraded,
                    reranker_degraded=retrieved.reranker_degraded,
                    deep_decision=deep_decision,
                    recovery_rounds=recovery_rounds,
                    assessor_degraded=assessor_degraded,
                    answer_status="not_generated",
                ),
                usage=_usage_dict(base_usage),
            )
        author = LanguageModelAnswerAuthor(
            self._language_model, max_output_tokens=self._max_output_tokens
        )
        with start_span(
            "rag.answer_generation",
            attributes={"provider.name": self._language_model.info().name},
        ) as span:
            try:
                authored = await author.draft(plan=effective_plan, roots=roots)
            except AppError as error:
                failed_usage = _error_usage(error)
                span.set_attribute("rag.degraded", True)
                span.set_attribute("error.code", error.code.value)
                span.set_attribute("rag.llm_calls", failed_usage.llm_calls)
                return QueryExecution(
                    command.query_id,
                    QueryRunStatus.ABSTAINED,
                    "回答模型未返回可核验的结构，已安全拒答。",
                    (),
                    diagnostics=self._diagnostics(
                        mode=command.mode,
                        candidate_count=retrieved.fusion.unique_leaf_count,
                        citation_count=0,
                        planner_provider=planned.provider,
                        planner_degraded=planned.degraded,
                        reranker_degraded=retrieved.reranker_degraded,
                        deep_decision=deep_decision,
                        recovery_rounds=recovery_rounds,
                        assessor_degraded=assessor_degraded,
                        answer_status="generation_degraded",
                    ),
                    usage=_usage_dict(base_usage + failed_usage),
                )
            span.set_attribute("rag.degraded", False)
            span.set_attribute("rag.llm_calls", authored.llm_calls)
            span.set_attribute("rag.input_tokens", authored.input_tokens)
            span.set_attribute("rag.output_tokens", authored.output_tokens)
            span.set_attribute("rag.citation_count", len(authored.draft.citations))
        with start_span("rag.answer_verification") as span:
            span.set_attribute("provider.name", self._language_model.info().name)
            answer = await AnswerVerificationService(author).finalize(
                plan=effective_plan,
                roots=roots,
                draft=authored.draft,
                evidence_conflicts=evidence_conflicts,
            )
            span.set_attribute("rag.answer.status", answer.status.value)
            span.set_attribute(
                "rag.answer.draft_citation_count", len(authored.draft.citations)
            )
            span.set_attribute("rag.answer.issue_count", len(answer.issues))
            span.set_attribute("rag.answer.repair_count", answer.repair_count)
            span.set_attribute("rag.answer.missing_count", len(answer.missing_requirements))
            span.set_attribute("rag.citation_count", len(answer.citations))
            span.set_attribute("rag.llm_calls", author.repair_usage.llm_calls)
            span.set_attribute("rag.input_tokens", author.repair_usage.input_tokens)
            span.set_attribute("rag.output_tokens", author.repair_usage.output_tokens)
        total_usage = base_usage + ModelUsage(
            authored.llm_calls,
            authored.input_tokens,
            authored.output_tokens,
        ) + author.repair_usage
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ABSTAINED
            if answer.status is AnswerStatus.ABSTAINED
            else QueryRunStatus.ANSWERED,
            answer.answer,
            answer.citations,
            diagnostics=self._diagnostics(
                mode=command.mode,
                candidate_count=retrieved.fusion.unique_leaf_count,
                citation_count=len(answer.citations),
                planner_provider=planned.provider,
                planner_degraded=planned.degraded,
                reranker_degraded=retrieved.reranker_degraded,
                deep_decision=deep_decision,
                recovery_rounds=recovery_rounds,
                assessor_degraded=assessor_degraded,
                answer_status=answer.status.value,
            ),
            usage=_usage_dict(total_usage),
        )

    async def _retrieve(
        self,
        command: QueryCommand,
        scope: QueryScope,
        queries: tuple[str, ...],
        rerank_query: str,
        mode: RetrievalMode,
        *,
        emit: ProgressSink | None = None,
    ) -> _RetrievalOutcome:
        with start_span(
            "rag.retrieval",
            attributes={
                "rag.branch_count": len(queries),
                "rag.retrieval_mode": mode.value,
            },
        ):
            if mode is RetrievalMode.HYBRID:
                searched = await asyncio.gather(
                    *(
                        self._search_branch(command, scope, query, branch_index)
                        for branch_index, query in enumerate(queries)
                    )
                )
                branches = tuple(
                    branch
                    for result in searched
                    for branch in (result.dense, result.sparse)
                )
            else:
                branches = (
                    await self._search_single_branch(
                        command, scope, queries[0], mode
                    ),
                )
        with start_span(
            "rag.rrf_fusion", attributes={"rag.retrieval_mode": mode.value}
        ) as span:
            fused = self._fusion.fuse_branches(branches)
            span.set_attribute("rag.candidate_count", len(fused.hits))
            span.set_attribute(
                "rag.fusion.ranked_list_count", fused.diagnostic.ranked_list_count
            )
            span.set_attribute(
                "rag.fusion.input_hit_count", fused.diagnostic.input_hit_count
            )
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

        if emit is not None:
            await self._progress(
                emit, QueryProgressStage.RERANKING, "使用当前 Reranker 重排授权候选"
            )
        authorization = ScopeAuthorization(command.tenant_id, True)
        async with self._database.session() as session:
            scope_root = ScopeRootService(
                PostgreSQLContextRepository(session),
                max_parent_chars=self._max_parent_chars,
            )
            with start_span("rag.auth_and_scope") as span:
                if fused.hits:
                    prepared = await scope_root.prepare_candidates(
                        authorization, scope, fused.hits
                    )
                    resolved_scope = prepared.scope
                    items = prepared.items
                    rejected_count = prepared.rejected_count
                else:
                    resolved_scope = await scope_root.resolve_scope(authorization, scope)
                    items = ()
                    rejected_count = 0
                span.set_attribute("rag.input_count", len(fused.hits))
                span.set_attribute("rag.output_count", len(items))
                span.set_attribute("rag.rejected_count", rejected_count)
            with start_span("rag.rerank") as span:
                reranked = await self._reranking.rerank(rerank_query, items)
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

            if emit is not None:
                await self._progress(emit, QueryProgressStage.RECOVERING, "恢复授权 Root 原文")
            with start_span("rag.root_restore") as span:
                recovered = await scope_root.recover(resolved_scope, reranked.hits)
                span.set_attribute("rag.input_count", len(reranked.hits))
                span.set_attribute("rag.output_count", len(recovered.roots))
                span.set_attribute("rag.rejected_count", recovered.rejected_count)
                span.set_attribute("rag.truncated_count", recovered.truncated_count)
                span.set_attribute("rag.used_chars", recovered.used_chars)
        return _RetrievalOutcome(
            recovered,
            reranked.hits,
            fused.diagnostic,
            reranked.degraded,
        )

    async def _search_single_branch(
        self,
        command: QueryCommand,
        scope: QueryScope,
        query: str,
        mode: RetrievalMode,
    ) -> SearchBranchResult:
        with start_span(
            "rag.retrieval.branch",
            attributes={
                "rag.branch.index": 0,
                "rag.branch.query": query,
                "rag.retrieval_mode": mode.value,
                "rag.branch.recovery": True,
                "rag.branch.sparse_algorithm": self._sparse.info().name,
            },
        ) as span:
            if mode is RetrievalMode.DENSE_ONLY:
                result = await self._search.search_dense(
                    query=query,
                    tenant_id=command.tenant_id,
                    index_revision=self._index_revision,
                    scope=scope,
                )
            elif mode is RetrievalMode.SPARSE_ONLY:
                result = await self._search.search_sparse(
                    query=query,
                    tenant_id=command.tenant_id,
                    index_revision=self._index_revision,
                    scope=scope,
                )
            else:
                raise ValueError("single-branch retrieval requires a single mode")
            span.set_attribute(
                f"rag.branch.{result.method.value}_requested",
                result.diagnostic.requested_top_k,
            )
            span.set_attribute(f"rag.branch.{result.method.value}_returned", len(result.hits))
            span.set_attribute("rag.branch.unique_count", len(result.hits))
            return result

    @staticmethod
    def _evidence(
        retrieved: _RetrievalOutcome,
        *,
        round_number: int = 0,
        route: RecoveryRoute | None = None,
    ) -> tuple[EvidenceItem, ...]:
        hits = {item.leaf_id: item for item in retrieved.selected_hits}
        result: list[EvidenceItem] = []
        for root in retrieved.recovered.roots:
            for leaf_id in root.leaf_ids:
                hit = hits.get(leaf_id)
                if hit is None:
                    continue
                score = hit.rerank_score if hit.rerank_score is not None else hit.fused_score
                confidence = (
                    float(score)
                    if 0 <= score <= 1
                    else 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, score))))
                )
                result.append(
                    EvidenceItem(
                        leaf_id,
                        root.root_id,
                        confidence,
                        (),
                        round_number,
                        route,
                        root.evidence_text or root.text,
                    )
                )
        return tuple(result)

    def _diagnostics(
        self,
        *,
        mode: QueryMode,
        candidate_count: int,
        citation_count: int,
        planner_provider: str,
        planner_degraded: bool,
        reranker_degraded: bool,
        deep_decision: str | None,
        recovery_rounds: int,
        assessor_degraded: bool,
        answer_status: str,
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
            "deep_decision": deep_decision,
            "recovery_rounds": recovery_rounds,
            "assessor_degraded": assessor_degraded,
            "answer_status": answer_status,
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
                "rag.branch.sparse_algorithm": self._sparse.info().name,
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


def _repairable_scope_fields(
    requested: QueryScope, planned: QueryScope
) -> tuple[str, ...]:
    """Only planner-inferred filters may be relaxed; caller filters are immutable."""
    fields = (
        "collection_ids",
        "document_ids",
        "titles",
        "organizations",
        "doc_types",
        "versions",
        "sections",
    )
    return tuple(
        name
        for name in fields
        if not getattr(requested, name) and bool(getattr(planned, name))
    )


def _usage_dict(usage: ModelUsage) -> dict[str, int]:
    return {
        "llm_calls": usage.llm_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }


def _error_usage(error: AppError) -> ModelUsage:
    def value(name: str) -> int:
        item = error.details.get(name)
        return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else 0

    return ModelUsage(value("llm_calls"), value("input_tokens"), value("output_tokens"))
