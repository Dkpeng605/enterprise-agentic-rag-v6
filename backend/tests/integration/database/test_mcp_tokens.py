"""Real PostgreSQL acceptance for active MCP bearer-token lookup."""

import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete

from enterprise_rag.adapters.database import Database, PostgreSQLMcpTokenStore
from enterprise_rag.adapters.database.models import ApiTokenModel, TenantModel, UserModel

BACKEND_ROOT = Path(__file__).parents[3]
NOW = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
TENANT_ID = UUID("01900000-0000-7000-8000-000000005321")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000005322")
TOKEN_ID = UUID("01900000-0000-7000-8000-000000005323")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005324")
RAW_TOKEN = "postgres-contract-token-" + "p" * 40
PEPPER = "postgres-contract-pepper-at-least-32-bytes"


def database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.fail(
            "TEST_DATABASE_URL is required; start infra/compose/compose.dev.yml before tests"
        )
    return value


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url())
    command.upgrade(config, "head")


@pytest.mark.anyio
async def test_store_accepts_only_active_pepper_hash_and_never_persists_raw_token() -> None:
    database = Database(database_url())
    digest = hmac.new(PEPPER.encode(), RAW_TOKEN.encode(), hashlib.sha256).hexdigest()
    try:
        await _cleanup(database)
        async with database.session() as session:
            session.add(TenantModel(id=TENANT_ID, name="MCP tenant", slug="mcp-http-contract"))
            session.add(
                UserModel(
                    id=ACTOR_ID,
                    email="mcp-http-contract@example.invalid",
                    password_hash="not-a-login-account",
                )
            )
            await session.flush()
            session.add(
                ApiTokenModel(
                    id=TOKEN_ID,
                    tenant_id=TENANT_ID,
                    actor_id=ACTOR_ID,
                    name="contract token",
                    token_prefix=RAW_TOKEN[:12],
                    token_hash=digest,
                    scopes=["mcp:access", "knowledge:read"],
                    collection_ids=[str(COLLECTION_ID)],
                    expires_at=NOW + timedelta(hours=1),
                )
            )

        store = PostgreSQLMcpTokenStore(database)
        grant = await store.authenticate(digest, now=NOW)
        wrong = await store.authenticate("0" * 64, now=NOW)
        expired = await store.authenticate(digest, now=NOW + timedelta(hours=2))

        assert grant is not None
        assert grant.token_id == TOKEN_ID
        assert grant.collection_ids == (COLLECTION_ID,)
        assert wrong is None
        assert expired is None
        assert RAW_TOKEN not in repr(grant)

        async with database.session() as session:
            model = await session.get(ApiTokenModel, TOKEN_ID)
            assert model is not None
            assert model.token_hash == digest
            assert RAW_TOKEN not in model.token_hash
            model.collection_ids = []

        assert await store.authenticate(digest, now=NOW) is None

        async with database.session() as session:
            model = await session.get(ApiTokenModel, TOKEN_ID)
            assert model is not None
            model.collection_ids = [str(COLLECTION_ID)]
            model.revoked_at = NOW

        assert await store.authenticate(digest, now=NOW) is None
    finally:
        await _cleanup(database)
        await database.dispose()


async def _cleanup(database: Database) -> None:
    async with database.session() as session:
        await session.execute(delete(ApiTokenModel).where(ApiTokenModel.id == TOKEN_ID))
        await session.execute(delete(UserModel).where(UserModel.id == ACTOR_ID))
        await session.execute(delete(TenantModel).where(TenantModel.id == TENANT_ID))
