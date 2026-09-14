"""Black-box MCP stdio interoperability tests using the official SDK client."""

import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextResourceContents

from enterprise_rag.mcp.catalog import MCP_RESOURCES, MCP_TOOLS

BACKEND_ROOT = Path(__file__).parents[2]


@pytest.mark.anyio
async def test_real_stdio_client_can_list_call_and_read_without_stdout_corruption(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(BACKEND_ROOT / "tests" / "fixtures"), str(BACKEND_ROOT / "src")]
    )
    environment["ENTERPRISE_RAG_MCP_STDIO_FACTORY"] = (
        "mcp_stdio_server:build_server"
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "enterprise_rag.mcp.stdio"],
        cwd=BACKEND_ROOT,
        env=environment,
    )

    stderr_path = tmp_path / "mcp-stderr.log"
    with stderr_path.open("w+", encoding="utf-8") as stderr:
        async with Client(stdio_client(parameters, errlog=stderr)) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                item.name for item in MCP_TOOLS
            }
            assert all(
                tool.annotations and tool.annotations.read_only_hint for tool in tools.tools
            )

            resources = await client.list_resources()
            expected_resources = [
                item.uri for item in MCP_RESOURCES if item.kind == "resource"
            ]
            assert [str(resource.uri) for resource in resources.resources] == expected_resources
            templates = await client.list_resource_templates()
            assert {template.uri_template for template in templates.resource_templates} == {
                item.uri for item in MCP_RESOURCES if item.kind == "template"
            }

            query = await client.call_tool(
                "query_knowledge_base",
                {"question": "What is in this tenant?", "mode": "deep"},
            )
            assert query.is_error is False
            assert query.structured_content["status"] == "answered"
            assert query.structured_content["diagnostics"] == {"mode": "deep"}

            collections = await client.call_tool("list_collections")
            assert collections.structured_content["items"][0]["name"] == (
                "Fixture Collection"
            )

            collection_directory = await client.read_resource("rag://collections")
            assert "Fixture Collection" in _resource_text(collection_directory.contents[0])
            document = await client.read_resource(
                "rag://documents/01900000-0000-7000-8000-000000005206"
            )
            assert "Fixture Guide" in _resource_text(document.contents[0])
            section = await client.read_resource(
                "rag://documents/01900000-0000-7000-8000-000000005206/sections/root_fixture"
            )
            assert "Bounded fixture section" in _resource_text(section.contents[0])

        stderr.seek(0)
        assert "fixture-catalog-log" in stderr.read()


def test_stdio_entrypoint_requires_an_explicit_composition_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enterprise_rag.mcp.stdio import FACTORY_ENV, load_server

    monkeypatch.delenv(FACTORY_ENV, raising=False)

    with pytest.raises(RuntimeError, match=FACTORY_ENV):
        load_server()


def _resource_text(content: object) -> str:
    assert isinstance(content, TextResourceContents)
    return content.text
