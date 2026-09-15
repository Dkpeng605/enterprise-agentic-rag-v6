"""Use the official MCP client against the live Mac endpoint without printing secrets/content."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextResourceContents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--query", default="文档主要介绍了什么？")
    parser.add_argument("--run-query", action="store_true")
    arguments = parser.parse_args()
    token = arguments.token_file.read_text(encoding="utf-8").strip()
    if not 32 <= len(token) <= 512:
        raise SystemExit("token file does not contain one valid MCP bearer token")
    result = asyncio.run(
        _smoke(
            endpoint=arguments.endpoint,
            token=token,
            query=arguments.query,
            run_query=arguments.run_query,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


async def _smoke(
    *, endpoint: str, token: str, query: str, run_query: bool
) -> dict[str, object]:
    http = httpx2.AsyncClient(
        headers={"authorization": f"Bearer {token}"},
        timeout=180,
    )
    async with http, Client(
        streamable_http_client(endpoint, http_client=http, terminate_on_close=False)
    ) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        collections = await client.call_tool("list_collections")
        searched = await client.call_tool(
            "search_documents",
            {"query": query, "strategy": "hybrid", "top_k": 3},
        )
        items = _items(searched.structured_content)
        search_error = _error_code(searched.structured_content)
        document_ok = False
        resource_ok = False
        verification_ok = False
        if items:
            first = items[0]
            document_id = first.get("document_id")
            root_id = first.get("root_id")
            leaf_id = first.get("leaf_id")
            if all(isinstance(value, str) for value in (document_id, root_id, leaf_id)):
                summary = await client.call_tool(
                    "get_document_summary", {"document_id": document_id}
                )
                sections = await client.call_tool(
                    "list_document_sections", {"document_id": document_id, "limit": 3}
                )
                resource = await client.read_resource(
                    f"rag://documents/{document_id}/sections/{root_id}"
                )
                document_ok = not summary.is_error and bool(
                    _items(sections.structured_content)
                )
                resource_ok = bool(resource.contents)
                quote = _resource_quote(resource.contents[0]) if resource.contents else None
                if quote is not None:
                    verified = await client.call_tool(
                        "verify_answer",
                        {
                            "answer": "MCP smoke",
                            "citations": [
                                {
                                    "id": 1,
                                    "document_id": document_id,
                                    "root_id": root_id,
                                    "chunk_ids": [leaf_id],
                                    "quote": quote,
                                }
                            ],
                        },
                    )
                    verification_ok = bool(
                        (verified.structured_content or {}).get("valid")
                    )
        query_status: str | None = None
        if run_query:
            queried = await client.call_tool(
                "query_knowledge_base", {"question": query, "mode": "standard"}
            )
            query_status = str((queried.structured_content or {}).get("status"))
        return {
            "endpoint": endpoint,
            "tool_count": len(tools.tools),
            "resource_count": len(resources.resources),
            "resource_template_count": len(templates.resource_templates),
            "collection_count": len(_items(collections.structured_content)),
            "search_hit_count": len(items),
            "search_error": search_error,
            "document_ok": document_ok,
            "resource_ok": resource_ok,
            "verification_ok": verification_ok,
            "query_status": query_status,
        }


def _items(payload: dict[str, object] | None) -> list[dict[str, object]]:
    if payload is None:
        return []
    value = payload.get("items")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _resource_quote(content: object) -> str | None:
    if not isinstance(content, TextResourceContents):
        return None
    try:
        payload = json.loads(content.text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text[: min(24, len(text))]


def _error_code(payload: dict[str, object] | None) -> str | None:
    if payload is None:
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


if __name__ == "__main__":
    main()
