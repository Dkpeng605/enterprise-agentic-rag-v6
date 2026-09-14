import pytest

from enterprise_rag.mcp.catalog import MCP_RESOURCES, MCP_TOOLS, McpCapabilityCatalog


def test_mcp_catalog_matches_the_registered_read_only_surface() -> None:
    catalog = McpCapabilityCatalog(stdio_factory_declared=False).to_dict()

    assert catalog["server_name"] == "enterprise-agentic-rag-v6"
    assert [item["name"] for item in catalog["tools"]] == [item.name for item in MCP_TOOLS]
    assert [item["uri"] for item in catalog["resources"]] == [
        item.uri for item in MCP_RESOURCES
    ]
    assert all(item["read_only"] is True for item in catalog["tools"])
    assert {item["status"] for item in catalog["transports"]} == {
        "requires_factory",
        "external_composition_required",
    }


def test_mounted_http_transport_is_explicit_and_does_not_expose_secrets() -> None:
    payload = McpCapabilityCatalog(
        stdio_factory_declared=True,
        http_mounted_endpoint="https://mcp.example.test/mcp",
    ).to_dict()

    transports = {item["name"]: item for item in payload["transports"]}
    assert transports["stdio"]["status"] == "factory_declared"
    assert transports["streamable_http"]["status"] == "mounted"
    assert transports["streamable_http"]["endpoint"] == "https://mcp.example.test/mcp"
    assert "token" not in repr(payload).lower()


@pytest.mark.parametrize("key", ["api_key", "authorization", "prompt"])
def test_mcp_catalog_has_no_sensitive_configuration_fields(key: str) -> None:
    assert all(key not in item.to_dict() for item in MCP_TOOLS)
