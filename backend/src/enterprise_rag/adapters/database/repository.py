"""Small typed async repository primitive shared by concrete repositories."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.adapters.database.models import Base


class AsyncRepository[ModelT: Base]:
    def __init__(self, session: AsyncSession, model_type: type[ModelT]) -> None:
        self.session = session
        self.model_type = model_type

    async def add(self, model: ModelT) -> ModelT:
        self.session.add(model)
        await self.session.flush()
        return model

    async def get(self, identifier: UUID | str) -> ModelT | None:
        return await self.session.get(self.model_type, identifier)
