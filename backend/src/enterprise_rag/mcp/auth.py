"""Opaque bearer-token verification without retaining raw credentials."""

import hashlib
import hmac
from collections.abc import Callable
from datetime import datetime

from mcp.server.auth.provider import AccessToken

from enterprise_rag.domain.common import utc_now
from enterprise_rag.ports.mcp_auth import McpTokenStore

Clock = Callable[[], datetime]


class McpBearerTokenVerifier:
    def __init__(
        self,
        store: McpTokenStore,
        *,
        pepper: str,
        resource: str,
        clock: Clock = utc_now,
    ) -> None:
        if len(pepper.encode()) < 32:
            raise ValueError("MCP token pepper must contain at least 32 bytes")
        if not resource.startswith(("http://", "https://")):
            raise ValueError("MCP resource must be an absolute HTTP URL")
        self._store = store
        self._pepper = pepper.encode()
        self._resource = resource.rstrip("/")
        self._clock = clock

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) < 32 or len(token) > 512:
            return None
        digest = hmac.new(self._pepper, token.encode(), hashlib.sha256).hexdigest()
        grant = await self._store.authenticate(digest, now=self._clock())
        if grant is None:
            return None
        return AccessToken(
            token=str(grant.token_id),
            client_id=f"mcp-token:{grant.token_id}",
            scopes=list(grant.scopes),
            expires_at=int(grant.expires_at.timestamp()),
            resource=self._resource,
            subject=str(grant.actor_id),
            claims={
                "iss": self._resource,
                "token_id": str(grant.token_id),
                "tenant_id": str(grant.tenant_id),
                "actor_id": str(grant.actor_id),
                "collection_ids": [str(value) for value in grant.collection_ids],
            },
        )
