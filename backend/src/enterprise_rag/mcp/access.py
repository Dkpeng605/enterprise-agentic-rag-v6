"""Per-request MCP authorization derived from server-side transport identity."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.services.auth import Principal

MCP_ACCESS_SCOPE = "mcp:access"
KNOWLEDGE_READ_SCOPE = "knowledge:read"
QUERY_EXECUTE_SCOPE = "query:execute"
ANSWER_VERIFY_SCOPE = "answer:verify"
ALL_MCP_SCOPES = frozenset(
    {MCP_ACCESS_SCOPE, KNOWLEDGE_READ_SCOPE, QUERY_EXECUTE_SCOPE, ANSWER_VERIFY_SCOPE}
)


@dataclass(frozen=True, slots=True)
class McpAccess:
    principal: Principal
    scopes: frozenset[str]
    collection_ids: tuple[UUID, ...] | None = None

    @classmethod
    def trusted_process(cls, principal: Principal) -> "McpAccess":
        return cls(principal, ALL_MCP_SCOPES)

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AppError(ErrorCode.FORBIDDEN, "The MCP token scope is insufficient.")

    def constrain_collections(self, requested: Sequence[UUID]) -> tuple[UUID, ...]:
        if self.collection_ids is None:
            return tuple(requested)
        allowed = frozenset(self.collection_ids)
        if requested and not set(requested).issubset(allowed):
            raise AppError(ErrorCode.FORBIDDEN, "The MCP collection scope is insufficient.")
        return tuple(requested) if requested else self.collection_ids


AccessResolver = Callable[[], McpAccess]


def http_access() -> McpAccess:
    token = get_access_token()
    claims = token.claims if token is not None else None
    if token is None or not isinstance(claims, dict):
        raise AppError(ErrorCode.AUTHENTICATION_REQUIRED, "A valid MCP token is required.")
    try:
        token_id = UUID(str(claims["token_id"]))
        tenant_id = UUID(str(claims["tenant_id"]))
        actor_id = UUID(str(claims["actor_id"]))
        raw_collections = claims.get("collection_ids")
        if not isinstance(raw_collections, list) or not raw_collections:
            raise ValueError("collection scope is missing")
        collection_ids = tuple(UUID(str(value)) for value in raw_collections)
    except (KeyError, TypeError, ValueError) as error:
        raise AppError(
            ErrorCode.AUTHENTICATION_REQUIRED, "A valid MCP token is required."
        ) from error
    return McpAccess(
        Principal(token_id, tenant_id, actor_id, actor_type="mcp_token", role="token"),
        frozenset(token.scopes),
        collection_ids,
    )
