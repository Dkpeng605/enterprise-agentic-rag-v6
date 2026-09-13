import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from enterprise_rag.adapters.database import Database, PostgreSQLEvaluationRunStore
from enterprise_rag.adapters.database.models import EvaluationRunModel
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.api import create_app
from enterprise_rag.config import AppSettings
from enterprise_rag.domain import AppError, ErrorCode
from enterprise_rag.domain.common import new_uuid7
from enterprise_rag.services.auth import DEMO_ACTOR_ID

BACKEND_ROOT = Path(__file__).parents[3]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
SESSION_SECRET = "evaluation-integration-secret-with-more-than-32-bytes"
NOW = datetime(2026, 9, 13, 7, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    database = Database(DATABASE_URL)
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
    settings = AppSettings.model_validate(
        {
            "app": {
                "public_base_url": "https://testserver",
                "commit_sha": "evaluation-test-sha",
            },
            "evaluation": {
                "max_cases": 30,
                "max_llm_calls": 0,
                "golden_manifest": str(
                    REPOSITORY_ROOT / "evals/golden/v1/manifest.yaml"
                ),
            },
        }
    )
    application = create_app(
        settings,
        database=database,
        object_store=LocalObjectStore(tmp_path / "objects"),
        session_secret=SESSION_SECRET,
        clock=lambda: NOW,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=application),
        base_url="https://testserver",
    ) as client:
        yield client
    await database.dispose()


async def session(client: httpx2.AsyncClient) -> tuple[str, UUID]:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200
    return response.json()["csrf_token"], UUID(response.json()["tenant"]["id"])


def request(mode: str = "all") -> dict[str, object]:
    return {
        "dataset_revision": "enterprise-demo-golden-2026-09-09",
        "mode": mode,
        "provider_profile": "deterministic-sparse-v1",
        "max_cases": 4,
        "max_llm_calls": 0,
    }


@pytest.mark.anyio
async def test_anonymous_evaluation_run_persists_progress_report_and_history(
    api: httpx2.AsyncClient,
) -> None:
    csrf, tenant_id = await session(api)
    catalog = await api.get("/api/v1/evaluations/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["case_counts"] == {"all": 30, "standard": 21, "deep": 9}
    assert catalog.json()["profiles"][0]["requires_remote"] is False
    assert catalog.json()["max_llm_calls"] == 0

    created = await api.post(
        "/api/v1/evaluations/runs",
        json=request(),
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    detail = await api.get(f"/api/v1/evaluations/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "succeeded"
    assert detail.json()["completed_cases"] == detail.json()["total_cases"] == 4
    assert detail.json()["estimated_llm_calls"] == 0
    assert detail.json()["report"]["config_snapshot"]["commit_sha"] == "evaluation-test-sha"
    assert detail.json()["aggregate_metrics"]["document_recall_at_5"] == 1.0

    history = await api.get("/api/v1/evaluations/runs?status=succeeded&limit=1")
    assert history.status_code == 200
    assert history.json()["items"][0]["id"] == run_id
    assert history.json()["items"][0]["report"] is None
    assert history.json()["next_cursor"] is None

    database = Database(DATABASE_URL)
    try:
        async with database.session() as db_session:
            count = await db_session.scalar(
                select(func.count())
                .select_from(EvaluationRunModel)
                .where(EvaluationRunModel.tenant_id == tenant_id)
            )
        assert count == 1
        assert await PostgreSQLEvaluationRunStore(database).get(
            UUID("01900000-0000-7000-8000-00000000eeee"), UUID(run_id)
        ) is None
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_comparison_returns_deltas_or_stable_incomparability_reasons(
    api: httpx2.AsyncClient,
) -> None:
    csrf, _ = await session(api)
    run_ids: list[str] = []
    for mode in ("all", "all", "deep"):
        response = await api.post(
            "/api/v1/evaluations/runs",
            json=request(mode),
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 202
        run_ids.append(response.json()["id"])

    comparable = await api.get(
        "/api/v1/evaluations/compare",
        params={"base_run_id": run_ids[0], "candidate_run_id": run_ids[1]},
    )
    assert comparable.status_code == 200
    assert comparable.json()["comparable"] is True
    assert comparable.json()["reasons"] == []
    assert comparable.json()["deltas"]["mrr_at_10"] == 0.0

    mismatch = await api.get(
        "/api/v1/evaluations/compare",
        params={"base_run_id": run_ids[1], "candidate_run_id": run_ids[2]},
    )
    assert mismatch.status_code == 200
    assert mismatch.json()["comparable"] is False
    assert set(mismatch.json()["reasons"]) == {"MODE_MISMATCH", "CASE_SET_MISMATCH"}
    assert mismatch.json()["deltas"] == {}

    page = await api.get("/api/v1/evaluations/runs?limit=1")
    assert page.json()["next_cursor"] is not None
    next_page = await api.get(
        "/api/v1/evaluations/runs",
        params={"limit": 1, "cursor": page.json()["next_cursor"]},
    )
    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["id"] != page.json()["items"][0]["id"]


@pytest.mark.anyio
async def test_evaluation_rejects_budget_overflow_csrf_and_cross_tenant_ids(
    api: httpx2.AsyncClient,
) -> None:
    csrf, _ = await session(api)
    missing_csrf = await api.post("/api/v1/evaluations/runs", json=request())
    assert missing_csrf.status_code == 403

    over_budget = request()
    over_budget["max_cases"] = 31
    rejected = await api.post(
        "/api/v1/evaluations/runs",
        json=over_budget,
        headers={"X-CSRF-Token": csrf},
    )
    assert rejected.status_code == 400

    missing = await api.get(
        "/api/v1/evaluations/runs/01900000-0000-7000-8000-00000000ffff"
    )
    assert missing.status_code == 404

    database = Database(DATABASE_URL)
    store = PostgreSQLEvaluationRunStore(database)
    try:
        tenant_id = UUID((await api.get("/api/v1/auth/me")).json()["tenant"]["id"])

        async def create_active() -> None:
            await store.create(
                run_id=new_uuid7(),
                tenant_id=tenant_id,
                actor_id=DEMO_ACTOR_ID,
                dataset_revision="dataset-v1",
                mode="all",
                provider_profile="local-v1",
                provider="local",
                model="v1",
                prompt_revision="none",
                index_revision="index-v1",
                commit_sha="test",
                max_cases=1,
                max_llm_calls=0,
                estimated_llm_calls=0,
                case_ids=("case-1",),
                now=NOW,
            )

        await create_active()
        with pytest.raises(AppError) as conflict:
            await create_active()
        assert conflict.value.code is ErrorCode.CONFLICT
    finally:
        await database.dispose()
