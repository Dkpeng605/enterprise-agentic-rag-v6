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

No database, model API, Milvus, authentication, or RAG configuration is required for the current development startup. Those capabilities are introduced only by their acceptance PRs.

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
uv run --project backend ruff check backend/src backend/tests
uv run --project backend mypy backend/src backend/tests
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
- M1-04 validated settings and secret loading: implemented by the current PR
- Next: M1-05 common plugin ports and registry

Product RAG behavior has not been implemented yet. The repository currently proves packaging, validated configuration loading, application startup, frontend mounting, automated tests, type-checking, and production frontend builds.
