"""Trusted local MCP token issuance, listing, and revocation acceptance."""

import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from enterprise_rag.adapters.database import Database, PostgreSQLMcpTokenStore
from enterprise_rag.adapters.database.models import (
    ApiTokenModel,
    CollectionModel,
    MembershipModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.mcp.access import ALL_MCP_SCOPES
from enterprise_rag.mcp.tokens import McpTokenOperator

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
PEPPER = "operator-integration-pepper-at-least-32-bytes"
NOW = datetime(2026, 9, 15, 2, 0, tzinfo=UTC)
TENANT_ID = UUID("01900000-0000-7000-8000-000000005501")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000005502")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005503")
OUTSIDE_COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005504")


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


@pytest.mark.anyio
async def test_operator_issues_hash_only_lists_metadata_and_revokes() -> None:
    database = Database(DATABASE_URL)
    try:
        await _seed(database)
        operator = McpTokenOperator(database, pepper=PEPPER, clock=lambda: NOW)
        issued = await operator.issue(
            tenant_slug="operator-test",
            actor_email="operator@example.invalid",
            name="Mac Claude Desktop",
            collection_ids=None,
            scopes=tuple(sorted(ALL_MCP_SCOPES)),
            expires_in=timedelta(days=30),
        )

        assert issued.raw_token.startswith("rag_mcp_")
        assert issued.raw_token not in repr(issued)
        assert issued.collection_ids == (COLLECTION_ID,)
        digest = hmac.new(
            PEPPER.encode(), issued.raw_token.encode(), hashlib.sha256
        ).hexdigest()
        async with database.session() as session:
            persisted = await session.scalar(
                select(ApiTokenModel).where(ApiTokenModel.id == issued.id)
            )
            assert persisted is not None
            assert persisted.token_hash == digest
            assert issued.raw_token not in repr(persisted.__dict__)

        listed = await operator.list(tenant_slug="operator-test")
        assert [item.id for item in listed] == [issued.id]
        assert listed[0].token_prefix == issued.raw_token[:12]
        grant = await PostgreSQLMcpTokenStore(database).authenticate(digest, now=NOW)
        assert grant is not None
        assert grant.scopes == tuple(sorted(ALL_MCP_SCOPES))

        assert await operator.revoke(tenant_slug="operator-test", token_id=issued.id) is True
        assert await operator.revoke(tenant_slug="operator-test", token_id=issued.id) is False
        assert await PostgreSQLMcpTokenStore(database).authenticate(digest, now=NOW) is None
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_operator_rejects_collection_outside_tenant() -> None:
    database = Database(DATABASE_URL)
    try:
        await _seed(database)
        operator = McpTokenOperator(database, pepper=PEPPER, clock=lambda: NOW)
        with pytest.raises(AppError) as raised:
            await operator.issue(
                tenant_slug="operator-test",
                actor_email="operator@example.invalid",
                name="Invalid scope",
                collection_ids=(OUTSIDE_COLLECTION_ID,),
                scopes=tuple(sorted(ALL_MCP_SCOPES)),
                expires_in=timedelta(days=1),
            )
        assert raised.value.code is ErrorCode.VALIDATION_ERROR
    finally:
        await database.dispose()


async def _seed(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add_all(
            [
                TenantModel(id=TENANT_ID, name="Operator", slug="operator-test"),
                UserModel(
                    id=ACTOR_ID,
                    email="operator@example.invalid",
                    password_hash="not-a-login-account",
                ),
            ]
        )
        await session.flush()
        session.add(
            CollectionModel(id=COLLECTION_ID, tenant_id=TENANT_ID, name="Knowledge")
        )
        session.add(
            MembershipModel(tenant_id=TENANT_ID, user_id=ACTOR_ID, role="tenant_admin")
        )
