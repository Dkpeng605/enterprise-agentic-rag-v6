"""ASGI entrypoint."""

from enterprise_rag.api import create_app

app = create_app()
