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


class DemoSeedResponse(ApiModel):
    collection_id: UUID
    documents: list[UploadResponse]


class McpToolCapabilityResponse(ApiModel):
    name: str
    description: str
    required_scopes: list[str]
    read_only: bool


class McpResourceCapabilityResponse(ApiModel):
    uri: str
    kind: Literal["resource", "template"]
    description: str
    required_scopes: list[str]


class McpTransportResponse(ApiModel):
    name: Literal["stdio", "streamable_http"]
    status: Literal[
        "factory_declared",
        "requires_factory",
        "mounted",
        "external_composition_required",
    ]
    endpoint: str | None
    detail: str


class McpCapabilityCatalogResponse(ApiModel):
    server_name: str
    server_version: str
    tools: list[McpToolCapabilityResponse]
    resources: list[McpResourceCapabilityResponse]
    transports: list[McpTransportResponse]


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


class JobListItemResponse(JobResponse):
    created_at: datetime


class JobListResponse(ApiModel):
    items: list[JobListItemResponse]
    next_cursor: str | None


class DocumentDetailResponse(DocumentResponse):
    root_count: int
    leaf_count: int
    recent_job: JobResponse | None
    version_error_code: str | None
    version_error_message: str | None


class CleaningAuditResponse(ApiModel):
    rule: str
    occurrences: int
    before_sha256: str
    after_sha256: str


class PipelineRootSummaryResponse(ApiModel):
    id: str
    ordinal: int
    kind: str
    source_locator: dict[str, object]
    raw_chars: int
    clean_chars: int
    changed: bool
    leaf_count: int
    cleaning_audit: list[CleaningAuditResponse]


class PipelineLeafResponse(ApiModel):
    id: str
    ordinal: int
    text: str
    retrieval_text: str
    start_offset: int | None
    end_offset: int | None
    token_count: int
    overlap_chars: int
    metadata: dict[str, object]


class PipelineRootDetailResponse(ApiModel):
    summary: PipelineRootSummaryResponse
    raw_text: str
    clean_text: str
    metadata: dict[str, object]
    leaves: list[PipelineLeafResponse]


class DocumentPipelineResponse(ApiModel):
    document_id: UUID
    version_id: UUID
    source_name: str
    parser_provider: str | None
    parser_version: str | None
    cleaner_provider: str | None
    cleaner_version: str | None
    splitter_provider: str | None
    splitter_version: str | None
    splitter_settings: dict[str, object]
    llm_cleaning: dict[str, object]
    root_count: int
    leaf_count: int
    roots: list[PipelineRootSummaryResponse]
    next_cursor: int | None


class LlmCleaningPreflightResponse(ApiModel):
    document_id: UUID
    version_id: UUID
    available: bool
    reason: str | None
    provider: str | None
    model: str | None
    remote: bool
    root_count: int
    input_chars: int
    max_roots: int
    max_input_chars: int
    estimated_calls: int
    max_output_tokens: int
    already_applied: bool


class LlmCleaningRequest(ApiModel):
    expected_version_id: UUID
    confirm_remote_processing: bool


class LlmCleaningResponse(ApiModel):
    document_id: UUID
    version_id: UUID
    provider: str
    model: str
    root_count: int
    changed_root_count: int
    leaf_count_before: int
    leaf_count_after: int
    input_chars: int
    output_chars: int
    input_tokens: int
    output_tokens: int
    retry_count: int
    llm_calls: int
    applied_at: datetime


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
    status: Literal["answered", "partial", "abstained", "no_results"]
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


class QueryWaterfallStageResponse(ApiModel):
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    offset_ms: float
    duration_ms: float
    status: str
    degraded: bool


class QueryRankChangeResponse(ApiModel):
    leaf_id: str
    root_id: str | None
    dense_rank: int | None
    sparse_rank: int | None
    rrf_rank: int | None
    rerank_rank: int | None
    dense_score: float | None
    sparse_score: float | None
    rrf_score: float | None
    rerank_score: float | None
    matched_queries: list[str]


class QueryRecoveryRoundResponse(ApiModel):
    round_number: int
    route: str
    retrieval_mode: str
    target_count: int
    returned_count: int
    added_count: int
    duplicate_count: int


class QueryDegradationResponse(ApiModel):
    component: str
    provider: str | None


class QueryPlanResponse(ApiModel):
    provider: str
    degraded: bool
    original_query: str
    rewritten_query: str
    intent: str
    language: str
    sub_queries: list[str]
    requirements: list[str]
    use_sub_queries: bool


class QueryRetrievalBranchResponse(ApiModel):
    branch_index: int
    query: str
    dense_requested: int
    dense_returned: int
    sparse_requested: int
    sparse_returned: int
    sparse_algorithm: str
    overlap_count: int
    unique_count: int


class QueryStageMetricResponse(ApiModel):
    stage: str
    input_count: int
    output_count: int
    dropped_count: int
    attributes: dict[str, int | float | str]
    covered_requirements: list[str]
    missing_requirements: list[str]
    issues: list[str]


class QueryTraceViewResponse(ApiModel):
    summary: TraceSummaryResponse
    usage: dict[str, int | float]
    stages: list[QueryWaterfallStageResponse]
    rankings: list[QueryRankChangeResponse]
    recovery_rounds: list[QueryRecoveryRoundResponse]
    degradations: list[QueryDegradationResponse]
    plan: QueryPlanResponse | None
    retrieval_branches: list[QueryRetrievalBranchResponse]
    stage_metrics: list[QueryStageMetricResponse]


class IngestionTraceStageResponse(ApiModel):
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    offset_ms: float
    duration_ms: float
    status: str
    root_count: int | None
    leaf_count: int | None
    expected_count: int | None
    verified_count: int | None
    batch_count: int | None


class IngestionTraceBatchResponse(ApiModel):
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    phase: str
    batch_index: int
    batch_count: int
    item_count: int
    written_count: int | None
    offset_ms: float
    duration_ms: float
    status: str


class IngestionTraceViewResponse(ApiModel):
    summary: TraceSummaryResponse
    attempt: int
    progress: int
    completed: bool
    error_code: str | None
    stages: list[IngestionTraceStageResponse]
    batches: list[IngestionTraceBatchResponse]


class EvaluationProfileResponse(ApiModel):
    id: str
    label: str
    provider: str
    model: str
    prompt_revision: str
    requires_remote: bool
    estimated_llm_calls_per_case: int


class EvaluationCatalogResponse(ApiModel):
    dataset_revision: str
    dataset_label: str
    case_counts: dict[str, int]
    profiles: list[EvaluationProfileResponse]
    max_cases: int
    max_llm_calls: int


class EvaluationRunCreate(ApiModel):
    dataset_revision: str = Field(min_length=1, max_length=100)
    mode: Literal["all", "standard", "deep"] = "all"
    provider_profile: str = Field(min_length=1, max_length=100)
    max_cases: int = Field(ge=1, le=10_000)
    max_llm_calls: int = Field(ge=0, le=100_000)


class EvaluationRunResponse(ApiModel):
    id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    dataset_revision: str | None
    mode: Literal["all", "standard", "deep"]
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
    case_ids: list[str]
    aggregate_metrics: dict[str, float | None]
    usage: dict[str, int]
    error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    report: dict[str, object] | None = None


class EvaluationRunListResponse(ApiModel):
    items: list[EvaluationRunResponse]
    next_cursor: str | None


class EvaluationComparisonResponse(ApiModel):
    base_run_id: UUID
    candidate_run_id: UUID
    comparable: bool
    reasons: list[str]
    base_metrics: dict[str, float | None]
    candidate_metrics: dict[str, float | None]
    deltas: dict[str, float | None]


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


class ProviderOptionResponse(ApiModel):
    kind: str
    key: str
    name: str
    model: str
    label: str
    provider: str
    capabilities: list[str]
    is_remote: bool
    dimension: int | None = None
    input_token_limit: int | None = None
    language_note: str | None = None
    note: str | None = None
    selected: bool
    available: bool
    unavailable_reason: str | None = None
    requires_restart: bool


class ProviderCatalogResponse(ApiModel):
    providers: list[ProviderDiagnosticResponse]
    options: list[ProviderOptionResponse]
    selection: dict[str, str | bool]


class ProviderIndexDocumentResponse(ApiModel):
    document_id: str
    title: str
    version_id: str
    stored_revisions: list[str]
    active_revision: str
    compatible: bool
    root_count: int
    leaf_count: int
    vector_count: int


class ProviderIndexStatusResponse(ApiModel):
    active_revision: str
    embedding_model: str
    embedding_dimension: int
    total_documents: int
    compatible_documents: int
    incompatible_documents: int
    documents: list[ProviderIndexDocumentResponse]


class ProviderReindexItemResponse(ApiModel):
    document_id: str
    title: str
    status: str
    old_revisions: list[str]
    leaf_count: int
    error: str | None = None


class ProviderReindexResponse(ApiModel):
    active_revision: str
    requested_count: int
    rebuilt_count: int
    skipped_count: int
    failed_count: int
    cleanup_failed_count: int
    items: list[ProviderReindexItemResponse]


class ProviderSelectionRequest(ApiModel):
    kind: Literal["embedding", "reranker", "sparse_encoder", "llm", "vision"]
    key: str = Field(min_length=1, max_length=500)


class DocumentCountsResponse(ApiModel):
    pending: int
    processing: int
    ready: int
    failed: int
    deleting: int


class OverviewActivityResponse(ApiModel):
    id: str
    kind: Literal["ingestion", "evaluation"]
    status: str
    label: str
    started_at: datetime
    progress: int | None


class WorkspaceOverviewResponse(ApiModel):
    generated_at: datetime
    collection_count: int
    document_counts: DocumentCountsResponse
    root_count: int
    leaf_count: int
    queries_24h: int
    query_errors_24h: int
    query_error_rate: float | None
    query_outcome_counts: dict[str, int]
    query_abstention_rate: float | None
    query_answer_rate: float | None
    query_generation_degraded_24h: int
    query_p95_ms: float | None
    recent_activity: list[OverviewActivityResponse]
