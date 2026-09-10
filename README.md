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

## Backend smoke test

```bash
uv sync --project backend
uv run --project backend pytest
uv run --project backend uvicorn enterprise_rag.main:app --reload
```

The development API is then available at `http://127.0.0.1:8000`.

## Frontend smoke test

```bash
pnpm install
pnpm --dir frontend test
pnpm --dir frontend build
pnpm --dir frontend dev
```

The Vite development server prints its local URL when it starts.

## Current milestone

M1 establishes the specification, repository skeleton, CI, configuration, plugin contracts, and domain types. Product RAG behavior is intentionally added only by its corresponding acceptance PR.

