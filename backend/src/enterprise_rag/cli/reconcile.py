"""Run the production ReconcileService or print its runtime fingerprint."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.embeddings import DEFAULT_MODEL
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.config import AppSettings, load_settings
from enterprise_rag.production_worker import _build_sparse, _build_vector_store
from enterprise_rag.services import ReconcileService
from enterprise_rag.services.index_revision import index_revision


async def _fingerprint(settings: AppSettings) -> dict[str, str]:
    embedding_model = settings.credentials.embedding_model or DEFAULT_MODEL
    sparse = _build_sparse(settings)
    try:
        info = sparse.info()
        return {
            "app_commit_sha": settings.app.commit_sha,
            "embedding_model": embedding_model,
            "sparse_provider": info.name,
            "sparse_version": info.version,
            "index_revision": index_revision(
                embedding_model,
                sparse_provider=info.name,
                sparse_version=info.version,
            ),
        }
    finally:
        await sparse.aclose()


async def _run(*, settings: AppSettings, apply: bool) -> int:
    if settings.credentials.database_url is None:
        raise RuntimeError("DATABASE_URL is required")
    runtime_root = settings.ingestion.object_store_root.resolve()
    database = Database(settings.credentials.database_url.get_secret_value())
    object_store = LocalObjectStore(runtime_root)
    vector_store = _build_vector_store(settings)
    try:
        fingerprint = await _fingerprint(settings)
        report = await ReconcileService(database, vector_store, object_store).run(
            now=datetime.now(UTC), apply=apply
        )
        print(
            json.dumps(
                {
                    **fingerprint,
                    "mode": "apply" if apply else "dry-run",
                    "issues": [
                        {
                            "kind": issue.kind.value,
                            "identity": issue.identity,
                            "expected": issue.expected,
                            "actual": issue.actual,
                            "repaired": issue.repaired,
                        }
                        for issue in report.issues
                    ],
                    "repaired_count": report.repaired_count,
                    "unresolved_count": report.unresolved_count,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if report.unresolved_count == 0 else 2
    finally:
        await vector_store.aclose()
        await object_store.aclose()
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply only safe reconcile repairs")
    parser.add_argument("--fingerprint", action="store_true", help="print provider/index identity only")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    arguments = parser.parse_args()
    del arguments.json
    settings = load_settings()
    if arguments.fingerprint:
        print(json.dumps(asyncio.run(_fingerprint(settings)), ensure_ascii=False, sort_keys=True))
        return 0
    return asyncio.run(_run(settings=settings, apply=arguments.apply))


if __name__ == "__main__":
    raise SystemExit(main())
