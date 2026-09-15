# Infrastructure

M8-00 supplies the standalone ingestion Worker entrypoint; it uses PostgreSQL leases and requires server-backed Milvus when
run beside the API. Local Compose continues to use Milvus Lite in the single-process API+Worker composition. Production
runtime images are now in `production/backend.Dockerfile` and `production/frontend.Dockerfile`. The backend image runs the
FastAPI or Worker command as non-root `app` and contains the `/health/live` probe; the frontend image serves the built SPA
on internal port 8080 with Caddy. Production Compose, outer reverse-proxy routing, immutable registry tags, deployment,
backup, and restore assets will be added by their acceptance PRs. Production design must remain compatible with the 2GB VPS
limits in `DEV_SPEC.md`.
