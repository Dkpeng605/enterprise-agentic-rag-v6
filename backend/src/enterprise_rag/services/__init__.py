"""Application services that coordinate ports and transactional adapters."""

from enterprise_rag.services.auth import AnonymousSessionService, Principal, SessionGrant
from enterprise_rag.services.documents import DocumentRegistrationService, RegisterDocument
from enterprise_rag.services.fusion import FusionDiagnostic, FusionResult, ReciprocalRankFusion
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
from enterprise_rag.services.reranking import RerankingService, RerankItem, RerankOutcome
from enterprise_rag.services.retrieval import (
    DualSearchResult,
    DualSearchService,
    SearchBranchResult,
    SearchDiagnostic,
    SearchMethod,
)
from enterprise_rag.services.scope_root import (
    PreparedRerankCandidates,
    RecoveredContext,
    RootContext,
    ScopeRootService,
)
from enterprise_rag.services.workspace import WorkspaceService

__all__ = [
    "DeletionResult",
    "DeletionStep",
    "DocumentDeletionService",
    "DocumentRegistrationService",
    "DualSearchResult",
    "DualSearchService",
    "EnrichedImage",
    "FusionDiagnostic",
    "FusionResult",
    "ImageEnricher",
    "ImageEnrichmentResult",
    "IngestionPipeline",
    "AnonymousSessionService",
    "PipelineRunResult",
    "Principal",
    "ProjectionError",
    "ProjectionRequest",
    "ProjectionResult",
    "ProjectionService",
    "PreparedRerankCandidates",
    "ReconcileIssue",
    "ReconcileIssueKind",
    "ReconcileReport",
    "ReconcileService",
    "RecoveredContext",
    "ReciprocalRankFusion",
    "RegisterDocument",
    "RerankItem",
    "RerankOutcome",
    "RerankingService",
    "RootContext",
    "SessionGrant",
    "SearchBranchResult",
    "SearchDiagnostic",
    "SearchMethod",
    "ScopeRootService",
    "WorkspaceService",
]
