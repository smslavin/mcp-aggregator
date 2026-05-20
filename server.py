"""MCP Aggregator — single SSE endpoint that proxies multiple backend MCP servers.

Startup: reads backends.json, connects to each backend via SSE, calls tools/list,
prefixes every tool as {backend_name}__{tool_name}, then serves them all from one
endpoint on port 8100.

Runtime: each tool call opens a fresh SSE session to the originating backend,
forwards the call, and streams the result back.
"""

import asyncio
import json
import logging
import os

import uvicorn
from dotenv import load_dotenv
from mcp import ClientSession, types
from mcp.client.sse import sse_client
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mcp-aggregator")

# Populated at startup. prefixed_name -> (backend_url, original_tool_name)
_tool_registry: dict[str, tuple[str, str]] = {}
_tool_list: list[types.Tool] = []


async def _discover_backend(name: str, url: str) -> int:
    async with sse_client(url, timeout=10) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            for tool in result.tools:
                prefixed = f"{name}__{tool.name}"
                _tool_registry[prefixed] = (url, tool.name)
                _tool_list.append(
                    types.Tool(
                        name=prefixed,
                        description=f"[{name}] {tool.description or ''}".strip(),
                        inputSchema=tool.inputSchema,
                    )
                )
                logger.info("  registered: %s", prefixed)
            return len(result.tools)


async def discover_all(backends: list[dict]) -> None:
    for backend in backends:
        name = backend["name"]
        url = backend["url"]
        logger.info("Discovering tools from '%s' at %s", name, url)
        try:
            count = await _discover_backend(name, url)
            logger.info("  %d tool(s) from '%s'", count, name)
        except Exception as e:
            logger.error("Failed to reach backend '%s' (%s): %s", name, url, e)


async def _proxy_call(prefixed_name: str, arguments: dict) -> types.CallToolResult:
    url, original_name = _tool_registry[prefixed_name]
    async with sse_client(url, timeout=5, sse_read_timeout=60) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(original_name, arguments)


def build_starlette_app(server: Server) -> Starlette:
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1], server.create_initialization_options()
            )
        return Response()

    return Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )


async def main() -> None:
    backends_path = os.path.join(os.path.dirname(__file__), "backends.json")
    with open(backends_path) as f:
        backends = json.load(f)

    await discover_all(backends)
    logger.info(
        "Ready — %d tool(s) aggregated from %d backend(s)",
        len(_tool_list),
        len(backends),
    )

    server = Server("mcp-aggregator")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _tool_list

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        if name not in _tool_registry:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
        try:
            return await _proxy_call(name, arguments)
        except Exception as e:
            logger.error("Tool call failed — %s: %s", name, e)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Backend error: {e}")],
                isError=True,
            )

    app = build_starlette_app(server)
    port = int(os.environ.get("AGGREGATOR_PORT", 8100))

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    userver = uvicorn.Server(config)
    logger.info("Starting aggregator on port %d", port)
    await userver.serve()


if __name__ == "__main__":
    asyncio.run(main())
