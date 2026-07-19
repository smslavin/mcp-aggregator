"""MCP Aggregator — single endpoint that proxies multiple backend MCP servers.

Startup: reads backends.json, connects to each backend, calls tools/list,
prefixes every tool as {backend_name}__{tool_name}, then serves them all from one
endpoint on port 8100.

Runtime: each tool call is routed to the originating backend. Streamable HTTP
and stdio backends use a persistent pooled session (stdio subprocesses can't be
respawned per call — a "connect" tool call establishes state later calls depend
on); SSE backends open a fresh connection per call.

Serves two transports on the same port:
  GET/POST /mcp  — Streamable HTTP (Claude Desktop, modern MCP clients)
  GET      /sse  — Legacy SSE (older clients, custom chat UIs)

Management API (unauthenticated — see README for OT environment guidance):
  GET    /backends         — list active backends and tool counts
  POST   /backends         — add a backend at runtime
  DELETE /backends/{name}  — remove a backend at runtime
  POST   /backends/reload  — re-read backends.json and reconcile
"""

import asyncio
import json
import logging
import os
import sys
import uvicorn
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dotenv import load_dotenv
from mcp import ClientSession, types
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mcp-aggregator")


@dataclass
class BackendEntry:
    backend_name: str
    original_name: str
    default_args: dict
    transport: str  # "sse" | "streamable_http" | "stdio"
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None


_tool_registry: dict[str, BackendEntry] = {}
_tool_list: list[types.Tool] = []
_backends: dict[str, dict] = {}  # name -> raw config, for management API
_registry_lock = asyncio.Lock()
_session_pool: dict[str, ClientSession] = {}  # pooled (streamable_http, stdio) backends
_pool_tasks: dict[str, asyncio.Task] = {}
_shutdown_event = asyncio.Event()
_backends_path: str = ""  # set in main(), used by reload endpoint


# ---------------------------------------------------------------------------
# Transport abstraction
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _make_client(
    transport: str,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 10,
    read_timeout: float = 60,
):
    if transport == "streamable_http":
        async with streamablehttp_client(
            url, timeout=timeout, sse_read_timeout=read_timeout
        ) as (read, write, _):
            yield read, write
    elif transport == "stdio":
        params = StdioServerParameters(command=command, args=args or [], env=env)
        async with stdio_client(params) as (read, write):
            yield read, write
    else:
        async with sse_client(url, timeout=timeout, sse_read_timeout=read_timeout) as (
            read,
            write,
        ):
            yield read, write


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _backend_desc(backend: dict) -> str:
    if backend.get("transport") == "stdio":
        return " ".join([backend.get("command", "")] + (backend.get("args") or []))
    return backend.get("url", "")


async def _discover_backend(backend: dict) -> int:
    name = backend["name"]
    transport = backend.get("transport", "sse")
    url = backend.get("url")
    command = backend.get("command")
    args = backend.get("args")
    env = backend.get("env")

    include = set(backend.get("include_tools") or [])
    exclude = set(backend.get("exclude_tools") or [])
    if include and exclude:
        raise ValueError(
            f"Backend '{name}': include_tools and exclude_tools are mutually exclusive"
        )

    default_args: dict = backend.get("default_args") or {}

    async with _make_client(
        transport, url=url, command=command, args=args, env=env, timeout=10
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            registered = 0
            async with _registry_lock:
                _backends[name] = backend
                for tool in result.tools:
                    if include and tool.name not in include:
                        continue
                    if exclude and tool.name in exclude:
                        continue
                    prefixed = f"{name}__{tool.name}"
                    _tool_registry[prefixed] = BackendEntry(
                        backend_name=name,
                        original_name=tool.name,
                        default_args=default_args,
                        transport=transport,
                        url=url,
                        command=command,
                        args=args,
                        env=env,
                    )
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
        desc = _backend_desc(backend)
        logger.info("Discovering tools from '%s' at %s", name, desc)
        try:
            count = await _discover_backend(backend)
            logger.info("  %d tool(s) from '%s'", count, name)
        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to reach backend '%s' (%s): %s", name, desc, e)


# ---------------------------------------------------------------------------
# Connection pool (Streamable HTTP and stdio backends — anything that needs
# connection state preserved across calls)
#
# TODO: Research connection pooling for SSE backends. SSE requires a
# long-running read loop, so a persistent session likely needs a per-backend
# asyncio.Queue to serialize calls onto the single open stream.
# ---------------------------------------------------------------------------

_POOLED_TRANSPORTS = ("streamable_http", "stdio")


async def _run_persistent_pool(name: str, backend: dict) -> None:
    """Keep one persistent ClientSession open for a backend that needs
    connection state preserved across calls (Streamable HTTP, stdio).

    Reconnects automatically on drop (HTTP) or respawns the subprocess
    (stdio). Exits cleanly when _shutdown_event fires.
    """
    transport = backend.get("transport", "sse")
    backoff = 2.0
    while True:
        try:
            async with _make_client(
                transport,
                url=backend.get("url"),
                command=backend.get("command"),
                args=backend.get("args"),
                env=backend.get("env"),
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    _session_pool[name] = session
                    logger.info("Pool ready: '%s' (%s)", name, transport)
                    await _shutdown_event.wait()
                    return
        except asyncio.CancelledError:
            return
        except Exception as e:
            _session_pool.pop(name, None)
            logger.warning(
                "Pool '%s' dropped, reconnecting in %.0fs: %s", name, backoff, e
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(_shutdown_event.wait()), timeout=backoff
                )
                return
            except asyncio.TimeoutError:
                pass


async def _start_pool_tasks(backends: list[dict]) -> None:
    for backend in backends:
        if backend.get("transport") in _POOLED_TRANSPORTS:
            name = backend["name"]
            task = asyncio.create_task(_run_persistent_pool(name, backend))
            _pool_tasks[name] = task


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


async def _proxy_call(prefixed_name: str, arguments: dict) -> types.CallToolResult:
    entry = _tool_registry[prefixed_name]
    merged = {**entry.default_args, **arguments}

    session = _session_pool.get(entry.backend_name)
    if session is not None:
        return await session.call_tool(entry.original_name, merged)

    # SSE backends, or pooled backends whose pool session isn't ready yet
    async with _make_client(
        entry.transport,
        url=entry.url,
        command=entry.command,
        args=entry.args,
        env=entry.env,
        timeout=5,
        read_timeout=60,
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(entry.original_name, merged)


# ---------------------------------------------------------------------------
# Management API handlers
# ---------------------------------------------------------------------------


async def _remove_backend(name: str) -> int:
    async with _registry_lock:
        keys = [k for k in _tool_registry if k.startswith(f"{name}__")]
        for k in keys:
            del _tool_registry[k]
        _tool_list[:] = [t for t in _tool_list if not t.name.startswith(f"{name}__")]
        _backends.pop(name, None)

    task = _pool_tasks.pop(name, None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _session_pool.pop(name, None)

    return len(keys)


async def handle_backends(request: Request) -> JSONResponse:
    if request.method == "GET":
        async with _registry_lock:
            counts: dict[str, int] = {}
            for entry in _tool_registry.values():
                counts[entry.backend_name] = counts.get(entry.backend_name, 0) + 1
            result = [
                {
                    "name": name,
                    "url": cfg.get("url"),
                    "command": cfg.get("command"),
                    "transport": cfg.get("transport", "sse"),
                    "tools": counts.get(name, 0),
                    "pooled": name in _session_pool,
                }
                for name, cfg in _backends.items()
            ]
        return JSONResponse({"backends": result, "total_tools": len(_tool_registry)})

    # POST — add a new backend
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    name = body.get("name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if name in _backends:
        return JSONResponse(
            {"error": f"Backend '{name}' already exists — remove it first"},
            status_code=409,
        )

    try:
        count = await _discover_backend(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Discovery failed: {e}"}, status_code=502)

    if body.get("transport") in _POOLED_TRANSPORTS:
        task = asyncio.create_task(_run_persistent_pool(name, body))
        _pool_tasks[name] = task

    logger.info("Management: added backend '%s' (%d tools)", name, count)
    return JSONResponse({"added": count}, status_code=201)


async def handle_remove_backend(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    if name not in _backends:
        return JSONResponse({"error": f"Backend '{name}' not found"}, status_code=404)

    removed = await _remove_backend(name)
    logger.info("Management: removed backend '%s' (%d tools)", name, removed)
    return JSONResponse({"removed": removed})


async def handle_reload_backends(request: Request) -> JSONResponse:
    if not _backends_path:
        return JSONResponse({"error": "backends_path not configured"}, status_code=500)

    try:
        with open(_backends_path) as f:
            new_backends = json.load(f)
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not read backends file: {e}"}, status_code=500
        )

    new_names = {b["name"] for b in new_backends}
    old_names = set(_backends.keys())

    removed_total = 0
    for name in old_names - new_names:
        removed_total += await _remove_backend(name)
        logger.info("Management: reload removed backend '%s'", name)

    added_total = 0
    errors = []
    for backend in new_backends:
        if backend["name"] not in old_names:
            try:
                count = await _discover_backend(backend)
                added_total += count
                if backend.get("transport") in _POOLED_TRANSPORTS:
                    task = asyncio.create_task(
                        _run_persistent_pool(backend["name"], backend)
                    )
                    _pool_tasks[backend["name"]] = task
                logger.info(
                    "Management: reload added backend '%s' (%d tools)",
                    backend["name"],
                    count,
                )
            except Exception as e:
                errors.append({"backend": backend["name"], "error": str(e)})

    return JSONResponse(
        {"added_tools": added_total, "removed_tools": removed_total, "errors": errors}
    )


# ---------------------------------------------------------------------------
# Starlette app
# ---------------------------------------------------------------------------


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
        _shutdown_event.set()
        if _pool_tasks:
            await asyncio.gather(*_pool_tasks.values(), return_exceptions=True)

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
            # Management API — /backends/reload must come before /backends/{name}
            Route(
                "/backends/reload", endpoint=handle_reload_backends, methods=["POST"]
            ),
            Route(
                "/backends/{name}", endpoint=handle_remove_backend, methods=["DELETE"]
            ),
            Route("/backends", endpoint=handle_backends, methods=["GET", "POST"]),
        ],
    )
    return fix_accept_header


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    global _backends_path

    backends_file = os.environ.get("BACKENDS_FILE", "backends.json")
    _backends_path = (
        backends_file
        if os.path.isabs(backends_file)
        else os.path.join(os.path.dirname(__file__), backends_file)
    )
    with open(_backends_path) as f:
        backends = json.load(f)

    await discover_all(backends)
    await _start_pool_tasks(backends)
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
