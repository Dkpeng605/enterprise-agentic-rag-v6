"""ASGI entrypoint for development and production compositions."""

from enterprise_rag.config import load_settings
from enterprise_rag.observability import configure_json_logging

settings = load_settings()
configure_json_logging(environment=settings.app.environment)
if settings.app.environment == "production":
    from enterprise_rag.production_api import build_production_api_app

    app = build_production_api_app(settings)
else:
    from enterprise_rag.api import create_app

    app = create_app(settings)
