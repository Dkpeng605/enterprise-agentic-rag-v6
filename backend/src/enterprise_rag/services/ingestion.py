"""Lease-aware, resumable ingestion Pipeline orchestration."""

import asyncio
import logging
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import TracerProvider

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.ingestion import (
    IngestionContentRepository,
    IngestionWork,
)
from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.domain.common import utc_now
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.jobs import JobSnapshot, JobStatus
from enterprise_rag.observability import (
    ApplicationMetrics,
    bind_context,
    bind_metrics,
    current_metrics,
    start_span,
)
from enterprise_rag.ports.cleaner import Cleaner, CleanResult
from enterprise_rag.ports.loader import BinarySource, IngestionContext, LoadedRoot, Loader
from enterprise_rag.ports.object_store import ObjectStore
from enterprise_rag.ports.splitter import Splitter
from enterprise_rag.ports.traces import TraceCompletion, TraceRecorder
from enterprise_rag.ports.vector_store import VectorStore
from enterprise_rag.ports.vision import CaptionStatus
from enterprise_rag.services.images import EnrichedImage, ImageEnricher
from enterprise_rag.services.projection import ProjectionRequest, ProjectionService

Clock = Callable[[], datetime]
LOGGER = logging.getLogger(__name__)
_TERMINAL_INPUT_ERRORS = frozenset(
    {
        ErrorCode.DOCUMENT_UNSUPPORTED_TYPE,
        ErrorCode.DOCUMENT_EMPTY,
        ErrorCode.DOCUMENT_ENCRYPTED,
        ErrorCode.DOCUMENT_CORRUPT,
        ErrorCode.DOCUMENT_ENCODING_INVALID,
        ErrorCode.OCR_LANGUAGE_MISSING,
    }
)


class _PipelineCancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    job: JobSnapshot
    completed: bool


@dataclass(frozen=True, slots=True)
class _StoredSource(BinarySource):
    name: str
    media_type: str
    object_key: str
    object_store: ObjectStore

    def chunks(self) -> AsyncIterator[bytes]:
        return self.object_store.read(self.object_key)


class _LeaseHeartbeat:
    """Renew a running job while an external Provider call is still in flight."""

    def __init__(
        self,
        job: JobSnapshot,
        owner: str,
        database: Database,
        lease_for: timedelta,
        clock: Clock,
    ) -> None:
        self._job = job
        self._owner = owner
        self._database = database
        self._lease_for = lease_for
        self._clock = clock
        self._progress = job.progress
        self._stage = job.stage or "running"
        self._interval = max(0.1, min(lease_for.total_seconds() / 3, 30.0))
        self._task: asyncio.Task[None] | None = None

    def update(self, progress: int, stage: str) -> None:
        self._progress = progress
        self._stage = stage

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"ingestion-lease-heartbeat-{self._job.id}"
        )

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                async with self._database.session() as session:
                    await IngestionJobRepository(session).heartbeat(
                        self._job.id,
                        owner=self._owner,
                        now=self._clock(),
                        lease_for=self._lease_for,
                        progress=self._progress,
                        stage=self._stage,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The next interval retries. The foreground checkpoint remains
                # authoritative if a database outage outlasts the lease.
                LOGGER.warning(
                    "rag.ingestion.lease_heartbeat_failed",
                    extra={
                        "event_code": "INGESTION_LEASE_HEARTBEAT_FAILED",
                        "outcome": "degraded",
                        "job_id": str(self._job.id),
                    },
                    exc_info=True,
                )


_ACTIVE_LEASE: ContextVar[_LeaseHeartbeat | None] = ContextVar(
    "enterprise_rag_active_ingestion_lease", default=None
)


class IngestionPipeline:
    def __init__(
        self,
        *,
        database: Database,
        object_store: ObjectStore,
        loaders: Sequence[Loader],
        cleaner: Cleaner,
        splitter: Splitter,
        image_enricher: ImageEnricher,
        projection: ProjectionService,
        vector_store: VectorStore,
        temporary_root: Path,
        index_revision: str,
        lease_for: timedelta = timedelta(minutes=2),
        retry_delay: timedelta = timedelta(seconds=5),
        tracer_provider: TracerProvider | None = None,
        trace_recorder: TraceRecorder | None = None,
        metrics: ApplicationMetrics | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if not loaders:
            raise ValueError("ingestion Pipeline requires at least one Loader")
        if lease_for <= timedelta(0) or retry_delay < timedelta(0):
            raise ValueError("lease and retry durations are invalid")
        if not index_revision.strip():
            raise ValueError("index_revision must not be blank")
        self._database = database
        self._object_store = object_store
        self._loaders = tuple(loaders)
        self._cleaner = cleaner
        self._splitter = splitter
        self._image_enricher = image_enricher
        self._projection = projection
        self._vector_store = vector_store
        self._temporary_root = temporary_root
        self._temporary_root.mkdir(parents=True, exist_ok=True)
        self._index_revision = index_revision
        self._lease_for = lease_for
        self._retry_delay = retry_delay
        self._tracer_provider = tracer_provider
        self._trace_recorder = trace_recorder
        self._metrics = metrics
        self._clock = clock

    def _with_cleaning_audit(self, result: CleanResult) -> CleanResult:
        provider = self._cleaner.info()
        metadata = dict(result.root.metadata)
        metadata["cleaning"] = {
            "provider": provider.name,
            "version": provider.version,
            "audit": [
                {
                    "rule": item.rule,
                    "occurrences": item.occurrences,
                    "before_sha256": item.before_sha256,
                    "after_sha256": item.after_sha256,
                }
                for item in result.audit
            ],
        }
        return replace(result, root=replace(result.root, metadata=metadata))

    async def run_once(self, *, owner: str) -> PipelineRunResult | None:
        async with self._database.session() as session:
            jobs = IngestionJobRepository(session)
            leased = await jobs.lease_next(
                owner=owner,
                now=self._clock(),
                lease_for=self._lease_for,
                job_type="ingest",
            )
            if leased is None:
                return None
            running = await jobs.start(leased.id, owner=owner, now=self._clock())
        return await self._execute(running, owner=owner)

    async def _execute(self, job: JobSnapshot, *, owner: str) -> PipelineRunResult:
        started_at = self._clock()
        started_monotonic = perf_counter()
        trace_id: str | None = None
        database = getattr(self, "_database", None)
        lease_for = getattr(self, "_lease_for", None)
        clock = getattr(self, "_clock", None)
        lease = (
            _LeaseHeartbeat(job, owner, database, lease_for, clock)
            if isinstance(database, Database)
            and isinstance(lease_for, timedelta)
            and callable(clock)
            else None
        )
        lease_token = _ACTIVE_LEASE.set(lease)
        if lease is not None:
            await lease.start()
        metrics = getattr(self, "_metrics", None)
        try:
            with bind_metrics(metrics), bind_context(
                tenant_id=job.tenant_id,
                job_id=job.id,
                document_id=job.document_id,
            ), start_span(
                "rag.ingestion",
                attributes={"rag.ingestion.attempt": job.attempts},
                tracer_provider=self._tracer_provider,
            ) as span:
                context = span.get_span_context()
                if context.is_valid:
                    trace_id = f"{context.trace_id:032x}"
                result = await self._execute_traced(job, owner=owner)
                span.set_attribute("rag.ingestion.status", result.job.status.value)
                span.set_attribute("rag.ingestion.completed", result.completed)
                LOGGER.info(
                    "rag.ingestion.terminal",
                    extra={
                        "event_code": "INGESTION_TERMINAL",
                        "outcome": result.job.status.value,
                    },
                )
            try:
                await self._record_trace(trace_id, job, result, started_at)
            finally:
                if metrics is not None:
                    metrics.observe_ingestion_job(
                        status=result.job.status.value,
                        job_type=result.job.type,
                    )
                    metrics.observe_ingestion_stage(
                        stage="total",
                        duration_seconds=perf_counter() - started_monotonic,
                    )
            return result
        finally:
            _ACTIVE_LEASE.reset(lease_token)
            if lease is not None:
                await lease.close()

    async def _record_trace(
        self,
        trace_id: str | None,
        job: JobSnapshot,
        result: PipelineRunResult,
        started_at: datetime,
    ) -> None:
        recorder = getattr(self, "_trace_recorder", None)
        if recorder is None or trace_id is None:
            return
        completion = TraceCompletion(
            trace_id,
            "ingestion",
            job.tenant_id,
            None,
            "worker",
            job.id,
            None,
            None,
            result.job.status.value,
            started_at,
            self._clock(),
            {},
            {
                "attempt": result.job.attempts,
                "progress": result.job.progress,
                "completed": result.completed,
                "error_code": result.job.error_code,
            },
        )
        try:
            await recorder.record(completion)
        except Exception:
            LOGGER.warning(
                "rag.trace.persist_failed",
                extra={"event_code": "TRACE_PERSIST_FAILED", "outcome": "degraded"},
            )

    async def _execute_traced(self, job: JobSnapshot, *, owner: str) -> PipelineRunResult:
        work: IngestionWork | None = None
        try:
            async with self._database.session() as session:
                work = await IngestionContentRepository(session).prepare(
                    job_id=job.id, owner=owner, now=self._clock()
                )
            current_span = trace.get_current_span()
            current_span.set_attribute("app.tenant_id", str(work.tenant_id))
            current_span.set_attribute("app.document_id", str(work.document_id))
            await self._checkpoint(job.id, owner, 10, "loading")
            loader = self._select_loader(work.media_type, work.source_name)
            loader_info = loader.info()
            with tempfile.TemporaryDirectory(
                prefix="ingestion-", dir=self._temporary_root
            ) as temporary:
                context = IngestionContext(
                    tenant_id=work.tenant_id,
                    document_id=work.document_id,
                    version_id=work.version_id,
                    temporary_directory=Path(temporary),
                    index_revision=self._index_revision,
                )
                with start_span(
                    "rag.ingestion.load", tracer_provider=self._tracer_provider
                ) as stage_span, _observe_ingestion_stage("load"):
                    loaded = await loader.load(
                        _StoredSource(
                            work.source_name,
                            work.media_type,
                            work.object_key,
                            self._object_store,
                        ),
                        context,
                    )
                    stage_span.set_attribute("rag.ingestion.root_count", len(loaded))
                    stage_span.set_attribute("rag.provider.name", loader_info.name)
                    stage_span.set_attribute("rag.provider.version", loader_info.version)
                await self._checkpoint(job.id, owner, 30, "images")
                with start_span(
                    "rag.ingestion.images", tracer_provider=self._tracer_provider
                ), _observe_ingestion_stage("images"):
                    enriched = [await self._enrich_root(root) for root in loaded]
                await self._checkpoint(job.id, owner, 40, "cleaning")
                with start_span(
                    "rag.ingestion.clean", tracer_provider=self._tracer_provider
                ) as stage_span, _observe_ingestion_stage("clean"):
                    cleaned = await self._cleaner.clean_all(enriched, context)
                    cleaned = [self._with_cleaning_audit(result) for result in cleaned]
                    stage_span.set_attribute(
                        "rag.ingestion.changed_root_count",
                        sum(result.root.raw_text != result.root.clean_text for result in cleaned),
                    )
                    stage_span.set_attribute(
                        "rag.ingestion.cleaning_rule_count",
                        sum(len(result.audit) for result in cleaned),
                    )
                    stage_span.set_attribute(
                        "rag.ingestion.cleaning_occurrence_count",
                        sum(
                            audit.occurrences
                            for result in cleaned
                            for audit in result.audit
                        ),
                    )
                await self._checkpoint(job.id, owner, 55, "splitting")
                with start_span(
                    "rag.ingestion.split", tracer_provider=self._tracer_provider
                ) as stage_span, _observe_ingestion_stage("split"):
                    split_results = [
                        await self._splitter.split(result.root, context) for result in cleaned
                    ]
                    stage_span.set_attribute(
                        "rag.ingestion.leaf_count",
                        sum(len(result.leaves) for result in split_results),
                    )
            roots = tuple(result.root for result in split_results)
            leaves = tuple(leaf for result in split_results for leaf in result.leaves)
            await self._checkpoint(job.id, owner, 65, "persisting")
            with start_span(
                "rag.ingestion.persist", tracer_provider=self._tracer_provider
            ), _observe_ingestion_stage("persist"):
                async with self._database.session() as session:
                    await IngestionContentRepository(session).replace_content(
                        version_id=work.version_id,
                        roots=roots,
                        leaves=leaves,
                        parser_provider=loader_info.name,
                        parser_version=loader_info.version,
                    )
            await self._checkpoint(job.id, owner, 75, "projecting")
            with start_span(
                "rag.ingestion.project", tracer_provider=self._tracer_provider
            ) as stage_span, _observe_ingestion_stage("project"):
                projection = await self._projection.project(
                    ProjectionRequest(
                        work.tenant_id,
                        work.collection_id,
                        work.document_id,
                        work.version_id,
                        self._index_revision,
                        leaves,
                    )
                )
                stage_span.set_attribute(
                    "rag.ingestion.expected_count", projection.expected_count
                )
                stage_span.set_attribute(
                    "rag.ingestion.verified_count", projection.verified_count
                )
                stage_span.set_attribute(
                    "rag.ingestion.batch_count", projection.batches
                )
            await self._checkpoint(job.id, owner, 95, "finalizing")
            with start_span(
                "rag.ingestion.finalize", tracer_provider=self._tracer_provider
            ), _observe_ingestion_stage("finalize"):
                async with self._database.session() as session:
                    await IngestionContentRepository(session).finalize(
                        work, expected_leaves=len(leaves)
                    )
                    succeeded = await IngestionJobRepository(session).succeed(
                        job.id, owner=owner, now=self._clock()
                    )
            return PipelineRunResult(succeeded, True)
        except _PipelineCancelled:
            if work is not None:
                await self._compensate(work)
                async with self._database.session() as session:
                    await IngestionContentRepository(session).mark_failed(
                        work,
                        error_code="JOB_CANCELLED",
                        error_message="The ingestion job was cancelled.",
                    )
            async with self._database.session() as session:
                cancelled = await IngestionJobRepository(session).get(job.id)
            if cancelled is None:
                raise RuntimeError("cancelled ingestion job disappeared") from None
            return PipelineRunResult(cancelled, False)
        except Exception as error:
            if work is not None:
                await self._compensate(work)
            code, message = self._safe_error(error)
            terminal = isinstance(error, AppError) and error.code in _TERMINAL_INPUT_ERRORS
            async with self._database.session() as session:
                jobs = IngestionJobRepository(session)
                if terminal:
                    failed = await jobs.fail(
                        job.id,
                        owner=owner,
                        now=self._clock(),
                        error_code=code,
                        error_message=message,
                    )
                else:
                    failed = await jobs.retry(
                        job.id,
                        owner=owner,
                        now=self._clock(),
                        delay=self._retry_delay,
                        error_code=code,
                        error_message=message,
                    )
                if work is not None and failed.status is JobStatus.FAILED:
                    await IngestionContentRepository(session).mark_failed(
                        work, error_code=code, error_message=message
                    )
            return PipelineRunResult(failed, False)

    async def _checkpoint(
        self,
        job_id: UUID,
        owner: str,
        progress: int,
        stage: str,
        *,
        lease: _LeaseHeartbeat | None = None,
    ) -> None:
        active_lease = lease or _ACTIVE_LEASE.get()
        if active_lease is not None:
            active_lease.update(progress, stage)
        async with self._database.session() as session:
            snapshot = await IngestionJobRepository(session).checkpoint(
                job_id,
                owner=owner,
                now=self._clock(),
                lease_for=self._lease_for,
                progress=progress,
                stage=stage,
            )
        if snapshot.status is JobStatus.CANCELLED:
            raise _PipelineCancelled

    async def _enrich_root(self, root: LoadedRoot) -> LoadedRoot:
        result = await self._image_enricher.enrich(root.images)
        images = [self._image_metadata(image) for image in result.images]
        captions = tuple(image.caption for image in result.images if image.caption)
        metadata = dict(root.metadata)
        metadata.update(
            {
                "images": images,
                "image_captions": captions,
                "vision_degraded": result.degraded,
                "vision_provider": result.vision_provider,
                "vision_model": result.vision_model,
                "vision_remote": result.vision_remote,
                "vision_image_count": len(result.images),
                "vision_caption_count": result.caption_count,
                "vision_caption_status_counts": {
                    status.value: sum(
                        image.caption_status is status for image in result.images
                    )
                    for status in CaptionStatus
                },
            }
        )
        return replace(root, metadata=metadata)

    async def _compensate(self, work: IngestionWork) -> None:
        try:
            await self._vector_store.delete_by_version(work.tenant_id, work.version_id)
        except Exception:
            # Compensation must not strand the PostgreSQL job in `running`. The next
            # retry can re-open the vector revision and reconcile the same version.
            LOGGER.warning(
                "rag.ingestion.vector_compensation_degraded",
                extra={
                    "event_code": "INGESTION_VECTOR_COMPENSATION_DEGRADED",
                    "outcome": "degraded",
                },
                exc_info=True,
            )
        async with self._database.session() as session:
            await IngestionContentRepository(session).reset_content(work.version_id)

    def _select_loader(self, media_type: str, source_name: str) -> Loader:
        suffix = Path(source_name).suffix
        for loader in self._loaders:
            if loader.supports(media_type, suffix):
                return loader
        raise AppError(
            ErrorCode.DOCUMENT_UNSUPPORTED_TYPE,
            "No configured Loader supports the document type.",
            {"media_type": media_type, "suffix": suffix.casefold()},
        )

    @staticmethod
    def _safe_error(error: BaseException) -> tuple[str, str]:
        if isinstance(error, AppError):
            return error.code.value, error.message
        return ErrorCode.INTERNAL_ERROR.value, "The ingestion Pipeline failed."

    @staticmethod
    def _image_metadata(image: EnrichedImage) -> dict[str, object]:
        return {
            "page": image.page,
            "ordinal": image.ordinal,
            "name": image.name,
            "media_type": image.media_type,
            "sha256": image.sha256,
            "width": image.width,
            "height": image.height,
            "object_key": image.object_key,
            "caption": image.caption,
            "caption_status": image.caption_status.value,
            "caption_error_code": image.caption_error_code,
        }


@contextmanager
def _observe_ingestion_stage(stage: str) -> Iterator[None]:
    started = perf_counter()
    try:
        yield
    finally:
        metrics = current_metrics()
        if metrics is not None:
            metrics.observe_ingestion_stage(
                stage=stage,
                duration_seconds=perf_counter() - started,
            )
