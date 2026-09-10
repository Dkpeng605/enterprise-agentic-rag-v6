"""Application services that coordinate ports and transactional adapters."""

from enterprise_rag.services.documents import DocumentRegistrationService, RegisterDocument
from enterprise_rag.services.images import EnrichedImage, ImageEnricher, ImageEnrichmentResult
from enterprise_rag.services.ingestion import IngestionPipeline, PipelineRunResult
from enterprise_rag.services.lifecycle import (
    DeletionResult,
    DeletionStep,
    DocumentDeletionService,
    ReconcileIssue,
    ReconcileIssueKind,
    ReconcileReport,
    ReconcileService,
)
from enterprise_rag.services.projection import (
    ProjectionError,
    ProjectionRequest,
    ProjectionResult,
    ProjectionService,
)

__all__ = [
    "DeletionResult",
    "DeletionStep",
    "DocumentDeletionService",
    "DocumentRegistrationService",
    "EnrichedImage",
    "ImageEnricher",
    "ImageEnrichmentResult",
    "IngestionPipeline",
    "PipelineRunResult",
    "ProjectionError",
    "ProjectionRequest",
    "ProjectionResult",
    "ProjectionService",
    "ReconcileIssue",
    "ReconcileIssueKind",
    "ReconcileReport",
    "ReconcileService",
    "RegisterDocument",
]
