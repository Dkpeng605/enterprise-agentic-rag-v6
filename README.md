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

- `GET /` — service name, version, and skeleton status
- `GET /docs` — interactive OpenAPI documentation
- `GET /openapi.json` — OpenAPI schema

Start the frontend in a second terminal:

```bash
pnpm --dir frontend dev
```

The Vite development server prints its local URL. The current page confirms that the Vue 3 and TypeScript application mounted successfully.

No database, model API, Milvus, authentication, or RAG configuration is required at M1-03. Those capabilities are introduced only by their acceptance PRs.

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
- M1-03 CI and protected-main workflow: implemented by the current PR
- Next: M1-04 validated settings and secret loading

Product RAG behavior has not been implemented yet. The repository currently proves packaging, application startup, frontend mounting, automated tests, type-checking, and production frontend builds.
