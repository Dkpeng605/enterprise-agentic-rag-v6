# Backend

The backend uses FastAPI and a `src` package layout. At milestone M1-02 it intentionally exposes only a minimal service descriptor so the application, tests, and packaging can be verified before product behavior is introduced.

```bash
uv sync --project backend
uv run --project backend pytest
uv run --project backend uvicorn enterprise_rag.main:app --reload
```

