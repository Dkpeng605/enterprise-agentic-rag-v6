"""Explicit HTTP request and response schemas for the workspace API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from enterprise_rag.domain.documents import DocumentVisibility


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ErrorDetailModel(ApiModel):
    code: str
    message: str
    request_id: UUID
    details: dict[str, object]


class ErrorResponseModel(ApiModel):
    error: ErrorDetailModel


class TenantModel(ApiModel):
    id: UUID
    slug: str


class AuthMeResponse(ApiModel):
    actor_type: Literal["anonymous", "user"]
    role: str
    tenant: TenantModel
    permissions: list[str]
    csrf_token: str
    expires_at: datetime
    email: str | None = None


class LoginRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1_024)


class CollectionCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    visibility: DocumentVisibility = DocumentVisibility.TENANT


class CollectionPatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    visibility: DocumentVisibility | None = None


class CollectionDelete(ApiModel):
    confirm_name: str = Field(min_length=1, max_length=200)


class CollectionResponse(ApiModel):
    id: UUID
    name: str
    description: str | None
    visibility: str
    is_seed: bool
    document_count: int
    ready_document_count: int
    created_at: datetime
    updated_at: datetime


class CollectionListResponse(ApiModel):
    items: list[CollectionResponse]


class CollectionDeleteResponse(ApiModel):
    id: UUID
    status: str
    job_ids: list[UUID]


class UploadResponse(ApiModel):
    document_id: UUID
    version_id: UUID
    job_id: UUID
    deduplicated: bool
    status: str


class DocumentResponse(ApiModel):
    id: UUID
    collection_id: UUID
    title: str
    organization: str | None
    status: str
    visibility: str
    version_id: UUID
    source_name: str
    media_type: str
    size_bytes: int
    sha256_prefix: str
    created_at: datetime
    updated_at: datetime


class JobResponse(ApiModel):
    id: UUID
    document_id: UUID
    version_id: UUID
    type: str
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    heartbeat_at: datetime | None
    progress: int
    stage: str | None
    error_code: str | None
    error_message: str | None
    cancel_requested: bool


class DocumentDetailResponse(DocumentResponse):
    root_count: int
    leaf_count: int
    recent_job: JobResponse | None
    version_error_code: str | None
    version_error_message: str | None


class DocumentListResponse(ApiModel):
    items: list[DocumentResponse]
    next_cursor: str | None


class DocumentDeleteResponse(ApiModel):
    document_id: UUID
    job_id: UUID
    status: str
    already_requested: bool


class QueryScopeRequest(ApiModel):
    collection_ids: list[UUID] = Field(default_factory=list, max_length=100)
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)
    titles: list[str] = Field(default_factory=list, max_length=100)
    organizations: list[str] = Field(default_factory=list, max_length=100)
    doc_types: list[str] = Field(default_factory=list, max_length=100)
    versions: list[str] = Field(default_factory=list, max_length=100)
    sections: list[str] = Field(default_factory=list, max_length=100)


class QueryHistoryTurn(ApiModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4_000)


class QueryRequestModel(ApiModel):
    query: str = Field(min_length=1, max_length=2_000)
    mode: Literal["standard", "deep"] = "standard"
    scope: QueryScopeRequest = Field(default_factory=QueryScopeRequest)
    history: list[QueryHistoryTurn] = Field(default_factory=list, max_length=12)


class QueryCitationResponse(ApiModel):
    id: int
    document_id: UUID
    root_id: str
    chunk_ids: list[str]
    source_name: str
    title: str
    page: int | None
    section: str | None
    quote: str
    score: float | None


class QueryResponseModel(ApiModel):
    query_id: UUID
    status: Literal["answered", "abstained", "no_results"]
    answer: str
    citations: list[QueryCitationResponse]
    diagnostics: dict[str, object]
    usage: dict[str, object]
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class TraceSummaryResponse(ApiModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    trace_type: Literal["query", "ingestion", "evaluation"]
    subject_id: UUID
    mode: str | None
    status: str
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    span_count: int
    degraded: bool


class TraceListResponse(ApiModel):
    items: list[TraceSummaryResponse]
    next_cursor: str | None


class TraceSpanResponse(ApiModel):
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    status: str
    attributes: dict[str, object]
    events: list[dict[str, object]]


class TraceDetailResponse(ApiModel):
    summary: TraceSummaryResponse
    actor_type: str
    request_id: UUID | None
    usage: dict[str, object]
    attributes: dict[str, object]
    spans: list[TraceSpanResponse]


class LivenessResponse(ApiModel):
    status: Literal["live"]
    service: str
    version: str


class HealthCheckResponse(ApiModel):
    name: str
    kind: str
    required: bool
    status: Literal["healthy", "degraded", "unavailable"]
    latency_ms: float
    code: str | None


class ProviderDiagnosticResponse(ApiModel):
    kind: str
    name: str
    version: str
    capabilities: list[str]
    is_remote: bool
    health: str


class HealthReportResponse(ApiModel):
    status: Literal["healthy", "degraded", "unavailable"]
    ready: bool
    checks: list[HealthCheckResponse]
    providers: list[ProviderDiagnosticResponse]
