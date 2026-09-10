# Enterprise Agentic RAG v6

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

## Start the current project

Install all locked dependencies from the repository root:

```bash
uv sync --project backend --locked
pnpm install --frozen-lockfile
```

Start the development PostgreSQL service and apply migrations:

```bash
docker compose -f infra/compose/compose.dev.yml up -d postgres
DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test \
  uv run --project backend alembic -c backend/alembic.ini upgrade head
```

Start the backend in the first terminal:

```bash
uv run --project backend uvicorn enterprise_rag.main:app --reload
```

The development API is available at `http://127.0.0.1:8000`. The current skeleton exposes:

- `GET /` — service name, version, configuration status, and active environment
- `GET /docs` — interactive OpenAPI documentation
- `GET /openapi.json` — OpenAPI schema

Start the frontend in a second terminal:

```bash
pnpm --dir frontend dev
```

The Vite development server prints its local URL. The current page confirms that the Vue 3 and TypeScript application mounted successfully.

PostgreSQL is required for migration and integration tests. The current HTTP skeleton does not query it yet. Model APIs, Milvus, authentication, and RAG behavior are introduced only by their acceptance PRs.

Milvus Lite is embedded through PyMilvus and needs no separate service. Contract tests create isolated temporary `.db` files; runtime data belongs under ignored `data/runtime/`, never in Git. A single Milvus Lite file must only be opened by one application process.

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
uv run --project backend ruff check backend/src backend/tests backend/migrations
uv run --project backend mypy backend/src backend/tests backend/migrations
uv run --project backend pytest -q
uv build --project backend
```

Frontend:

```bash
pnpm --dir frontend test
pnpm --dir frontend typecheck
pnpm --dir frontend build
```

The same commands run on every GitHub pull request. Both `backend-quality` and `frontend-quality` must pass before merge.

## Contribution workflow

1. Create one branch for one acceptance slice.
2. Add or update the acceptance test, implementation, and necessary documentation together.
3. Review and update this README on every PR so its startup commands and milestone status remain accurate.
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
- M2-03 Milvus Lite vector-store adapter: implemented by the current PR
- Next: M2-04 local object store

The PostgreSQL job repository now owns enqueue, exclusive lease, start, heartbeat, retry, cooperative cancellation, success, and expired-lease recovery transitions. Workers identify themselves with an owner string and must renew a time-limited lease; stale or wrong-owner updates are rejected. Progress is monotonic, retries stop at `max_attempts`, and concurrent workers use `FOR UPDATE SKIP LOCKED` so only one can claim a job.

The VectorStore port requires an index revision on every record and search. Milvus collections are isolated by revision so embedding dimensions cannot be mixed. Every search expression injects `tenant_id` and `status == "ready"`; optional collection and document scopes only narrow that mandatory filter. Dense and sparse vectors, scalar filtering, idempotent upsert, count, version deletion, persistence, and close behavior run against real Milvus Lite files in contract tests.

M1 is complete. Product RAG behavior has not been implemented yet. The repository now provides the tested engineering foundation: packaging, CI and protected-main workflow, validated configuration loading, provider discovery and lifecycle rules, immutable domain models, stable content IDs, UUIDv7 identifiers, unified errors, application startup, and frontend mounting.

All future adapters implement the common `Provider` lifecycle contract and are owned by one application-scoped registry. Provider keys are `(kind, name)`; duplicate registration, unknown names, missing capabilities, and resource-close failures produce stable sanitized errors.

Root and Leaf IDs are derived from immutable identity fields and content hashes. Reprocessing the same version with the same index revision produces the same IDs; changing content, ordinal, kind, or index revision produces different IDs. Domain timestamps must be timezone-aware UTC, metadata is copied into deeply immutable structures, and `to_dict()` outputs JSON-compatible API values.

M1 was delivered through independently checked pull requests: [specification #1](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/1), [anonymous demo boundary #2](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/2), [Monorepo #3](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/3), [CI and branch protection #4](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/4), [settings #5](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/5), [plugin registry #6](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/6), and [domain types #7](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/7).
