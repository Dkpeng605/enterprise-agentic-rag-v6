from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOT = REPOSITORY_ROOT / "infra" / "production"


def test_backend_runtime_image_is_minimal_non_root_and_has_liveness_probe() -> None:
    dockerfile = (PRODUCTION_ROOT / "backend.Dockerfile").read_text(encoding="utf-8")

    assert "FROM ghcr.io/astral-sh/uv:" in dockerfile
    assert "uv sync --project /build/backend --frozen --no-dev" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "FROM python:3.12-slim-bookworm" in dockerfile
    assert "tesseract-ocr" in dockerfile
    assert "COPY --from=build /opt/venv /opt/venv" in dockerfile
    assert "/build/backend/.venv" not in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "health/live" in dockerfile
    assert "USER app" in dockerfile
    assert '"--workers", "1"' in dockerfile
    assert ".env" not in dockerfile


def test_frontend_runtime_image_builds_static_assets_as_non_root() -> None:
    dockerfile = (PRODUCTION_ROOT / "frontend.Dockerfile").read_text(encoding="utf-8")
    caddyfile = (PRODUCTION_ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert "FROM node:22-bookworm-slim AS build" in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "pnpm --dir /app/frontend build" in dockerfile
    assert "FROM caddy:" in dockerfile
    assert "COPY --from=build /app/frontend/dist /srv" in dockerfile
    assert "USER app" in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "try_files {path} /index.html" in caddyfile
    assert "file_server" in caddyfile


def test_production_build_context_excludes_credentials_and_runtime_data() -> None:
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for entry in (".env", ".env.*", "data/", "artifacts/", "node_modules/", ".git/"):
        assert entry in dockerignore
