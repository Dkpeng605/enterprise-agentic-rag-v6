"""Async SQLAlchemy engine and transaction boundaries."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    """Own the PostgreSQL engine and issue transaction-scoped sessions."""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 3,
        max_overflow: int = 2,
        echo: bool = False,
    ) -> None:
        if not url.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("database URL must use PostgreSQL")
        async_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        self.engine: AsyncEngine = create_async_engine(
            async_url,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
            echo=echo,
        )
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Commit a successful unit of work and roll back failures."""

        async with self._sessions() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
