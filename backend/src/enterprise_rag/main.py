"""ASGI entrypoint."""

from enterprise_rag.api import create_app
from enterprise_rag.config import load_settings
from enterprise_rag.observability import configure_json_logging

settings = load_settings()
configure_json_logging(environment=settings.app.environment)
app = create_app(settings)
