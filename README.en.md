# Enterprise Agentic RAG v6

[简体中文](README.md) | English

Evaluation-driven, fully pluggable enterprise Agentic RAG platform.

The project is being built from scratch through small, reviewed pull requests. The complete architecture, acceptance criteria, and 64-PR roadmap live in [DEV_SPEC.md](DEV_SPEC.md).

## Repository layout

```text
backend/   FastAPI backend and Python tests
frontend/  Vue 3 + TypeScript application
evals/     Versioned evaluation datasets and fixtures
infra/     Local and production infrastructure
docs/      Architecture decisions and operational documentation
```

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 or newer
- pnpm 11
- Tesseract 5 with the `chi_sim` and `eng` language data installed

## Start the current project

Install all locked dependencies from the repository root:

```bash
uv sync --project backend --locked
pnpm install --frozen-lockfile
```

### Full semantic demo on macOS (recommended)

This entry point runs selectable local/remote multilingual embedding and CrossEncoder reranker providers, the native
Milvus BM25 Sparse provider, Milvus Lite, and the background document parsing/ingestion worker. It calls the LLM through OpenAI-compatible
Chat Completions. The defaults keep document and query text on the Mac. Selecting a SiliconFlow remote
profile sends the corresponding Leaf/query or candidate text to SiliconFlow. The OpenAI-compatible LLM
Planner receives the current question, at most 12 conversation turns, and the server-constrained Scope;
the answering call receives only bounded Root evidence recovered after tenant authorization.

Install the system dependencies and prepare an untracked local configuration:

```bash
brew install tesseract tesseract-lang
cp .env.mac.example .env
```

Edit `.env` and replace `LLM_API_KEY` with your TokenHub token. To use the remote BGE profiles, also set
`SILICONFLOW_API_KEY`. TokenHub currently exposes the case-sensitive model ID `MiniMax-M3`. Git ignores
`.env`; never commit it or paste it into an issue or log. Start PostgreSQL, migrations, and the full
FastAPI+Worker composition in the first terminal:

```bash
./scripts/mac-backend.sh
```

The script creates and migrates `enterprise_rag_dev` exclusively for the application and keeps
`enterprise_rag_test` isolated for integration tests. Test fixtures may clear only the test database and
can no longer cascade-delete Mac workspace data. Both databases use the same persistent PostgreSQL
container, but they are separate databases.

The Mac composition also mounts the official SDK v2 Streamable HTTP MCP endpoint at
`http://127.0.0.1:8000/mcp`. If development has no explicit `MCP_TOKEN_PEPPER`, it atomically creates a
dedicated 0600 secret at the ignored `data/runtime/mac/mcp-token-pepper`. This secret is never reused as a
session, LLM, or Provider key and is never logged. Production forbids automatic generation and requires a
separate HTTPS `MCP_PUBLIC_BASE_URL` plus an `MCP_TOKEN_PEPPER` of at least 32 bytes.

The first upload or query downloads these ONNX models into the ignored
`data/runtime/mac/model-cache/` directory and reuses them afterward:

- Embedding: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions);
- Reranker: `jinaai/jina-reranker-v2-base-multilingual`;
- LLM: `MiniMax-M3`, called through `LLM_BASE_URL` from `.env`.

Image captioning is disabled by default (`vision=none`): extracted images remain in the local ObjectStore and
do not leave the Mac. To demonstrate real image understanding, configure
`ENTERPRISE_RAG__PROVIDERS__VISION=openai_compatible`, `VISION_BASE_URL`, `VISION_API_KEY`, and `VISION_MODEL`
in `.env`. The adapter calls an OpenAI-compatible `/chat/completions` endpoint with text plus a base64 data URI.
Only the image bytes are sent; no unauthorized document text is included. 429, 5xx, and transport failures use
bounded retries. Administrators may select the disabled or enabled profile at `/admin/providers`; the selection
is restart-bound and secrets are never returned to the frontend.

The MiniLM registry describes a 512-token input window, but the cached FastEmbed tokenizer on this Mac
reports an actual limit of 128; the UI and Splitter use the runtime limit. The Provider administration page
also shows the profile declaration and the effective limit detected by the current process. For a 512-token, 512-dimensional
local profile, set `EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5` and
`ENTERPRISE_RAG__INGESTION__EMBEDDING_DIMENSION=512`. Model selection is pluggable; switching a model or
dimension creates a new index revision, so old and new vectors are never mixed.

System administrators can select SiliconFlow `BAAI/bge-m3` (1,024 dimensions and an official 8,192-token
input limit) and `BAAI/bge-reranker-v2-m3` at `/admin/providers`. Both use the shared
`SILICONFLOW_API_KEY`; `EMBEDDING_API_KEY` and `RERANK_API_KEY` may override it independently. The default
endpoint is `https://api.siliconflow.cn/v1`, with per-kind base URL overrides available. Remote profiles
are visibly disabled when credentials are missing, and secrets are never returned to the UI. Because the
remote BGE-M3 API does not expose a local tokenizer object, its split counter is explicitly labelled as an
estimate; the configured Leaf budget remains far below 8,192.

The Mac runtime can also be switched directly through .env without using the administration page:
set `ENTERPRISE_RAG__PROVIDERS__EMBEDDING=openai_compatible` or
`ENTERPRISE_RAG__PROVIDERS__RERANKER=openai_compatible`, configure the corresponding
`EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY`/`EMBEDDING_MODEL` or
`RERANK_BASE_URL`/`RERANK_API_KEY`/`RERANK_MODEL`, and set the Embedding dimension to the model's
actual dimension. The Mac composition now honors these explicit Provider settings instead of rejecting
them as unsupported local selections. New UI selections persist `embedding_provider` and
`reranker_provider`; older model-only selection files remain compatible, and current/pending state is
reported from the Provider actually assembled after restart.

Start the Vue 3 frontend in a second terminal:

```bash
pnpm --dir=frontend dev
```

Open `http://127.0.0.1:5173`. Anonymous users automatically receive all Demo Tenant business
permissions. Create a collection, upload PDF/DOCX/XLSX/XLS/CSV/HTML/TXT/Markdown, inspect real
parsing and ingestion progress, then use Knowledge Chat to exercise Dense/Sparse retrieval, RRF,
CrossEncoder reranking, Root recovery, LLM generation, and citations. Inspect provider status in
Tenant Overview or at `http://127.0.0.1:8000/health/doctor`. After administrator login, the “Manage and select”
link opens `/admin/providers`: it reads the live registry, shows selectable Embedding/Reranker/Sparse profiles with
dimensions, effective token limits, language notes, and local/remote attributes, and saves the next-start selection.
Selection is not a hot swap; restart the backend to apply it. The page distinguishes the current runtime profile
from the pending restart profile. After restart, the `/admin/providers` page shows
the active index revision, per-document Root/Leaf/vector counts, and incompatible old revisions. The administrator
can click “Rebuild incompatible documents”: vectors are projected into the new Milvus revision first, PostgreSQL
Root/Leaf content is swapped only after projection succeeds, and old-revision vectors are deleted last. A projection
or database-swap failure leaves the old index intact. Changing only the Reranker does not require vector rebuild.
Compatibility checks PostgreSQL Root revisions, the Leaf count, and the current-revision Milvus vector count
together. A document is rebuilt rather than skipped when vectors are missing or mismatched even if its Roots
already carry the current revision. Once a restart applies a saved Provider selection, the UI reports it only as
the running profile instead of continuing to label the same model as pending. The pending comparison also includes
the Embedding dimension: even when the model name is unchanged, a persisted target dimension that differs from the
inspected runtime dimension is exposed as `pending_embedding_dimension` instead of being reported as applied.
The corresponding endpoints are `GET /api/v1/admin/providers/index-status` and
`POST /api/v1/admin/providers/reindex`.

After the first page visit creates the Demo Tenant, a trusted local terminal can issue an MCP token bound to all
currently active collections. The raw token is written to stdout once while non-secret metadata goes to stderr;
do not copy it into Git, issues, or terminal logs:

```bash
umask 077
uv run --project backend --env-file .env enterprise-rag-mcp-token issue \
  > /tmp/enterprise-rag-mcp-token
```

Configure the single-line value from `/tmp/enterprise-rag-mcp-token` in the MCP client, use
`http://127.0.0.1:8000/mcp` as the endpoint, and send it as a Bearer credential. The default token has all four
MCP scopes and can access only collections present in the Demo Tenant when it was issued; tool arguments cannot
widen that allowlist. List non-secret metadata or revoke a token with:

The token collection allowlist is a server-side authorization boundary, not an explicit user-selected query filter.
An allowlisted collection with no ready document therefore does not make `query_knowledge_base` fail when another
authorized collection has evidence; only collections explicitly supplied by the caller are treated as explicit scope
filters. `scripts/mac-mcp-smoke.py --run-query` reports the real response `status` field.

```bash
uv run --project backend --env-file .env enterprise-rag-mcp-token list
uv run --project backend --env-file .env enterprise-rag-mcp-token revoke <TOKEN_UUID>
rm -f /tmp/enterprise-rag-mcp-token
```

Before revocation, run a live official-SDK smoke that prints neither content nor credentials. Add `--run-query`
only when the current LLM should also be called:

```bash
uv run --project backend --env-file .env python scripts/mac-mcp-smoke.py \
  --token-file /tmp/enterprise-rag-mcp-token
```

This CLI is a local operator boundary, not an anonymous HTTP API. Anonymous users still cannot issue, read, or
revoke tokens from the frontend.

The Overview page's “Load demo data” button calls the CSRF-protected `/api/v1/demo/seed` endpoint and submits
two non-sensitive Markdown examples from the repository through the same upload registration, PostgreSQL job,
parser, cleaner, Root/Leaf splitter, and vector projection pipeline. These are not frontend fixtures; repeated
clicks are content-digest idempotent and return the existing documents/jobs. After ingestion, the persisted records
can be inspected in Documents, Ingestion Trace, Query Trace, Knowledge Chat, and Evaluations.

After a document reaches `ready`, open it in Documents and select “Inspect parsing, cleaning, and splitting.”
The page shows the actual Parser, deterministic Cleaner, Splitter settings, Root raw/clean comparisons and rule
audits, plus every Leaf's full text, token count, offsets, and boundaries. New ingestion uses disjoint Leaves by
default, with Root recovery providing the surrounding context. It reads the PostgreSQL source of
truth rather than inferring chunks in the browser. Documents ingested before audit metadata was introduced are
explicitly labeled as legacy data; re-uploading creates a complete record.

The same page offers an opt-in, one-pass remote LLM cleaning action that is off by default. Its preflight shows
the Provider/Model, Root and character counts, one-call budget, and the risk of data leaving the Mac. Only after
the checkbox confirmation does the current version's `clean_text`—not the original file—leave the machine. One
pass is limited to 20 Roots, 12,000 input characters, and 8,000 output tokens; oversized, non-ready, stale, or
already-cleaned versions are rejected. The adapter removes MiniMax's common `<think>...</think>` reasoning wrapper and
JSON code fence before validation, without sending hidden reasoning into the cleaning validator. The response must preserve Root ordinals and pass checks for the complete lexical
sequence, numbers, URLs, emails, quoted values, headings, table headers, and fenced code before the service rechunks and
rebuilds the Dense/Sparse index. The LLM may repair PDF/OCR whitespace, paragraph line reflow, heading/table spacing,
and a word broken by a line-break hyphen, but it may not merge distinct words or change lexical order, facts, numbers,
or code. Only a complete noise line that was at an original Root edge and repeats across Roots may be removed. The UI
shows changed Roots,
before/after Leaf counts, token usage, retries, and persisted hash
audits. Failures attempt to restore the previous vectors and ready state. This synchronous process-local lock is
for the single-process Mac demo; multi-replica production coordination remains M8 work.

Both Standard and Deep use the real model chain in this Mac composition. Deep now composes the M4
Evidence Ledger and a maximum two-round Recovery Controller. Whenever evidence exists, the current
OpenAI-compatible LLM assesses the single original-question requirement's coverage, gaps, and conflicts. A gap executes Rewrite
Hybrid, HyDE Dense-only, or Exact-term Sparse-only and then repeats RRF, PostgreSQL authorization,
reranking, and Root restoration. Scope repair may remove only a Planner-added condition that the caller
did not explicitly select. It cannot relax caller Collection or Document scope. Assessor failure is
reported as degradation and continues only through bounded recovery before abstention.

Neither mode trusts free-form answer text. The LLM must return structured paragraphs, citation IDs,
Root and Leaf IDs, contiguous quotes copied from Root evidence, and coverage of that original requirement. The backend
deterministically verifies factual paragraphs, ownership, quotes, and coverage. A rejected draft gets at
most one schema regeneration for malformed JSON and one semantic repair with exactly the same authorized
roots. Fully covered results are `answered`; when valid citations remain but requirements are missing, the
API returns `partial`, preserves only independently verified paragraphs and citations, and lists the gaps.
Malformed citation structure, conflicts, or no retainable evidence remain `abstained`; a partial result is
never presented as complete.

Structured calls now explicitly send `response_format: {"type":"json_object"}` through the
OpenAI-compatible adapter instead of relying only on prompt instructions. Query Planner, Evidence Assessor,
Answer Author/Repair, the optional LLM Judge, and manual LLM cleaning share the
`CompletionRequest.json_mode` switch; ordinary free-text calls omit the field. Provider discovery exposes the
`json-mode` capability. If an upstream does not support this OpenAI-compatible extension, the service retains a
sanitized stable error and follows the existing bounded abstention policy rather than treating Markdown or
unexpected text as structured facts.

The Mac QueryRunner first calls the same timeout/retry-bounded OpenAI-compatible LLM for a strict JSON
QueryPlan. It rewrites context-dependent questions into standalone retrieval queries. Sub-query execution is
opt-in: the Planner must explicitly return `use_sub_queries=true` and two to four alternative routes serving the
same original question before parallel branches activate. The separate aspects of a comparison, multi-part, or
multi-hop question are not alternative routes and must not be split merely for presentation. With
`use_sub_queries=false`, the backend executes only the single rewritten query even if the model accidentally fills
extra routes. The backend still validates every field, list bound, UUID, and Scope. Malformed JSON, expanded Scope,
or a Provider failure falls back atomically to one deterministic retrieval route. Query Trace displays the rewrite,
sub-queries, Planner Provider/degradation, Planner tokens, per-branch Dense/Sparse returns and overlap,
RRF deduplication and drops, authorization filtering, reranking, Root recovery, Deep evidence
assessment/recovery rounds, answer generation, citation verification/repair, and per-stage tokens.
Requirements are independent of retrieval branching: every QueryPlan has exactly one requirement, copied from the
original user question. A sub-query is only an alternative route to evidence for that same requirement; it creates
no new answer obligation and does not need separate coverage. If any branch supplies sufficient reliable evidence,
the assessor and final verifier may complete the single requirement even when other branches return no evidence.
Before retrieval, the runner rebinds `original_query` and the single requirement to the server-received query, so a
Provider cannot replace the user's question through a fabricated plan field. When the LLM explicitly disables
branching and returns an empty `sub_queries` list, the backend synthesizes exactly one `rewritten_query` route.
Leaf `matched_queries` is accepted only as provenance belonging to the current plan; it cannot create coverage by
itself.
Planner, Assessor, answer, and Repair calls all count toward query usage; a completed Planner call is still
reported when retrieval finds no evidence. These runtime counts are not Recall@K; gold-labelled quality
metrics remain in Evaluations.

The local real-LLM smoke distinguishes a model request failure from a structured answer failing verification.
The Answer Author allows one schema regeneration; when structure is valid but citation or requirement
verification fails, it allows one Repair. Root recovery still retains the complete clean text for deterministic
verification, but Answer/Assessor receive only the original text of authorized, reranked Leaves selected in this
run, avoiding repeated delivery of a long document to a reasoning model. The Answer Author system contract also
forbids chain-of-thought output and limits the result to four short paragraphs, six citations, and short quotes;
unsupported requirements are reported as a concise gap. Every attempt uses the same authorized Root set, and failure returns a safe abstention instead of raw model text. If a TokenHub/MiniMax-M3 answer contains long `<think>`
reasoning, adjust the local output bound in `config/macos.example.yaml` or with `.env` variable
`ENTERPRISE_RAG__COST_GUARD__ANSWER_MAX_OUTPUT_TOKENS`, then inspect `answer_generation` usage in Trace.
Do not hide the issue by disabling `response_format`, removing citation verification, or marking answer
errors as answered.

Result diagnosis keeps three dimensions separate: Query `status` (`answered`/`partial`/`abstained`/`no_results`; a
lower-level Trace may also record `error`/`cancelled`),
answer-stage `answer_status` (including `not_generated`, `generation_degraded`, `answered`, `repaired`, `partial`, and
`abstained`), and component flags such as `planner_degraded`, `reranker_degraded`, `assessor_degraded`, and
`generation_degraded`. `assessor_degraded` means that bounded recovery continued after an assessor failure; it
does not mean that retrieval returned no evidence. `generation_degraded` means no verifiable answer structure was
obtained and must not be relabelled as success. Query Trace `degraded=true` uses an OR over these stable flags, so
Assessor and Answer Generation degradation are included; `degraded=false` keeps only runs with no recorded component
degradation. Per-run candidate counts and abstention rates are diagnostics, not Recall/MRR, and do not replace
gold-labelled Evaluation metrics.

Workspace Overview also exposes the last 24 hours of `query_outcome_counts`, `query_abstention_rate`,
`query_answer_rate`, and `query_generation_degraded_24h`. Abstention rate is defined as
`abstained / all query traces`; `partial` contributes to the effective-answer rate but not to the full-answer rate,
while `no_results` and `error` are not disguised as either abstentions or answers. The separate
`generation_degraded` count helps identify safe abstentions caused by malformed structured model output; it is not, by
itself, a retrieval-quality metric.

If an older checkout shared the application and test database, stop the backend and run the read-only
check before applying deletion. The command reconciles PostgreSQL facts with Milvus projections by
`tenant/version/index_revision`. New projections persist a revision marker, so a stale revision for a
valid version can be deleted precisely while the active revision is retained. A version-scoped delete
is used only when PostgreSQL has no such tenant/version at all. The command does not delete objects,
documents, or jobs:

```bash
uv run --project backend --env-file .env python scripts/mac-reconcile-vectors.py
uv run --project backend --env-file .env python scripts/mac-reconcile-vectors.py --apply
```

Milvus Lite permits only one process to hold its file, so the backend must be stopped. A
`vector_count_mismatch` remains unresolved because it may represent missing vectors for a valid version
and requires re-ingestion or manual investigation. Legacy rows without a revision marker remain
unchanged and are reported as `unknown_vector_revision` when their version still exists; this is an
intentional safety boundary because collection names and the current Provider cannot prove ownership.
Marked stale projections are reported as `orphan_vector`, and `--apply` deletes only that
tenant/version/revision.

### Local single-process Worker and production Worker

The local `mac_runtime` entrypoint starts the polling Worker inside the same FastAPI process. It reuses the real Loader,
Cleaner, Splitter, Embedding, Sparse, Vision, Projection, Milvus, and persistent Trace components; uploaded Jobs are
processed with PostgreSQL leases, heartbeats, expiry recovery, and bounded retries. Start the local service with the
`./scripts/mac-backend.sh` command above, and do not start another process that opens the same Milvus Lite
`vectors.db`.

M8-00 now provides the standalone `enterprise-rag-worker` process entrypoint. It reuses the real Loader, Cleaner, Splitter,
Embedding, Sparse, Vision, Projection, and Trace components, and coordinates multiple Workers through PostgreSQL `SKIP LOCKED`,
leases, heartbeats, expiry recovery, and bounded retries. Each process runs one Pipeline at a time; `SIGINT`/`SIGTERM` stops
new claims and closes resources in reverse order. Start it with:

```bash
uv run --project backend --env-file .env enterprise-rag-worker
```

For local development with `milvus_lite`, the API and standalone Worker must not open the same `vectors.db` concurrently;
run the standalone Worker only while the API is stopped. Production must set `providers.vector_store=milvus_remote` and
provide `VECTOR_STORE_URI`, `VECTOR_STORE_TOKEN` (and optionally `VECTOR_STORE_DATABASE`) so the API and multiple Workers
share a server-backed Milvus. This completes the production Worker prerequisite; the M8-01 images and M8-02 Compose are
documented below, while backups, deployment drills, and public release remain later M8 slices.

### M8-01 production images

`infra/production/backend.Dockerfile` is a two-stage build for a production FastAPI/Worker image. The runtime includes the
system libraries required by PDF/OCR ingestion, fixes one Uvicorn worker and a `/health/live` healthcheck, and runs as the
non-root `app` user with UID 10001. Compose can override the same image command to start the standalone
`enterprise-rag-worker`, so the backend environment is not duplicated.

`infra/production/frontend.Dockerfile` compiles Vue 3/TypeScript in a Node build stage and serves the resulting SPA with
Caddy on internal port 8080 as the non-root `app` user, including history fallback. Neither image contains `.env` files,
runtime data, dependency caches, model weights, or host build output; the root `.dockerignore` removes them before the build
context is sent.

Reproducible local `linux/amd64` records from `docker image inspect` (2026-09-16): backend `278238911` bytes (about
265.3 MiB), frontend `22704397` bytes (about 21.7 MiB). These are build records for the current base images and dependency
lock, not a promise of runtime capacity; M8-02 now includes the production Compose, private network, and outer Caddy
configuration, while public release remains a later deployment, restore-drill, and public-acceptance slice.

```bash
docker build --platform=linux/amd64 -f infra/production/backend.Dockerfile -t enterprise-rag-backend:local .
docker build --platform=linux/amd64 -f infra/production/frontend.Dockerfile -t enterprise-rag-frontend:local .
```

### Production API composition root (Compose prerequisite)

When `APP_ENVIRONMENT=production`, `enterprise_rag.main:app` composes the remote Embedding, remote Reranker,
OpenAI-compatible LLM (used by planning, answering, evidence assessment, and manual cleaning), Milvus native BM25,
and server-backed Milvus. It owns HTTP queries, workspace, administration, Trace, evaluation, and MCP only; it does not
start an ingestion Worker inside the API process. The separate `enterprise-rag-worker` claims ingestion through PostgreSQL
leases, and the API and Worker share the same `index_revision`.

The production composition rejects local Embedding/Reranker/LLM, Milvus Lite, Hashing Sparse, and plaintext MCP URLs. It
requires database, session, admin bootstrap, LLM/Embedding/Reranker, remote Milvus, MCP pepper, and metrics-token settings.
Model identity is bound to production environment variables; the local Provider selection file cannot override a production
model request.

After migrations, the API can be started as one Uvicorn worker (public domain and server deployment remain M8-04 through M8-06):

```bash
APP_ENVIRONMENT=production \
  uv run --project backend uvicorn enterprise_rag.main:app --host 0.0.0.0 --port 8000 --workers 1
```

### M8-02 production Compose/Caddy

`infra/production/compose.yml` orchestrates PostgreSQL, a one-shot migration, the API, the standalone Worker, the frontend,
and the outer Caddy gateway. API, Worker, PostgreSQL, and frontend join only the `private` network and have no host ports;
only the gateway maps host ports 80/443. API and Worker share the `runtime-data` ObjectStore volume and remote Milvus;
API/Worker start only after migrations succeed. Each long-running service has health checks, CPU/memory bounds, restart policy,
and JSON log rotation. `gateway.Caddyfile` routes API/MCP/SSE to the backend and the SPA to the frontend, preserving HTTPS,
security headers, SPA history fallback, and streaming flush behavior.

Prepare the production environment file (the example contains placeholders only and must not be committed with real values):

```bash
cp infra/production/env.production.example infra/production/.env.production
# Edit infra/production/.env.production with immutable image tags, domain, database, Providers, and every secret
docker compose --env-file infra/production/.env.production \
  -f infra/production/compose.yml up -d
docker compose --env-file infra/production/.env.production \
  -f infra/production/compose.yml ps
```

This completes the container topology and locally reviewable configuration, not a public release. SSH deployment, backup/restore,
domain setup, and 24-hour public acceptance remain M8-04 through M8-06; GHCR immutable images were delivered by M8-03.
To stop services while retaining volumes, use `down` without `--volumes`:

```bash
docker compose --env-file infra/production/.env.production \
  -f infra/production/compose.yml down
```

### M8-03 GHCR immutable images

`.github/workflows/images.yml` runs only for pushes to `main`, uses `GITHUB_TOKEN` to publish both `linux/amd64` images to
GHCR, and creates only a `${commit_sha}` tag—never a drifting `latest` tag. The Dockerfiles also set
`org.opencontainers.image.revision`, `org.opencontainers.image.version`, and `org.opencontainers.image.source`, so a deployment
manifest can be audited and rolled back by exact commit:

```bash
docker pull ghcr.io/<owner>/enterprise-agentic-rag-backend:<commit-sha>
docker pull ghcr.io/<owner>/enterprise-agentic-rag-frontend:<commit-sha>
docker image inspect ghcr.io/<owner>/enterprise-agentic-rag-backend:<commit-sha> \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
```

Compose `BACKEND_IMAGE`/`FRONTEND_IMAGE` should use the complete references for the same commit. GHCR publication alone does
not mean SSH deployment or public acceptance has completed.

### M8-04 SSH deploy and rollback

`.github/workflows/deploy.yml` is a manually dispatched workflow that only accepts runs from `main`. Its job is bound to the
GitHub Environment `production`, so the required reviewers configured for that Environment must approve each run; concurrent
deployments are never cancelled. A deploy accepts only a 40-character commit SHA already published by M8-03. The runner
checks that it is an ancestor of `origin/main` and that both images exist before using verified SSH to upload the Compose/Caddy
files and `scripts/production-deploy.sh`. The server's `infra/production/.env.production` is never overwritten from Git.

Configure these Secrets in the GitHub Repository/Environment `production` (never commit them):

```text
DEPLOY_HOST                 VPS hostname or IP
DEPLOY_USER                 dedicated user with Docker permission
DEPLOY_SSH_PRIVATE_KEY      deployment private key
DEPLOY_KNOWN_HOSTS          complete SSH host-key line verified out of band
DEPLOY_REGISTRY_USERNAME    GHCR read-only account
DEPLOY_REGISTRY_TOKEN       GHCR packages:read token
```

Optional Environment Variables are `DEPLOY_PORT` (default `22`), `DEPLOY_PATH` (default
`/opt/enterprise-agentic-rag-v6`), and `PUBLIC_BASE_URL` (only for the Environment link). The server must have Docker
Engine/Compose plugin and a prepared `${DEPLOY_PATH}/infra/production/.env.production` containing the public domain,
remote Providers, Milvus, administrator bootstrap, and all production secrets. The script atomically updates only
`APP_COMMIT_SHA`, `BACKEND_IMAGE`, and `FRONTEND_IMAGE`; all other server configuration remains in place.

Run deploy or rollback as follows. Leave `release_sha` empty to deploy the current `main` commit, and first confirm that the
M8-03 image workflow succeeded for that commit:

```bash
gh workflow run deploy.yml --ref main \
  -f action=deploy -f confirmation=DEPLOY \
  [-f release_sha=<40-character-commit-sha>]
gh run watch

gh workflow run deploy.yml --ref main \
  -f action=rollback -f confirmation=ROLLBACK
gh run watch
```

Each deploy first creates a custom-format PostgreSQL `pg_dump` at
`backups/deploy/pre-deploy-<sha>-<timestamp>.dump`, then runs `docker compose run --rm migrate`, and starts the API,
standalone Worker, frontend, and gateway. Smoke checks Caddy HTTPS `/health/live`, anonymous `/auth/me` and the isolated
`/workspace/overview`, and verifies that administrator login returns `super_admin` inside the API container; it never prints
content, cookies, or credentials. A failed deploy restores the pre-deploy environment file and attempts to restart the last
valid SHA image pair. An explicit rollback only switches to the recorded previous immutable image pair; it never deletes
PostgreSQL, ObjectStore, Root/Leaf, Trace, Milvus, or volumes and never performs a migration downgrade. Production migrations
must therefore remain backward-compatible. Until a pre-production host completes one successful deploy and rollback drill,
the system must not be described as publicly released.

### M8-05 Backup and restore

`scripts/production-backup.sh` and `.github/workflows/backup.yml` provide a manually confirmed production backup and
isolated restore drill. A backup contains a PostgreSQL custom-format dump, an ObjectStore archive, the Milvus backup-hook
reference, runtime Provider/index fingerprints, Compose/Caddy manifests, and SHA-256 checksums. It never contains
`.env.production`, secrets, or model files. Production backups require `BACKUP_AGE_RECIPIENT`; encrypted restore requires
`BACKUP_AGE_IDENTITY`. Plaintext is allowed only when the development/drill operator explicitly sets
`BACKUP_ALLOW_PLAINTEXT=1`.

Before restore, the script verifies the backup directory name, the metadata-to-commit/image binding, every checksum, and
the Milvus hook. The target must be an absolute path and must not be `DEPLOY_PATH` or one of its descendants. The script
never runs `docker compose down --volumes`, deletes the whole Milvus file, or overwrites the live environment file.
`restore-drill` is the isolated restore entrypoint: it runs migration, restores ObjectStore/Milvus, runs revision-aware
reconcile, and reports success only after HTTPS `/health/live`, anonymous workspace, and query smoke checks pass:

```bash
gh workflow run backup.yml --ref main \
  -f action=backup -f confirmation=BACKUP
gh workflow run backup.yml --ref main \
  -f action=restore-drill -f confirmation=RESTORE-DRILL \\
  -f backup_path=/absolute/path/to/release-backup
gh run watch
```

The workflow is manual, restricted to `main`, and bound to the `production` Environment. The restore target, Age
identity, Milvus hook, and SSH host key must be configured through that Environment. The repository now has static
contract coverage for the script and workflow, but does not claim a real production restore from static checks; M8-05
remains pending until one empty-target restore drill succeeds on a non-live host.

Stop the backend and frontend with `Ctrl+C`; keep PostgreSQL and the model cache for quicker restarts.
To stop PostgreSQL only:

```bash
docker compose -f infra/compose/compose.dev.yml stop postgres
```

### Offline browser acceptance

To start the current interactive offline journey directly—PostgreSQL, migrations, the
FastAPI+Worker process, and Vue—without model credentials:

```bash
docker compose -f infra/compose/compose.e2e.yml up --build postgres backend frontend
```

If Docker Desktop on macOS reports
`x-docker-expose-session-sharedkey ... non-printable ASCII` for a repository path containing
non-ASCII characters, build with the classic builder before starting the services, or clone into
an ASCII-only path:

```bash
DOCKER_BUILDKIT=0 docker compose -f infra/compose/compose.e2e.yml build
docker compose -f infra/compose/compose.e2e.yml up postgres backend frontend
```

These compatibility commands have been verified from the repository's current Chinese path through
the complete browser journey.

The development and E2E Compose files define different stable project names
(`enterprise-agentic-rag-v6-dev` and `enterprise-agentic-rag-v6-e2e`). E2E rebuilds, stops, and
`--volumes` cleanup therefore affect only the E2E PostgreSQL instance and runtime volume; they cannot
accidentally operate on the local development database. Do not override this isolation by reusing a
generic `compose` project name.

Open `http://127.0.0.1:4173`. This composition uses deterministic hashing Dense/Sparse
retrieval and extractive answers for local demonstrations and acceptance; it is not a claim
about production semantic-model quality. Anonymous sessions have all business permissions in
the Demo Tenant. To test the isolated administration surface, use the acceptance-only credentials
`admin` / `admin`. Stop it and remove its demo data with:

```bash
docker compose -f infra/compose/compose.e2e.yml down --volumes --remove-orphans
```

Install the PDF/OCR system dependencies:

```bash
# macOS (Homebrew)
brew install tesseract tesseract-lang

# Ubuntu/Debian
sudo apt-get update
sudo apt-get install --yes tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim
```

Start the development PostgreSQL service and apply migrations:

```bash
docker compose -f infra/compose/compose.dev.yml up -d postgres
./scripts/ensure-local-databases.sh
export DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_dev
export SESSION_SECRET=development-only-change-me-32-bytes-minimum
uv run --project backend alembic -c backend/alembic.ini upgrade head
```

`ensure-local-databases.sh` first reuses an existing container bound to `55432` only when Docker identifies it as a
PostgreSQL service from an older Compose project, then creates missing development/test databases. An unrelated port
occupant fails loudly; the script never stops or deletes an existing user database.

Start the backend in the first terminal:

```bash
ENTERPRISE_RAG_CONFIG_FILE=config/development.example.yaml \
  uv run --project backend uvicorn enterprise_rag.main:app --reload
```

The development API is available at `http://127.0.0.1:8000`. The backend exposes:

- `GET /` — service name, version, configuration status, and active environment
- `GET /docs` — interactive OpenAPI documentation
- `GET /openapi.json` — OpenAPI schema
- `GET /api/v1/auth/me` — create an anonymous demo session and obtain a CSRF token
- `POST /api/v1/auth/login` and `POST /api/v1/auth/logout` — Argon2id administrator login and CSRF-protected session revocation
- `GET /api/v1/system/status` — server-enforced system administrator boundary; anonymous identities always receive 403
- `GET /api/v1/workspace/overview` — current-tenant operational metrics and recent activity aggregates
- `/api/v1/collections` — demo-tenant collection CRUD
- `/api/v1/documents` — streaming upload, filtering, and cursor pagination
- `/api/v1/documents/{id}` — document detail and idempotent deletion
- `GET /api/v1/documents/{id}/pipeline` and `/pipeline/roots/{root_id}` — tenant-scoped processing, Root/Leaf, and cleaning audit views
- `GET /api/v1/documents/{id}/llm-cleaning/preflight` and `POST /api/v1/documents/{id}/llm-cleaning` — one-pass remote-cleaning preflight, explicit confirmation, rechunking, and index rebuild
- `GET /api/v1/ingestion-jobs` and `GET /api/v1/ingestion-jobs/{id}` — cursor-paginated job filtering and detail
- `POST /api/v1/queries` and `POST /api/v1/queries/stream` — synchronous and SSE query contracts; the development skeleton returns 503 without a QueryRunner, while the production entrypoint uses the real remote composition
- `GET /api/v1/traces`, `/api/v1/traces/query`, and `/api/v1/traces/ingestion` — tenant-scoped Trace filtering and cursor pagination; Query lists support mode/status/degraded
- `GET /api/v1/traces/query/{trace_id}` — sanitized Query waterfall, rank movement, Recovery, and degradation projection
- `/api/v1/evaluations/catalog`, `/api/v1/evaluations/runs`, and `/api/v1/evaluations/compare` — budget preflight, tenant run history, reports, and controlled comparison
- `GET /api/v1/traces/{trace_id}` — stage timing, candidate ranks, scores, and degradation details
- `GET /health/live`, `GET /health/ready`, and `GET /health/doctor` — liveness, readiness, and sanitized Provider diagnostics
- `GET /api/v1/admin/providers` and `POST /api/v1/admin/providers/select` — system-admin live Provider registry, selectable Embedding/Reranker/Vision/Sparse profiles, and restart-bound selection
- `GET /api/v1/workspace/mcp` — current composition's MCP Server, six read-only tools, four resource forms, and stdio/HTTP transport status
- `GET /metrics` — Prometheus text exposition; production requires a dedicated bearer token

The stdio MCP server uses the official Python SDK v2 and exposes six read-only knowledge tools plus four tenant-scoped resource forms. Build an `MCPServer` in your own composition module, then configure its factory explicitly:

```bash
ENTERPRISE_RAG_MCP_STDIO_FACTORY=your_package.bootstrap:build_mcp_server \
  uv run --project backend enterprise-rag-mcp-stdio
```

The factory must be a no-argument function returning `MCPServer`. The default development entrypoint remains deliberately unconfigured; `backend/tests/fixtures/mcp_stdio_server.py` is only a real-SDK subprocess contract fixture. stdout is reserved for stdio JSON-RPC and application logs must use stderr.

The composition root creates the Streamable HTTP server with `build_http_mcp_app(...)`; its fixed endpoint is
`/mcp`. The Mac runtime now shares the REST `KnowledgeApplication` and uses the real PostgreSQL Catalog, current
Dense/Sparse providers, and Milvus revision rather than fixtures:

```bash
export MCP_PUBLIC_BASE_URL='https://rag.example.com'
export MCP_TOKEN_PEPPER='replace-with-at-least-32-random-bytes'
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend uvicorn enterprise_rag.mac_runtime:app
```

Public `MCP_PUBLIC_BASE_URL` must use HTTPS; only development/test compositions allow plaintext loopback HTTP.
Clients send `Authorization: Bearer <token>`. PostgreSQL stores only a peppered HMAC and binds each token to an
active tenant, active actor, tool scopes, and a non-empty collection allowlist; it never stores the raw token.
The trusted CLI above currently handles issuance, listing, and revocation. Anonymous demo users cannot invoke this
operator boundary; M9-03 will add the system-administrator UI.

The default backend entry point writes one-line JSON application logs with environment plus request/trace/span/tenant correlation and stable event fields. HTTP accepts W3C `traceparent`; Query, Standard RAG stages, and Ingestion stages are manually instrumented with OpenTelemetry. When PostgreSQL is configured, FastAPI composes a bounded in-memory exporter and PostgreSQL Trace Store by default; custom deployments may still inject an SDK `TracerProvider`/`TraceService` into `create_app`, `KnowledgeApplication`, and `IngestionPipeline`. Logs and traces exclude query strings, request bodies, raw questions, Root text, prompts, Authorization, cookies, and secrets. A Trace write failure does not change the business result.

Before a write, call `GET /api/v1/auth/me`, retain its Cookie, and send the returned `csrf_token` in the `X-CSRF-Token` header. Development HTTP cookies omit Secure; production or an HTTPS base URL always enables Secure.

Start the frontend in a second terminal:

```bash
pnpm --dir=frontend dev
```

Vite proxies `/api` and `/health` to `127.0.0.1:8000` with same-origin browser semantics. The frontend now includes a responsive shell, the complete route table, anonymous-session bootstrap, administrator login, system route guards, public SSE chat, tenant overview, Collection/Document management, ingestion-job monitoring, Query and Ingestion Trace inspectors, the MCP capability catalog, and the budgeted evaluation workspace. The chat page keeps collection scope selection in both desktop and mobile viewports; mobile uses a bounded scrolling control area instead of hiding the actual knowledge scope. Anonymous visitors may use `/workspace/*` without login; `/workspace/overview` reads current-tenant collection, document, index, 24-hour query, and recent activity aggregates alongside `/health/doctor` Provider states. `/workspace/documents` provides collection CRUD, filtering, upload, detail, and safe deletion, while `/workspace/ingestion` shows persisted job progress. `/admin/providers` shows the live Provider registry and selectable Embedding/Reranker/Vision profiles; all other `/admin/*` routes still require a system administrator. `/workspace/mcp` renders the backend definitions shared with the SDK server: tool names, read-only annotations, required scopes, resource URIs/templates, and current transport composition. The Mac API now reports Streamable HTTP as `mounted` at `http://127.0.0.1:8000/mcp`; compositions without an injected HTTP factory still report that external composition is required. The page never exposes tokens, prompts, authorization headers, or document content. Regenerate the committed OpenAPI types with:

```bash
pnpm --dir=frontend generate:api
```

The first administrator login uses `ADMIN_BOOTSTRAP_EMAIL` and `ADMIN_BOOTSTRAP_PASSWORD`. Local development defaults to
account `admin` and password `admin`. A bootstrap account is created in PostgreSQL only when no system administrator exists,
and its password is stored with Argon2id. Remove the bootstrap password from the environment after creation. Production
rejects this weak pair and requires an explicit strong credential pair.

PostgreSQL is required by migrations, anonymous sessions, collection/document APIs, query budgets, and integration tests. Without a configured database, object directory, or session secret, the static OpenAPI contract remains available while business routes return a stable 503. M4 now provides composable QueryRunner, synchronous/SSE, retrieval, and Agentic RAG contracts and services. The current `enterprise_rag.main:app` does not yet inject a concrete QueryRunner, so query endpoints return a stable 503 until later milestones compose production Providers and process entry points.

Milvus Lite is embedded through PyMilvus and needs no separate service. Contract tests create isolated temporary `.db` files; runtime data belongs under ignored `data/runtime/`, never in Git. A single Milvus Lite file must only be opened by one application process.

The local object store also needs no separate service. Its default directory is `data/runtime/object-store`; content is streamed into a private temporary file and becomes visible only after its size and SHA-256 checks pass. User filenames are metadata only and never become filesystem paths.

## Configure the backend

Settings use this deterministic priority, from lowest to highest:

```text
code defaults < YAML file < environment variables < explicit test/bootstrap overrides
```

The default development configuration starts without a file or secrets. To load the checked example YAML:

```bash
ENTERPRISE_RAG_CONFIG_FILE=config/development.example.yaml \
  uv run --project backend uvicorn enterprise_rag.main:app --reload
```

`.env.example` lists every supported deployment variable with deliberately unusable values. Copy it only for local editing, keep the resulting `.env` untracked, and load it explicitly:

```bash
cp .env.example .env
uv run --project backend uvicorn enterprise_rag.main:app --reload --env-file .env
```

Flat deployment variables such as `DATABASE_URL`, `SESSION_SECRET`, `METRICS_TOKEN`, and `LLM_API_KEY` are supported. Any regular setting can also be overridden with a nested name such as `ENTERPRISE_RAG__DEEP__LOW_THRESHOLD=0.50`.

Production startup fails before serving traffic when required credentials are missing, thresholds are invalid, YAML is malformed, or a configured Provider name is unknown. Secret values use masked types and are never included in validation error details.

## Run the local quality gates

Backend:

```bash
docker compose -f infra/compose/compose.dev.yml up -d postgres
export TEST_DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test
(cd backend && uv run --no-env-file ruff check src tests migrations)
(cd backend && uv run --no-env-file mypy src tests migrations)
(cd backend && uv run --no-env-file pytest -q)
uv build --project backend
```

`--no-env-file` is important because `uv run` reads the repository-root `.env` by default. Quality
gates must prevent local LLM credentials and development database settings from changing the
semantics of unconfigured-application tests. PostgreSQL integration tests receive their test
connection only through an explicit `TEST_DATABASE_URL`; when both `DATABASE_URL` and
`TEST_DATABASE_URL` are visible to Alembic, migrations always prefer the latter so a baseline
downgrade cannot touch the development database. Repository-relative YAML paths resolve from
both the repository root and the `backend/` working directory.

Frontend:

```bash
pnpm --dir=frontend test
pnpm --dir=frontend check:api
pnpm --dir=frontend typecheck
pnpm --dir=frontend build
```

Full browser acceptance (the first run downloads the pinned Playwright image):

```bash
docker compose -p enterprise-rag-browser-e2e -f infra/compose/compose.e2e.yml \
  up --build --abort-on-container-exit --exit-code-from e2e
docker compose -p enterprise-rag-browser-e2e -f infra/compose/compose.e2e.yml \
  down --volumes --remove-orphans
```

The same commands run on every GitHub pull request. `backend-quality`, `frontend-quality`, and
`browser-e2e` must pass before merge.

## Contribution workflow

1. Create one branch for one acceptance slice.
2. Add or update the acceptance test, implementation, and necessary documentation together.
3. Review and update both README language versions on every PR so startup commands and milestone status remain accurate.
4. Push the branch and open a PR using the repository template.
5. Merge with Squash Merge only after all required checks pass.
6. Delete the merged branch and start the next slice from the latest `main`.

Direct pushes and force pushes to `main` are prohibited by branch protection.

## Current milestone

- M1-01 detailed developer specification: complete
- M1-02 runnable Monorepo skeleton: complete
- M1-03 CI and protected-main workflow: complete
- M1-04 validated settings and secret loading: complete
- M1-05 common plugin contract and registry: complete
- M1-06 immutable domain types and unified errors: complete
- M2-01 PostgreSQL schema, Alembic, and async repository baseline: complete
- M2-02 concurrency-safe ingestion job state machine: complete
- M2-03 Milvus Lite vector-store adapter: complete
- M2-04 crash-safe local object store: complete
- M2-05 document registration and concurrency-safe deduplication: complete
- M2-06 asynchronous deletion Saga and cross-store reconcile: complete
- M2 storage and lifecycle milestone: complete
- M3-01 PDF and OCR loader: complete
- M3-02 DOCX/HTML/TXT/Markdown loaders: complete
- M3-03 XLSX/XLS/CSV loaders: complete
- M3-04 deterministic Cleaner and audit report: complete
- M3-05 structure-aware Root/Leaf Splitter: complete
- M3-06 image storage, Vision port, and caption degradation: complete
- M3-07 local multilingual and OpenAI-compatible Embedding Providers: complete
- M3-08 sparse encoding and compensating projection: complete
- M3-09 recoverable ingestion Pipeline: complete
- M3-10 anonymous workspace and document API: complete
- M3 multi-format ingestion milestone: complete
- M4-01 dual Dense/Sparse Search: complete
- M4-02 multi-path/multi-query RRF: complete
- M4-03 local/HTTP/Noop Reranker with safe degradation: complete
- M4-04 Scope/Root authorization filtering and recovery: complete
- M4-05 structured QueryPlan with deterministic fallback: complete
- M4-06 Standard explicit state graph: complete
- M4-07 Deep Evidence Ledger and Recovery: complete
- M4-08 Answer Verify/Repair/Abstain: complete
- M4-09 Query REST/SSE API: complete
- M4-10 PostgreSQL Cost Guard and Provider timeout/retry: complete
- M4 Hybrid Retrieval and Agentic RAG milestone: complete
- M5-01 MCP Application Layer: complete
- M5-02 stdio MCP: complete
- M5-03 HTTP MCP: complete
- M5-04 Trace/Logging: complete
- M5-05 Trace Persistence: complete
- M5-06 Metrics/Health: complete
- M5 MCP and end-to-end observability milestone: complete
- M6-01 Evaluator Contracts: complete
- M6-02 Golden Set: complete
- M6-03 Eval Runner: complete
- M6-04 LLM Judge: complete
- M6-05 CI Quality Gate: complete
- M6-06 Public Benchmark Adapter: complete
- M6 evaluation loop and public Benchmark Adapter milestone: complete
- M7-01 Shell/Auth: complete
- M7-02 Public Chat: complete
- M7-03 Overview: complete
- M7-04 Documents/Ingestion: complete
- M7-05 Query Trace: complete
- M7-06 Ingestion Trace: complete
- M7-07 Evaluation UI: complete
- M7-08 Browser E2E: complete
- M7 Vue3/TypeScript public and administration milestone: complete
- M7-R1 macOS real-provider development composition: complete
- M7-R2A document pipeline inspector: complete
- M7-R2B query planning and stage-level retrieval metrics: complete
- M7-R2C explicitly triggered one-pass LLM cleaning: complete
- M7-R3 development/test database isolation, vector repair tool, and LLM Query Planner: complete
- M7-R4 real Deep Recovery and citation verification/repair: complete
- M7-R5 Provider-switch index compatibility and safe rebuild: complete
- M7-R6 MCP capability catalog and transport-state UI: complete
- M7-R7 Provider rebuild consistency and failure proof: complete
- M7-R11 real OpenAI-compatible Vision Provider and catalog selection: complete
- M7-R8 Mac Streamable HTTP MCP real composition: complete
- M7-R9 native Milvus BM25 Sparse: complete
- M7-R10 Provider failure paths and projection integrity: complete
- M7-R12 revision-aware Milvus reconcile: complete
- M7-R13 abstention diagnosis and evidence-budget fixes: complete
- M7-R14 partial-answer status and abstention-rate semantics: complete
- M7-R15 protected image preview in the Pipeline Inspector: complete
- M7-R16 Provider selection dimension state consistency: complete
- M7-R17 unified sub-query and requirement semantics across query graphs: complete
- M7-R18 retrieval-switch execution boundary regression: complete
- M7-R19 Sparse Provider current-state projection fix: complete
- M8-00 standalone ingestion Worker prerequisite: complete
- M8-01 production images: complete
- Production API composition root (M8-02 prerequisite): complete
- M8-02 production Compose/Caddy: complete (not publicly released)
- M8-03 GHCR immutable images: complete
- M8-04 Deploy/Rollback: implemented, awaiting the pre-production host drill
- M8-05 Backup/Restore: implemented, awaiting an isolated non-live restore drill
- Next: M8-06 public release

The query application layer now exposes synchronous REST and streaming SSE APIs over one shared `QueryRunner` contract. Anonymous sessions may run Standard or Deep queries, while tenant and actor identities remain server-bound. SSE uses a stable accepted/progress/heartbeat/completed/error protocol; disconnects cancel execution, errors are sanitized, and an unconfigured runner returns 503 before stream headers are sent.

The `/chat` public page now consumes that SSE contract with Standard/Deep selection, Collection scope,
public stage status, expandable citations, explicit cancellation, 429 `Retry-After`, and bounded abstention.
A disconnect retains the Query ID and never starts an infinite reconnect loop; internal diagnostics and hidden
reasoning stay out of the public UI. The default `enterprise_rag.main:app` still has no production QueryRunner
composition. M7-08 provides deterministic offline acceptance through `enterprise_rag.local_runtime:app`, while
M7-R1 composes local semantic embeddings, a multilingual reranker, and an OpenAI-compatible LLM through
`enterprise_rag.mac_runtime:app` for a complete Mac development demo. Neither is the M8 production entry point.

`/workspace/overview` uses a tenant-scoped aggregate endpoint for Collections, document states, Roots/Leaves,
24-hour query count/P95/error rate, and recent ingestion/evaluation activity, alongside six doctor-backed
Provider capability slots. Empty workspaces, unregistered Providers, and null metrics retain their real
semantics. Loading, empty, degraded, and error/retry each have explicit UI states, errors may show a Request ID,
and anonymous users cannot see VPS resources or cross-tenant operations.

`/workspace/documents` now supports Collection create/edit/name-confirmed deletion, seed protection, cursor-filtered
Documents, multipart upload, detail, and idempotent deletion. `/workspace/ingestion` exposes status filters,
stage, progress, attempts, heartbeat, and stable errors, polling only while an active job exists. Anonymous
`demo_operator` users can complete this single-tenant business journey, while system routes and cross-tenant
resources remain denied at both frontend and backend boundaries. The offline Compose composition includes a
real ingestion Worker; the default composition root still requires deployments to provide a Worker process.

`/workspace/documents/{document_id}/pipeline` is a PostgreSQL source-of-truth document processing inspector. It
shows the Parser, Cleaner, Splitter, and settings actually used for the version, then exposes paged Roots and
on-demand detail with raw/clean text, deterministic rule counts and before/after hashes, Leaf text, enriched
retrieval text, tokens, offsets, and boundaries. New ingestion uses disjoint Leaves while Root recovery restores
the full context after retrieval. Both endpoints and the page are session-tenant scoped;
missing legacy metadata is shown as unavailable and is never replaced with an invented default.

`/workspace/traces/queries` shows persisted Query Traces for the current tenant with Standard/Deep, outcome,
and degradation filters. A sanitized backend projection shows the original/rewritten query, intent and
sub-queries; per-branch Dense/Sparse requested/returned counts and overlap; RRF inputs, deduplication, Root quota,
and Top-K drops; authorization filtering; reranking; Root recovery; LLM usage; the latency waterfall;
Dense/Sparse→RRF→Rerank movement; Deep Recovery rounds; and stable degraded components. Multi-branch candidates
use the best rank per method. Missing legacy telemetry remains absent instead of being inferred. Query text is
returned only by this tenant-scoped projection; generic logs, Prometheus, prompts, evidence text, exception stacks,
and hidden reasoning still exclude it. Per-run signals must not be read as Recall@K/MRR/NDCG; those require a
gold-labelled Evaluation Run.

`/workspace/traces/ingestion` shows persisted Ingestion Traces for the current tenant with succeeded, failed,
retry-wait, and cancelled filters. A sanitized backend projection drives the actual Worker stage waterfall,
Root/Leaf and projection-verification counts, plus real staging/activation VectorStore batches. Failed jobs expose
only a stable error code and a link back to the matching job. File content, object paths, exception messages/stacks,
and lease owners never reach the API or page, while legacy traces without batch spans retain an honest empty state.

`/workspace/evaluations` provides a server-owned catalog, preflight budget, per-case progress, persisted history,
complete reports, and controlled comparisons. The current public profile uses 30 versioned Golden Cases, the real
local Hashing Sparse Encoder, and deterministic metrics. It estimates zero LLM calls and is explicitly identified
as a smoke evaluation rather than an online generation-quality claim. Each tenant may have one active Run. Only
successful Runs with complete and identical Dataset, Mode, Case-set, Index, Prompt, and Provider snapshots receive
Candidate − Base deltas; every other comparison shows the backend's specific incompatibility reasons. Reports can
be exported as JSON or Markdown.

The Cost Guard atomically reserves a per-minute query slot and worst-case call/token capacity with PostgreSQL conditional upserts before QueryRunner can enter Provider logic. Minute limits are isolated per anonymous session, UTC daily capacity is shared by all anonymous sessions, and Standard/Deep use different weights. Successful calls refund unused capacity from trustworthy usage; failures or unverifiable usage conservatively consume the reservation, and 429 responses include `Retry-After`. The LLM decorator adds configurable per-attempt timeout, bounded transient-only retries, and a retry count. `cost_guard.answer_max_output_tokens` separately controls the `max_tokens` sent for each Answer Author/schema-regeneration/Repair request. Reasoning models such as MiniMax-M3 count hidden reasoning inside completion, so the macOS example defaults to 6000 to avoid truncating the structured JSON before the answer; it does not bypass citation verification or expand the evidence scope. Apply the new tables first with the `alembic upgrade head` command above.

`KnowledgeApplication` is now the only query use-case boundary for HTTP, MCP, and the later CLI. The official MCP SDK v2 stdio adapter exposes six read-only tools plus collection/document/section resources, with every identity bound by the server process. Tools return both human-readable and structured content while sanitizing errors. The entry point reserves stdout for JSON-RPC, and a real SDK-client subprocess test covers list/call/read plus buffered-output isolation.

The Streamable HTTP adapter requires bearer authentication at `/mcp` and checks its connection scope before protocol dispatch. Raw tokens never reach the database; immutable authentication claims establish request identity, and tool scopes plus collection allowlists can only narrow access. Public composition rejects HTTP and validates Host/Origin. Token administration remains a later system-admin milestone and is not part of anonymous demo business permissions.

The observability baseline combines task-local correlation context, JSON Lines logs, and OpenTelemetry spans. HTTP upstream trace context propagates through Query and RAG stages without leaking tenant/query/job context across async tasks. Standard and Ingestion major stages have dedicated child spans. Telemetry uses field allow-lists, rejects sensitive attributes, and does not automatically attach exception messages to spans.

Traces now persist by tenant in `trace_runs`/`trace_spans`; synchronous Query, SSE Query, and Ingestion perform idempotent upserts after their root span ends. Bounded events preserve Dense/Sparse, RRF, and Rerank Leaf/Root ranks and scores, while the degradation summary is derived only from stable `*_degraded` fields. Anonymous users can read all traces in the demo tenant, and a cross-tenant trace ID always returns 404. Run `alembic upgrade head` as shown above after creating or updating an environment.

Prometheus metrics use an application-local Registry and cover HTTP, Query, Retrieval/Recovery, Provider/Token, Ingestion, Milvus, Evaluation, and Rate Limit activity. HTTP labels use route templates only, never raw paths or tenant/user/document/query IDs. `live` has no external dependency; `ready` returns 503 when required configuration, PostgreSQL, or a Provider is unavailable; `doctor` returns only stable status codes and public Provider metadata.

The evaluation domain now provides immutable Case, runtime-observation, metric-result, and replaceable Evaluator contracts that are independent of transport, persistence, and retrieval implementations. The built-in deterministic evaluator calculates Document/Root Recall@5, Root MRR@10, citation coverage, strict source-text citation validity, and abstention accuracy, with stable `null`/`0` semantics for missing gold, uncited answers, and correct abstentions. Evaluators also declare supported metrics and estimated LLM cost so the later Runner can enforce its budget before execution.

The first Golden Set lives in `evals/golden/v1`: 30 rewritten Cases are split evenly between Chinese and English and exactly cover keyword, paraphrase, comparison, metadata-scope, table, OCR, and unanswerable categories. A versioned manifest, JSON Schema, and strict loader validate unknown fields, schema revision, duplicate or dangling IDs, collection scope, verbatim expected facts, and category/language counts so evaluation-input drift cannot pass silently.

The Eval Runner estimates worst-case LLM calls from the selected Case count and component capability declarations before execution, rejecting an over-budget run with zero Provider calls. Configuration, dataset revision, commit, and Subject/Evaluator versions form stable hashes, and only complete successful results are cached by request hash. This zero-cost command produces a Runner-mechanics acceptance report. Its output is explicitly labeled `golden-fixture-oracle` and must not be presented as product retrieval accuracy:

```bash
cd backend
uv run enterprise-rag-eval \
  --manifest ../evals/golden/v1/manifest.yaml \
  --report ../artifacts/evals/local-smoke.json \
  --commit-sha "$(git rev-parse HEAD)"
```

The LLM Judge is an optional adapter that is disabled by default. It calculates faithfulness and relevancy separately without replacing deterministic metrics. When enabled, its worst-case calls enter the Eval Runner budget; without a credential it remains unavailable and deterministic evaluation still runs in full. Judge output must be a strict two-field JSON score object; malformed output fails that evaluation instead of becoming a fabricated low score. Example development configuration:

```yaml
evaluation:
  max_cases: 30
  max_llm_calls: 30
  llm_judge_enabled: true
  llm_judge_max_output_tokens: 128
```

Required CI now runs all 30 Golden Cases through the real `HashingSparseEncoder` at zero external cost and checks both absolute thresholds and the maximum 0.02 regression from its versioned baseline. It runs inside the required `backend-quality` check, so failure blocks a main merge. Run the same gate locally with:

```bash
cd backend
uv run enterprise-rag-quality-gate \
  --manifest ../evals/golden/v1/manifest.yaml \
  --policy ../evals/quality-gate-v1.yaml \
  --report ../artifacts/evals/ci-smoke.json \
  --commit-sha "$(git rev-parse HEAD)"
```

The MultiDoc2Dial Adapter is isolated from the product Domain and ingestion path. The official download checks its HTTPS host, 8 MB bound, and pinned SHA-256; archives and outputs stay under the Git-ignored `artifacts/` directory. Run a sample first, review the dataset terms, and only then run full conversion:

```bash
cd backend
uv run enterprise-rag-benchmark download \
  --output ../artifacts/benchmarks/multidoc2dial.zip

uv run enterprise-rag-benchmark convert \
  --archive ../artifacts/benchmarks/multidoc2dial.zip \
  --mode sample --max-cases 100 \
  --commit-sha "$(git rev-parse HEAD)" \
  --checkpoint ../artifacts/benchmarks/sample.checkpoint.json \
  --output ../artifacts/benchmarks/sample.json \
  --report ../artifacts/benchmarks/sample-report.json
```

For full conversion, replace `--mode sample --max-cases 100` with `--mode full` and use separate checkpoint/output/report paths. Only a complete full run sets `is_full_dataset=true`. Conversion reports contain no model or index results and therefore are not product Benchmark scores.

Local development may scrape `http://127.0.0.1:8000/metrics` directly. Production must configure `METRICS_TOKEN` and send it when scraping:

```bash
curl -H "Authorization: Bearer $METRICS_TOKEN" https://your-host.example/metrics
```

The PostgreSQL job repository owns enqueue, exclusive lease, start, heartbeat, retry, cancel, success, and expired-lease recovery transitions. Workers identify themselves with an owner string and renew a time-limited lease; stale or wrong-owner updates are rejected. Progress is monotonic, retries stop at `max_attempts`, and concurrent workers use `FOR UPDATE SKIP LOCKED` so only one can claim a job. During long Loader, OCR, remote-cleaning, Embedding, and projection Provider calls, the local Pipeline renews the lease in a background task, so a slow Provider cannot leave a job stranded at an intermediate progress value; heartbeat failures retry and the foreground checkpoint remains the final state boundary.

The VectorStore port requires an index revision on every record and search. Milvus collections are isolated by revision so embedding dimensions cannot be mixed. Every search expression injects `tenant_id` and `status == "ready"`; optional collection and document scopes only narrow that mandatory filter. Dense and sparse vectors, scalar filtering, idempotent upsert, count, version deletion, persistence, and close behavior run against real Milvus Lite files in contract tests.

The ObjectStore port accepts an asynchronous byte stream and publishes immutable objects under canonical SHA-256 keys. The local adapter bounds optional upload size, verifies an optional caller digest, fsyncs complete content, and atomically publishes without replacing an existing object. Interrupted and rejected uploads remove their `.part` files; traversal, absolute, malformed, mismatched-prefix, and symlink-escape keys are rejected before filesystem access.

Document registration streams bytes to ObjectStore before opening its PostgreSQL unit of work. The deduplication identity is `(tenant_id, collection_id, sha256)`: repeats return the original document/version, while another collection or tenant gets independent logical ownership and can safely reuse the immutable physical object. A new hash under the same logical name creates a new version. PostgreSQL transaction advisory locks serialize both content and logical-name races, with primary/unique constraints as integrity backstops. If the existing version for the same digest is `failed`, a duplicate upload keeps the original version/object ownership, clears the failure, and creates a new queued ingestion job; succeeded or active jobs remain idempotently reused. `POST /api/v1/documents` now exposes this capability and creates or reuses its ingestion job in the same transaction.

Application errors keep their explicit details deeply immutable, but the exception object itself is not frozen because Python must attach traceback state while errors cross asynchronous transaction context managers.

Deletion requests immediately move a tenant-owned document out of `ready`, clear its active version, cancel ingestion work, and enqueue one reusable delete job. The worker runs an idempotent Saga across Milvus, PostgreSQL content, and unreferenced object files before persisting document/version tombstones and completing the job. Shared content-addressed files remain until no non-deleted version references them.

Reconcile compares Milvus `(tenant_id, version_id, index_revision)` projections and local object keys with the PostgreSQL fact source and also finds expired worker leases. New projection rows persist the revision in hidden diagnostic metadata, preventing old and new collections for one version from being aggregated together. Its default mode is read-only. Apply mode removes only proven orphan or stale-revision projections and files and recovers leases; a PostgreSQL-missing version is the only case that uses version-scoped deletion. Legacy projections without a marker, missing files, and vector count mismatches remain explicit unresolved findings because ownership or reconstruction cannot be proven safely. `scripts/mac-reconcile-vectors.py` is the local entry point; HTTP/MCP continue to reuse the same Application Service rather than duplicating business logic.

M7-R7 through M7-R12 now cover Provider rebuild consistency, real Mac Streamable HTTP MCP, native Milvus BM25, remote-Provider failure paths, a real OpenAI-compatible Vision adapter, and revision-aware reconcile. A restart does not automatically remove a persisted Milvus collection; when PostgreSQL and Milvus cross-store facts diverge, stop the API first and use tenant/version/revision-scoped reconcile or safe rebuild. Never delete the entire Milvus file.

M1 through M7 are complete (52/64 slices). The repository now provides the tested engineering foundation, complete multi-format ingestion, anonymous demo-tenant collection/document APIs, Hybrid Retrieval/Agentic RAG services, MCP, Trace/Metrics/Health, the EDD evaluation loop, a public Benchmark Adapter, the complete Vue3/TypeScript workspace, and a reproducible Compose browser journey. M7-R1 also provides a real-provider Mac development composition outside the 64 release slices. Production images, processes, and public deployment remain M8 work, so neither the offline acceptance nor Mac development composition is presented as production.

The PDF Loader streams input through a temporary file, extracts each page's text first, and invokes Tesseract `chi_sim+eng` OCR when content falls below `pdf_ocr_min_chars`. Its output preserves one-based page numbers, extraction mode, and each embedded image's media type, dimensions, content hash, and bytes for image enrichment. Blank pages do not create empty Roots; entirely empty, encrypted, corrupt, type-mismatched, and missing-language inputs produce stable errors, and all success/failure paths remove temporary files. The Loader is wired into the background ingestion Pipeline; the HTTP upload endpoint arrives in M3-10.

The text-document Loader supports DOCX, HTML, TXT, and Markdown. DOCX headings become Section Roots, tables become normalized Markdown, and embedded-image bytes are retained. HTML scripts, styles, navigation, and active embedded objects are removed; body structure is converted to Markdown, while external image locations are recorded without network access. TXT and Markdown are accepted only as UTF-8. These Loaders are connected to the Cleaner, Splitter, and persistence pipeline.

The spreadsheet Loader parses XLSX, legacy XLS, and CSV independently. Each worksheet becomes header-bearing row blocks; continuation blocks repeat the header and preserve source row numbers. Empty outer rows and columns are trimmed while formula cache values and expressions remain traceable. CSV accepts UTF-8/UTF-8-SIG by default; a legacy encoding must be selected explicitly with `csv_fallback_encoding`. These Loaders are connected to the complete background pipeline.

The deterministic Cleaner preserves both raw and cleaned text and records each effective rule, occurrence count, and before/after content hash. It normalizes invisible controls, common OCR artifacts, and whitespace, and uses batch Root statistics to remove repeated headers and footers; fenced code is isolated so indentation, blank lines, and wrapped code content are not rewritten. Re-cleaning the same text makes no further changes, and no LLM rewrites document content by default. The manually triggered LLM pass is only a layout-repair supplement: it may fix PDF/OCR whitespace, paragraph line reflow, heading/table spacing, and line-break hyphenation, subject to backend lexical, order, fact, and code-fence validation.

The structure-aware Splitter uses a versioned paragraph- and sentence-aware strategy: within each Root it preserves headings, paragraphs, lists, code fences, table rows, and complete sentences before applying target/max limits. New macOS ingestion creates disjoint Leaves; Root recovery restores the complete context after retrieval. It falls back to a token hard cut only when one structural unit itself exceeds the budget, and records `boundary=token_limit_hard_cut` and `hard_cut=true` in Leaf metadata. The real macOS runtime reuses the FastEmbed tokenizer and model input limit; the effective safe budget is the smaller of the configured cap and `model_input_limit - 1`. Each Root/Leaf records the actual tokenizer, budget, boundary, and hard-cut count for inspection. Continuation table chunks repeat headers and count them toward the token cap; Root/Leaf IDs remain stable for the same version, index revision, content, and order.

Image enrichment writes the original image extracted by a Loader to the content-addressed ObjectStore before invoking the pluggable Vision port. The default `vision: none` keeps the image and skips captioning. `openai_compatible` sends text plus a `data:image/*;base64,...` payload to `/chat/completions`, requires exactly one non-empty string caption, and applies bounded retries for 429/5xx/transport failures. Root metadata records image dimensions, MIME, hash, object key, caption status, and sanitized error code so the Pipeline Inspector can show the actual result. A Vision failure degrades only the caption, without discarding the stored image or exposing provider errors. ObjectStore failure still aborts ingestion because image persistence is not optional data.

The Pipeline Inspector's `IMAGE ENRICHMENT` panel reads persisted facts from the selected Root. It shows image count, page/ordinal/name, MIME, dimensions, SHA-256, ObjectStore key, caption text, `created/skipped/degraded` status, the actual Vision Provider/model, and status counts. It also checks each caption against the saved Leaf `retrieval_text` and explicitly marks whether it entered retrieval. Images can now be genuinely previewed in the panel through `GET /api/v1/documents/{document_id}/images/{sha256}`: the server permits only the current tenant's `ready` document, active indexed version, and a SHA-256 recorded in that version's Root metadata; the object key is derived from and revalidated against the digest, and the response includes `ETag`, the digest, and `nosniff`. This is not a public static path. A preview failure leaves metadata/caption facts visible and never fabricates historical results from the current configuration.

The Embedding port has local multilingual and OpenAI-compatible implementations, with FastEmbed profiles selected by `EMBEDDING_MODEL`. The local default is `paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions, mean pooling, and a registry description reporting 512 input tokens), which downloads approximately 0.22GB on first use. The runtime does not blindly trust the registry: it first reads the actual truncation limit; if a FastEmbed ONNX wrapper hides that field, it probes `token_count` with text beyond the registry limit and detects the tokenizer's capped runtime limit (the current cached model reports 128). The optional Chinese-focused `BAAI/bge-small-zh-v1.5` profile is verified at 512 dimensions and 512 input tokens; set `ENTERPRISE_RAG__INGESTION__EMBEDDING_DIMENSION=512` when selecting it, and isolate the change in a new index revision. The Provider exposes the real tokenizer's `count_tokens` and limit, rejects an individual input at the model limit, and shares that counter with the Splitter. The remote adapter applies item/token batch limits plus bounded retries for timeouts, rate limits, and 5xx responses. The SiliconFlow `BAAI/bge-m3` profile validates 1,024-dimensional output and uses the provider's documented 8,192-token limit while explicitly labelling its local counter as a deterministic estimate because the HTTP API does not expose a tokenizer. Both validate count, order, dimension, and finite values and return L2-normalized vectors. Run the real-model check explicitly with:

```bash
(cd backend && RUN_MODEL_TESTS=1 uv run pytest -q \
  tests/contract/test_embedding_providers.py -m model)
```

The Sparse port has two explicit modes: offline/evaluation `hashing_lexical` uses stable multilingual lexical hashes,
log-TF weights, and L2 normalization to produce precomputed vectors; the complete Mac composition's
`milvus_builtin_bm25` passes text to Milvus's native BM25 Function and `jieba` Analyzer, which own corpus TF/IDF.
Neither mode is mislabeled as the other. `IndexSchema` includes the sparse mode in the revision, so an old Hashing
collection cannot be mixed with a BM25 collection. The Projection Service writes mode-specific Dense/Sparse records
in `processing` batches, verifies their count, activates them as `ready`, and verifies again. Repeated runs overwrite
the same Leaf IDs. A partial write or verification failure removes only the target `index_revision`, with bounded
delete retries, so a working old revision cannot be accidentally deleted. Document deletion and full reconciliation
still support version-wide cleanup.

The Provider Reindex Service treats the old PostgreSQL Root/Leaf rows and old Milvus projection as rollback facts:
it re-splits with the active Embedding tokenizer, projects the new revision, verifies both the ProjectionResult and an
independent `count_by_version_revision(tenant, version, target_revision)`, swaps Root/Leaf rows in one transaction,
and retires old revisions only afterward. A projection adapter that silently writes too few vectors only removes the
target revision; old PostgreSQL facts and real old-revision queries remain available. A database-swap failure follows
the same rule. The administration UI therefore distinguishes “Provider changed, index not rebuilt” from “the active
revision is fully searchable” instead of presenting incompatible vectors as valid. Remote Embedding and Reranker
transport failures use bounded retries and sanitized errors; invalid reranker identities fall back to a bounded RRF order.

The ingestion Pipeline creates or reuses its Job in the document-registration transaction, then executes Loader → image enrichment → Cleaner → Splitter → PostgreSQL → Milvus → final commit. Each checkpoint renews the lease, advances monotonic progress, and observes cancellation. Deterministic input errors fail immediately; transient failures retry up to the configured limit. Failure and cancellation compensate PostgreSQL content and Milvus projections for that version, and a document becomes `ready` only after both stores verify successfully. The service runs through `run_once(owner=...)`; the M7-08 offline composition includes a single-process polling Worker, and M8-00 now provides the standalone production Worker process and resource bounds.

The anonymous workspace API uses server-side sessions to bind every request to one fixed demo tenant. Anonymous `demo_operator` sessions can manage collections and documents inside that tenant but cannot access the system administration surface; writes require a rotating CSRF token. Administrators use separate database sessions, Argon2id passwords, and system roles; frontend guards improve UX while the backend still authorizes every system request. Collection CRUD, streaming upload, document cursor pagination, details, job lookup, and idempotent asynchronous deletion all use the unified error model and request IDs. Cross-tenant identifiers always appear as 404.

The dual Search Service creates Dense and Sparse inputs separately—Hashing returns a query vector while BM25 returns
query text—and runs two independent retrieval paths concurrently. Tenant and authorized collection/document scope
are included in both requests before the VectorStore call, where Milvus also forces `status=ready`; scope is never
applied after retrieval. Raw branch scores remain separate with minimal diagnostics, and Query Trace identifies the
actual Sparse algorithm for every branch.

RRF Fusion evaluates every query's Dense/Sparse ranked lists with `Σ 1/(k+rank)` and never adds incomparable raw scores. A single-query run keeps at most three Leaves per Root by default; only an explicitly enabled alternative-route run adapts that quota to `max(3, sub-query count)` so evidence from those routes is not discarded too early. The global default remains 30 candidates. Exact score ties use the Leaf ID for stable ordering, and diagnostics report the effective Root quota and every drop. This only widens the candidate funnel and preserves alternative evidence routes; it never creates requirements for sub-queries, requires every route to be covered, or relaxes tenant authorization, Root recovery, and citation verification.

The Reranker port provides local FastEmbed CrossEncoder, HTTP, and explicit Noop implementations. The default `local_cross_encoder` uses the approximately 0.08GB `Xenova/ms-marco-MiniLM-L-6-v2`; that default model is claimed only as English-capable. Chinese or multilingual deployments must explicitly select a suitable model; the 2GB production server can use SiliconFlow `BAAI/bge-reranker-v2-m3` through the official `/v1/rerank` contract. The service reranks the first 20 RRF candidates and selects eight by default with strict candidate-ID alignment. Selection then uses only reranker score and stable RRF order; it does not reserve a slot for each sub-query. `matched_queries` is provenance, not a coverage obligation. Timeouts, malformed responses, duplicate or unknown IDs, and non-finite scores produce sanitized diagnostics and a stable RRF fallback. Run the real local model check with:

```bash
(cd backend && RUN_MODEL_TESTS=1 uv run pytest -q \
  tests/contract/test_reranker_providers.py -m model)
```

The Scope/Root service resolves server-side authorization and user metadata constraints to an explicit set of currently ready PostgreSQL document IDs. Anonymous users retain full business access inside the demo tenant but cannot override the tenant in a request; restricted identities use the union of allowed Collections and Documents. Title, organization, media type, active-version UUID, and section are checked against the fact source, while contradictory explicit constraints return `QUERY_SCOPE_CONFLICT` without disclosing resource existence. Recalled Leaves are rechecked before reranking, and selected Roots are rechecked again before entering context, joining tenant, active Collection, ready Document, and indexed active Version. Stale vectors, deleting content, and unauthorized records are therefore discarded. The default 18,000-character Root-recovery limit is used for context-budget accounting and deterministic truncation diagnostics; `RootContext` still retains complete clean text for citation-quote verification, while Answer/Assessor receive only authorized, reranked Leaf source fragments so a valid quote in the latter half of a Root is not falsely rejected.

The Query Planning Service treats structured Planner output as untrusted input and strictly validates fields, intent, the `use_sub_queries` switch, sub-query limits, requirement limits, UUIDs, and scope narrowing; the service has a hard cap of four sub-queries that configuration cannot expand. A model cannot change Standard/Deep mode, invent Collection or Document IDs, or replace explicit caller metadata. The Mac composition uses the current OpenAI-compatible LLM for rewriting; the default is one retrieval route, and only an explicit `use_sub_queries=true` with two to four alternative routes for the same original question activates parallel retrieval. The separate aspects of a comparison, multi-part, or multi-hop question are not alternative routes. With `false`, the service forces the single rewritten query even if the model accidentally fills extra routes. Planner calls/tokens are recorded separately. Regardless of factual, comparison, multi-part, procedural, or summary intent, requirements are deterministically normalized to exactly one value: the original user question. Sub-queries are alternative retrieval routes for that requirement, not separate coverage obligations; evidence from any one route can be sufficient. Any malformed response or Provider failure falls back as one unit to a deterministic single-route plan that preserves the original scope and uses the latest user turn to resolve pronouns. Provider exception text never enters the QueryPlan.
The runners also re-apply this switch through one shared active-route policy: `use_sub_queries=false` always executes only the rewritten query, while `true` is the only state that can start two to four alternative routes. A custom Planner that carries extra routes while disabled therefore cannot accidentally trigger parallel execution.

The Standard Query Graph is an explicit state machine connecting Plan → Search → RRF → PostgreSQL Authorize → Rerank → Root Recover → Answer. Every run returns its actual transitions. Empty RRF output, authorized Leaves, or rechecked Roots terminate as NoResults without invoking the answer model. Standard counts the Planner attempt as LLM call one and final answer generation as call two, with a runtime hard ceiling; Planner degradation adds no call. Unclassified failures terminate as Failed with a sanitized error code and no exception text exposed to clients.

Standard and Semantic query graphs share `canonicalize_plan()`: before retrieval, both rebind `original_query` and the single
requirement to the question actually received by the server. An injected Planner therefore cannot forge the answer obligation;
rewrite text, Scope, the sub-query switch, RRF, authorization, reranking, and Citation Verify remain unchanged.

Deep Recovery uses an Evidence Ledger deduplicated by Leaf ID across rounds and reserves final slots for new Recovery evidence. The reusable controller defaults to 0.45/0.80 thresholds for direct recovery, assessor use, or direct answer. The real Mac composition enables the stricter `always_assess` policy: every evidence-bearing Deep decision calls the current LLM to judge coverage of the one original-question requirement, conflicts, and a decision, while the score remains derived from coverage and retrieval confidence. Multiple sub-queries are alternative evidence routes, not separate requirements; one route with sufficient evidence can cover the requirement even when other routes return nothing. An assessor response with unknown requirements, a non-exhaustive coverage partition, or `answer` despite a gap is treated as untrusted and falls back to deterministic coverage of the original requirement; it cannot turn routes into new answer obligations or crash the query. Recovery is capped at two rounds before Abstain. Its four routes are Rewrite Hybrid, HyDE Dense-only, Exact-term Sparse-only, and Scope repair that removes only a Planner-added field absent from explicit caller scope. Every route repeats retrieval, RRF, authorization, reranking, and Root restoration. The Mac exact-term route uses the real Milvus BM25 Provider; offline evaluation deliberately remains on Hashing Lexical for deterministic zero-cost scores. Neither code nor documentation treats the two modes as interchangeable.
The bounded evidence window sent to the Assessor is confidence-first with stable original-order tie-breaking. This keeps a reliable alternative route from being truncated; it does not reserve a coverage slot for every sub-query.

Answer Verification requires every factual paragraph to bind citations. A cited Root must come from the current authorized context, each Leaf must belong to that Root, and every quote must be a real contiguous substring of Root clean text, while the single original-question QueryPlan requirement must be covered. Structural or coverage errors get at most one Repair using exactly the same evidence and are then fully revalidated. Evidence conflicts are not hidden by rewriting and instead cause immediate Abstain. Fully covered answers produce domain Citations carrying document, Root and Leaf IDs, page or section, quote, and score; if the original requirement remains missing, the status stays `abstained`, but independently verified paragraphs and citations may be returned as a bounded partial result with explicit gaps and no leaked provider error.
When a Provider copies a citation with Chinese/typographic quotation marks or line whitespace changed to ASCII quotes or ordinary spaces, verification permits only this narrow equivalent normalization. The returned quote is always re-sliced from the original contiguous Root substring, so normalization cannot authorize text that is absent from the source.

All future adapters implement the common `Provider` lifecycle contract and are owned by one application-scoped registry. Provider keys are `(kind, name)`; duplicate registration, unknown names, missing capabilities, and resource-close failures produce stable sanitized errors.

Root and Leaf IDs are derived from immutable identity fields and content hashes. Reprocessing the same version with the same index revision produces the same IDs; changing content, ordinal, kind, or index revision produces different IDs. Domain timestamps must be timezone-aware UTC, metadata is copied into deeply immutable structures, and `to_dict()` outputs JSON-compatible API values.

M1 was delivered through independently checked pull requests: [specification #1](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/1), [anonymous demo boundary #2](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/2), [Monorepo #3](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/3), [CI and branch protection #4](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/4), [settings #5](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/5), [plugin registry #6](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/6), and [domain types #7](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/7).
