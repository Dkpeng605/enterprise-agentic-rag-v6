"""FastAPI composition root, request boundary, and workspace routes."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Final
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from enterprise_rag import __version__
from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.api.routes import create_api_router
from enterprise_rag.config import AppSettings, load_settings
from enterprise_rag.domain.common import new_uuid7, utc_now
from enterprise_rag.domain.errors import AppError, ErrorCode, ErrorDetail, ErrorResponse
from enterprise_rag.ports.object_store import ObjectStore
from enterprise_rag.services.auth import AnonymousSessionService
from enterprise_rag.services.workspace import WorkspaceService

SERVICE_NAME: Final = "enterprise-agentic-rag-v6"
Clock = Callable[[], datetime]
RequestHandler = Callable[[Request], Awaitable[Response]]

STATUS_BY_ERROR = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.CSRF_INVALID: 403,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.UPLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.DOCUMENT_UNSUPPORTED_TYPE: 415,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
}


def create_app(
    settings: AppSettings | None = None,
    *,
    database: Database | None = None,
    object_store: ObjectStore | None = None,
    session_secret: str | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """Build the ASGI application and optionally compose configured infrastructure."""

    active_settings = settings or load_settings()
    owns_database = database is None and active_settings.credentials.database_url is not None
    if database is None and active_settings.credentials.database_url is not None:
        database = Database(active_settings.credentials.database_url.get_secret_value())
    if object_store is None and database is not None:
        object_store = LocalObjectStore(active_settings.ingestion.object_store_root)
    selected_secret = session_secret
    if selected_secret is None and active_settings.credentials.session_secret is not None:
        selected_secret = active_settings.credentials.session_secret.get_secret_value()
    workspace_ready = (
        database is not None and object_store is not None and selected_secret is not None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_database and database is not None:
            await database.dispose()
        if owns_database and object_store is not None:
            await object_store.aclose()

    application = FastAPI(
        title="Enterprise Agentic RAG v6",
        version=__version__,
        description="Evaluation-driven, fully pluggable enterprise Agentic RAG platform.",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def request_identity(request: Request, call_next: RequestHandler) -> Response:
        request_id = new_uuid7()
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request_id)
        return response

    @application.exception_handler(AppError)
    async def app_error_handler(request: Request, error: AppError) -> JSONResponse:
        request_id = _request_id(request)
        return JSONResponse(
            status_code=STATUS_BY_ERROR.get(error.code, 400),
            content=error.to_response(request_id).to_dict(),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {
                "location": [str(part) for part in item["loc"]],
                "type": item["type"],
            }
            for item in error.errors()
        ]
        response = ErrorResponse(
            ErrorDetail(
                ErrorCode.VALIDATION_ERROR,
                "Request validation failed.",
                _request_id(request),
                {"fields": fields},
            )
        )
        return JSONResponse(status_code=422, content=response.to_dict())

    @application.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, _: Exception) -> JSONResponse:
        response = ErrorResponse(
            ErrorDetail(
                ErrorCode.INTERNAL_ERROR,
                "The service could not complete the request.",
                _request_id(request),
            )
        )
        return JSONResponse(status_code=500, content=response.to_dict())

    @application.get("/", tags=["service"])
    async def service_descriptor() -> dict[str, str]:
        return {
            "service": SERVICE_NAME,
            "version": __version__,
            "status": "workspace-api-ready" if workspace_ready else "configuration-ready",
            "environment": active_settings.app.environment,
        }

    auth: AnonymousSessionService | None = None
    workspace: WorkspaceService | None = None
    if database is not None and object_store is not None and selected_secret is not None:
        auth = AnonymousSessionService(
            database,
            secret=selected_secret,
            tenant_slug=active_settings.security.anonymous_demo_tenant_slug,
            session_minutes=active_settings.security.session_minutes,
            enabled=active_settings.security.anonymous_demo_full_access,
        )
        workspace = WorkspaceService(
            database,
            object_store,
            max_upload_bytes=min(
                active_settings.ingestion.max_upload_bytes,
                active_settings.security.anonymous_max_file_bytes,
            ),
            max_documents=active_settings.security.anonymous_max_ready_documents,
            max_attempts=active_settings.ingestion.max_attempts,
        )
    application.include_router(
        create_api_router(
            auth=auth,
            workspace=workspace,
            tenant_slug=active_settings.security.anonymous_demo_tenant_slug,
            allowed_suffixes=active_settings.ingestion.allowed_suffixes,
            max_upload_bytes=min(
                active_settings.ingestion.max_upload_bytes,
                active_settings.security.anonymous_max_file_bytes,
            ),
            session_minutes=active_settings.security.session_minutes,
            cookie_secure=(
                active_settings.app.environment == "production"
                or str(active_settings.app.public_base_url).startswith("https://")
            ),
            clock=clock,
        )
    )

    return application


def _request_id(request: Request) -> UUID:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else new_uuid7()
