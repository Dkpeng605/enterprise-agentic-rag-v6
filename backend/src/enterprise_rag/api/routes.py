"""FastAPI routes for anonymous demo document and query workflows."""

import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
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
    DocumentResponse,
    ErrorResponseModel,
    JobResponse,
    QueryRequestModel,
    QueryResponseModel,
    TenantModel,
    UploadResponse,
)
from enterprise_rag.domain.common import to_json_value
from enterprise_rag.domain.documents import DocumentStatus, DocumentVisibility
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.jobs import JobSnapshot
from enterprise_rag.domain.retrieval import QueryMode, QueryScope
from enterprise_rag.ports.planner import ConversationRole, ConversationTurn
from enterprise_rag.services.auth import SESSION_COOKIE, AnonymousSessionService, Principal
from enterprise_rag.services.knowledge import KnowledgeApplication, KnowledgeQuery
from enterprise_rag.services.workspace import (
    CollectionSnapshot,
    DocumentDetail,
    DocumentSummary,
    WorkspaceService,
)

Clock = Callable[[], datetime]
BUSINESS_PERMISSIONS = [
    "collections:manage",
    "documents:manage",
    "ingestion:read",
    "query:execute",
    "traces:read",
    "evaluations:run",
]
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
    tenant_slug: str,
    allowed_suffixes: tuple[str, ...],
    max_upload_bytes: int,
    session_minutes: int,
    cookie_secure: bool,
    clock: Clock,
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
            actor_type="anonymous",
            role="demo_operator",
            tenant=TenantModel(id=grant.principal.tenant_id, slug=tenant_slug),
            permissions=BUSINESS_PERMISSIONS,
            csrf_token=grant.csrf_token,
            expires_at=grant.expires_at,
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

    @router.get("/system/status", tags=["system"])
    async def system_status(
        principal: Annotated[Principal, Depends(reader)],
    ) -> None:
        del principal
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Anonymous sessions cannot access the system administration API.",
        )

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
