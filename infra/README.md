# Infrastructure

M8-00 supplies the standalone ingestion Worker entrypoint; it uses PostgreSQL leases and requires server-backed Milvus when
run beside the API. Local Compose continues to use Milvus Lite in the single-process API+Worker composition. Production
runtime images are now in `production/backend.Dockerfile` and `production/frontend.Dockerfile`. The backend image runs the
FastAPI or Worker command as non-root `app` and contains the `/health/live` probe; the frontend image serves the built SPA
on internal port 8080 with Caddy. Production Compose, outer reverse-proxy routing, immutable registry tags, and the approved
SSH deployment entrypoint are present; backup and restore assets remain a later acceptance slice. Production design must
remain compatible with the 2GB VPS
limits in `DEV_SPEC.md`. With `APP_ENVIRONMENT=production`, `enterprise_rag.main:app` selects the remote API composition;
it does not start an ingestion Worker, which remains a separate service using the same remote Milvus revision.

M8-04 adds the manually approved SSH deployment path. Configure the `production` GitHub Environment with the
`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_PRIVATE_KEY`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_REGISTRY_USERNAME`, and
`DEPLOY_REGISTRY_TOKEN` secrets. `DEPLOY_PORT` and `DEPLOY_PATH` are optional Environment Variables. The host must keep
`infra/production/.env.production` outside Git; the workflow transfers only `compose.yml`, `gateway.Caddyfile`, and the
deployment script. `DEPLOY_KNOWN_HOSTS` is mandatory and strict host-key checking is enabled.

`scripts/production-deploy.sh` records a non-secret release state at `.deploy-state`, takes a pre-migration PostgreSQL
custom-format dump under `backups/deploy/`, runs the one-shot migration, starts the immutable image pair, and performs
gateway, anonymous-workspace, and administrator-login smoke checks. A failed deploy restores the previous image references
when they are valid. The explicit `rollback` operation swaps to the recorded previous pair without deleting volumes or
downgrading migrations; migrations must be backward-compatible. The workflow is deliberately manual and Environment-gated,
so creating it does not claim that a VPS or public domain has already been configured.
