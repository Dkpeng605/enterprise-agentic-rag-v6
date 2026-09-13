FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY backend /app/backend
COPY config /app/config
COPY evals /app/evals
RUN uv sync --project /app/backend --frozen --no-dev

ENV PYTHONPATH=/app/backend/src
CMD ["uv", "run", "--project", "/app/backend", "--no-sync", "uvicorn", "enterprise_rag.local_runtime:app", "--host", "0.0.0.0", "--port", "8000"]
