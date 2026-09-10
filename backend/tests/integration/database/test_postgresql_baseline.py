import asyncio
import os
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from enterprise_rag.adapters.database import AsyncRepository, Database
from enterprise_rag.adapters.database.models import Base, TenantModel

BACKEND_ROOT = Path(__file__).parents[3]
EXPECTED_TABLES = {
    "collections",
    "document_content_claims",
    "document_versions",
    "documents",
    "index_revisions",
    "ingestion_jobs",
    "leaves",
    "memberships",
    "roots",
    "tenants",
    "users",
}
TENANT_ID = UUID("01900000-0000-7000-8000-000000000101")


def database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.fail(
            "TEST_DATABASE_URL is required; start infra/compose/compose.dev.yml before tests"
        )
    return value


def alembic_config() -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url())
    return config


async def table_names(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        return set(names)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.mark.anyio
async def test_fresh_upgrade_matches_declared_initial_metadata() -> None:
    names = await table_names(database_url())

    assert names >= EXPECTED_TABLES
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_repeated_upgrade_is_idempotent() -> None:
    command.upgrade(alembic_config(), "head")
    command.upgrade(alembic_config(), "head")


def test_downgrade_removes_all_application_tables_and_can_reupgrade() -> None:
    config = alembic_config()
    command.downgrade(config, "base")
    assert not EXPECTED_TABLES.intersection(asyncio.run(table_names(database_url())))
    command.upgrade(config, "head")
    assert asyncio.run(table_names(database_url())) >= EXPECTED_TABLES


@pytest.mark.anyio
async def test_async_repository_commits_and_reads_models() -> None:
    database = Database(database_url())
    try:
        async with database.session() as session:
            repository = AsyncRepository(session, TenantModel)
            await repository.add(TenantModel(id=TENANT_ID, name="Demo", slug="m2-01-demo"))

        async with database.session() as session:
            repository = AsyncRepository(session, TenantModel)
            tenant = await repository.get(TENANT_ID)
            assert tenant is not None
            assert tenant.slug == "m2-01-demo"
            await session.delete(tenant)
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_database_session_rolls_back_failed_unit_of_work() -> None:
    rolled_back_id = UUID("01900000-0000-7000-8000-000000000102")
    database = Database(database_url())
    try:
        with pytest.raises(RuntimeError, match="force rollback"):
            async with database.session() as session:
                repository = AsyncRepository(session, TenantModel)
                await repository.add(
                    TenantModel(id=rolled_back_id, name="Rollback", slug="rollback")
                )
                raise RuntimeError("force rollback")

        async with database.session() as session:
            repository = AsyncRepository(session, TenantModel)
            assert await repository.get(rolled_back_id) is None
    finally:
        await database.dispose()
