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


@dataclass(frozen=True, slots=True)
class QueryTraceView:
    summary: TraceSummary
    usage: dict[str, int | float]
    stages: tuple[QueryWaterfallStage, ...]
    rankings: tuple[QueryRankChange, ...]
    recovery_rounds: tuple[QueryRecoveryRound, ...]
    degradations: tuple[QueryDegradation, ...]


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
    candidates: dict[str, dict[str, str | int | float | None]] = {}
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
                    candidate[f"{method}_rank"] = rank
                    candidate[f"{method}_score"] = _number(
                        attributes.get("rag.score")
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
            else:
                candidate["rerank_rank"] = rank
                candidate["rerank_score"] = _number(
                    attributes.get("rag.rerank_score")
                )
                candidate["rrf_score"] = _number(
                    attributes.get("rag.fused_score")
                ) or candidate.get("rrf_score")
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
    values: list[QueryDegradation] = []
    for key, value in detail.attributes.items():
        if not key.endswith("_degraded") or value is not True:
            continue
        component = key.removesuffix("_degraded")
        provider = _text(detail.attributes.get(f"{component}_provider"))
        values.append(QueryDegradation(component, provider))
    return tuple(sorted(values, key=lambda item: item.component))


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
