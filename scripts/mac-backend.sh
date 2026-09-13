#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run: cp .env.mac.example .env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a
if [[ -z "${LLM_API_KEY:-}" || "${LLM_API_KEY}" == "replace-with-your-token" ]]; then
  echo "LLM_API_KEY must be configured in the untracked .env file." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for the local PostgreSQL service." >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. See https://docs.astral.sh/uv/" >&2
  exit 1
fi

docker compose -f infra/compose/compose.dev.yml up -d postgres
uv sync --project backend --locked
uv run --project backend --no-env-file alembic -c backend/alembic.ini upgrade head
exec uv run --project backend --no-env-file uvicorn enterprise_rag.mac_runtime:app \
  --host 127.0.0.1 \
  --port 8000 \
  --env-file .env
