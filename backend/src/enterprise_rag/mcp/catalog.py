"""Sanitized MCP capability metadata shared by the SDK server and workspace UI."""

import os
from dataclasses import dataclass

MCP_SERVER_NAME = "enterprise-agentic-rag-v6"
MCP_SERVER_VERSION = "0.1.0"
STDIO_FACTORY_ENV = "ENTERPRISE_RAG_MCP_STDIO_FACTORY"


@dataclass(frozen=True, slots=True)
class McpToolDefinition:
    name: str
    description: str
    protocol_description: str
    required_scopes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "required_scopes": list(self.required_scopes),
            "read_only": True,
        }


@dataclass(frozen=True, slots=True)
class McpResourceDefinition:
    uri: str
    kind: str
    description: str
    protocol_description: str
    required_scopes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "kind": self.kind,
            "description": self.description,
            "required_scopes": list(self.required_scopes),
        }


QUERY_KNOWLEDGE_TOOL = McpToolDefinition(
    "query_knowledge_base",
    "在授权知识范围内回答问题并返回引用",
    "Answer a question from authorized knowledge with citations.",
    ("mcp:access", "query:execute"),
)
SEARCH_DOCUMENTS_TOOL = McpToolDefinition(
    "search_documents",
    "在授权文档中执行 Dense、Sparse 或 Hybrid 搜索",
    "Search authorized documents without generating an answer.",
    ("mcp:access", "knowledge:read"),
)
LIST_COLLECTIONS_TOOL = McpToolDefinition(
    "list_collections",
    "列出当前令牌可见的集合",
    "List collections visible to the authenticated tenant.",
    ("mcp:access", "knowledge:read"),
)
GET_DOCUMENT_SUMMARY_TOOL = McpToolDefinition(
    "get_document_summary",
    "读取授权文档的有限元数据摘要",
    "Read an authorized document's bounded metadata summary.",
    ("mcp:access", "knowledge:read"),
)
LIST_DOCUMENT_SECTIONS_TOOL = McpToolDefinition(
    "list_document_sections",
    "分页读取授权文档的 Root 区段摘要",
    "List bounded section summaries for an authorized document.",
    ("mcp:access", "knowledge:read"),
)
VERIFY_ANSWER_TOOL = McpToolDefinition(
    "verify_answer",
    "根据授权证据核验外部答案的引用",
    "Verify submitted citations against authorized evidence without storing input.",
    ("mcp:access", "answer:verify"),
)
MCP_TOOLS = (
    QUERY_KNOWLEDGE_TOOL,
    SEARCH_DOCUMENTS_TOOL,
    LIST_COLLECTIONS_TOOL,
    GET_DOCUMENT_SUMMARY_TOOL,
    LIST_DOCUMENT_SECTIONS_TOOL,
    VERIFY_ANSWER_TOOL,
)

COLLECTIONS_RESOURCE = McpResourceDefinition(
    "rag://collections",
    "resource",
    "授权集合目录",
    "Authorized collection directory.",
    ("mcp:access", "knowledge:read"),
)
COLLECTION_RESOURCE = McpResourceDefinition(
    "rag://collections/{collection_id}",
    "template",
    "授权集合元数据",
    "Authorized collection metadata.",
    ("mcp:access", "knowledge:read"),
)
DOCUMENT_RESOURCE = McpResourceDefinition(
    "rag://documents/{document_id}",
    "template",
    "授权文档元数据",
    "Authorized document metadata.",
    ("mcp:access", "knowledge:read"),
)
SECTION_RESOURCE = McpResourceDefinition(
    "rag://documents/{document_id}/sections/{root_id}",
    "template",
    "授权 Root 区段内容",
    "One authorized, length-bounded Root section.",
    ("mcp:access", "knowledge:read"),
)
MCP_RESOURCES = (
    COLLECTIONS_RESOURCE,
    COLLECTION_RESOURCE,
    DOCUMENT_RESOURCE,
    SECTION_RESOURCE,
)


@dataclass(frozen=True, slots=True)
class McpCapabilityCatalog:
    """A public, non-secret description of the current MCP composition."""

    stdio_factory_declared: bool
    http_mounted_endpoint: str | None = None

    def to_dict(self) -> dict[str, object]:
        http_mounted = self.http_mounted_endpoint is not None
        return {
            "server_name": MCP_SERVER_NAME,
            "server_version": MCP_SERVER_VERSION,
            "tools": [item.to_dict() for item in MCP_TOOLS],
            "resources": [item.to_dict() for item in MCP_RESOURCES],
            "transports": [
                {
                    "name": "stdio",
                    "status": (
                        "factory_declared" if self.stdio_factory_declared else "requires_factory"
                    ),
                    "endpoint": None,
                    "detail": (
                        "当前进程已声明独立 stdio 组合函数；可用性由 MCP 客户端连接验证"
                        if self.stdio_factory_declared
                        else "需要通过 ENTERPRISE_RAG_MCP_STDIO_FACTORY 提供组合函数"
                    ),
                },
                {
                    "name": "streamable_http",
                    "status": "mounted" if http_mounted else "external_composition_required",
                    "endpoint": self.http_mounted_endpoint,
                    "detail": (
                        "当前组合已声明挂载 Streamable HTTP MCP"
                        if http_mounted
                        else "当前 Mac API 进程未挂载；公网部署时由 HTTPS MCP 组合提供"
                    ),
                },
            ],
        }

    @classmethod
    def from_environment(cls) -> "McpCapabilityCatalog":
        return cls(stdio_factory_declared=bool(os.environ.get(STDIO_FACTORY_ENV, "").strip()))
