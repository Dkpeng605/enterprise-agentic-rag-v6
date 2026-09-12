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
export DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test
export SESSION_SECRET=development-only-change-me-32-bytes-minimum
uv run --project backend alembic -c backend/alembic.ini upgrade head
```

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
- `/api/v1/collections` — demo-tenant collection CRUD
- `/api/v1/documents` — streaming upload, filtering, and cursor pagination
- `/api/v1/documents/{id}` and `/api/v1/ingestion-jobs/{id}` — document and ingestion status
- `POST /api/v1/queries` and `POST /api/v1/queries/stream` — synchronous and SSE query contracts; the current entry point returns 503 until a QueryRunner is injected

Before a write, call `GET /api/v1/auth/me`, retain its Cookie, and send the returned `csrf_token` in the `X-CSRF-Token` header. Development HTTP cookies omit Secure; production or an HTTPS base URL always enables Secure.

Start the frontend in a second terminal:

```bash
pnpm --dir=frontend dev
```

The Vite development server prints its local URL. The current page confirms that the Vue 3 and TypeScript application mounted successfully.

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

Flat deployment variables such as `DATABASE_URL`, `SESSION_SECRET`, and `LLM_API_KEY` are supported. Any regular setting can also be overridden with a nested name such as `ENTERPRISE_RAG__DEEP__LOW_THRESHOLD=0.50`.

Production startup fails before serving traffic when required credentials are missing, thresholds are invalid, YAML is malformed, or a configured Provider name is unknown. Secret values use masked types and are never included in validation error details.

## Run the local quality gates

Backend:

```bash
docker compose -f infra/compose/compose.dev.yml up -d postgres
export TEST_DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test
(cd backend && uv run ruff check src tests migrations)
(cd backend && uv run mypy src tests migrations)
(cd backend && uv run pytest -q)
uv build --project backend
```

Frontend:

```bash
pnpm --dir=frontend test
pnpm --dir=frontend typecheck
pnpm --dir=frontend build
```

The same commands run on every GitHub pull request. Both `backend-quality` and `frontend-quality` must pass before merge.

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
- Next: M5-02 stdio MCP

The query application layer now exposes synchronous REST and streaming SSE APIs over one shared `QueryRunner` contract. Anonymous sessions may run Standard or Deep queries, while tenant and actor identities remain server-bound. SSE uses a stable accepted/progress/heartbeat/completed/error protocol; disconnects cancel execution, errors are sanitized, and an unconfigured runner returns 503 before stream headers are sent.

The Cost Guard atomically reserves a per-minute query slot and worst-case call/token capacity with PostgreSQL conditional upserts before QueryRunner can enter Provider logic. Minute limits are isolated per anonymous session, UTC daily capacity is shared by all anonymous sessions, and Standard/Deep use different weights. Successful calls refund unused capacity from trustworthy usage; failures or unverifiable usage conservatively consume the reservation, and 429 responses include `Retry-After`. The LLM decorator adds configurable per-attempt timeout, bounded transient-only retries, and a retry count. Apply the new tables first with the `alembic upgrade head` command above.

`KnowledgeApplication` is now the only query use-case boundary for HTTP, MCP, and the later CLI. It centralizes server-side identity binding, query IDs, synchronous execution, and SSE streaming. HTTP routes no longer construct QueryCommand themselves, while a transport-neutral MCP facade delegates to the exact same service and produces an equivalent QueryExecution for the same input. The actual stdio protocol process arrives in M5-02.

The PostgreSQL job repository owns enqueue, exclusive lease, start, heartbeat, retry, cancel, success, and expired-lease recovery transitions. Workers identify themselves with an owner string and renew a time-limited lease; stale or wrong-owner updates are rejected. Progress is monotonic, retries stop at `max_attempts`, and concurrent workers use `FOR UPDATE SKIP LOCKED` so only one can claim a job.

The VectorStore port requires an index revision on every record and search. Milvus collections are isolated by revision so embedding dimensions cannot be mixed. Every search expression injects `tenant_id` and `status == "ready"`; optional collection and document scopes only narrow that mandatory filter. Dense and sparse vectors, scalar filtering, idempotent upsert, count, version deletion, persistence, and close behavior run against real Milvus Lite files in contract tests.

The ObjectStore port accepts an asynchronous byte stream and publishes immutable objects under canonical SHA-256 keys. The local adapter bounds optional upload size, verifies an optional caller digest, fsyncs complete content, and atomically publishes without replacing an existing object. Interrupted and rejected uploads remove their `.part` files; traversal, absolute, malformed, mismatched-prefix, and symlink-escape keys are rejected before filesystem access.

Document registration streams bytes to ObjectStore before opening its PostgreSQL unit of work. The deduplication identity is `(tenant_id, collection_id, sha256)`: repeats return the original document/version, while another collection or tenant gets independent logical ownership and can safely reuse the immutable physical object. A new hash under the same logical name creates a new version. PostgreSQL transaction advisory locks serialize both content and logical-name races, with primary/unique constraints as integrity backstops. `POST /api/v1/documents` now exposes this capability and creates or reuses its ingestion job in the same transaction.

Application errors keep their explicit details deeply immutable, but the exception object itself is not frozen because Python must attach traceback state while errors cross asynchronous transaction context managers.

Deletion requests immediately move a tenant-owned document out of `ready`, clear its active version, cancel ingestion work, and enqueue one reusable delete job. The worker runs an idempotent Saga across Milvus, PostgreSQL content, and unreferenced object files before persisting document/version tombstones and completing the job. Shared content-addressed files remain until no non-deleted version references them.

Reconcile compares Milvus version projections and local object keys with the PostgreSQL fact source and also finds expired worker leases. Its default mode is read-only. Apply mode removes only proven orphan vectors/files and recovers leases; missing files and vector count mismatches remain explicit unresolved findings because this storage slice does not yet have loaders or embeddings with which to reconstruct them. HTTP and CLI entry points for these application services are delivered by their later API/CLI slices.

M1, M2, M3, and M4 are complete. The repository now provides the tested engineering foundation, complete multi-format ingestion, anonymous demo-tenant collection/document APIs, Hybrid Retrieval/Agentic RAG services, synchronous/SSE query contracts, a PostgreSQL Cost Guard, lifecycle state, Milvus Lite projections, crash-safe local objects, idempotent deletion, and cross-store reconciliation. A concrete production QueryRunner/Provider composition entry point is not wired yet, so the default entry point does not pretend to be a usable complete query product.

The PDF Loader streams input through a temporary file, extracts each page's text first, and invokes Tesseract `chi_sim+eng` OCR when content falls below `pdf_ocr_min_chars`. Its output preserves one-based page numbers, extraction mode, and each embedded image's media type, dimensions, content hash, and bytes for image enrichment. Blank pages do not create empty Roots; entirely empty, encrypted, corrupt, type-mismatched, and missing-language inputs produce stable errors, and all success/failure paths remove temporary files. The Loader is wired into the background ingestion Pipeline; the HTTP upload endpoint arrives in M3-10.

The text-document Loader supports DOCX, HTML, TXT, and Markdown. DOCX headings become Section Roots, tables become normalized Markdown, and embedded-image bytes are retained. HTML scripts, styles, navigation, and active embedded objects are removed; body structure is converted to Markdown, while external image locations are recorded without network access. TXT and Markdown are accepted only as UTF-8. These Loaders are connected to the Cleaner, Splitter, and persistence pipeline.

The spreadsheet Loader parses XLSX, legacy XLS, and CSV independently. Each worksheet becomes header-bearing row blocks; continuation blocks repeat the header and preserve source row numbers. Empty outer rows and columns are trimmed while formula cache values and expressions remain traceable. CSV accepts UTF-8/UTF-8-SIG by default; a legacy encoding must be selected explicitly with `csv_fallback_encoding`. These Loaders are connected to the complete background pipeline.

The deterministic Cleaner preserves both raw and cleaned text and records each effective rule, occurrence count, and before/after content hash. It normalizes invisible controls, common OCR artifacts, and whitespace, and uses batch Root statistics to remove repeated headers and footers. Re-cleaning the same text makes no further changes, and no LLM rewrites document content.

The structure-aware Splitter uses a versioned deterministic multilingual tokenizer, prioritizes headings, paragraphs, lists, code fences, and table rows within each Root, and then enforces target/max/overlap limits. Continuation table chunks repeat headers and count them toward the token cap. Root/Leaf IDs remain stable for the same version, index revision, content, and order. This lightweight tokenizer is not represented as equivalent to any remote model tokenizer; replacing it requires a new index revision.

Image enrichment writes the original image extracted by a Loader to the content-addressed ObjectStore before invoking the pluggable Vision port. The default `vision: none` keeps the image and skips captioning. A Vision failure degrades only the caption, without discarding the stored image or exposing provider errors. ObjectStore failure still aborts ingestion because image persistence is not optional data.

The Embedding port has local multilingual and OpenAI-compatible implementations. The local default is FastEmbed ONNX `paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions, mean pooling), which downloads approximately 0.22GB on first use. The remote adapter applies item/token batch limits plus bounded retries for timeouts, rate limits, and 5xx responses. Both validate count, order, dimension, and finite values and return L2-normalized vectors. Run the real-model check explicitly with:

```bash
(cd backend && RUN_MODEL_TESTS=1 uv run pytest -q \
  tests/contract/test_embedding_providers.py -m model)
```

The Sparse Encoder produces Milvus sparse vectors with stable multilingual lexical hashes, log-TF weights, and L2 normalization; it is not represented as BM25. The Projection Service writes Dense/Sparse records in `processing` batches, verifies their count, activates them as `ready`, and verifies again. Repeated runs overwrite the same Leaf IDs. A partial write or verification failure removes every vector for the version with bounded delete retries.

The ingestion Pipeline creates or reuses its Job in the document-registration transaction, then executes Loader → image enrichment → Cleaner → Splitter → PostgreSQL → Milvus → final commit. Each checkpoint renews the lease, advances monotonic progress, and observes cancellation. Deterministic input errors fail immediately; transient failures retry up to the configured limit. Failure and cancellation compensate PostgreSQL content and Milvus projections for that version, and a document becomes `ready` only after both stores verify successfully. The service currently runs through `run_once(owner=...)`; the long-running Worker entry point is deferred to deployment work.

The anonymous workspace API uses server-side sessions to bind every request to one fixed demo tenant. Anonymous `demo_operator` sessions can manage collections and documents inside that tenant but cannot access the system administration surface; writes require a rotating CSRF token. Collection CRUD, streaming upload, document cursor pagination, details, job lookup, and idempotent asynchronous deletion all use the unified error model and request IDs. Cross-tenant identifiers always appear as 404.

The dual Search Service creates Dense and Sparse query vectors separately and runs two independent retrieval paths concurrently. Tenant and authorized collection/document scope are included in both requests before the VectorStore call, where Milvus also forces `status=ready`; scope is never applied after retrieval. Raw branch scores remain separate with minimal diagnostics, ready for M4-02 fusion.

RRF Fusion evaluates every query's Dense/Sparse ranked lists with `Σ 1/(k+rank)` and never adds incomparable raw scores. A Leaf is deduplicated across paths, each Root keeps at most three Leaves by default, and the global default is 30 candidates. Exact score ties use the Leaf ID for stable ordering, and diagnostics report every quota drop.

The Reranker port provides local FastEmbed CrossEncoder, HTTP, and explicit Noop implementations. The default `local_cross_encoder` uses the approximately 0.08GB `Xenova/ms-marco-MiniLM-L-6-v2`; that default model is claimed only as English-capable. Chinese or multilingual deployments must explicitly select a suitable model, while the 2GB production server should use a remote Reranker. The service reranks the first 20 RRF candidates and selects eight by default with strict candidate-ID alignment. Timeouts, malformed responses, duplicate or unknown IDs, and non-finite scores produce sanitized diagnostics and a stable RRF fallback. Run the real local model check with:

```bash
(cd backend && RUN_MODEL_TESTS=1 uv run pytest -q \
  tests/contract/test_reranker_providers.py -m model)
```

The Scope/Root service resolves server-side authorization and user metadata constraints to an explicit set of currently ready PostgreSQL document IDs. Anonymous users retain full business access inside the demo tenant but cannot override the tenant in a request; restricted identities use the union of allowed Collections and Documents. Title, organization, media type, active-version UUID, and section are checked against the fact source, while contradictory explicit constraints return `QUERY_SCOPE_CONFLICT` without disclosing resource existence. Recalled Leaves are rechecked before reranking, and selected Roots are rechecked again before entering context, joining tenant, active Collection, ready Document, and indexed active Version. Stale vectors, deleting content, and unauthorized records are therefore discarded. Recovered content has a strict default 18,000-character budget, merges Leaf references per Root, and records deterministic truncation.

The Query Planning Service treats structured Planner output as untrusted input and strictly validates fields, intent, sub-query and requirement limits, UUIDs, and scope narrowing. A model cannot change Standard/Deep mode, invent Collection or Document IDs, or replace explicit caller metadata. Any malformed response or Provider failure falls back as one unit to a deterministic plan that preserves the original scope, recognizes Chinese and English comparison, procedural, and summary intent, splits multiple conditions, and uses the latest user turn to resolve pronouns. Provider exception text never enters the QueryPlan.

The Standard Query Graph is an explicit state machine connecting Plan → Search → RRF → PostgreSQL Authorize → Rerank → Root Recover → Answer. Every run returns its actual transitions. Empty RRF output, authorized Leaves, or rechecked Roots terminate as NoResults without invoking the answer model. Standard counts the Planner attempt as LLM call one and final answer generation as call two, with a runtime hard ceiling; Planner degradation adds no call. Unclassified failures terminate as Failed with a sanitized error code and no exception text exposed to clients.

Deep Recovery uses an Evidence Ledger deduplicated by Leaf ID across rounds and reserves final slots for new Recovery evidence. Deterministic evidence scores answer at or above 0.80, recover below 0.45, and invoke the Evidence Assessor only in the middle band. Recovery is capped at two rounds before Abstain. Its four routes are Rewrite Hybrid, HyDE Dense-only, Exact-term Sparse-only, and Scope repair that removes only a proven bad field. The current Sparse implementation is hashing lexical, not BM25, so neither code nor documentation mislabels the exact-term route; a true BM25 Provider can replace it later.

Answer Verification requires every factual paragraph to bind citations. A cited Root must come from the current authorized context, each Leaf must belong to that Root, and every quote must be a real contiguous substring of Root clean text, while all QueryPlan requirements must be covered. Structural or coverage errors get at most one Repair using exactly the same evidence and are then fully revalidated. Evidence conflicts are not hidden by rewriting and instead cause immediate Abstain. Only verified answers produce domain Citations carrying document, Root and Leaf IDs, page or section, quote, and score; every other result returns a bounded abstention with no citations or leaked provider error.

All future adapters implement the common `Provider` lifecycle contract and are owned by one application-scoped registry. Provider keys are `(kind, name)`; duplicate registration, unknown names, missing capabilities, and resource-close failures produce stable sanitized errors.

Root and Leaf IDs are derived from immutable identity fields and content hashes. Reprocessing the same version with the same index revision produces the same IDs; changing content, ordinal, kind, or index revision produces different IDs. Domain timestamps must be timezone-aware UTC, metadata is copied into deeply immutable structures, and `to_dict()` outputs JSON-compatible API values.

M1 was delivered through independently checked pull requests: [specification #1](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/1), [anonymous demo boundary #2](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/2), [Monorepo #3](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/3), [CI and branch protection #4](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/4), [settings #5](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/5), [plugin registry #6](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/6), and [domain types #7](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/7).
