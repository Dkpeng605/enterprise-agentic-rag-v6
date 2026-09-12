"""Authenticated MCP Streamable HTTP composition."""

from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette

from enterprise_rag.domain.common import utc_now
from enterprise_rag.mcp.access import MCP_ACCESS_SCOPE, http_access
from enterprise_rag.mcp.auth import McpBearerTokenVerifier
from enterprise_rag.mcp.server import McpCatalog, build_mcp_server
from enterprise_rag.ports.mcp_auth import McpTokenStore
from enterprise_rag.services.knowledge import McpApplicationService

Clock = Callable[[], datetime]


def build_http_mcp_app(
    application: McpApplicationService,
    catalog: McpCatalog,
    token_store: McpTokenStore,
    *,
    token_pepper: str,
    public_base_url: str,
    allow_insecure_http: bool = False,
    clock: Clock = utc_now,
) -> Starlette:
    base_url = public_base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public_base_url must be an absolute HTTP URL")
    if parsed.scheme != "https" and not allow_insecure_http:
        raise ValueError("public MCP requires HTTPS")
    resource = f"{base_url}/mcp"
    verifier = McpBearerTokenVerifier(
        token_store, pepper=token_pepper, resource=resource, clock=clock
    )
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(resource),
        resource_server_url=AnyHttpUrl(resource),
        validate_token_resource=True,
        required_scopes=[MCP_ACCESS_SCOPE],
    )
    server = build_mcp_server(
        application,
        catalog,
        http_access,
        auth=auth,
        token_verifier=verifier,
    )
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        session_idle_timeout=300,
        max_sessions=100,
        max_request_body_size=1_048_576,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[parsed.netloc],
            allowed_origins=[origin],
        ),
        host=parsed.hostname or "127.0.0.1",
    )
