import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from enterprise_rag.adapters.database import Database, PostgreSQLUsageStore
from enterprise_rag.domain import AppError, ErrorCode
from enterprise_rag.ports import UsageAmounts, UsageLimits, UsageSnapshot

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
NOW = datetime(2026, 9, 12, 2, 3, 4, tzinfo=UTC)
TENANT_ID = UUID("01900000-0000-7000-8000-000000001b01")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000001b02")
QUERY_IDS = (
    UUID("01900000-0000-7000-8000-000000001b03"),
    UUID("01900000-0000-7000-8000-000000001b04"),
)
RATE_LIMIT_IDS = (
    UUID("01900000-0000-7000-8000-000000001b05"),
    UUID("01900000-0000-7000-8000-000000001b06"),
)


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


@pytest.fixture
async def usage_store() -> AsyncIterator[PostgreSQLUsageStore]:
    database = Database(DATABASE_URL)
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Usage', 'usage-test')"),
            {"id": TENANT_ID},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (:id, 'usage@example.test', 'unused')"
            ),
            {"id": ACTOR_ID},
        )
    yield PostgreSQLUsageStore(database)
    await database.dispose()


@pytest.mark.anyio
async def test_postgres_reservation_is_atomic_and_settlement_is_idempotent(
    usage_store: PostgreSQLUsageStore,
) -> None:
    limits = UsageLimits(10, 6, 1_000, 100)
    requested = UsageAmounts(6, 1_000, 100)

    results = await asyncio.gather(
        *(
            usage_store.reserve(
                query_id=query_id,
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
                rate_limit_id=rate_limit_id,
                now=NOW,
                requested=requested,
                limits=limits,
            )
            for query_id, rate_limit_id in zip(QUERY_IDS, RATE_LIMIT_IDS, strict=True)
        ),
        return_exceptions=True,
    )

    reservations = [item for item in results if not isinstance(item, BaseException)]
    errors = [item for item in results if isinstance(item, AppError)]
    assert len(reservations) == len(errors) == 1
    assert errors[0].code is ErrorCode.RATE_LIMITED
    reservation = reservations[0]
    settled = await usage_store.settle(reservation, UsageAmounts(2, 100, 20))
    repeated = await usage_store.settle(reservation, UsageAmounts(1, 1, 1))

    assert settled == UsageSnapshot(1, 2, 100, 20)
    assert repeated == settled


@pytest.mark.anyio
async def test_postgres_query_limit_rolls_back_daily_reservation(
    usage_store: PostgreSQLUsageStore,
) -> None:
    limits = UsageLimits(1, 20, 2_000, 200)
    requested = UsageAmounts(2, 100, 10)
    first = await usage_store.reserve(
        query_id=QUERY_IDS[0],
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        rate_limit_id=RATE_LIMIT_IDS[0],
        now=NOW,
        requested=requested,
        limits=limits,
    )
    await usage_store.settle(first, UsageAmounts(1, 50, 5))

    with pytest.raises(AppError) as raised:
        await usage_store.reserve(
            query_id=QUERY_IDS[1],
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            rate_limit_id=RATE_LIMIT_IDS[0],
            now=NOW,
            requested=requested,
            limits=limits,
        )

    assert raised.value.details["budget"] == "queries_per_minute"
    assert await usage_store.snapshot(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        rate_limit_id=RATE_LIMIT_IDS[0],
        now=NOW,
    ) == UsageSnapshot(1, 1, 50, 5)
