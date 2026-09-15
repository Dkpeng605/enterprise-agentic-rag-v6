# Infrastructure

M8-00 supplies the standalone ingestion Worker entrypoint; it uses PostgreSQL leases and requires server-backed Milvus when
run beside the API. Local Compose continues to use Milvus Lite in the single-process API+Worker composition. Production
Compose, Caddy, immutable images, deployment, backup, and restore assets will be added by their acceptance PRs. Production
design must remain compatible with the 2GB VPS limits in `DEV_SPEC.md`.
