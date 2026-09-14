"""FastAPI routes for anonymous demo document and query workflows."""

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyCookie, APIKeyHeader

from enterprise_rag.api.schemas import (
    AuthMeResponse,
    CollectionCreate,
    CollectionDelete,
    CollectionDeleteResponse,
    CollectionListResponse,
    CollectionPatch,
    CollectionResponse,
    DocumentDeleteResponse,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentPipelineResponse,
    DocumentResponse,
    ErrorResponseModel,
    EvaluationCatalogResponse,
    EvaluationComparisonResponse,
    EvaluationRunCreate,
    EvaluationRunListResponse,
    EvaluationRunResponse,
    IngestionTraceViewResponse,
    JobListItemResponse,
    JobListResponse,
    JobResponse,
    LlmCleaningPreflightResponse,
    LlmCleaningRequest,
    LlmCleaningResponse,
    LoginRequest,
    PipelineRootDetailResponse,
    QueryRequestModel,
    QueryResponseModel,
    QueryTraceViewResponse,
    TenantModel,
    TraceDetailResponse,
    TraceListResponse,
    TraceSpanResponse,
    TraceSummaryResponse,
    UploadResponse,
    WorkspaceOverviewResponse,
)
from enterprise_rag.domain.common import to_json_value
from enterprise_rag.domain.documents import DocumentStatus, DocumentVisibility
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.jobs import JobSnapshot, JobStatus
from enterprise_rag.domain.retrieval import QueryMode, QueryScope
from enterprise_rag.ports.planner import ConversationRole, ConversationTurn
from enterprise_rag.ports.traces import StoredSpan, TraceDetail, TraceSummary
from enterprise_rag.services.auth import SESSION_COOKIE, AnonymousSessionService, Principal
from enterprise_rag.services.evaluation_workspace import EvaluationWorkspaceService
from enterprise_rag.services.ingestion_trace import IngestionTraceView
from enterprise_rag.services.knowledge import KnowledgeApplication, KnowledgeQuery
from enterprise_rag.services.manual_llm_cleaning import ManualLlmCleaningService
from enterprise_rag.services.overview import WorkspaceOverviewService
from enterprise_rag.services.query_trace import QueryTraceView
from enterprise_rag.services.traces import TraceService
from enterprise_rag.services.workspace import (
    CollectionSnapshot,
    DocumentDetail,
    DocumentPipelineSnapshot,
    DocumentSummary,
    JobListItem,
    PipelineRootDetail,
    WorkspaceService,
)

Clock = Callable[[], datetime]
MEDIA_TYPES_BY_SUFFIX = {
    ".pdf": frozenset({"application/pdf"}),
    ".docx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
        }
    ),
    ".xlsx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/zip",
        }
    ),
    ".xls": frozenset({"application/vnd.ms-excel", "application/xls"}),
    ".csv": frozenset({"text/csv", "application/csv", "text/plain"}),
    ".html": frozenset({"text/html", "application/xhtml+xml"}),
    ".htm": frozenset({"text/html", "application/xhtml+xml"}),
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/plain", "text/x-markdown"}),
}
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Invalid request", "model": ErrorResponseModel},
    401: {"description": "Authentication required", "model": ErrorResponseModel},
    403: {"description": "Forbidden or invalid CSRF token", "model": ErrorResponseModel},
    404: {"description": "Tenant-scoped resource not found", "model": ErrorResponseModel},
    409: {"description": "Resource conflict", "model": ErrorResponseModel},
    413: {"description": "Upload too large", "model": ErrorResponseModel},
    415: {"description": "Unsupported media type", "model": ErrorResponseModel},
    422: {"description": "Schema validation failed", "model": ErrorResponseModel},
    429: {"description": "Demo quota exceeded", "model": ErrorResponseModel},
}


def create_api_router(
    *,
    auth: AnonymousSessionService | None,
    workspace: WorkspaceService | None,
    knowledge: KnowledgeApplication | None,
    allowed_suffixes: tuple[str, ...],
    max_upload_bytes: int,
    session_minutes: int,
    cookie_secure: bool,
    clock: Clock,
    traces: TraceService | None = None,
    overview: WorkspaceOverviewService | None = None,
    evaluations: EvaluationWorkspaceService | None = None,
    manual_llm_cleaning: ManualLlmCleaningService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)
    cookie_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)
    csrf_scheme = APIKeyHeader(name="X-CSRF-Token", auto_error=False)

    async def reader(
        token: Annotated[str | None, Depends(cookie_scheme)],
    ) -> Principal:
        return await _auth().authenticate(token, now=clock())

    async def writer(
        token: Annotated[str | None, Depends(cookie_scheme)],
        csrf_token: Annotated[str | None, Depends(csrf_scheme)],
    ) -> Principal:
        return await _auth().require_csrf(token, csrf_token, now=clock())

    def _auth() -> AnonymousSessionService:
        if auth is None:
            raise _unavailable()
        return auth

    def _workspace() -> WorkspaceService:
        if workspace is None:
            raise _unavailable()
        return workspace

    def _knowledge() -> KnowledgeApplication:
        if knowledge is None:
            raise _unavailable()
        return knowledge

    def _traces() -> TraceService:
        if traces is None:
            raise _unavailable()
        return traces

    def _overview() -> WorkspaceOverviewService:
        if overview is None:
            raise _unavailable()
        return overview

    def _evaluations() -> EvaluationWorkspaceService:
        if evaluations is None:
            raise _unavailable()
        return evaluations

    def _manual_llm_cleaning() -> ManualLlmCleaningService:
        if manual_llm_cleaning is None:
            raise _unavailable()
        return manual_llm_cleaning

    @router.get("/auth/me", response_model=AuthMeResponse, tags=["auth"])
    async def auth_me(
        response: Response,
        token: Annotated[str | None, Depends(cookie_scheme)],
    ) -> AuthMeResponse:
        grant = await _auth().get_or_create(token, now=clock())
        response.set_cookie(
            SESSION_COOKIE,
            grant.token,
            max_age=session_minutes * 60,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )
        return AuthMeResponse(
            actor_type=cast(Literal["anonymous", "user"], grant.principal.actor_type),
            role=grant.principal.role,
            tenant=TenantModel(id=grant.principal.tenant_id, slug=grant.tenant_slug),
            permissions=list(grant.permissions),
            csrf_token=grant.csrf_token,
            expires_at=grant.expires_at,
            email=grant.email,
        )

    @router.post("/auth/login", response_model=AuthMeResponse, tags=["auth"])
    async def login(body: LoginRequest, response: Response) -> AuthMeResponse:
        grant = await _auth().login(body.email, body.password, now=clock())
        response.set_cookie(
            SESSION_COOKIE,
            grant.token,
            max_age=session_minutes * 60,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )
        return AuthMeResponse(
            actor_type="user",
            role=grant.principal.role,
            tenant=TenantModel(id=grant.principal.tenant_id, slug=grant.tenant_slug),
            permissions=list(grant.permissions),
            csrf_token=grant.csrf_token,
            expires_at=grant.expires_at,
            email=grant.email,
        )

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
    async def logout(
        response: Response,
        principal: Annotated[Principal, Depends(writer)],
        token: Annotated[str | None, Depends(cookie_scheme)],
    ) -> None:
        del principal
        await _auth().revoke(token, now=clock())
        response.delete_cookie(SESSION_COOKIE, path="/")

    @router.get(
        "/workspace/overview",
        response_model=WorkspaceOverviewResponse,
        tags=["workspace"],
    )
    async def workspace_overview(
        principal: Annotated[Principal, Depends(reader)],
    ) -> WorkspaceOverviewResponse:
        snapshot = await _overview().get(principal.tenant_id, now=clock())
        return WorkspaceOverviewResponse.model_validate(snapshot.to_dict())

    @router.get(
        "/collections",
        response_model=CollectionListResponse,
        tags=["collections"],
    )
    async def list_collections(
        principal: Annotated[Principal, Depends(reader)],
    ) -> CollectionListResponse:
        items = await _workspace().list_collections(principal.tenant_id)
        return CollectionListResponse(items=[_collection(item) for item in items])

    @router.post(
        "/collections",
        response_model=CollectionResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["collections"],
    )
    async def create_collection(
        body: CollectionCreate,
        principal: Annotated[Principal, Depends(writer)],
    ) -> CollectionResponse:
        item = await _workspace().create_collection(
            principal.tenant_id,
            name=_required_text(body.name, "name"),
            description=_optional_text(body.description),
            visibility=body.visibility,
        )
        return _collection(item)

    @router.get(
        "/collections/{collection_id}",
        response_model=CollectionResponse,
        tags=["collections"],
    )
    async def get_collection(
        collection_id: UUID,
        principal: Annotated[Principal, Depends(reader)],
    ) -> CollectionResponse:
        return _collection(await _workspace().get_collection(principal.tenant_id, collection_id))

    @router.patch(
        "/collections/{collection_id}",
        response_model=CollectionResponse,
        tags=["collections"],
    )
    async def update_collection(
        collection_id: UUID,
        body: CollectionPatch,
        principal: Annotated[Principal, Depends(writer)],
    ) -> CollectionResponse:
        item = await _workspace().update_collection(
            principal.tenant_id,
            collection_id,
            name=_required_text(body.name, "name") if body.name is not None else None,
            description=_optional_text(body.description),
            visibility=body.visibility,
            description_set="description" in body.model_fields_set,
        )
        return _collection(item)

    @router.delete(
        "/collections/{collection_id}",
        response_model=CollectionDeleteResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["collections"],
    )
    async def delete_collection(
        collection_id: UUID,
        body: CollectionDelete,
        principal: Annotated[Principal, Depends(writer)],
    ) -> CollectionDeleteResponse:
        result = await _workspace().delete_collection(
            principal.tenant_id,
            collection_id,
            confirm_name=body.confirm_name,
            now=clock(),
        )
        return CollectionDeleteResponse(
            id=result.id, status=result.status, job_ids=list(result.job_ids)
        )

    @router.post(
        "/documents",
        response_model=UploadResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["documents"],
    )
    async def upload_document(
        file: Annotated[UploadFile, File()],
        collection_id: Annotated[UUID, Form()],
        title: Annotated[str, Form(min_length=1, max_length=500)],
        principal: Annotated[Principal, Depends(writer)],
        organization: Annotated[str | None, Form(max_length=200)] = None,
        visibility: Annotated[DocumentVisibility, Form()] = DocumentVisibility.TENANT,
    ) -> UploadResponse:
        filename = Path(file.filename or "").name
        suffix = Path(filename).suffix.casefold()
        if not filename or suffix not in allowed_suffixes:
            raise AppError(
                ErrorCode.UNSUPPORTED_MEDIA_TYPE,
                "The uploaded file type is not supported.",
                {"suffix": suffix},
            )
        media_type = (file.content_type or "application/octet-stream").split(";", 1)[0]
        if media_type.casefold() not in MEDIA_TYPES_BY_SUFFIX.get(suffix, frozenset()):
            raise AppError(
                ErrorCode.UNSUPPORTED_MEDIA_TYPE,
                "The upload MIME type does not match its file extension.",
                {"media_type": media_type, "suffix": suffix},
            )

        async def chunks() -> AsyncIterator[bytes]:
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_upload_bytes:
                    raise AppError(
                        ErrorCode.UPLOAD_TOO_LARGE,
                        "The uploaded file exceeds the demo limit.",
                        {"max_bytes": max_upload_bytes},
                    )
                yield chunk

        try:
            result = await _workspace().upload_document(
                tenant_id=principal.tenant_id,
                actor_id=principal.actor_id,
                collection_id=collection_id,
                title=_required_text(title, "title"),
                organization=_optional_text(organization),
                visibility=visibility,
                source_name=filename,
                media_type=media_type,
                chunks=chunks(),
                now=clock(),
            )
        finally:
            await file.close()
        return UploadResponse.model_validate(result)

    @router.get(
        "/documents",
        response_model=DocumentListResponse,
        tags=["documents"],
    )
    async def list_documents(
        principal: Annotated[Principal, Depends(reader)],
        collection: Annotated[UUID | None, Query()] = None,
        document_status: Annotated[DocumentStatus | None, Query(alias="status")] = None,
        media_type: Annotated[str | None, Query(alias="type")] = None,
        keyword: Annotated[str | None, Query(max_length=200)] = None,
        cursor: Annotated[str | None, Query(max_length=1_000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> DocumentListResponse:
        page = await _workspace().list_documents(
            principal.tenant_id,
            collection_id=collection,
            status=document_status.value if document_status is not None else None,
            media_type=media_type,
            keyword=keyword,
            cursor=cursor,
            limit=limit,
        )
        return DocumentListResponse(
            items=[_document(item) for item in page.items], next_cursor=page.next_cursor
        )

    @router.get(
        "/documents/{document_id}",
        response_model=DocumentDetailResponse,
        tags=["documents"],
    )
    async def get_document(
        document_id: UUID,
        principal: Annotated[Principal, Depends(reader)],
    ) -> DocumentDetailResponse:
        return _document_detail(await _workspace().get_document(principal.tenant_id, document_id))

    @router.get(
        "/documents/{document_id}/pipeline",
        response_model=DocumentPipelineResponse,
        tags=["documents"],
    )
    async def inspect_document_pipeline(
        document_id: UUID,
        principal: Annotated[Principal, Depends(reader)],
        cursor: Annotated[int | None, Query(ge=0)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> DocumentPipelineResponse:
        return _document_pipeline(
            await _workspace().inspect_document_pipeline(
                principal.tenant_id,
                document_id,
                cursor=cursor,
                limit=limit,
            )
        )

    @router.get(
        "/documents/{document_id}/pipeline/roots/{root_id}",
        response_model=PipelineRootDetailResponse,
        tags=["documents"],
    )
    async def inspect_pipeline_root(
        document_id: UUID,
        root_id: str,
        principal: Annotated[Principal, Depends(reader)],
    ) -> PipelineRootDetailResponse:
        return _pipeline_root(
            await _workspace().inspect_pipeline_root(
                principal.tenant_id, document_id, root_id
            )
        )

    @router.get(
        "/documents/{document_id}/llm-cleaning/preflight",
        response_model=LlmCleaningPreflightResponse,
        tags=["documents"],
    )
    async def llm_cleaning_preflight(
        document_id: UUID,
        principal: Annotated[Principal, Depends(reader)],
    ) -> LlmCleaningPreflightResponse:
        if manual_llm_cleaning is None:
            pipeline = await _workspace().inspect_document_pipeline(
                principal.tenant_id, document_id, cursor=None, limit=1
            )
            return LlmCleaningPreflightResponse(
                document_id=document_id,
                version_id=pipeline.version_id,
                available=False,
                reason="This runtime has no remote LLM cleaning provider configured.",
                provider=None,
                model=None,
                remote=True,
                root_count=pipeline.root_count,
                input_chars=0,
                max_roots=0,
                max_input_chars=0,
                estimated_calls=0,
                max_output_tokens=0,
                already_applied=False,
            )
        preflight = await manual_llm_cleaning.preflight(principal.tenant_id, document_id)
        return LlmCleaningPreflightResponse.model_validate(asdict(preflight))

    @router.post(
        "/documents/{document_id}/llm-cleaning",
        response_model=LlmCleaningResponse,
        tags=["documents"],
    )
    async def run_llm_cleaning(
        document_id: UUID,
        body: LlmCleaningRequest,
        principal: Annotated[Principal, Depends(writer)],
    ) -> LlmCleaningResponse:
        result = await _manual_llm_cleaning().clean(
            tenant_id=principal.tenant_id,
            actor_id=principal.actor_id,
            document_id=document_id,
            expected_version_id=body.expected_version_id,
            confirm_remote_processing=body.confirm_remote_processing,
            now=clock(),
        )
        return LlmCleaningResponse.model_validate(asdict(result))

    @router.delete(
        "/documents/{document_id}",
        response_model=DocumentDeleteResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["documents"],
    )
    async def delete_document(
        document_id: UUID,
        principal: Annotated[Principal, Depends(writer)],
    ) -> DocumentDeleteResponse:
        job_id, document_status, repeated = await _workspace().delete_document(
            principal.tenant_id, document_id, now=clock()
        )
        return DocumentDeleteResponse(
            document_id=document_id,
            job_id=job_id,
            status=document_status,
            already_requested=repeated,
        )

    @router.get(
        "/ingestion-jobs",
        response_model=JobListResponse,
        tags=["ingestion"],
    )
    async def list_ingestion_jobs(
        principal: Annotated[Principal, Depends(reader)],
        job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
        cursor: Annotated[str | None, Query(max_length=1_000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> JobListResponse:
        page = await _workspace().list_jobs(
            principal.tenant_id,
            status=job_status.value if job_status is not None else None,
            cursor=cursor,
            limit=limit,
        )
        return JobListResponse(
            items=[_job_list_item(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/ingestion-jobs/{job_id}",
        response_model=JobResponse,
        tags=["ingestion"],
    )
    async def get_ingestion_job(
        job_id: UUID,
        principal: Annotated[Principal, Depends(reader)],
    ) -> JobResponse:
        return _job(await _workspace().get_job(principal.tenant_id, job_id))

    @router.post(
        "/queries",
        response_model=QueryResponseModel,
        tags=["queries"],
    )
    async def execute_query(
        body: QueryRequestModel,
        principal: Annotated[Principal, Depends(reader)],
    ) -> QueryResponseModel:
        result = await _knowledge().execute(principal, _knowledge_query(body))
        return QueryResponseModel.model_validate(result.to_dict())

    @router.post(
        "/queries/stream",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
        tags=["queries"],
    )
    async def stream_query(
        body: QueryRequestModel,
        request: Request,
        principal: Annotated[Principal, Depends(reader)],
    ) -> StreamingResponse:
        query = _knowledge_query(body)
        service = _knowledge()

        async def events() -> AsyncIterator[str]:
            async for event in service.stream(
                principal, query, disconnected=request.is_disconnected
            ):
                payload = json.dumps(
                    to_json_value(event.data), ensure_ascii=False, separators=(",", ":")
                )
                yield f"id: {event.sequence}\nevent: {event.event}\ndata: {payload}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get(
        "/traces",
        response_model=TraceListResponse,
        tags=["traces"],
    )
    async def list_traces(
        principal: Annotated[Principal, Depends(reader)],
        trace_type: Annotated[
            Literal["query", "ingestion", "evaluation"] | None,
            Query(alias="type"),
        ] = None,
        cursor: Annotated[str | None, Query(max_length=1_000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> TraceListResponse:
        page = await _traces().list_traces(
            principal.tenant_id,
            trace_type=trace_type,
            cursor=cursor,
            limit=limit,
        )
        return TraceListResponse(
            items=[_trace_summary(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/traces/query",
        response_model=TraceListResponse,
        tags=["traces"],
    )
    async def list_query_traces(
        principal: Annotated[Principal, Depends(reader)],
        mode: Annotated[Literal["standard", "deep"] | None, Query()] = None,
        trace_status: Annotated[
            Literal["answered", "abstained", "no_results", "error", "cancelled"] | None,
            Query(alias="status"),
        ] = None,
        degraded: Annotated[bool | None, Query()] = None,
        cursor: Annotated[str | None, Query(max_length=1_000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> TraceListResponse:
        page = await _traces().list_traces(
            principal.tenant_id,
            trace_type="query",
            cursor=cursor,
            limit=limit,
            mode=mode,
            status=trace_status,
            degraded=degraded,
        )
        return TraceListResponse(
            items=[_trace_summary(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/traces/query/{trace_id}",
        response_model=QueryTraceViewResponse,
        tags=["traces"],
    )
    async def get_query_trace(
        trace_id: str,
        principal: Annotated[Principal, Depends(reader)],
    ) -> QueryTraceViewResponse:
        return _query_trace_view(
            await _traces().get_query_trace(principal.tenant_id, trace_id)
        )

    @router.get(
        "/traces/ingestion",
        response_model=TraceListResponse,
        tags=["traces"],
    )
    async def list_ingestion_traces(
        principal: Annotated[Principal, Depends(reader)],
        trace_status: Annotated[
            Literal["succeeded", "failed", "retry_wait", "cancelled"] | None,
            Query(alias="status"),
        ] = None,
        cursor: Annotated[str | None, Query(max_length=1_000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> TraceListResponse:
        page = await _traces().list_traces(
            principal.tenant_id,
            trace_type="ingestion",
            cursor=cursor,
            limit=limit,
            status=trace_status,
        )
        return TraceListResponse(
            items=[_trace_summary(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/traces/ingestion/{trace_id}",
        response_model=IngestionTraceViewResponse,
        tags=["traces"],
    )
    async def get_ingestion_trace(
        trace_id: str,
        principal: Annotated[Principal, Depends(reader)],
    ) -> IngestionTraceViewResponse:
        return _ingestion_trace_view(
            await _traces().get_ingestion_trace(principal.tenant_id, trace_id)
        )

    @router.get(
        "/evaluations/catalog",
        response_model=EvaluationCatalogResponse,
        tags=["evaluations"],
    )
    async def evaluation_catalog(
        principal: Annotated[Principal, Depends(reader)],
    ) -> EvaluationCatalogResponse:
        del principal
        return EvaluationCatalogResponse.model_validate(_evaluations().catalog().to_dict())

    @router.post(
        "/evaluations/runs",
        response_model=EvaluationRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["evaluations"],
    )
    async def create_evaluation_run(
        body: EvaluationRunCreate,
        background_tasks: BackgroundTasks,
        principal: Annotated[Principal, Depends(writer)],
    ) -> EvaluationRunResponse:
        try:
            run = await _evaluations().create_run(
                tenant_id=principal.tenant_id,
                actor_id=principal.actor_id,
                dataset_revision=body.dataset_revision,
                mode=body.mode,
                provider_profile=body.provider_profile,
                max_cases=body.max_cases,
                max_llm_calls=body.max_llm_calls,
            )
        except ValueError as error:
            raise AppError(ErrorCode.VALIDATION_ERROR, str(error)) from error
        background_tasks.add_task(
            _evaluations().execute_run, principal.tenant_id, run.id
        )
        return _evaluation_run(run, include_report=False)

    @router.get(
        "/evaluations/runs",
        response_model=EvaluationRunListResponse,
        tags=["evaluations"],
    )
    async def list_evaluation_runs(
        principal: Annotated[Principal, Depends(reader)],
        run_status: Annotated[
            Literal["queued", "running", "succeeded", "failed"] | None,
            Query(alias="status"),
        ] = None,
        cursor: Annotated[str | None, Query(max_length=1_000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> EvaluationRunListResponse:
        page = await _evaluations().list_runs(
            principal.tenant_id, status=run_status, cursor=cursor, limit=limit
        )
        return EvaluationRunListResponse(
            items=[_evaluation_run(run, include_report=False) for run in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/evaluations/runs/{run_id}",
        response_model=EvaluationRunResponse,
        tags=["evaluations"],
    )
    async def get_evaluation_run(
        run_id: UUID,
        principal: Annotated[Principal, Depends(reader)],
    ) -> EvaluationRunResponse:
        return _evaluation_run(
            await _evaluations().get_run(principal.tenant_id, run_id),
            include_report=True,
        )

    @router.get(
        "/evaluations/compare",
        response_model=EvaluationComparisonResponse,
        tags=["evaluations"],
    )
    async def compare_evaluation_runs(
        base_run_id: Annotated[UUID, Query()],
        candidate_run_id: Annotated[UUID, Query()],
        principal: Annotated[Principal, Depends(reader)],
    ) -> EvaluationComparisonResponse:
        comparison = await _evaluations().compare(
            principal.tenant_id, base_run_id, candidate_run_id
        )
        return EvaluationComparisonResponse.model_validate(comparison.to_dict())

    @router.get(
        "/traces/{trace_id}",
        response_model=TraceDetailResponse,
        tags=["traces"],
    )
    async def get_trace(
        trace_id: str,
        principal: Annotated[Principal, Depends(reader)],
    ) -> TraceDetailResponse:
        return _trace_detail(
            await _traces().get_trace(principal.tenant_id, trace_id)
        )

    @router.get("/system/status", tags=["system"])
    async def system_status(
        principal: Annotated[Principal, Depends(reader)],
    ) -> dict[str, str]:
        if principal.actor_type != "user" or principal.role not in {
            "super_admin",
            "system_admin",
        }:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "The current identity cannot access the system administration API.",
            )
        return {"status": "available", "role": principal.role}

    return router


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_text(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "A required text field is blank.",
            {"field": field},
        )
    return stripped


def _unavailable() -> AppError:
    return AppError(
        ErrorCode.SERVICE_UNAVAILABLE,
        "The workspace infrastructure is not configured.",
    )


def _trace_summary(item: TraceSummary) -> TraceSummaryResponse:
    return TraceSummaryResponse(
        trace_id=item.trace_id,
        trace_type=cast(
            Literal["query", "ingestion", "evaluation"], item.trace_type
        ),
        subject_id=item.subject_id,
        mode=item.mode,
        status=item.status,
        started_at=item.started_at,
        finished_at=item.finished_at,
        duration_ms=item.duration_ms,
        span_count=item.span_count,
        degraded=item.degraded,
    )


def _trace_span(item: StoredSpan) -> TraceSpanResponse:
    return TraceSpanResponse(
        span_id=item.span_id,
        parent_span_id=item.parent_span_id,
        name=item.name,
        started_at=item.started_at,
        finished_at=item.finished_at,
        duration_ms=item.duration_ms,
        status=item.status,
        attributes=dict(item.attributes),
        events=[dict(event) for event in item.events],
    )


def _trace_detail(item: TraceDetail) -> TraceDetailResponse:
    return TraceDetailResponse(
        summary=_trace_summary(item.summary),
        actor_type=item.actor_type,
        request_id=item.request_id,
        usage=dict(item.usage),
        attributes=dict(item.attributes),
        spans=[_trace_span(span) for span in item.spans],
    )


def _query_trace_view(item: QueryTraceView) -> QueryTraceViewResponse:
    return QueryTraceViewResponse.model_validate(asdict(item))


def _ingestion_trace_view(item: IngestionTraceView) -> IngestionTraceViewResponse:
    return IngestionTraceViewResponse.model_validate(asdict(item))


def _evaluation_run(item: Any, *, include_report: bool) -> EvaluationRunResponse:
    report = item.report if isinstance(item.report, dict) else None
    aggregate = report.get("aggregate_metrics") if report is not None else None
    usage = report.get("usage") if report is not None else None
    return EvaluationRunResponse(
        id=item.id,
        status=item.status,
        dataset_revision=item.dataset_revision,
        mode=item.mode,
        provider_profile=item.provider_profile,
        provider=item.provider,
        model=item.model,
        prompt_revision=item.prompt_revision,
        index_revision=item.index_revision,
        commit_sha=item.commit_sha,
        max_cases=item.max_cases,
        max_llm_calls=item.max_llm_calls,
        estimated_llm_calls=item.estimated_llm_calls,
        completed_cases=item.completed_cases,
        total_cases=item.total_cases,
        case_ids=list(item.case_ids),
        aggregate_metrics=aggregate if isinstance(aggregate, dict) else {},
        usage=usage if isinstance(usage, dict) else {},
        error_code=item.error_code,
        started_at=item.started_at,
        finished_at=item.finished_at,
        created_at=item.created_at,
        report=report if include_report else None,
    )


def _knowledge_query(body: QueryRequestModel) -> KnowledgeQuery:
    try:
        scope = QueryScope(
            tuple(body.scope.collection_ids),
            tuple(body.scope.document_ids),
            tuple(body.scope.titles),
            tuple(body.scope.organizations),
            tuple(body.scope.doc_types),
            tuple(body.scope.versions),
            tuple(body.scope.sections),
        )
        history = tuple(
            ConversationTurn(ConversationRole(item.role), item.content.strip())
            for item in body.history
        )
        return KnowledgeQuery(body.query.strip(), QueryMode(body.mode), scope, history)
    except ValueError as error:
        raise AppError(ErrorCode.VALIDATION_ERROR, "The query request is invalid.") from error


def _collection(item: CollectionSnapshot) -> CollectionResponse:
    return CollectionResponse.model_validate(item)


def _document(item: DocumentSummary) -> DocumentResponse:
    return DocumentResponse(
        id=item.id,
        collection_id=item.collection_id,
        title=item.title,
        organization=item.organization,
        status=item.status,
        visibility=item.visibility,
        version_id=item.version_id,
        source_name=item.source_name,
        media_type=item.media_type,
        size_bytes=item.size_bytes,
        sha256_prefix=item.sha256[:12],
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _job(item: JobSnapshot) -> JobResponse:
    return JobResponse(
        id=item.id,
        document_id=item.document_id,
        version_id=item.version_id,
        type=item.type,
        status=item.status.value,
        attempts=item.attempts,
        max_attempts=item.max_attempts,
        available_at=item.available_at,
        heartbeat_at=item.heartbeat_at,
        progress=item.progress,
        stage=item.stage,
        error_code=item.error_code,
        error_message=item.error_message,
        cancel_requested=item.cancel_requested,
    )


def _job_list_item(item: JobListItem) -> JobListItemResponse:
    return JobListItemResponse(**_job(item.snapshot).model_dump(), created_at=item.created_at)


def _document_detail(item: DocumentDetail) -> DocumentDetailResponse:
    summary = _document(item.summary).model_dump()
    return DocumentDetailResponse(
        **summary,
        root_count=item.root_count,
        leaf_count=item.leaf_count,
        recent_job=_job(item.recent_job) if item.recent_job else None,
        version_error_code=item.version_error_code,
        version_error_message=item.version_error_message,
    )


def _document_pipeline(item: DocumentPipelineSnapshot) -> DocumentPipelineResponse:
    return DocumentPipelineResponse.model_validate(asdict(item))


def _pipeline_root(item: PipelineRootDetail) -> PipelineRootDetailResponse:
    return PipelineRootDetailResponse.model_validate(asdict(item))
