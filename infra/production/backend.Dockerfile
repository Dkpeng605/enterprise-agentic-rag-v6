# syntax=docker/dockerfile:1.7

ARG VCS_REF=unknown
ARG IMAGE_VERSION=0.1.0

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY backend/pyproject.toml backend/uv.lock /build/backend/
RUN uv sync --project /build/backend --frozen --no-dev --no-install-project

COPY backend/src /build/backend/src
COPY backend/alembic.ini /build/backend/alembic.ini
COPY backend/migrations /build/backend/migrations
COPY README.md /build/backend/README.md
RUN uv sync --project /build/backend --frozen --no-dev

FROM python:3.12-slim-bookworm

ARG VCS_REF
ARG IMAGE_VERSION
LABEL org.opencontainers.image.title="enterprise-agentic-rag-backend" \
      org.opencontainers.image.version="$IMAGE_VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.source="https://github.com/Dkpeng605/enterprise-agentic-rag-v6"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/backend/src

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        libgomp1 \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /app /data/runtime \
    && chown -R app:app /app /data

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /build/backend/src /app/backend/src
COPY --from=build /build/backend/alembic.ini /app/backend/alembic.ini
COPY --from=build /build/backend/migrations /app/backend/migrations
COPY config /app/config
COPY evals /app/evals
RUN chown -R app:app /app

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"

ENTRYPOINT ["uvicorn", "enterprise_rag.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
