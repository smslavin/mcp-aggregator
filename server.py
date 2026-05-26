"""MCP Aggregator — single endpoint that proxies multiple backend MCP servers.

Startup: reads backends.json, connects to each backend via SSE, calls tools/list,
prefixes every tool as {backend_name}__{tool_name}, then serves them all from one
endpoint on port 8100.

Runtime: each tool call opens a fresh SSE session to the originating backend,
forwards the call, and streams the result back.

Serves two transports on the same port:
  GET/POST /mcp  — Streamable HTTP (Claude Desktop, modern MCP clients)
  GET      /sse  — Legacy SSE (older clients, custom chat UIs)
"""

import asyncio
import json
import logging
import os
import sys
import uvicorn
from dotenv import load_dotenv
from mcp import ClientSession, types
from mcp.client.sse import sse_client
from mcp.server import Server
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
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

# Populated at startup. prefixed_name -> (backend_url, original_tool_name, default_args)
_tool_registry: dict[str, tuple[str, str, dict]] = {}
_tool_list: list[types.Tool] = []


async def _discover_backend(backend: dict) -> int:
    name = backend["name"]
    url = backend["url"]

    include = set(backend.get("include_tools") or [])
    exclude = set(backend.get("exclude_tools") or [])
    if include and exclude:
        raise ValueError(
            f"Backend '{name}': include_tools and exclude_tools are mutually exclusive"
        )

    default_args: dict = backend.get("default_args") or {}

    async with sse_client(url, timeout=10) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            registered = 0
            for tool in result.tools:
                if include and tool.name not in include:
                    continue
                if exclude and tool.name in exclude:
                    continue
                prefixed = f"{name}__{tool.name}"
                _tool_registry[prefixed] = (url, tool.name, default_args)
                _tool_list.append(
                    types.Tool(
                        name=prefixed,
                        description=f"[{name}] {tool.description or ''}".strip(),
                        inputSchema=tool.inputSchema,
                    )
                )
                logger.info("  registered: %s", prefixed)
                registered += 1
            return registered


async def discover_all(backends: list[dict]) -> None:
    for backend in backends:
        name = backend["name"]
        url = backend["url"]
        logger.info("Discovering tools from '%s' at %s", name, url)
        try:
            count = await _discover_backend(backend)
            logger.info("  %d tool(s) from '%s'", count, name)
        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to reach backend '%s' (%s): %s", name, url, e)


async def _proxy_call(prefixed_name: str, arguments: dict) -> types.CallToolResult:
    url, original_name, default_args = _tool_registry[prefixed_name]
    merged = {**default_args, **arguments}
    async with sse_client(url, timeout=5, sse_read_timeout=60) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(original_name, merged)


def build_starlette_app(server: Server) -> Starlette:
    # --- Streamable HTTP transport (Claude Desktop, modern clients) ---
    session_manager = StreamableHTTPSessionManager(app=server, stateless=False)
    streamable_app = StreamableHTTPASGIApp(session_manager)

    # --- Legacy SSE transport (older clients, custom chat UIs) ---
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1], server.create_initialization_options()
            )
        return Response()

    async def lifespan(app):
        async with session_manager.run():
            yield

    async def fix_accept_header(scope, receive, send):
        """Inject Accept: text/event-stream on GET /mcp when the client omits it.

        Claude Desktop sends a GET to open the notification stream but may not
        include the required Accept header, causing a 406. We patch it here so
        the StreamableHTTP handler sees the correct value.
        """
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path") == "/mcp"
        ):
            headers = list(scope.get("headers", []))
            accept_vals = [v for k, v in headers if k == b"accept"]
            if not accept_vals or b"text/event-stream" not in accept_vals[0]:
                logger.info("Injecting Accept: text/event-stream for GET /mcp")
                headers = [(k, v) for k, v in headers if k != b"accept"]
                headers.append((b"accept", b"text/event-stream"))
                scope = dict(scope, headers=headers)
        await starlette_inner(scope, receive, send)

    starlette_inner = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/mcp", endpoint=streamable_app, methods=["GET", "POST", "DELETE"]),
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
    return fix_accept_header


async def main() -> None:
    backends_file = os.environ.get("BACKENDS_FILE", "backends.json")
    backends_path = (
        backends_file if os.path.isabs(backends_file)
        else os.path.join(os.path.dirname(__file__), backends_file)
    )
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

    if "--stdio" in sys.argv:
        from mcp.server.stdio import stdio_server

        logger.info("Starting in stdio mode")
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    else:
        app = build_starlette_app(server)
        port = int(os.environ.get("AGGREGATOR_PORT", 8100))
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
        userver = uvicorn.Server(config)
        logger.info("Starting aggregator on port %d", port)
        await userver.serve()


if __name__ == "__main__":
    asyncio.run(main())
