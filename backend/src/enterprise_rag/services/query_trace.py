"""Stable, sanitized projection for the Query Trace workspace."""

from dataclasses import dataclass

from enterprise_rag.ports.traces import StoredSpan, TraceDetail, TraceSummary


@dataclass(frozen=True, slots=True)
class QueryWaterfallStage:
    span_id: str
    parent_span_id: str | None
    name: str
    offset_ms: float
    duration_ms: float
    status: str
    degraded: bool


@dataclass(frozen=True, slots=True)
class QueryRankChange:
    leaf_id: str
    root_id: str | None
    dense_rank: int | None = None
    sparse_rank: int | None = None
    rrf_rank: int | None = None
    rerank_rank: int | None = None
    dense_score: float | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    matched_queries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryRecoveryRound:
    round_number: int
    route: str
    retrieval_mode: str
    target_count: int
    returned_count: int
    added_count: int
    duplicate_count: int


@dataclass(frozen=True, slots=True)
class QueryDegradation:
    component: str
    provider: str | None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class QueryPlanSnapshot:
    provider: str
    degraded: bool
    original_query: str
    rewritten_query: str
    intent: str
    language: str
    sub_queries: tuple[str, ...]
    requirements: tuple[str, ...] = ()
    use_sub_queries: bool = False


@dataclass(frozen=True, slots=True)
class QueryRetrievalBranch:
    branch_index: int
    query: str
    dense_requested: int
    dense_returned: int
    sparse_requested: int
    sparse_returned: int
    sparse_algorithm: str
    overlap_count: int
    unique_count: int


@dataclass(frozen=True, slots=True)
class QueryStageMetric:
    stage: str
    input_count: int
    output_count: int
    dropped_count: int
    attributes: dict[str, int | float | str]
    covered_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryTraceView:
    summary: TraceSummary
    usage: dict[str, int | float]
    stages: tuple[QueryWaterfallStage, ...]
    rankings: tuple[QueryRankChange, ...]
    recovery_rounds: tuple[QueryRecoveryRound, ...]
    degradations: tuple[QueryDegradation, ...]
    plan: QueryPlanSnapshot | None
    retrieval_branches: tuple[QueryRetrievalBranch, ...]
    stage_metrics: tuple[QueryStageMetric, ...]


def project_query_trace(detail: TraceDetail) -> QueryTraceView:
    if detail.summary.trace_type != "query":
        raise ValueError("only query traces can be projected")
    stages = tuple(_waterfall(detail.summary, span) for span in detail.spans)
    return QueryTraceView(
        detail.summary,
        _usage(detail),
        stages,
        _rankings(detail.spans),
        _recovery_rounds(detail.spans),
        _degradations(detail),
        _plan(detail.spans),
        _retrieval_branches(detail.spans),
        _stage_metrics(detail.spans),
    )


def _waterfall(summary: TraceSummary, span: StoredSpan) -> QueryWaterfallStage:
    return QueryWaterfallStage(
        span.span_id,
        span.parent_span_id,
        span.name,
        max(0.0, (span.started_at - summary.started_at).total_seconds() * 1_000),
        span.duration_ms,
        span.status,
        _bool(span.attributes.get("rag.degraded")),
    )


def _usage(detail: TraceDetail) -> dict[str, int | float]:
    return {
        key: value
        for key, value in detail.usage.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def _rankings(spans: tuple[StoredSpan, ...]) -> tuple[QueryRankChange, ...]:
    candidates: dict[str, dict[str, object]] = {}
    insertion_order: dict[str, int] = {}
    for span in spans:
        for event in span.events:
            name = event.get("name")
            if name not in {
                "rag.retrieval.candidate",
                "rag.fusion.candidate",
                "rag.rerank.candidate",
            }:
                continue
            attributes = event.get("attributes")
            if not isinstance(attributes, dict):
                continue
            leaf_id = _text(attributes.get("rag.leaf_id"))
            if leaf_id is None:
                continue
            insertion_order.setdefault(leaf_id, len(insertion_order))
            candidate = candidates.setdefault(leaf_id, {"leaf_id": leaf_id})
            root_id = _text(attributes.get("rag.root_id"))
            if root_id is not None:
                candidate["root_id"] = root_id
            rank = _integer(attributes.get("rag.rank"))
            if name == "rag.retrieval.candidate":
                method = _text(attributes.get("rag.method"))
                if method in {"dense", "sparse"}:
                    _record_best_rank(
                        candidate,
                        method,
                        rank,
                        _number(attributes.get("rag.score")),
                    )
            elif name == "rag.fusion.candidate":
                candidate["rrf_rank"] = rank
                candidate["rrf_score"] = _number(
                    attributes.get("rag.fused_score")
                )
                candidate["dense_rank"] = _integer(
                    attributes.get("rag.dense_rank")
                ) or candidate.get("dense_rank")
                candidate["sparse_rank"] = _integer(
                    attributes.get("rag.sparse_rank")
                ) or candidate.get("sparse_rank")
                candidate["matched_queries"] = _merge_texts(
                    candidate.get("matched_queries"),
                    attributes.get("rag.matched_queries"),
                )
            else:
                candidate["rerank_rank"] = rank
                candidate["rerank_score"] = _number(
                    attributes.get("rag.rerank_score")
                )
                candidate["rrf_score"] = _number(
                    attributes.get("rag.fused_score")
                ) or candidate.get("rrf_score")
                candidate["matched_queries"] = _merge_texts(
                    candidate.get("matched_queries"),
                    attributes.get("rag.matched_queries"),
                )
    projected = tuple(
        QueryRankChange(
            leaf_id=leaf_id,
            root_id=_text(candidate.get("root_id")),
            dense_rank=_integer(candidate.get("dense_rank")),
            sparse_rank=_integer(candidate.get("sparse_rank")),
            rrf_rank=_integer(candidate.get("rrf_rank")),
            rerank_rank=_integer(candidate.get("rerank_rank")),
            dense_score=_number(candidate.get("dense_score")),
            sparse_score=_number(candidate.get("sparse_score")),
            rrf_score=_number(candidate.get("rrf_score")),
            rerank_score=_number(candidate.get("rerank_score")),
            matched_queries=_texts(candidate.get("matched_queries")),
        )
        for leaf_id, candidate in candidates.items()
    )
    return tuple(
        sorted(
            projected,
            key=lambda item: (
                item.rerank_rank or 10_000,
                item.rrf_rank or 10_000,
                insertion_order[item.leaf_id],
            ),
        )
    )


def _recovery_rounds(spans: tuple[StoredSpan, ...]) -> tuple[QueryRecoveryRound, ...]:
    rounds: list[QueryRecoveryRound] = []
    for span in spans:
        if span.name != "rag.deep_recovery.round":
            continue
        values = span.attributes
        round_number = _integer(values.get("rag.recovery.round"))
        route = _text(values.get("rag.recovery.route"))
        retrieval_mode = _text(values.get("rag.recovery.retrieval_mode"))
        if round_number is None or route is None or retrieval_mode is None:
            continue
        rounds.append(
            QueryRecoveryRound(
                round_number,
                route,
                retrieval_mode,
                _integer(values.get("rag.recovery.target_count")) or 0,
                _integer(values.get("rag.recovery.returned_count")) or 0,
                _integer(values.get("rag.recovery.added_count")) or 0,
                _integer(values.get("rag.recovery.duplicate_count")) or 0,
            )
        )
    return tuple(sorted(rounds, key=lambda item: item.round_number))


def _degradations(detail: TraceDetail) -> tuple[QueryDegradation, ...]:
    values: dict[str, QueryDegradation] = {}
    for key, value in detail.attributes.items():
        if not key.endswith("_degraded") or value is not True:
            continue
        component = key.removesuffix("_degraded")
        # The run-level diagnostic predates the span-level name
        # ``evidence_assessor``.  Normalize it before merging so one assessor
        # fallback is rendered once in the API/UI, while the span can still
        # supply its provider and stable reason code.
        if component == "assessor":
            component = "evidence_assessor"
        provider = _text(detail.attributes.get(f"{component}_provider"))
        values[component] = QueryDegradation(
            component,
            provider,
            _text(detail.attributes.get(f"{component}_degradation_code")),
        )
    span_components = {
        "rag.query_planning": "planner",
        "rag.rerank": "reranker",
        "rag.deep_recovery.assess": "evidence_assessor",
        "rag.answer_generation": "answer_generation",
    }
    for span in detail.spans:
        span_component = span_components.get(span.name)
        if span_component is None or not _bool(span.attributes.get("rag.degraded")):
            continue
        reason_code = _text(span.attributes.get("rag.recovery.assessor_failure_code"))
        current = values.get(span_component)
        if current is None or (current.reason_code is None and reason_code is not None):
            values[span_component] = QueryDegradation(
                span_component,
                _text(span.attributes.get("provider.name")),
                reason_code,
            )
        elif current.provider is None:
            values[span_component] = QueryDegradation(
                current.component,
                _text(span.attributes.get("provider.name")),
                current.reason_code,
            )
    return tuple(sorted(values.values(), key=lambda item: item.component))


def _plan(spans: tuple[StoredSpan, ...]) -> QueryPlanSnapshot | None:
    span = next((item for item in spans if item.name == "rag.query_planning"), None)
    if span is None:
        return None
    values = span.attributes
    provider = _text(values.get("provider.name"))
    original = _text(values.get("rag.plan.original"))
    rewritten = _text(values.get("rag.plan.rewritten"))
    intent = _text(values.get("rag.plan.intent"))
    language = _text(values.get("rag.plan.language"))
    sub_queries = _texts(values.get("rag.plan.sub_queries"))
    requirements = _texts(values.get("rag.plan.requirements"))
    if None in {provider, original, rewritten, intent, language} or not sub_queries:
        return None
    # Older traces did not persist the explicit switch. Their multi-route plan
    # was necessarily an opted-in plan under the previous contract, so infer it
    # only when the field is absent; an explicit false must remain false.
    use_sub_queries = (
        _bool(values.get("rag.plan.use_sub_queries"))
        if "rag.plan.use_sub_queries" in values
        else len(sub_queries) > 1
    )
    return QueryPlanSnapshot(
        provider or "",
        _bool(values.get("rag.degraded")),
        original or "",
        rewritten or "",
        intent or "",
        language or "",
        sub_queries,
        requirements,
        use_sub_queries,
    )


def _retrieval_branches(spans: tuple[StoredSpan, ...]) -> tuple[QueryRetrievalBranch, ...]:
    branches: list[QueryRetrievalBranch] = []
    for span in spans:
        if span.name != "rag.retrieval.branch":
            continue
        values = span.attributes
        if _bool(values.get("rag.branch.recovery")):
            continue
        index = _integer(values.get("rag.branch.index"))
        query = _text(values.get("rag.branch.query"))
        if index is None or query is None:
            continue
        branches.append(
            QueryRetrievalBranch(
                index,
                query,
                _integer(values.get("rag.branch.dense_requested")) or 0,
                _integer(values.get("rag.branch.dense_returned")) or 0,
                _integer(values.get("rag.branch.sparse_requested")) or 0,
                _integer(values.get("rag.branch.sparse_returned")) or 0,
                _text(values.get("rag.branch.sparse_algorithm")) or "unknown",
                _integer(values.get("rag.branch.overlap_count")) or 0,
                _integer(values.get("rag.branch.unique_count")) or 0,
            )
        )
    return tuple(sorted(branches, key=lambda item: item.branch_index))


def _stage_metrics(spans: tuple[StoredSpan, ...]) -> tuple[QueryStageMetric, ...]:
    metrics: list[QueryStageMetric] = []
    for span in spans:
        values = span.attributes
        if span.name == "rag.query_planning":
            sub_query_count = _integer(values.get("rag.plan.sub_query_count")) or 0
            metrics.append(
                QueryStageMetric(
                    "query_planning",
                    1,
                    sub_query_count,
                    0,
                    {
                        "llm_calls": _integer(values.get("rag.llm_calls")) or 0,
                        "input_tokens": _integer(values.get("rag.input_tokens")) or 0,
                        "output_tokens": _integer(values.get("rag.output_tokens")) or 0,
                    },
                )
            )
        elif span.name == "rag.rrf_fusion":
            input_count = _integer(values.get("rag.fusion.input_hit_count")) or 0
            output_count = _integer(values.get("rag.candidate_count")) or 0
            root_dropped = _integer(values.get("rag.fusion.root_quota_dropped")) or 0
            top_k_dropped = _integer(values.get("rag.fusion.top_k_dropped")) or 0
            unique_count = _integer(values.get("rag.fusion.unique_leaf_count")) or 0
            metrics.append(
                QueryStageMetric(
                    "rrf_fusion",
                    input_count,
                    output_count,
                    max(0, input_count - output_count),
                    {
                        "ranked_lists": _integer(
                            values.get("rag.fusion.ranked_list_count")
                        )
                        or 0,
                        "unique_leaves": unique_count,
                        "duplicate_collapsed": max(0, input_count - unique_count),
                        "root_quota_dropped": root_dropped,
                        "top_k_dropped": top_k_dropped,
                        "max_leaves_per_root": _integer(
                            values.get("rag.fusion.max_leaves_per_root")
                        )
                        or 0,
                    },
                )
            )
        elif span.name in {"rag.auth_and_scope", "rag.rerank", "rag.root_restore"}:
            input_count = _integer(values.get("rag.input_count")) or 0
            output_count = _integer(values.get("rag.output_count")) or 0
            rejected = _integer(values.get("rag.rejected_count")) or 0
            attributes: dict[str, int | float | str] = {}
            if span.name == "rag.rerank":
                attributes["rerank_candidates"] = (
                    _integer(values.get("rag.candidate_count")) or 0
                )
                attributes["sub_queries_with_candidates"] = _integer(
                    values.get("rag.sub_query_candidate_count")
                ) or 0
                attributes["sub_queries_selected"] = _integer(
                    values.get("rag.sub_query_selected_count")
                ) or 0
            if span.name == "rag.root_restore":
                attributes["truncated_roots"] = (
                    _integer(values.get("rag.truncated_count")) or 0
                )
                attributes["used_chars"] = _integer(values.get("rag.used_chars")) or 0
            metrics.append(
                QueryStageMetric(
                    span.name.removeprefix("rag."),
                    input_count,
                    output_count,
                    rejected if span.name != "rag.rerank" else max(0, input_count - output_count),
                    attributes,
                )
            )
        elif span.name == "rag.answer_generation":
            llm_calls = _integer(values.get("rag.llm_calls")) or 0
            citations = _integer(values.get("rag.citation_count")) or 0
            metrics.append(
                QueryStageMetric(
                    "answer_generation",
                    citations,
                    citations,
                    0,
                    {
                        "llm_calls": llm_calls,
                        "input_tokens": _integer(values.get("rag.input_tokens")) or 0,
                        "output_tokens": _integer(values.get("rag.output_tokens")) or 0,
                        "citations": citations,
                    },
                )
            )
        elif span.name == "rag.deep_recovery.assess":
            evidence = _integer(values.get("rag.recovery.evidence_count")) or 0
            covered = _integer(values.get("rag.recovery.covered_count")) or 0
            assessment_attributes: dict[str, int | float | str] = {
                "llm_calls": _integer(values.get("rag.llm_calls")) or 0,
                "input_tokens": _integer(values.get("rag.input_tokens")) or 0,
                "output_tokens": _integer(values.get("rag.output_tokens")) or 0,
                "decision": _text(values.get("rag.recovery.decision")) or "unknown",
            }
            failure_code = _text(values.get("rag.recovery.assessor_failure_code"))
            if failure_code is not None:
                assessment_attributes["failure_code"] = failure_code
            metrics.append(
                QueryStageMetric(
                    "evidence_assessment",
                    evidence,
                    covered,
                    _integer(values.get("rag.recovery.missing_count")) or 0,
                    assessment_attributes,
                    covered_requirements=_texts(
                        values.get("rag.recovery.covered_requirements")
                    ),
                    missing_requirements=_texts(
                        values.get("rag.recovery.missing_requirements")
                    ),
                )
            )
        elif span.name == "rag.answer_verification":
            input_count = _integer(values.get("rag.answer.draft_citation_count")) or 0
            citations = _integer(values.get("rag.citation_count")) or 0
            metrics.append(
                QueryStageMetric(
                    "answer_verification",
                    input_count,
                    citations,
                    max(0, input_count - citations),
                    {
                        "status": _text(values.get("rag.answer.status")) or "unknown",
                        "issues": _integer(values.get("rag.answer.issue_count")) or 0,
                        "repairs": _integer(values.get("rag.answer.repair_count")) or 0,
                        "llm_calls": _integer(values.get("rag.llm_calls")) or 0,
                        "input_tokens": _integer(values.get("rag.input_tokens")) or 0,
                        "output_tokens": _integer(values.get("rag.output_tokens")) or 0,
                    },
                    missing_requirements=_texts(
                        values.get("rag.answer.missing_requirements")
                    ),
                    issues=_texts(values.get("rag.answer.issues")),
                )
            )
    order = {
        "query_planning": 0,
        "rrf_fusion": 1,
        "auth_and_scope": 2,
        "rerank": 3,
        "root_restore": 4,
        "evidence_assessment": 5,
        "answer_generation": 6,
        "answer_verification": 7,
    }
    return tuple(sorted(metrics, key=lambda item: order.get(item.stage, 99)))


def _record_best_rank(
    candidate: dict[str, object],
    method: str,
    rank: int | None,
    score: float | None,
) -> None:
    if rank is None:
        return
    current = _integer(candidate.get(f"{method}_rank"))
    if current is None or rank < current:
        candidate[f"{method}_rank"] = rank
        candidate[f"{method}_score"] = score


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _bool(value: object) -> bool:
    return value is True


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _merge_texts(first: object, second: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*_texts(first), *_texts(second))))
