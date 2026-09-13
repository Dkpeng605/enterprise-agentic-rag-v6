"""Tenant-scoped aggregate metrics for the workspace overview."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    EvaluationRunModel,
    IngestionJobModel,
    LeafModel,
    RootModel,
    TraceRunModel,
)
from enterprise_rag.domain.common import require_utc


@dataclass(frozen=True, slots=True)
class OverviewActivity:
    id: str
    kind: str
    status: str
    label: str
    started_at: datetime
    progress: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "label": self.label,
            "started_at": self.started_at.isoformat(),
            "progress": self.progress,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceOverview:
    generated_at: datetime
    collection_count: int
    document_counts: dict[str, int]
    root_count: int
    leaf_count: int
    queries_24h: int
    query_errors_24h: int
    query_error_rate: float | None
    query_p95_ms: float | None
    recent_activity: tuple[OverviewActivity, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "collection_count": self.collection_count,
            "document_counts": dict(self.document_counts),
            "root_count": self.root_count,
            "leaf_count": self.leaf_count,
            "queries_24h": self.queries_24h,
            "query_errors_24h": self.query_errors_24h,
            "query_error_rate": self.query_error_rate,
            "query_p95_ms": self.query_p95_ms,
            "recent_activity": [item.to_dict() for item in self.recent_activity],
        }


class WorkspaceOverviewService:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, tenant_id: UUID, *, now: datetime) -> WorkspaceOverview:
        require_utc(now, "now")
        since = now - timedelta(hours=24)
        async with self._database.session() as session:
            collection_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(CollectionModel)
                    .where(
                        CollectionModel.tenant_id == tenant_id,
                        CollectionModel.status == "active",
                    )
                )
                or 0
            )
            document_rows = (
                await session.execute(
                    select(DocumentModel.status, func.count())
                    .where(
                        DocumentModel.tenant_id == tenant_id,
                        DocumentModel.status != "deleted",
                    )
                    .group_by(DocumentModel.status)
                )
            ).all()
            root_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(RootModel)
                    .where(RootModel.tenant_id == tenant_id)
                )
                or 0
            )
            leaf_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(LeafModel)
                    .where(LeafModel.tenant_id == tenant_id)
                )
                or 0
            )
            query_count, query_errors, query_p95 = (
                await session.execute(
                    select(
                        func.count(),
                        func.count().filter(
                            TraceRunModel.status.in_(("error", "failed", "cancelled"))
                        ),
                        func.percentile_disc(0.95).within_group(TraceRunModel.duration_ms),
                    ).where(
                        TraceRunModel.tenant_id == tenant_id,
                        TraceRunModel.trace_type == "query",
                        TraceRunModel.started_at >= since,
                    )
                )
            ).one()
            job_rows = list(
                (
                    await session.scalars(
                        select(IngestionJobModel)
                        .where(IngestionJobModel.tenant_id == tenant_id)
                        .order_by(IngestionJobModel.created_at.desc(), IngestionJobModel.id.desc())
                        .limit(6)
                    )
                ).all()
            )
            evaluation_rows = list(
                (
                    await session.scalars(
                        select(EvaluationRunModel)
                        .where(EvaluationRunModel.tenant_id == tenant_id)
                        .order_by(
                            EvaluationRunModel.created_at.desc(),
                            EvaluationRunModel.id.desc(),
                        )
                        .limit(6)
                    )
                ).all()
            )
            legacy_evaluation_rows = list(
                (
                    await session.scalars(
                        select(TraceRunModel)
                        .where(
                            TraceRunModel.tenant_id == tenant_id,
                            TraceRunModel.trace_type == "evaluation",
                        )
                        .order_by(
                            TraceRunModel.started_at.desc(), TraceRunModel.trace_id.desc()
                        )
                        .limit(6)
                    )
                ).all()
            )

        counts = {status: 0 for status in ("pending", "processing", "ready", "failed", "deleting")}
        for status, count in document_rows:
            if status in counts:
                counts[status] = int(count)
        queries = int(query_count)
        errors = int(query_errors)
        activities = [
            OverviewActivity(
                str(job.id),
                "ingestion",
                job.status,
                job.type,
                job.created_at,
                job.progress,
            )
            for job in job_rows
        ] + [
            OverviewActivity(
                str(run.id),
                "evaluation",
                run.status,
                run.mode,
                run.started_at or run.created_at,
                round(run.completed_cases / run.total_cases * 100),
            )
            for run in evaluation_rows
        ] + [
            OverviewActivity(
                trace.trace_id,
                "evaluation",
                trace.status,
                trace.mode or "evaluation",
                trace.started_at,
            )
            for trace in legacy_evaluation_rows
        ]
        activities.sort(key=lambda item: (item.started_at, item.id), reverse=True)
        return WorkspaceOverview(
            now,
            collection_count,
            counts,
            root_count,
            leaf_count,
            queries,
            errors,
            round(errors / queries, 4) if queries else None,
            float(query_p95) if query_p95 is not None else None,
            tuple(activities[:6]),
        )
