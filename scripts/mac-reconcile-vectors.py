#!/usr/bin/env python3
"""Dry-run or safely remove stale Mac Milvus projections."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.config import load_settings
from enterprise_rag.services import ReconcileService


async def run(*, apply: bool) -> int:
    settings = load_settings()
    if settings.credentials.database_url is None:
        raise RuntimeError("DATABASE_URL is required")
    runtime_root = settings.ingestion.object_store_root.resolve()
    database = Database(settings.credentials.database_url.get_secret_value())
    object_store = LocalObjectStore(runtime_root)
    vector_store = MilvusLiteVectorStore(runtime_root.parent / "milvus" / "vectors.db")
    try:
        report = await ReconcileService(database, vector_store, object_store).run_vectors(
            now=datetime.now(UTC), apply=apply
        )
        print(
            json.dumps(
                {
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
                indent=2,
            )
        )
        return 0 if report.unresolved_count == 0 else 2
    finally:
        await vector_store.aclose()
        await object_store.aclose()
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare version-owned Milvus projections with PostgreSQL. "
            "The backend must be stopped because Milvus Lite is single-process."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "delete only PostgreSQL-missing versions or explicitly marked stale revisions; "
            "legacy unmarked rows remain report-only"
        ),
    )
    arguments = parser.parse_args()
    return asyncio.run(run(apply=arguments.apply))


if __name__ == "__main__":
    raise SystemExit(main())
