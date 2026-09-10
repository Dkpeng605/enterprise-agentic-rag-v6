"""Lease-aware, resumable ingestion Pipeline orchestration."""

import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.ingestion import (
    IngestionContentRepository,
    IngestionWork,
)
from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.domain.common import utc_now
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.jobs import JobSnapshot, JobStatus
from enterprise_rag.ports.cleaner import Cleaner
from enterprise_rag.ports.loader import BinarySource, IngestionContext, LoadedRoot, Loader
from enterprise_rag.ports.object_store import ObjectStore
from enterprise_rag.ports.splitter import Splitter
from enterprise_rag.ports.vector_store import VectorStore
from enterprise_rag.services.images import EnrichedImage, ImageEnricher
from enterprise_rag.services.projection import ProjectionRequest, ProjectionService

Clock = Callable[[], datetime]
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
        self._clock = clock

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
        work: IngestionWork | None = None
        try:
            async with self._database.session() as session:
                work = await IngestionContentRepository(session).prepare(
                    job_id=job.id, owner=owner, now=self._clock()
                )
            await self._checkpoint(job.id, owner, 10, "loading")
            loader = self._select_loader(work.media_type, work.source_name)
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
                loaded = await loader.load(
                    _StoredSource(
                        work.source_name,
                        work.media_type,
                        work.object_key,
                        self._object_store,
                    ),
                    context,
                )
                await self._checkpoint(job.id, owner, 30, "images")
                enriched = [await self._enrich_root(root) for root in loaded]
                await self._checkpoint(job.id, owner, 40, "cleaning")
                cleaned = await self._cleaner.clean_all(enriched, context)
                await self._checkpoint(job.id, owner, 55, "splitting")
                split_results = [
                    await self._splitter.split(result.root, context) for result in cleaned
                ]
            roots = tuple(result.root for result in split_results)
            leaves = tuple(leaf for result in split_results for leaf in result.leaves)
            await self._checkpoint(job.id, owner, 65, "persisting")
            async with self._database.session() as session:
                await IngestionContentRepository(session).replace_content(
                    version_id=work.version_id, roots=roots, leaves=leaves
                )
            await self._checkpoint(job.id, owner, 75, "projecting")
            await self._projection.project(
                ProjectionRequest(
                    work.tenant_id,
                    work.collection_id,
                    work.document_id,
                    work.version_id,
                    self._index_revision,
                    leaves,
                )
            )
            await self._checkpoint(job.id, owner, 95, "finalizing")
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

    async def _checkpoint(self, job_id: UUID, owner: str, progress: int, stage: str) -> None:
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
            }
        )
        return replace(root, metadata=metadata)

    async def _compensate(self, work: IngestionWork) -> None:
        await self._vector_store.delete_by_version(work.tenant_id, work.version_id)
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
