"""Stable, sanitized projection for the Ingestion Trace workspace."""

from dataclasses import dataclass

from enterprise_rag.ports.traces import StoredSpan, TraceDetail, TraceSummary

_BATCH_SPAN = "rag.ingestion.projection.batch"


@dataclass(frozen=True, slots=True)
class IngestionTraceStage:
    span_id: str
    parent_span_id: str | None
    name: str
    offset_ms: float
    duration_ms: float
    status: str
    root_count: int | None
    leaf_count: int | None
    expected_count: int | None
    verified_count: int | None
    batch_count: int | None


@dataclass(frozen=True, slots=True)
class IngestionTraceBatch:
    span_id: str
    parent_span_id: str | None
    phase: str
    batch_index: int
    batch_count: int
    item_count: int
    written_count: int | None
    offset_ms: float
    duration_ms: float
    status: str


@dataclass(frozen=True, slots=True)
class IngestionTraceView:
    summary: TraceSummary
    attempt: int
    progress: int
    completed: bool
    error_code: str | None
    stages: tuple[IngestionTraceStage, ...]
    batches: tuple[IngestionTraceBatch, ...]


def project_ingestion_trace(detail: TraceDetail) -> IngestionTraceView:
    if detail.summary.trace_type != "ingestion":
        raise ValueError("only ingestion traces can be projected")
    stages: list[IngestionTraceStage] = []
    batches: list[IngestionTraceBatch] = []
    for span in detail.spans:
        if span.name == _BATCH_SPAN:
            batch = _batch(detail.summary, span)
            if batch is not None:
                batches.append(batch)
        elif span.name == "rag.ingestion" or span.name.startswith("rag.ingestion."):
            stages.append(_stage(detail.summary, span))
    return IngestionTraceView(
        detail.summary,
        _integer(detail.attributes.get("attempt")) or 0,
        _integer(detail.attributes.get("progress")) or 0,
        detail.attributes.get("completed") is True,
        _text(detail.attributes.get("error_code")),
        tuple(stages),
        tuple(
            sorted(
                batches,
                key=lambda item: (item.offset_ms, item.phase, item.batch_index),
            )
        ),
    )


def _stage(summary: TraceSummary, span: StoredSpan) -> IngestionTraceStage:
    values = span.attributes
    return IngestionTraceStage(
        span.span_id,
        span.parent_span_id,
        span.name,
        _offset(summary, span),
        span.duration_ms,
        span.status,
        _integer(values.get("rag.ingestion.root_count")),
        _integer(values.get("rag.ingestion.leaf_count")),
        _integer(values.get("rag.ingestion.expected_count")),
        _integer(values.get("rag.ingestion.verified_count")),
        _integer(values.get("rag.ingestion.batch_count")),
    )


def _batch(summary: TraceSummary, span: StoredSpan) -> IngestionTraceBatch | None:
    values = span.attributes
    phase = _text(values.get("rag.ingestion.batch.phase"))
    batch_index = _integer(values.get("rag.ingestion.batch.index"))
    batch_count = _integer(values.get("rag.ingestion.batch.count"))
    item_count = _integer(values.get("rag.ingestion.batch.item_count"))
    if phase is None or batch_index is None or batch_count is None or item_count is None:
        return None
    return IngestionTraceBatch(
        span.span_id,
        span.parent_span_id,
        phase,
        batch_index,
        batch_count,
        item_count,
        _integer(values.get("rag.ingestion.batch.written_count")),
        _offset(summary, span),
        span.duration_ms,
        span.status,
    )


def _offset(summary: TraceSummary, span: StoredSpan) -> float:
    return max(0.0, (span.started_at - summary.started_at).total_seconds() * 1_000)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
