"""Tenant-scoped PostgreSQL persistence for evaluation runs."""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.models import EvaluationRunModel
from enterprise_rag.domain.common import require_utc
from enterprise_rag.domain.errors import AppError, ErrorCode


@dataclass(frozen=True, slots=True)
class PersistedEvaluationRun:
    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    status: str
    dataset_revision: str | None
    mode: str
    provider_profile: str | None
    provider: str | None
    model: str | None
    prompt_revision: str | None
    index_revision: str | None
    commit_sha: str | None
    max_cases: int
    max_llm_calls: int
    estimated_llm_calls: int
    completed_cases: int
    total_cases: int
    case_ids: tuple[str, ...]
    report: dict[str, object] | None
    error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvaluationRunPage:
    items: tuple[PersistedEvaluationRun, ...]
    next_cursor: str | None


class PostgreSQLEvaluationRunStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        actor_id: UUID,
        dataset_revision: str,
        mode: str,
        provider_profile: str,
        provider: str,
        model: str,
        prompt_revision: str,
        index_revision: str,
        commit_sha: str,
        max_cases: int,
        max_llm_calls: int,
        estimated_llm_calls: int,
        case_ids: tuple[str, ...],
        now: datetime,
    ) -> PersistedEvaluationRun:
        require_utc(now, "now")
        row = EvaluationRunModel(
            id=run_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            status="queued",
            dataset_revision=dataset_revision,
            mode=mode,
            provider_profile=provider_profile,
            provider=provider,
            model=model,
            prompt_revision=prompt_revision,
            index_revision=index_revision,
            commit_sha=commit_sha,
            max_cases=max_cases,
            max_llm_calls=max_llm_calls,
            estimated_llm_calls=estimated_llm_calls,
            completed_cases=0,
            total_cases=len(case_ids),
            case_ids=list(case_ids),
            created_at=now,
        )
        async with self._database.session() as session:
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError as error:
                raise AppError(
                    ErrorCode.CONFLICT,
                    "An evaluation is already active for this tenant.",
                ) from error
        return _run(row)

    async def mark_running(
        self, tenant_id: UUID, run_id: UUID, *, now: datetime
    ) -> None:
        require_utc(now, "now")
        async with self._database.session() as session:
            started = await session.scalar(
                update(EvaluationRunModel)
                .where(
                    EvaluationRunModel.id == run_id,
                    EvaluationRunModel.tenant_id == tenant_id,
                    EvaluationRunModel.status == "queued",
                )
                .values(status="running", started_at=now)
                .returning(EvaluationRunModel.id)
            )
            if started is None:
                raise AppError(ErrorCode.CONFLICT, "The evaluation cannot be started.")

    async def update_progress(
        self, tenant_id: UUID, run_id: UUID, *, completed: int
    ) -> None:
        async with self._database.session() as session:
            await session.execute(
                update(EvaluationRunModel)
                .where(
                    EvaluationRunModel.id == run_id,
                    EvaluationRunModel.tenant_id == tenant_id,
                    EvaluationRunModel.status == "running",
                    EvaluationRunModel.completed_cases < completed,
                )
                .values(completed_cases=completed)
            )

    async def succeed(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        report: dict[str, object],
        now: datetime,
    ) -> None:
        require_utc(now, "now")
        async with self._database.session() as session:
            await session.execute(
                update(EvaluationRunModel)
                .where(
                    EvaluationRunModel.id == run_id,
                    EvaluationRunModel.tenant_id == tenant_id,
                    EvaluationRunModel.status == "running",
                )
                .values(
                    status="succeeded",
                    completed_cases=EvaluationRunModel.total_cases,
                    report=report,
                    finished_at=now,
                    error_code=None,
                )
            )

    async def fail(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        error_code: str,
        now: datetime,
    ) -> None:
        require_utc(now, "now")
        async with self._database.session() as session:
            await session.execute(
                update(EvaluationRunModel)
                .where(
                    EvaluationRunModel.id == run_id,
                    EvaluationRunModel.tenant_id == tenant_id,
                    EvaluationRunModel.status.in_(("queued", "running")),
                )
                .values(status="failed", error_code=error_code, finished_at=now)
            )

    async def get(
        self, tenant_id: UUID, run_id: UUID
    ) -> PersistedEvaluationRun | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(EvaluationRunModel).where(
                    EvaluationRunModel.id == run_id,
                    EvaluationRunModel.tenant_id == tenant_id,
                )
            )
        return _run(row) if row is not None else None

    async def list(
        self,
        tenant_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> EvaluationRunPage:
        if not 1 <= limit <= 100:
            raise AppError(ErrorCode.VALIDATION_ERROR, "The evaluation page size is invalid.")
        if status is not None and status not in {"queued", "running", "succeeded", "failed"}:
            raise AppError(ErrorCode.VALIDATION_ERROR, "The evaluation status is invalid.")
        cursor_value = _decode_cursor(cursor) if cursor else None
        statement = select(EvaluationRunModel).where(
            EvaluationRunModel.tenant_id == tenant_id
        )
        if status is not None:
            statement = statement.where(EvaluationRunModel.status == status)
        if cursor_value is not None:
            created_at, run_id = cursor_value
            statement = statement.where(
                or_(
                    EvaluationRunModel.created_at < created_at,
                    and_(
                        EvaluationRunModel.created_at == created_at,
                        EvaluationRunModel.id < run_id,
                    ),
                )
            )
        statement = statement.order_by(
            EvaluationRunModel.created_at.desc(), EvaluationRunModel.id.desc()
        ).limit(limit + 1)
        async with self._database.session() as session:
            rows = list((await session.scalars(statement)).all())
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit and selected:
            next_cursor = _encode_cursor(selected[-1].created_at, selected[-1].id)
        return EvaluationRunPage(tuple(_run(row) for row in selected), next_cursor)


def _run(model: EvaluationRunModel) -> PersistedEvaluationRun:
    return PersistedEvaluationRun(
        model.id,
        model.tenant_id,
        model.actor_id,
        model.status,
        model.dataset_revision,
        model.mode,
        model.provider_profile,
        model.provider,
        model.model,
        model.prompt_revision,
        model.index_revision,
        model.commit_sha,
        model.max_cases,
        model.max_llm_calls,
        model.estimated_llm_calls,
        model.completed_cases,
        model.total_cases,
        tuple(model.case_ids),
        dict(model.report) if model.report is not None else None,
        model.error_code,
        model.started_at,
        model.finished_at,
        model.created_at,
    )


def _encode_cursor(created_at: datetime, run_id: UUID) -> str:
    payload = json.dumps([created_at.isoformat(), str(run_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("invalid cursor shape")
        created_at = datetime.fromisoformat(value[0])
        run_id = UUID(value[1])
        require_utc(created_at, "cursor timestamp")
        return created_at, run_id
    except (
        binascii.Error,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise AppError(
            ErrorCode.VALIDATION_ERROR, "The evaluation cursor is invalid."
        ) from error
