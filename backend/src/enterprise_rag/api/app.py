"""FastAPI composition root, request boundary, and workspace routes."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from time import perf_counter
from typing import Final
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import propagate
from opentelemetry.trace import Status, StatusCode, TracerProvider

from enterprise_rag import __version__
from enterprise_rag.adapters.database import Database, PostgreSQLUsageStore
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.usage import InMemoryUsageStore
from enterprise_rag.api.routes import create_api_router
from enterprise_rag.config import AppSettings, load_settings
from enterprise_rag.domain.common import new_uuid7, utc_now
from enterprise_rag.domain.errors import AppError, ErrorCode, ErrorDetail, ErrorResponse
from enterprise_rag.observability import bind_context, start_span
from enterprise_rag.ports.object_store import ObjectStore
from enterprise_rag.ports.usage import UsageAmounts, UsageLimits, UsageStore
from enterprise_rag.services.auth import AnonymousSessionService
from enterprise_rag.services.cost_guard import BudgetedQueryRunner, CostGuard, QueryBudget
from enterprise_rag.services.knowledge import KnowledgeApplication
from enterprise_rag.services.query_api import QueryApiService, QueryRunner
from enterprise_rag.services.workspace import WorkspaceService

SERVICE_NAME: Final = "enterprise-agentic-rag-v6"
LOGGER = logging.getLogger(__name__)
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
    ErrorCode.LLM_INVALID_RESPONSE: 502,
    ErrorCode.LLM_UNAVAILABLE: 503,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
}


def create_app(
    settings: AppSettings | None = None,
    *,
    database: Database | None = None,
    object_store: ObjectStore | None = None,
    session_secret: str | None = None,
    query_runner: QueryRunner | None = None,
    usage_store: UsageStore | None = None,
    query_heartbeat_seconds: float = 15.0,
    tracer_provider: TracerProvider | None = None,
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
        started = perf_counter()
        with bind_context(request_id=request_id), start_span(
            "http.request",
            attributes={
                "http.request.method": request.method,
                "url.path": request.url.path,
            },
            tracer_provider=tracer_provider,
            context=propagate.extract(request.headers),
        ) as span:
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
            span.set_attribute("app.outcome", "error" if response.status_code >= 400 else "ok")
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            LOGGER.info(
                "http.request.completed",
                extra={
                    "event_code": "HTTP_REQUEST_COMPLETED",
                    "outcome": "error" if response.status_code >= 400 else "ok",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                },
            )
        response.headers["X-Request-ID"] = str(request_id)
        return response

    @application.exception_handler(AppError)
    async def app_error_handler(request: Request, error: AppError) -> JSONResponse:
        request_id = _request_id(request)
        response = JSONResponse(
            status_code=STATUS_BY_ERROR.get(error.code, 400),
            content=error.to_response(request_id).to_dict(),
        )
        retry_after = error.details.get("retry_after_seconds")
        if error.code is ErrorCode.RATE_LIMITED and isinstance(retry_after, int):
            response.headers["Retry-After"] = str(retry_after)
        return response

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
            knowledge=(
                KnowledgeApplication(
                    QueryApiService(
                        _budgeted_query_runner(
                            query_runner,
                            usage_store
                            or (
                                PostgreSQLUsageStore(database)
                                if database
                                else InMemoryUsageStore()
                            ),
                            active_settings,
                            clock,
                        ),
                        heartbeat_seconds=query_heartbeat_seconds,
                    ),
                    tracer_provider=tracer_provider,
                )
                if query_runner is not None
                else None
            ),
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


def _budgeted_query_runner(
    runner: QueryRunner, store: UsageStore, settings: AppSettings, clock: Clock
) -> BudgetedQueryRunner:
    security = settings.security
    guard = settings.cost_guard
    cost_guard = CostGuard(
        store,
        limits=UsageLimits(
            security.anonymous_queries_per_minute,
            security.anonymous_daily_llm_calls,
            security.anonymous_daily_input_tokens,
            security.anonymous_daily_output_tokens,
        ),
        budgets=QueryBudget(
            UsageAmounts(
                guard.standard_reserved_llm_calls,
                guard.standard_reserved_input_tokens,
                guard.standard_reserved_output_tokens,
            ),
            UsageAmounts(
                guard.deep_reserved_llm_calls,
                guard.deep_reserved_input_tokens,
                guard.deep_reserved_output_tokens,
            ),
        ),
        clock=clock,
    )
    return BudgetedQueryRunner(runner, cost_guard, timeout_seconds=guard.query_timeout_seconds)
