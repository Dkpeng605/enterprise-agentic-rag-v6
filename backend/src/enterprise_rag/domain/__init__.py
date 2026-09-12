"""Framework-independent enterprise RAG domain types."""

from enterprise_rag.domain.common import new_uuid7, stable_content_id, utc_now
from enterprise_rag.domain.documents import (
    Document,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    DocumentVisibility,
    LeafChunk,
    RootChunk,
    RootKind,
)
from enterprise_rag.domain.errors import AppError, ErrorCode, ErrorDetail, ErrorResponse
from enterprise_rag.domain.evaluation import (
    EvaluationCase,
    EvaluationCitation,
    EvaluationObservation,
    MetricSet,
)
from enterprise_rag.domain.golden_set import GoldenDocument, GoldenRoot, GoldenSet
from enterprise_rag.domain.jobs import TERMINAL_JOB_STATUSES, JobSnapshot, JobStatus
from enterprise_rag.domain.retrieval import (
    Citation,
    QueryIntent,
    QueryMode,
    QueryPlan,
    QueryScope,
    RetrievalHit,
)

__all__ = [
    "AppError",
    "Citation",
    "Document",
    "DocumentStatus",
    "DocumentVersion",
    "DocumentVersionStatus",
    "DocumentVisibility",
    "ErrorCode",
    "ErrorDetail",
    "ErrorResponse",
    "EvaluationCase",
    "EvaluationCitation",
    "EvaluationObservation",
    "GoldenDocument",
    "GoldenRoot",
    "GoldenSet",
    "LeafChunk",
    "MetricSet",
    "JobSnapshot",
    "JobStatus",
    "QueryIntent",
    "QueryMode",
    "QueryPlan",
    "QueryScope",
    "RetrievalHit",
    "RootChunk",
    "RootKind",
    "TERMINAL_JOB_STATUSES",
    "new_uuid7",
    "stable_content_id",
    "utc_now",
]
