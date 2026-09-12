"""Authenticated Streamable HTTP contracts exercised through the official client."""

import asyncio
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from enterprise_rag.mcp import build_http_mcp_app
from enterprise_rag.mcp.access import (
    ANSWER_VERIFY_SCOPE,
    KNOWLEDGE_READ_SCOPE,
    MCP_ACCESS_SCOPE,
    QUERY_EXECUTE_SCOPE,
)
from enterprise_rag.ports.mcp_auth import McpTokenGrant
from enterprise_rag.services import (
    KnowledgeApplication,
    McpApplicationService,
    Principal,
    QueryApiService,
    QueryCommand,
    QueryExecution,
    QueryRunStatus,
)
from enterprise_rag.services.query_api import ProgressSink

PEPPER = "mcp-http-contract-pepper-at-least-32-bytes"
FULL_TOKEN = "full-token-" + "x" * 40
READ_TOKEN = "read-token-" + "y" * 40
NO_CONNECT_TOKEN = "no-connect-token-" + "z" * 40
NOW = datetime(2026, 9, 12, 2, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2099, 1, 1, tzinfo=UTC)
TENANT_ID = UUID("01900000-0000-7000-8000-000000005301")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000005302")
FULL_TOKEN_ID = UUID("01900000-0000-7000-8000-000000005303")
READ_TOKEN_ID = UUID("01900000-0000-7000-8000-000000005304")
NO_CONNECT_TOKEN_ID = UUID("01900000-0000-7000-8000-000000005305")
ALLOWED_COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005306")
DENIED_COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005307")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000005308")
QUERY_ID = UUID("01900000-0000-7000-8000-000000005309")


class FixtureTokenStore:
    def __init__(self) -> None:
        self.grants = {
            _digest(FULL_TOKEN): McpTokenGrant(
                FULL_TOKEN_ID,
                TENANT_ID,
                ACTOR_ID,
                tuple(
                    sorted(
                        {
                            MCP_ACCESS_SCOPE,
                            KNOWLEDGE_READ_SCOPE,
                            QUERY_EXECUTE_SCOPE,
                            ANSWER_VERIFY_SCOPE,
                        }
                    )
                ),
                (ALLOWED_COLLECTION_ID,),
                EXPIRES_AT,
            ),
            _digest(READ_TOKEN): McpTokenGrant(
                READ_TOKEN_ID,
                TENANT_ID,
                ACTOR_ID,
                (MCP_ACCESS_SCOPE, KNOWLEDGE_READ_SCOPE),
                (ALLOWED_COLLECTION_ID,),
                EXPIRES_AT,
            ),
            _digest(NO_CONNECT_TOKEN): McpTokenGrant(
                NO_CONNECT_TOKEN_ID,
                TENANT_ID,
                ACTOR_ID,
                (KNOWLEDGE_READ_SCOPE,),
                (ALLOWED_COLLECTION_ID,),
                EXPIRES_AT,
            ),
        }
        self.seen_hashes: list[str] = []

    async def authenticate(
        self, token_hash: str, *, now: datetime
    ) -> McpTokenGrant | None:
        self.seen_hashes.append(token_hash)
        grant = self.grants.get(token_hash)
        return grant if grant is not None and grant.expires_at > now else None


class SlowRecordingRunner:
    def __init__(self) -> None:
        self.commands: list[QueryCommand] = []

    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        await asyncio.sleep(0.05)
        self.commands.append(command)
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ANSWERED,
            "Long HTTP request completed.",
            (),
            {"tenant_id": str(command.tenant_id)},
            {"llm_calls": 1, "input_tokens": 3, "output_tokens": 4},
        )


class ScopedCatalog:
    def __init__(self) -> None:
        self.seen_collection_scopes: list[tuple[UUID, ...] | None] = []

    async def search_documents(
        self,
        principal: Principal,
        *,
        query: str,
        strategy: str,
        top_k: int,
        filters: Mapping[str, object],
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        self.seen_collection_scopes.append(collection_ids)
        return {"items": [], "strategy": strategy}

    async def list_collections(
        self, principal: Principal, *, collection_ids: tuple[UUID, ...] | None
    ) -> Sequence[Mapping[str, object]]:
        self.seen_collection_scopes.append(collection_ids)
        return ({"id": str(ALLOWED_COLLECTION_ID), "name": "Allowed"},)

    async def get_document_summary(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        return {"id": str(document_id), "title": "Scoped document"}

    async def list_document_sections(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        return {"items": [], "next_cursor": None}

    async def verify_answer(
        self,
        principal: Principal,
        *,
        answer: str,
        citations: Sequence[Mapping[str, object]],
        question: str | None,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        return {"valid": True, "issues": []}

    async def get_collection_resource(
        self,
        principal: Principal,
        collection_id: UUID,
        *,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        return {"id": str(collection_id), "name": "Allowed"}

    async def get_section_resource(
        self,
        principal: Principal,
        document_id: UUID,
        root_id: str,
        *,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        return {"document_id": str(document_id), "root_id": root_id, "text": "Scoped"}


@pytest.fixture
def http_mcp() -> tuple[Starlette, FixtureTokenStore, SlowRecordingRunner, ScopedCatalog]:
    store = FixtureTokenStore()
    runner = SlowRecordingRunner()
    catalog = ScopedCatalog()
    application = McpApplicationService(
        KnowledgeApplication(QueryApiService(runner), query_id_factory=lambda: QUERY_ID)
    )
    app = build_http_mcp_app(
        application,
        catalog,
        store,
        token_pepper=PEPPER,
        public_base_url="http://testserver",
        allow_insecure_http=True,
        clock=lambda: NOW,
    )
    return app, store, runner, catalog


@pytest.mark.anyio
async def test_http_mcp_requires_bearer_and_reports_protocol_errors(
    http_mcp: tuple[Starlette, FixtureTokenStore, SlowRecordingRunner, ScopedCatalog],
) -> None:
    app = http_mcp[0]
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx2.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        missing = await client.post("/mcp", content=b"{}", headers=headers)
        invalid = await client.post(
            "/mcp",
            content=b"{}",
            headers={**headers, "authorization": "Bearer invalid-token"},
        )
        no_connect = await client.post(
            "/mcp",
            content=b"{}",
            headers={**headers, "authorization": f"Bearer {NO_CONNECT_TOKEN}"},
        )
        malformed = await client.post(
            "/mcp",
            content=b"{",
            headers={**headers, "authorization": f"Bearer {FULL_TOKEN}"},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert no_connect.status_code == 403
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == -32700
    assert FULL_TOKEN not in malformed.text


@pytest.mark.anyio
async def test_scopes_collection_bounds_and_long_request_use_official_client(
    http_mcp: tuple[Starlette, FixtureTokenStore, SlowRecordingRunner, ScopedCatalog],
) -> None:
    app, store, runner, catalog = http_mcp
    async with app.router.lifespan_context(app):
        read_http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"authorization": f"Bearer {READ_TOKEN}"},
        )
        async with read_http, Client(
            streamable_http_client(
                "http://testserver/mcp", http_client=read_http, terminate_on_close=False
            )
        ) as read_client:
            listed = await read_client.call_tool("list_collections")
            assert listed.is_error is False
            denied = await read_client.call_tool(
                "query_knowledge_base", {"question": "Must be denied"}
            )
            assert denied.is_error is True
            assert denied.structured_content["error"]["code"] == "FORBIDDEN"

        full_http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"authorization": f"Bearer {FULL_TOKEN}"},
            timeout=1,
        )
        async with full_http, Client(
            streamable_http_client(
                "http://testserver/mcp", http_client=full_http, terminate_on_close=False
            )
        ) as full_client:
            completed = await full_client.call_tool(
                "query_knowledge_base", {"question": "Complete after a delay"}
            )
            outside_scope = await full_client.call_tool(
                "query_knowledge_base",
                {
                    "question": "Cannot widen token scope",
                    "collection_ids": [str(DENIED_COLLECTION_ID)],
                },
            )

    assert completed.is_error is False
    assert completed.structured_content["answer"] == "Long HTTP request completed."
    assert outside_scope.is_error is True
    assert outside_scope.structured_content["error"]["code"] == "FORBIDDEN"
    assert runner.commands[0].tenant_id == TENANT_ID
    assert runner.commands[0].scope.collection_ids == (ALLOWED_COLLECTION_ID,)
    assert catalog.seen_collection_scopes == [(ALLOWED_COLLECTION_ID,)]
    assert all(len(value) == 64 for value in store.seen_hashes)
    assert FULL_TOKEN not in repr(store.seen_hashes)


def test_public_http_mcp_refuses_plain_http(
    http_mcp: tuple[Starlette, FixtureTokenStore, SlowRecordingRunner, ScopedCatalog],
) -> None:
    app, store, runner, catalog = http_mcp
    del app, runner
    application = McpApplicationService(
        KnowledgeApplication(
            QueryApiService(SlowRecordingRunner()), query_id_factory=lambda: QUERY_ID
        )
    )

    with pytest.raises(ValueError, match="HTTPS"):
        build_http_mcp_app(
            application,
            catalog,
            store,
            token_pepper=PEPPER,
            public_base_url="http://public.example",
        )


def _digest(token: str) -> str:
    return hmac.new(PEPPER.encode(), token.encode(), hashlib.sha256).hexdigest()
