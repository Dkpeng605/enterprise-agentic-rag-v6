"""stdio process entry point; stdout is reserved for MCP JSON-RPC."""

import asyncio
import importlib
import logging
import os
import sys
from collections.abc import Callable

from mcp.server import MCPServer

FACTORY_ENV = "ENTERPRISE_RAG_MCP_STDIO_FACTORY"
ServerFactory = Callable[[], MCPServer[None]]


def load_server() -> MCPServer[None]:
    factory_path = os.environ.get(FACTORY_ENV, "").strip()
    if ":" not in factory_path:
        raise RuntimeError(f"{FACTORY_ENV} must use module:function syntax")
    module_name, function_name = factory_path.split(":", 1)
    module = importlib.import_module(module_name)
    factory = getattr(module, function_name, None)
    if not callable(factory):
        raise RuntimeError("configured MCP stdio factory is not callable")
    server = factory()
    if not isinstance(server, MCPServer):
        raise RuntimeError("configured MCP stdio factory did not return MCPServer")
    return server


async def run() -> None:
    await load_server().run_stdio_async()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    # The SDK diverts fd 1 while serving. Disable delayed text buffering so an
    # accidental print cannot flush onto the JSON-RPC wire after fd restoration.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(line_buffering=True, write_through=True)
    try:
        asyncio.run(run())
    except Exception as error:
        logging.error("MCP stdio startup failed: %s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
