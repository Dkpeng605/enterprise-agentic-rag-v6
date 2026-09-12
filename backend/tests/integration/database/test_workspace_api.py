import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
    TenantModel,
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
        "/api/v1/ingestion-jobs/{job_id}",
    ):
        assert path in paths
    assert "multipart/form-data" in paths["/api/v1/documents"]["post"]["requestBody"]["content"]
    assert "cursor" in {
        parameter["name"] for parameter in paths["/api/v1/documents"]["get"]["parameters"]
    }
    assert "403" in paths["/api/v1/documents"]["post"]["responses"]
    assert "APIKeyCookie" in str(document["components"]["securitySchemes"])
