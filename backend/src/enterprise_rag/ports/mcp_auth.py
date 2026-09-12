"""Persistence contract for pepper-hashed MCP bearer tokens."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from enterprise_rag.domain.common import require_non_empty, require_utc, require_uuid7


@dataclass(frozen=True, slots=True)
class McpTokenGrant:
    token_id: UUID
    tenant_id: UUID
    actor_id: UUID
    scopes: tuple[str, ...]
    collection_ids: tuple[UUID, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("token_id", self.token_id),
            ("tenant_id", self.tenant_id),
            ("actor_id", self.actor_id),
        ):
            require_uuid7(value, name)
        require_utc(self.expires_at, "expires_at")
        if not self.scopes or len(self.scopes) != len(set(self.scopes)):
            raise ValueError("scopes must be non-empty and unique")
        for scope in self.scopes:
            require_non_empty(scope, "scope")
        if not self.collection_ids or len(self.collection_ids) != len(set(self.collection_ids)):
            raise ValueError("collection_ids must be non-empty and unique")
        for collection_id in self.collection_ids:
            require_uuid7(collection_id, "collection_id")


class McpTokenStore(Protocol):
    async def authenticate(self, token_hash: str, *, now: datetime) -> McpTokenGrant | None: ...
