"""Idempotent PostgreSQL TraceStore with tenant cursor pagination."""

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.models import TraceRunModel, TraceSpanModel
from enterprise_rag.domain.common import require_utc
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.traces import (
    StoredSpan,
    TraceCompletion,
    TraceDetail,
    TracePage,
    TraceSummary,
)


class PostgreSQLTraceStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def persist(
        self, completion: TraceCompletion, spans: tuple[StoredSpan, ...]
    ) -> None:
        async with self._database.session() as session:
            run_values = {
                "trace_id": completion.trace_id,
                "tenant_id": completion.tenant_id,
                "actor_id": completion.actor_id,
                "actor_type": completion.actor_type,
                "trace_type": completion.trace_type,
                "subject_id": completion.subject_id,
                "request_id": completion.request_id,
                "mode": completion.mode,
                "status": completion.status,
                "started_at": completion.started_at,
                "finished_at": completion.finished_at,
                "duration_ms": round(completion.duration_ms),
                "usage": dict(completion.usage),
                "attributes": dict(completion.attributes),
            }
            statement = insert(TraceRunModel).values(**run_values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[TraceRunModel.trace_id],
                    set_={key: value for key, value in run_values.items() if key != "trace_id"},
                )
            )
            for span in spans:
                span_values = {
                    "trace_id": span.trace_id,
                    "span_id": span.span_id,
                    "parent_span_id": span.parent_span_id,
                    "name": span.name,
                    "started_at": span.started_at,
                    "finished_at": span.finished_at,
                    "duration_ms": round(span.duration_ms),
                    "status": span.status,
                    "attributes": dict(span.attributes),
                    "events": [dict(event) for event in span.events],
                }
                span_statement = insert(TraceSpanModel).values(**span_values)
                await session.execute(
                    span_statement.on_conflict_do_update(
                        index_elements=[TraceSpanModel.trace_id, TraceSpanModel.span_id],
                        set_={
                            key: value
                            for key, value in span_values.items()
                            if key not in {"trace_id", "span_id"}
                        },
                    )
                )

    async def list(
        self,
        tenant_id: UUID,
        *,
        trace_type: str | None,
        cursor: str | None,
        limit: int,
    ) -> TracePage:
        if not 1 <= limit <= 100:
            raise AppError(ErrorCode.VALIDATION_ERROR, "The trace page size is invalid.")
        cursor_value = _decode_cursor(cursor) if cursor else None
        span_count = (
            select(func.count())
            .select_from(TraceSpanModel)
            .where(TraceSpanModel.trace_id == TraceRunModel.trace_id)
            .correlate(TraceRunModel)
            .scalar_subquery()
        )
        statement = select(TraceRunModel, span_count).where(
            TraceRunModel.tenant_id == tenant_id
        )
        if trace_type is not None:
            if trace_type not in {"query", "ingestion", "evaluation"}:
                raise AppError(ErrorCode.VALIDATION_ERROR, "The trace type is invalid.")
            statement = statement.where(TraceRunModel.trace_type == trace_type)
        if cursor_value is not None:
            started_at, trace_id = cursor_value
            statement = statement.where(
                or_(
                    TraceRunModel.started_at < started_at,
                    and_(
                        TraceRunModel.started_at == started_at,
                        TraceRunModel.trace_id < trace_id,
                    ),
                )
            )
        statement = statement.order_by(
            TraceRunModel.started_at.desc(), TraceRunModel.trace_id.desc()
        ).limit(limit + 1)
        async with self._database.session() as session:
            rows = list((await session.execute(statement)).all())
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = tuple(_summary(run, int(count)) for run, count in selected)
        next_cursor = None
        if has_more and selected:
            next_cursor = _encode_cursor(selected[-1][0].started_at, selected[-1][0].trace_id)
        return TracePage(items, next_cursor)

    async def get(self, tenant_id: UUID, trace_id: str) -> TraceDetail | None:
        async with self._database.session() as session:
            run = await session.scalar(
                select(TraceRunModel).where(
                    TraceRunModel.trace_id == trace_id,
                    TraceRunModel.tenant_id == tenant_id,
                )
            )
            if run is None:
                return None
            span_models = list(
                (
                    await session.scalars(
                        select(TraceSpanModel)
                        .where(TraceSpanModel.trace_id == trace_id)
                        .order_by(TraceSpanModel.started_at, TraceSpanModel.span_id)
                    )
                ).all()
            )
        spans = tuple(_span(model) for model in span_models)
        return TraceDetail(
            _summary(run, len(spans)),
            run.actor_type,
            run.request_id,
            run.usage,
            run.attributes,
            spans,
        )


def _summary(model: TraceRunModel, span_count: int) -> TraceSummary:
    degraded = any(
        bool(value) for key, value in model.attributes.items() if key.endswith("_degraded")
    )
    return TraceSummary(
        model.trace_id,
        model.trace_type,
        model.subject_id,
        model.mode,
        model.status,
        model.started_at,
        model.finished_at,
        float(model.duration_ms),
        span_count,
        degraded,
    )


def _span(model: TraceSpanModel) -> StoredSpan:
    return StoredSpan(
        model.trace_id,
        model.span_id,
        model.parent_span_id,
        model.name,
        model.started_at,
        model.finished_at,
        model.status,
        model.attributes,
        tuple(model.events),
    )


def _encode_cursor(started_at: datetime, trace_id: str) -> str:
    payload = json.dumps([started_at.isoformat(), trace_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("invalid cursor shape")
        started_at = datetime.fromisoformat(value[0])
        trace_id = value[1]
        require_utc(started_at, "cursor timestamp")
        invalid_trace_id = not isinstance(trace_id, str) or len(trace_id) != 32
        if not invalid_trace_id:
            invalid_trace_id = any(
                character not in "0123456789abcdef" for character in trace_id
            )
        if invalid_trace_id:
            raise ValueError("invalid trace ID")
        return started_at, trace_id
    except (
        binascii.Error,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise AppError(ErrorCode.VALIDATION_ERROR, "The trace cursor is invalid.") from error
