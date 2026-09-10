"""Minimal FastAPI application used to verify the M1 repository skeleton."""

from typing import Final

from fastapi import FastAPI

from enterprise_rag import __version__

SERVICE_NAME: Final = "enterprise-agentic-rag-v6"


def create_app() -> FastAPI:
    """Build the ASGI application without import-time infrastructure access."""

    application = FastAPI(
        title="Enterprise Agentic RAG v6",
        version=__version__,
        description="Evaluation-driven, fully pluggable enterprise Agentic RAG platform.",
    )

    @application.get("/", tags=["service"])
    async def service_descriptor() -> dict[str, str]:
        return {
            "service": SERVICE_NAME,
            "version": __version__,
            "status": "skeleton-ready",
        }

    return application

