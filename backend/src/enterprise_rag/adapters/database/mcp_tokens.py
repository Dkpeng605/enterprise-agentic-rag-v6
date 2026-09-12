"""PostgreSQL lookup for active, pepper-hashed MCP bearer tokens."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.models import ApiTokenModel, TenantModel, UserModel
from enterprise_rag.ports.mcp_auth import McpTokenGrant


class PostgreSQLMcpTokenStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def authenticate(self, token_hash: str, *, now: datetime) -> McpTokenGrant | None:
        async with self._database.session() as session:
            model = await session.scalar(
                select(ApiTokenModel)
                .join(TenantModel, TenantModel.id == ApiTokenModel.tenant_id)
                .join(UserModel, UserModel.id == ApiTokenModel.actor_id)
                .where(
                    ApiTokenModel.token_hash == token_hash,
                    ApiTokenModel.revoked_at.is_(None),
                    ApiTokenModel.expires_at > now,
                    TenantModel.status == "active",
                    UserModel.status == "active",
                )
            )
        if model is None:
            return None
        try:
            scopes = tuple(str(value) for value in model.scopes)
            collection_ids = tuple(UUID(str(value)) for value in model.collection_ids)
            return McpTokenGrant(
                model.id,
                model.tenant_id,
                model.actor_id,
                scopes,
                collection_ids,
                model.expires_at,
            )
        except (TypeError, ValueError):
            return None
