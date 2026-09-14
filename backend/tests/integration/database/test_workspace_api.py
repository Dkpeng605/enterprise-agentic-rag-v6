import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.database.models import (
    AnonymousSessionModel,
    AuthenticatedSessionModel,
    CollectionModel,
    LeafModel,
    RootModel,
    TenantModel,
    TraceRunModel,
    UserModel,
)
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.api import create_app
from enterprise_rag.config import AppSettings

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
SESSION_SECRET = "integration-only-session-secret-with-more-than-32-bytes"
ADMIN_EMAIL = "admin@example.test"
ADMIN_PASSWORD = "integration-admin-password"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
OTHER_TENANT_ID = UUID("01900000-0000-7000-8000-00000000e001")
OTHER_COLLECTION_ID = UUID("01900000-0000-7000-8000-00000000e002")


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
            "app": {"public_base_url": "https://testserver"},
            "security": {
                "anonymous_max_file_bytes": 1_024,
                "anonymous_max_ready_documents": 20,
            },
            "ingestion": {"max_upload_bytes": 1_024},
            "credentials": {
                "admin_bootstrap_email": ADMIN_EMAIL,
                "admin_bootstrap_password": ADMIN_PASSWORD,
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
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client
    await database.dispose()


async def start_session(client: httpx2.AsyncClient) -> tuple[str, str]:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert response.json()["actor_type"] == "anonymous"
    assert response.json()["role"] == "demo_operator"
    assert "documents:manage" in response.json()["permissions"]
    return response.json()["csrf_token"], response.json()["tenant"]["id"]


def csrf_headers(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


@pytest.mark.anyio
async def test_workspace_overview_is_tenant_scoped_and_aggregates_operational_metrics(
    api: httpx2.AsyncClient,
) -> None:
    csrf, tenant_id = await start_session(api)
    empty = await api.get("/api/v1/workspace/overview")
    assert empty.status_code == 200
    assert empty.json()["queries_24h"] == 0
    assert empty.json()["query_error_rate"] is None

    collection = await api.post(
        "/api/v1/collections",
        json={"name": "Overview"},
        headers=csrf_headers(csrf),
    )
    upload = await api.post(
        "/api/v1/documents",
        data={
            "collection_id": collection.json()["id"],
            "title": "Overview source",
            "visibility": "tenant",
        },
        files={"file": ("overview.txt", b"overview content", "text/plain")},
        headers=csrf_headers(csrf),
    )
    assert upload.status_code == 202
    document_id = UUID(upload.json()["document_id"])
    version_id = UUID(upload.json()["version_id"])
    database = Database(DATABASE_URL)
    try:
        async with database.session() as session:
            session.add(
                RootModel(
                    id="root_" + "1" * 64,
                    tenant_id=UUID(tenant_id),
                    document_id=document_id,
                    version_id=version_id,
                    index_revision="overview-v1",
                    ordinal=0,
                    kind="section",
                    source_locator={},
                    raw_text="overview content",
                    clean_text="overview content",
                    metadata_json={},
                    content_hash="1" * 64,
                )
            )
            await session.flush()
            session.add(
                LeafModel(
                    id="leaf_" + "2" * 64,
                    root_id="root_" + "1" * 64,
                    tenant_id=UUID(tenant_id),
                    document_id=document_id,
                    version_id=version_id,
                    ordinal=0,
                    text="overview content",
                    retrieval_text="overview content",
                    token_count=2,
                    metadata_json={},
                    content_hash="2" * 64,
                )
            )
            for index, (status, duration) in enumerate(
                (("answered", 100), ("error", 800), ("abstained", 250)), start=1
            ):
                session.add(
                    TraceRunModel(
                        trace_id=f"{index:032x}",
                        tenant_id=UUID(tenant_id),
                        actor_id=None,
                        actor_type="anonymous",
                        trace_type="query",
                        subject_id=UUID(f"01900000-0000-7000-8000-{index:012d}"),
                        request_id=None,
                        mode="standard",
                        status=status,
                        started_at=NOW - timedelta(hours=index),
                        finished_at=NOW - timedelta(hours=index) + timedelta(milliseconds=duration),
                        duration_ms=duration,
                        usage={},
                        attributes={},
                    )
                )
            session.add(
                TraceRunModel(
                    trace_id="f" * 32,
                    tenant_id=UUID(tenant_id),
                    actor_id=None,
                    actor_type="anonymous",
                    trace_type="evaluation",
                    subject_id=UUID("01900000-0000-7000-8000-000000009999"),
                    request_id=None,
                    mode="standard",
                    status="succeeded",
                    started_at=NOW - timedelta(minutes=30),
                    finished_at=NOW - timedelta(minutes=29),
                    duration_ms=60_000,
                    usage={},
                    attributes={},
                )
            )
            session.add(
                TenantModel(
                    id=OTHER_TENANT_ID,
                    name="Other overview tenant",
                    slug="other-overview-tenant",
                )
            )
            await session.flush()
            session.add(
                CollectionModel(
                    id=OTHER_COLLECTION_ID,
                    tenant_id=OTHER_TENANT_ID,
                    name="Must stay hidden",
                )
            )
            session.add(
                TraceRunModel(
                    trace_id="e" * 32,
                    tenant_id=OTHER_TENANT_ID,
                    actor_id=None,
                    actor_type="anonymous",
                    trace_type="query",
                    subject_id=UUID("01900000-0000-7000-8000-000000008888"),
                    request_id=None,
                    mode="standard",
                    status="error",
                    started_at=NOW - timedelta(minutes=10),
                    finished_at=NOW - timedelta(minutes=9),
                    duration_ms=60_000,
                    usage={},
                    attributes={},
                )
            )
    finally:
        await database.dispose()

    response = await api.get("/api/v1/workspace/overview")
    payload = response.json()
    assert response.status_code == 200
    assert payload["collection_count"] == 2
    assert payload["document_counts"]["pending"] == 1
    assert payload["root_count"] == payload["leaf_count"] == 1
    assert payload["queries_24h"] == 3
    assert payload["query_errors_24h"] == 1
    assert payload["query_error_rate"] == pytest.approx(1 / 3, abs=0.0001)
    assert payload["query_p95_ms"] == 800
    assert {item["kind"] for item in payload["recent_activity"]} == {
        "ingestion",
        "evaluation",
    }


@pytest.mark.anyio
async def test_demo_seed_enqueues_real_idempotent_documents(
    api: httpx2.AsyncClient,
) -> None:
    csrf, _ = await start_session(api)

    first = await api.post("/api/v1/demo/seed", headers=csrf_headers(csrf))
    assert first.status_code == 202
    first_payload = first.json()
    assert len(first_payload["documents"]) == 2
    assert all(item["status"] == "pending" for item in first_payload["documents"])
    assert all(item["deduplicated"] is False for item in first_payload["documents"])

    repeated = await api.post("/api/v1/demo/seed", headers=csrf_headers(csrf))
    assert repeated.status_code == 202
    repeated_payload = repeated.json()
    assert [item["document_id"] for item in repeated_payload["documents"]] == [
        item["document_id"] for item in first_payload["documents"]
    ]
    assert all(item["deduplicated"] is True for item in repeated_payload["documents"])

@pytest.mark.anyio
async def test_admin_login_uses_argon2_session_csrf_and_system_authorization(
    api: httpx2.AsyncClient,
) -> None:
    unknown = await api.post(
        "/api/v1/auth/login",
        json={"email": "missing@example.test", "password": "wrong"},
    )
    wrong = await api.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": "wrong"},
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]

    login = await api.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL.upper(), "password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200
    profile = login.json()
    assert profile["actor_type"] == "user"
    assert profile["role"] == "super_admin"
    assert profile["email"] == ADMIN_EMAIL
    assert "system:read" in profile["permissions"]
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "Secure" in login.headers["set-cookie"]

    database = Database(DATABASE_URL)
    try:
        async with database.session() as session:
            user = await session.scalar(select(UserModel).where(UserModel.email == ADMIN_EMAIL))
            stored = await session.scalar(select(AuthenticatedSessionModel))
        assert user is not None and user.password_hash.startswith("$argon2id$")
        assert stored is not None
        assert stored.token_hash != api.cookies.get("rag_session")
        assert stored.csrf_hash != profile["csrf_token"]
    finally:
        await database.dispose()

    system = await api.get("/api/v1/system/status")
    assert system.status_code == 200
    assert system.json() == {"status": "available", "role": "super_admin"}

    refreshed = await api.get("/api/v1/auth/me")
    assert refreshed.status_code == 200
    assert refreshed.json()["actor_type"] == "user"
    assert refreshed.json()["csrf_token"] != profile["csrf_token"]

    missing_csrf = await api.post("/api/v1/auth/logout")
    assert missing_csrf.status_code == 403
    logout = await api.post(
        "/api/v1/auth/logout",
        headers=csrf_headers(refreshed.json()["csrf_token"]),
    )
    assert logout.status_code == 204
    assert (await api.get("/api/v1/system/status")).status_code == 401


@pytest.mark.anyio
async def test_session_csrf_system_boundary_and_sanitized_errors(
    api: httpx2.AsyncClient,
) -> None:
    unauthenticated = await api.get("/api/v1/collections")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert unauthenticated.headers["x-request-id"] == unauthenticated.json()["error"]["request_id"]

    csrf, _ = await start_session(api)
    missing_csrf = await api.post("/api/v1/collections", json={"name": "Blocked"})
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "CSRF_INVALID"

    system = await api.get("/api/v1/system/status")
    assert system.status_code == 403
    assert system.json()["error"]["code"] == "FORBIDDEN"

    rotated = await api.get("/api/v1/auth/me")
    new_csrf = rotated.json()["csrf_token"]
    assert new_csrf != csrf
    stale_csrf = await api.post(
        "/api/v1/collections",
        json={"name": "Stale token"},
        headers=csrf_headers(csrf),
    )
    assert stale_csrf.status_code == 403
    csrf = new_csrf

    database = Database(DATABASE_URL)
    try:
        async with database.session() as session:
            stored = await session.scalar(select(AnonymousSessionModel))
        assert stored is not None
        assert len(stored.token_hash) == len(stored.csrf_hash) == 64
        assert stored.token_hash != api.cookies.get("rag_session")
        assert stored.csrf_hash != csrf
    finally:
        await database.dispose()

    validation = await api.post(
        "/api/v1/collections", json={"name": ""}, headers=csrf_headers(csrf)
    )
    assert validation.status_code == 422
    assert validation.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "input" not in str(validation.json())
    blank = await api.post("/api/v1/collections", json={"name": "   "}, headers=csrf_headers(csrf))
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "VALIDATION_ERROR"

    logout = await api.post("/api/v1/auth/logout", headers=csrf_headers(csrf))
    assert logout.status_code == 204
    assert (await api.get("/api/v1/collections")).status_code == 401


@pytest.mark.anyio
async def test_collection_crud_and_seed_collection_protection(api: httpx2.AsyncClient) -> None:
    csrf, _ = await start_session(api)
    initial = await api.get("/api/v1/collections")
    assert initial.status_code == 200
    assert len(initial.json()["items"]) == 1
    seed = initial.json()["items"][0]
    assert seed["is_seed"] is True

    blocked = await api.request(
        "DELETE",
        f"/api/v1/collections/{seed['id']}",
        json={"confirm_name": seed["name"]},
        headers=csrf_headers(csrf),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "CONFLICT"

    created = await api.post(
        "/api/v1/collections",
        json={
            "name": "产品手册",
            "description": "原始描述",
            "visibility": "tenant",
        },
        headers=csrf_headers(csrf),
    )
    assert created.status_code == 201
    collection_id = created.json()["id"]
    assert created.json()["document_count"] == 0

    duplicate = await api.post(
        "/api/v1/collections",
        json={"name": "产品手册"},
        headers=csrf_headers(csrf),
    )
    assert duplicate.status_code == 409

    updated = await api.patch(
        f"/api/v1/collections/{collection_id}",
        json={"name": "产品资料", "description": None, "visibility": "private"},
        headers=csrf_headers(csrf),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "产品资料"
    assert updated.json()["description"] is None
    assert updated.json()["visibility"] == "private"

    deleted = await api.request(
        "DELETE",
        f"/api/v1/collections/{collection_id}",
        json={"confirm_name": "产品资料"},
        headers=csrf_headers(csrf),
    )
    assert deleted.status_code == 202
    assert deleted.json()["status"] == "deleted"
    assert deleted.json()["job_ids"] == []
    assert (await api.get(f"/api/v1/collections/{collection_id}")).status_code == 404


@pytest.mark.anyio
async def test_upload_list_cursor_detail_job_and_idempotent_delete(
    api: httpx2.AsyncClient,
) -> None:
    csrf, _ = await start_session(api)
    seed = (await api.get("/api/v1/collections")).json()["items"][0]

    uploads = []
    for title in ("员工手册", "安全制度"):
        response = await api.post(
            "/api/v1/documents",
            data={
                "collection_id": seed["id"],
                "title": title,
                "organization": "示例公司",
                "visibility": "tenant",
            },
            files={"file": (f"{title}.txt", f"{title} Enterprise RAG".encode(), "text/plain")},
            headers=csrf_headers(csrf),
        )
        assert response.status_code == 202
        assert response.json()["status"] == "pending"
        uploads.append(response.json())

    first_page = await api.get("/api/v1/documents", params={"limit": 1})
    assert first_page.status_code == 200
    assert len(first_page.json()["items"]) == 1
    assert first_page.json()["next_cursor"]
    second_page = await api.get(
        "/api/v1/documents",
        params={"limit": 1, "cursor": first_page.json()["next_cursor"]},
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["items"]) == 1
    assert second_page.json()["items"][0]["id"] != first_page.json()["items"][0]["id"]

    filtered = await api.get("/api/v1/documents", params={"keyword": "员工"})
    assert len(filtered.json()["items"]) == 1
    assert filtered.json()["items"][0]["organization"] == "示例公司"
    assert len(filtered.json()["items"][0]["sha256_prefix"]) == 12

    document_id = uploads[0]["document_id"]
    detail = await api.get(f"/api/v1/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["root_count"] == 0
    assert detail.json()["leaf_count"] == 0
    assert detail.json()["recent_job"]["id"] == uploads[0]["job_id"]
    assert detail.json()["recent_job"]["status"] == "queued"

    job = await api.get(f"/api/v1/ingestion-jobs/{uploads[0]['job_id']}")
    assert job.status_code == 200
    assert job.json()["progress"] == 0
    assert "lease_owner" not in job.json()

    jobs_page = await api.get("/api/v1/ingestion-jobs", params={"limit": 1})
    assert jobs_page.status_code == 200
    assert len(jobs_page.json()["items"]) == 1
    assert jobs_page.json()["items"][0]["status"] == "queued"
    assert jobs_page.json()["items"][0]["created_at"]
    assert jobs_page.json()["next_cursor"]
    next_jobs = await api.get(
        "/api/v1/ingestion-jobs",
        params={"limit": 1, "cursor": jobs_page.json()["next_cursor"]},
    )
    assert len(next_jobs.json()["items"]) == 1
    assert next_jobs.json()["items"][0]["id"] != jobs_page.json()["items"][0]["id"]
    queued_jobs = await api.get("/api/v1/ingestion-jobs", params={"status": "queued"})
    assert len(queued_jobs.json()["items"]) == 2

    first_delete = await api.delete(f"/api/v1/documents/{document_id}", headers=csrf_headers(csrf))
    repeated_delete = await api.delete(
        f"/api/v1/documents/{document_id}", headers=csrf_headers(csrf)
    )
    assert first_delete.status_code == repeated_delete.status_code == 202
    assert first_delete.json()["job_id"] == repeated_delete.json()["job_id"]
    assert repeated_delete.json()["already_requested"] is True


@pytest.mark.anyio
async def test_cross_tenant_ids_are_hidden_and_upload_errors_are_stable(
    api: httpx2.AsyncClient,
) -> None:
    csrf, _ = await start_session(api)
    database = Database(DATABASE_URL)
    try:
        async with database.session() as session:
            session.add(TenantModel(id=OTHER_TENANT_ID, name="Other", slug="other-api"))
            await session.flush()
            session.add(
                CollectionModel(
                    id=OTHER_COLLECTION_ID,
                    tenant_id=OTHER_TENANT_ID,
                    name="Other tenant collection",
                )
            )
    finally:
        await database.dispose()

    hidden = await api.get(f"/api/v1/collections/{OTHER_COLLECTION_ID}")
    assert hidden.status_code == 404
    upload_hidden = await api.post(
        "/api/v1/documents",
        data={"collection_id": str(OTHER_COLLECTION_ID), "title": "Cross tenant"},
        files={"file": ("cross.txt", b"hidden", "text/plain")},
        headers=csrf_headers(csrf),
    )
    assert upload_hidden.status_code == 404

    seed = (await api.get("/api/v1/collections")).json()["items"][0]
    unsupported = await api.post(
        "/api/v1/documents",
        data={"collection_id": seed["id"], "title": "Executable"},
        files={"file": ("malware.exe", b"MZ", "application/octet-stream")},
        headers=csrf_headers(csrf),
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"

    mismatched = await api.post(
        "/api/v1/documents",
        data={"collection_id": seed["id"], "title": "MIME mismatch"},
        files={"file": ("mismatch.txt", b"text", "application/pdf")},
        headers=csrf_headers(csrf),
    )
    assert mismatched.status_code == 415
    assert mismatched.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"

    oversized = await api.post(
        "/api/v1/documents",
        data={"collection_id": seed["id"], "title": "Too large"},
        files={"file": ("large.txt", b"x" * 1_025, "text/plain")},
        headers=csrf_headers(csrf),
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "UPLOAD_TOO_LARGE"


@pytest.mark.anyio
async def test_openapi_describes_workspace_security_pagination_and_errors(
    api: httpx2.AsyncClient,
) -> None:
    document = (await api.get("/openapi.json")).json()
    paths = document["paths"]
    for path in (
        "/api/v1/auth/me",
        "/api/v1/collections",
        "/api/v1/documents",
        "/api/v1/documents/{document_id}",
        "/api/v1/ingestion-jobs",
        "/api/v1/ingestion-jobs/{job_id}",
    ):
        assert path in paths
    assert "multipart/form-data" in paths["/api/v1/documents"]["post"]["requestBody"]["content"]
    assert "cursor" in {
        parameter["name"] for parameter in paths["/api/v1/documents"]["get"]["parameters"]
    }
    assert "403" in paths["/api/v1/documents"]["post"]["responses"]
    assert "APIKeyCookie" in str(document["components"]["securitySchemes"])
