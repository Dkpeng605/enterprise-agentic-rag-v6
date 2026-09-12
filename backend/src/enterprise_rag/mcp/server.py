"""Official MCP SDK v2 server exposing read-only knowledge tools and resources."""

import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Protocol
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryMode, QueryScope
from enterprise_rag.services.auth import Principal
from enterprise_rag.services.knowledge import KnowledgeQuery, McpApplicationService

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


class McpCatalog(Protocol):
    async def search_documents(
        self,
        principal: Principal,
        *,
        query: str,
        strategy: str,
        top_k: int,
        filters: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    async def list_collections(self, principal: Principal) -> Sequence[Mapping[str, object]]: ...

    async def get_document_summary(
        self, principal: Principal, document_id: UUID
    ) -> Mapping[str, object]: ...

    async def list_document_sections(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, object]: ...

    async def verify_answer(
        self,
        principal: Principal,
        *,
        answer: str,
        citations: Sequence[Mapping[str, object]],
        question: str | None,
    ) -> Mapping[str, object]: ...

    async def get_collection_resource(
        self, principal: Principal, collection_id: UUID
    ) -> Mapping[str, object]: ...

    async def get_section_resource(
        self, principal: Principal, document_id: UUID, root_id: str
    ) -> Mapping[str, object]: ...


def build_mcp_server(
    application: McpApplicationService,
    catalog: McpCatalog,
    principal: Principal,
) -> MCPServer[None]:
    server: MCPServer[None] = MCPServer(
        "enterprise-agentic-rag-v6",
        version="0.1.0",
        instructions=(
            "Use read-only knowledge tools. Results are restricted to the authenticated tenant."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    async def query_knowledge_base(
        question: Annotated[str, Field(min_length=1, max_length=2_000)],
        mode: Literal["standard", "deep"] = "standard",
        collection_ids: Annotated[list[str], Field(max_length=100)] | None = None,
    ) -> CallToolResult:
        """Answer a question from authorized knowledge with citations."""

        try:
            scope = QueryScope(collection_ids=tuple(_uuids(collection_ids or ())))
            execution = await application.query_knowledge_base(
                principal,
                KnowledgeQuery(question.strip(), QueryMode(mode), scope),
            )
            payload = execution.to_dict()
            return _result(execution.answer, payload)
        except Exception as error:
            return _error(error)

    @server.tool(annotations=READ_ONLY)
    async def search_documents(
        query: Annotated[str, Field(min_length=1, max_length=2_000)],
        strategy: Literal["dense", "sparse", "hybrid"] = "hybrid",
        top_k: Annotated[int, Field(ge=1, le=20)] = 10,
        filters: dict[str, object] | None = None,
    ) -> CallToolResult:
        """Search authorized documents without generating an answer."""

        try:
            payload = dict(
                await catalog.search_documents(
                    principal,
                    query=query.strip(),
                    strategy=strategy,
                    top_k=top_k,
                    filters=filters or {},
                )
            )
            return _result(f"Found {len(_items(payload))} authorized results.", payload)
        except Exception as error:
            return _error(error)

    @server.tool(annotations=READ_ONLY)
    async def list_collections() -> CallToolResult:
        """List collections visible to the authenticated tenant."""

        try:
            payload = {"items": [dict(item) for item in await catalog.list_collections(principal)]}
            return _result(f"Found {len(payload['items'])} collections.", payload)
        except Exception as error:
            return _error(error)

    @server.tool(annotations=READ_ONLY)
    async def get_document_summary(document_id: str) -> CallToolResult:
        """Read an authorized document's bounded metadata summary."""

        try:
            payload = dict(await catalog.get_document_summary(principal, UUID(document_id)))
            return _result(f"Document: {payload.get('title', 'authorized document')}", payload)
        except Exception as error:
            return _error(error)

    @server.tool(annotations=READ_ONLY)
    async def list_document_sections(
        document_id: str,
        cursor: Annotated[str | None, Field(max_length=1_000)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> CallToolResult:
        """List bounded section summaries for an authorized document."""

        try:
            payload = dict(
                await catalog.list_document_sections(
                    principal, UUID(document_id), cursor=cursor, limit=limit
                )
            )
            return _result(f"Found {len(_items(payload))} sections.", payload)
        except Exception as error:
            return _error(error)

    @server.tool(annotations=READ_ONLY)
    async def verify_answer(
        answer: Annotated[str, Field(min_length=1, max_length=20_000)],
        citations: Annotated[list[dict[str, object]], Field(max_length=100)] | None = None,
        question: Annotated[str | None, Field(max_length=2_000)] = None,
    ) -> CallToolResult:
        """Verify submitted citations against authorized evidence without storing input."""

        try:
            payload = dict(
                await catalog.verify_answer(
                    principal,
                    answer=answer,
                    citations=citations or (),
                    question=question,
                )
            )
            return _result("Answer verification completed.", payload)
        except Exception as error:
            return _error(error)

    @server.resource("rag://collections", mime_type="application/json")
    async def collections_resource() -> str:
        """Authorized collection directory."""

        items = [dict(item) for item in await catalog.list_collections(principal)]
        return _json({"items": items})

    @server.resource("rag://collections/{collection_id}", mime_type="application/json")
    async def collection_resource(collection_id: str) -> str:
        """Authorized collection metadata."""

        return _json(await catalog.get_collection_resource(principal, UUID(collection_id)))

    @server.resource("rag://documents/{document_id}", mime_type="application/json")
    async def document_resource(document_id: str) -> str:
        """Authorized document metadata."""

        return _json(await catalog.get_document_summary(principal, UUID(document_id)))

    @server.resource(
        "rag://documents/{document_id}/sections/{root_id}", mime_type="application/json"
    )
    async def section_resource(document_id: str, root_id: str) -> str:
        """One authorized, length-bounded Root section."""

        return _json(
            await catalog.get_section_resource(principal, UUID(document_id), root_id)
        )

    return server


def _result(message: str, payload: Mapping[str, object]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=f"{message}\n\n```json\n{_json(payload)}\n```")],
        structured_content=dict(payload),
    )


def _error(error: Exception) -> CallToolResult:
    code = error.code if isinstance(error, AppError) else ErrorCode.INTERNAL_ERROR
    payload: dict[str, object] = {
        "error": {"code": code.value, "message": "The knowledge request failed."}
    }
    return CallToolResult(
        content=[TextContent(type="text", text=f"Knowledge request failed ({code.value}).")],
        structured_content=payload,
        is_error=True,
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _uuids(values: Sequence[str]) -> list[UUID]:
    return [UUID(value) for value in values]


def _items(payload: Mapping[str, object]) -> Sequence[object]:
    value = payload.get("items")
    return value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()
