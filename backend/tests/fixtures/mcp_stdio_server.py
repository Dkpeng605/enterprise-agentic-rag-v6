"""Deterministic MCP stdio process used only by the protocol contract test."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from mcp.server import MCPServer

from enterprise_rag.mcp import build_mcp_server
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

QUERY_ID = UUID("01900000-0000-7000-8000-000000005201")
SESSION_ID = UUID("01900000-0000-7000-8000-000000005202")
TENANT_ID = UUID("01900000-0000-7000-8000-000000005203")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000005204")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005205")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000005206")


class FixtureRunner:
    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ANSWERED,
            "The fixture answer is tenant scoped.",
            (),
            {"mode": command.mode.value},
            {"llm_calls": 1, "input_tokens": 8, "output_tokens": 7},
        )


class FixtureCatalog:
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
        return {
            "items": [{"document_id": str(DOCUMENT_ID), "title": "Fixture Guide"}],
            "strategy": strategy,
            "tenant_id": str(principal.tenant_id),
        }

    async def list_collections(
        self, principal: Principal, *, collection_ids: tuple[UUID, ...] | None
    ) -> Sequence[Mapping[str, object]]:
        # The SDK must divert accidental application output away from protocol stdout.
        print("fixture-catalog-log")
        return ({"id": str(COLLECTION_ID), "name": "Fixture Collection"},)

    async def get_document_summary(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        return {
            "id": str(document_id),
            "title": "Fixture Guide",
            "tenant_id": str(principal.tenant_id),
        }

    async def list_document_sections(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        return {
            "items": [{"root_id": "root_fixture", "heading": "Overview"}],
            "next_cursor": None,
        }

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
        return {"id": str(collection_id), "name": "Fixture Collection"}

    async def get_section_resource(
        self,
        principal: Principal,
        document_id: UUID,
        root_id: str,
        *,
        collection_ids: tuple[UUID, ...] | None,
    ) -> Mapping[str, object]:
        return {
            "document_id": str(document_id),
            "root_id": root_id,
            "text": "Bounded fixture section.",
        }


def build_server() -> MCPServer[None]:
    knowledge = KnowledgeApplication(
        QueryApiService(FixtureRunner()), query_id_factory=lambda: QUERY_ID
    )
    return build_mcp_server(
        McpApplicationService(knowledge),
        FixtureCatalog(),
        Principal(SESSION_ID, TENANT_ID, ACTOR_ID),
    )
