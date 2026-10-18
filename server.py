"""MCP Aggregator — single endpoint that proxies multiple backend MCP servers.

Startup: reads backends.json, discovers every backend's tools at once (each
within DISCOVERY_TIMEOUT_S), prefixes every tool as {backend_name}__{tool_name},
then serves them all from one endpoint on port 8100. A backend that doesn't
answer is retried in the background; its tools appear once it does, and
connected clients are told the tool list changed.

Runtime: each tool call is routed to the originating backend. Streamable HTTP
and stdio backends use a persistent pooled session (stdio subprocesses can't be
respawned per call — a "connect" tool call establishes state later calls depend
on). A call to a pooled backend whose session is down waits up to POOL_WAIT_S
for it to come back, then reports the backend as reconnecting. SSE backends
open a fresh connection per call.

Serves two transports on the same port:
  GET/POST /mcp  — Streamable HTTP (Claude Desktop, modern MCP clients)
  GET      /sse  — Legacy SSE (older clients, custom chat UIs)

Management API (same token rule as the MCP endpoints, see "Network access" below):
  GET    /backends         — list backends: status, tool counts, last error
  POST   /backends         — add a backend at runtime
  DELETE /backends/{name}  — remove a backend at runtime
  POST   /backends/reload  — re-read backends.json and reconcile
  POST   /backends/{name}/refresh — list a backend's tools again, e.g. after
                           it restarted with new ones; it keeps its tools if
                           it doesn't answer
  POST   /backends/refresh — the same for every backend

Network access:
  AGGREGATOR_BIND_HOST   Default 127.0.0.1. A non-loopback address requires
                         AGGREGATOR_AUTH_TOKEN, or the server refuses to start.
  AGGREGATOR_READ_ONLY   1/true/yes hides and refuses every backend tool not
                         annotated readOnlyHint=true. Tool annotations are
                         forwarded to clients either way.
  AGGREGATOR_AUTH_TOKEN  When set, every request must send
                         "Authorization: Bearer <token>". The aggregator exposes
                         backend write tools with no confirmation step, so it
                         must never be reachable without one.
  AGGREGATOR_ALLOWED_ORIGINS
                         Comma-separated browser origins allowed to call the
                         aggregator (e.g. http://localhost:6274 for the MCP
                         Inspector). Default none: requests a web page makes
                         are refused, and on a loopback bind so is any Host
                         other than 127.0.0.1/localhost/[::1].
"""

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import sys
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anyio
import httpx2
import uvicorn
from dotenv import load_dotenv
from mcp import ClientSession, MCPError, types
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.connection import Connection
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.session import ServerSession
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.subscriptions import ToolsListChanged
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

_POOLED_TRANSPORTS = ("streamable_http", "stdio")

# Timeouts. Discovery is bounded so a backend that accepts the connection and
# then hangs can't hold up startup or a reload. A pooled backend whose session
# is down gets POOL_WAIT_S to come back before a call to it is refused.
DISCOVERY_TIMEOUT_S = 15.0
CALL_TIMEOUT_S = 60.0
POOL_WAIT_S = 5.0


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
    read_only: bool = False


@dataclass(eq=False)
class BackendState:
    """Everything the aggregator knows about one backend.

    config is the entry as given. source is where it came from: "file" (the
    backends file, which reload reconciles) or "api" (POST /backends, which
    reload leaves alone unless the file now names it). status is "pending"
    until discovery succeeds, then "ready". A pooled backend also has a pool
    task keeping one session open; session is that session while it's up,
    and session_ready is set while it is.
    """

    config: dict
    source: str
    status: str = "pending"
    tools: int = 0
    last_error: str | None = None
    last_attempt: float | None = None  # wall-clock time of the last discovery
    last_attempt_s: float | None = None  # and how long it took
    pool_task: asyncio.Task | None = None
    session: ClientSession | None = None
    session_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when a call finds the backend no longer knows the pool's session.
    reconnect: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def name(self) -> str:
        return self.config["name"]

    @property
    def transport(self) -> str:
        return self.config.get("transport", "sse")

    @property
    def pooled(self) -> bool:
        return self.transport in _POOLED_TRANSPORTS


_tool_registry: dict[str, BackendEntry] = {}
_tool_list: list[types.Tool] = []
_registry_lock = asyncio.Lock()
_states: dict[str, BackendState] = {}  # by backend name
_shutdown_event = asyncio.Event()
_backends_path: str = ""  # set in main(), used by reload endpoint


def _reason(exc: BaseException) -> str:
    """The real error(s) as text, from inside anyio's exception groups.

    Everything raised inside a ClientSession arrives wrapped, and the group's
    own message is only "unhandled errors in a TaskGroup (1 sub-exception)".
    """
    leaves: list[BaseException] = []

    def collect(e: BaseException) -> None:
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                collect(sub)
        else:
            leaves.append(e)

    collect(exc)
    return "; ".join(
        f"{type(e).__name__}: {e}" if str(e) else type(e).__name__ for e in leaves
    )


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
        # streamable_http_client dropped its own timeout kwargs in mcp 2.0 —
        # it now takes an optional pre-configured http_client instead, and
        # doesn't manage that client's lifecycle when we provide one
        # ourselves, so we own the `async with` here.
        http_client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(
                connect=timeout,
                read=read_timeout,
                write=read_timeout,
                pool=read_timeout,
            ),
            follow_redirects=True,
        )
        async with http_client:
            async with streamable_http_client(url, http_client=http_client) as (
                read,
                write,
            ):
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


def _client_for(config: dict, **kwargs):
    return _make_client(
        config.get("transport", "sse"),
        url=config.get("url"),
        command=config.get("command"),
        args=config.get("args"),
        env=config.get("env"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Configuration checks
# ---------------------------------------------------------------------------


TRANSPORTS = ("sse", "streamable_http", "stdio")


def check_backend(backend) -> None:
    """Raise ValueError for an entry the aggregator can't use as written.

    An unknown transport used to fall through to SSE, and a name with "__"
    or a trailing "_" makes prefixed tool names collide with another
    backend's (mcp-aggregator#11).
    """
    if not isinstance(backend, dict):
        raise ValueError(f"backend entry must be an object, not {backend!r}")
    name = backend.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"backend entry has no name: {backend!r}")
    if "__" in name or name.endswith("_"):
        raise ValueError(
            f"backend '{name}': a name may not contain '__' or end with '_'"
        )
    transport = backend.get("transport", "sse")
    if transport not in TRANSPORTS:
        raise ValueError(
            f"backend '{name}': unknown transport {transport!r} (use {', '.join(TRANSPORTS)})"
        )
    needed = "command" if transport == "stdio" else "url"
    if not backend.get(needed):
        raise ValueError(f"backend '{name}': transport {transport} needs a {needed}")
    if backend.get("include_tools") and backend.get("exclude_tools"):
        raise ValueError(
            f"backend '{name}': include_tools and exclude_tools are mutually exclusive"
        )


def check_backends(backends) -> None:
    """Raise ValueError unless every entry is usable and the names are unique."""
    if not isinstance(backends, list):
        raise ValueError("the backends file must hold a JSON list")
    seen = set()
    for backend in backends:
        check_backend(backend)
        if backend["name"] in seen:
            raise ValueError(f"backend '{backend['name']}' is listed twice")
        seen.add(backend["name"])


def _backend_desc(backend: dict) -> str:
    if backend.get("transport") == "stdio":
        return " ".join([backend.get("command", "")] + (backend.get("args") or []))
    return backend.get("url", "")


# ---------------------------------------------------------------------------
# Backend lifecycle
#
# Every change goes through these functions: _add, _activate (discover, then
# start the pool), _remove, and _reload for the backends file. They keep
# _states, the tool registry and the pool tasks in step, and tell clients
# whenever the tool list changes.
# ---------------------------------------------------------------------------


def _add(config: dict, source: str) -> BackendState:
    """Record a checked backend. It has no tools until _activate succeeds."""
    state = BackendState(config=config, source=source)
    _states[state.name] = state
    return state


async def _list_tools(state: BackendState) -> list[types.Tool]:
    """The backend's tools, over its pooled session while that is up.

    Otherwise over a connection of its own: for a stdio backend, a process
    of its own, as at startup.
    """
    if state.session is not None:
        return (await state.session.list_tools()).tools
    async with _client_for(state.config, timeout=10) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools


def _backend_tools(name: str) -> list[types.Tool]:
    """The tools registered for a backend. Caller holds _registry_lock."""
    return [t for t in _tool_list if _tool_registry[t.name].backend_name == name]


async def _discover(state: BackendState) -> int:
    """List the backend's tools and register them in place of any it had.

    Raises when the backend can't be reached or doesn't answer within
    DISCOVERY_TIMEOUT_S, recording why in state.last_error; the tools it had
    stay registered. Clients hear of the change only if the tools changed.
    """
    config, name = state.config, state.name
    started = time.monotonic()
    state.last_attempt = time.time()
    try:
        with anyio.fail_after(DISCOVERY_TIMEOUT_S):
            tools = await _list_tools(state)
    except Exception as e:
        state.last_attempt_s = time.monotonic() - started
        state.last_error = _reason(e)
        if isinstance(e, TimeoutError):
            state.last_error = f"no answer within {DISCOVERY_TIMEOUT_S:g}s"
        raise RuntimeError(state.last_error) from e
    state.last_attempt_s = time.monotonic() - started

    include = set(config.get("include_tools") or [])
    exclude = set(config.get("exclude_tools") or [])
    default_args: dict = config.get("default_args") or {}
    registered = 0
    async with _registry_lock:
        if _states.get(name) is not state:  # removed or replaced meanwhile
            return 0
        before = [t.model_dump() for t in _backend_tools(name)]
        # Replace, never duplicate: reload and the retry can both discover it.
        _purge_tools(name)
        for tool in tools:
            if include and tool.name not in include:
                continue
            if exclude and tool.name in exclude:
                continue
            prefixed = f"{name}__{tool.name}"
            _tool_registry[prefixed] = BackendEntry(
                backend_name=name,
                original_name=tool.name,
                default_args=default_args,
                transport=state.transport,
                url=config.get("url"),
                command=config.get("command"),
                args=config.get("args"),
                env=config.get("env"),
                read_only=_is_read_only(tool),
            )
            _tool_list.append(
                types.Tool(
                    name=prefixed,
                    description=f"[{name}] {tool.description or ''}".strip(),
                    input_schema=tool.input_schema,
                    # Forward safety hints so clients can gate writes.
                    annotations=tool.annotations,
                    # And the shape of structuredContent, which call
                    # results already carry through unchanged.
                    output_schema=tool.output_schema,
                )
            )
            registered += 1
        state.status, state.tools, state.last_error = "ready", registered, None
        changed = [t.model_dump() for t in _backend_tools(name)] != before
    logger.info(
        "Discovered %d tool(s) from '%s' in %.1fs%s",
        registered,
        name,
        state.last_attempt_s,
        "" if changed else " (unchanged)",
    )
    if changed:
        _tools_changed()
    return registered


async def _refresh(state: BackendState) -> dict:
    """List a backend's tools again, without restarting the aggregator (#8).

    For a backend restarted with new or changed tools. A pending one is
    activated, as the retry would. If the backend doesn't answer, it keeps
    the tools it had: being down doesn't take them out of the listing.
    Returns what changed, or the error and how many tools were kept.
    """
    async with _registry_lock:
        before = {t.name for t in _backend_tools(state.name)}
    if state.status == "pending":
        ok = await _activate(state)
    else:
        try:
            await _discover(state)
            ok = True
        except Exception:
            ok = False
    async with _registry_lock:
        after = {t.name for t in _backend_tools(state.name)}
    if not ok:
        return {"backend": state.name, "error": state.last_error, "tools": len(after)}
    return {
        "backend": state.name,
        "tools": len(after),
        "added": sorted(after - before),
        "removed": sorted(before - after),
    }


def _refresh_soon(state: BackendState, why: str) -> None:
    """Refresh in the background, logging the outcome."""

    async def run():
        result = await _refresh(state)
        if "error" in result:
            logger.warning("Refresh of '%s' (%s) failed: %s", state.name, why, result["error"])
        elif result["added"] or result["removed"]:
            logger.info(
                "Refresh of '%s' (%s): added %s, removed %s",
                state.name, why, result["added"] or "none", result["removed"] or "none",
            )

    task = asyncio.create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _activate(state: BackendState) -> bool:
    """Discover the backend and, once it answers, start its pool.

    Returns False, with the reason in state.last_error, if it didn't answer.
    """
    logger.info(
        "Discovering tools from '%s' at %s", state.name, _backend_desc(state.config)
    )
    try:
        await _discover(state)
    except Exception:
        logger.warning(
            "Backend '%s' unreachable after %.1fs: %s",
            state.name,
            state.last_attempt_s or 0,
            state.last_error,
        )
        return False
    if state.pooled and state.pool_task is None and _states.get(state.name) is state:
        state.pool_task = asyncio.create_task(_run_persistent_pool(state))
    return True


async def _activate_all(states: list[BackendState]) -> list[BackendState]:
    """Activate backends concurrently; retry the ones that didn't answer.

    Returns those, which stay pending until the retry reaches them.
    """
    answered = await asyncio.gather(*(_activate(s) for s in states))
    failed = [s for s, ok in zip(states, answered) if not ok]
    if failed:
        _ensure_retry()
    return failed


async def _remove(name: str) -> int:
    """Forget a backend: its tools, its pool and its state. Returns the tool count."""
    state = _states.pop(name, None)
    async with _registry_lock:
        removed = _purge_tools(name)
    if state is not None and state.pool_task is not None:
        state.pool_task.cancel()
        try:
            await state.pool_task
        except asyncio.CancelledError:
            pass
    if removed:
        _tools_changed()
    return removed


def _purge_tools(name: str) -> int:
    """Drop a backend's tools from the registry. Caller holds _registry_lock."""
    keys = {k for k, e in _tool_registry.items() if e.backend_name == name}
    for k in keys:
        del _tool_registry[k]
    _tool_list[:] = [t for t in _tool_list if t.name not in keys]
    return len(keys)


async def _reload(entries: list[dict]) -> dict:
    """Make the file's backends match entries, which check_backends has passed.

    A file backend no longer listed is removed; one whose entry changed is
    replaced; new ones are added. Backends added through POST /backends stay,
    unless the file now names one, which the file's entry then replaces.
    """
    desired = {e["name"]: e for e in entries}
    removed_tools = 0
    for name, state in list(_states.items()):
        if name in desired:
            if state.source != "file" or state.config != desired[name]:
                removed_tools += await _remove(name)
                logger.info("Management: reload replaces backend '%s'", name)
        elif state.source == "file":
            removed_tools += await _remove(name)
            logger.info("Management: reload removed backend '%s'", name)
    new = [_add(cfg, "file") for name, cfg in desired.items() if name not in _states]
    failed = await _activate_all(new)
    return {
        "added_tools": sum(s.tools for s in new),
        "removed_tools": removed_tools,
        "errors": [{"backend": s.name, "error": s.last_error} for s in failed],
    }


# ---------------------------------------------------------------------------
# Retrying backends that didn't answer
# ---------------------------------------------------------------------------


_retry_task: asyncio.Task | None = None
_RETRY_MIN_S = 2.0
_RETRY_MAX_S = 60.0


def _ensure_retry() -> None:
    global _retry_task
    if _retry_task is None or _retry_task.done():
        _retry_task = asyncio.create_task(_retry_pending_backends())


async def _retry_pending_backends() -> None:
    """Retry pending backends until each one answers.

    As Windows services, a backend can still be starting when the aggregator
    discovers tools (a service counts as started before it listens). Backs
    off from 2 s to 60 s; exits once nothing is pending, or on shutdown. A
    backend removed meanwhile is no longer in _states, so it isn't retried.
    """
    delay = _RETRY_MIN_S
    while any(s.status == "pending" for s in _states.values()):
        try:
            await asyncio.wait_for(
                asyncio.shield(_shutdown_event.wait()), timeout=delay
            )
            return
        except asyncio.TimeoutError:
            pass
        pending = [s for s in _states.values() if s.status == "pending"]
        await asyncio.gather(*(_activate(s) for s in pending))
        delay = min(delay * 2, _RETRY_MAX_S)


# ---------------------------------------------------------------------------
# Connection pool (Streamable HTTP and stdio backends — anything that needs
# connection state preserved across calls)
#
# TODO: Research connection pooling for SSE backends. SSE requires a
# long-running read loop, so a persistent session likely needs a per-backend
# asyncio.Queue to serialize calls onto the single open stream.
# ---------------------------------------------------------------------------


async def _shutdown_or(event: asyncio.Event) -> bool:
    """Wait for shutdown or event; True if it was shutdown."""
    waits = [asyncio.ensure_future(_shutdown_event.wait()), asyncio.ensure_future(event.wait())]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()
    return _shutdown_event.is_set()


def _session_lost(e: BaseException) -> bool:
    """The backend refused the request because it doesn't know the session:
    it restarted. It refuses before running anything, so a retry is safe."""
    return isinstance(e, MCPError) and e.code == types.INVALID_REQUEST and e.message in (
        "Session not found",  # an mcp server's answer
        "Session terminated",  # the client's name for a bare 404
    )


async def _run_persistent_pool(state: BackendState) -> None:
    """Keep one persistent ClientSession open for a pooled backend.

    Reconnects automatically on drop (HTTP) or respawns the subprocess
    (stdio). Exits when cancelled or when _shutdown_event fires.

    The backend's tools are listed again after a reconnect, since a dropped
    session usually means the backend restarted, and whenever the backend
    sends notifications/tools/list_changed (#8). Discovery listed them just
    before the first connection.
    """

    async def on_message(message) -> None:
        # Runs in the session's receive loop: awaiting a request here would
        # wait on the loop itself, so the refresh runs as its own task.
        if isinstance(message, types.ToolListChangedNotification):
            _refresh_soon(state, "the backend's tool list changed")

    backoff = 2.0
    connected_before = False
    while True:
        try:
            async with _client_for(state.config) as (read, write):
                async with ClientSession(read, write, message_handler=on_message) as session:
                    await session.initialize()
                    state.session = session
                    state.session_ready.set()
                    logger.info("Pool ready: '%s' (%s)", state.name, state.transport)
                    if connected_before:
                        _refresh_soon(state, "reconnected")
                    connected_before = True
                    if await _shutdown_or(state.reconnect):
                        return
                    state.reconnect.clear()
                    logger.info("Pool '%s': the backend lost the session; reconnecting", state.name)
                    continue  # at once: the backend is up, it restarted
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(
                "Pool '%s' dropped, reconnecting in %.0fs: %s",
                state.name,
                backoff,
                _reason(e),
            )
        finally:
            state.session = None
            state.session_ready.clear()
        try:
            await asyncio.wait_for(
                asyncio.shield(_shutdown_event.wait()), timeout=backoff
            )
            return
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Telling clients the tool list changed
#
# Clients on the initialize handshake hear it as notifications/tools/list_changed
# on their session (the aggregator remembers every session that lists tools).
# Clients on the 2026-07-28 protocol hear it on a subscriptions/listen stream.
# ---------------------------------------------------------------------------


_subscriptions = InMemorySubscriptionBus()
_listening_connections: "weakref.WeakSet[Connection]" = weakref.WeakSet()
_background: set[asyncio.Task] = set()
NOTIFY_TIMEOUT_S = 2.0


def _remember_listener(session: ServerSession) -> None:
    """Remember the client connection a tools/list came in on.

    ctx.session is a new ServerSession for every request; the connection
    under it is what lasts, and what a notification outside any request goes
    out on (ServerSession._notify does the same). mcp exposes it only as
    _connection; the tests cover it, and requirements.txt pins mcp.
    """
    connection = getattr(session, "_connection", None)
    if connection is not None:
        _listening_connections.add(connection)


def _tools_changed() -> None:
    """Tell every connected client, without holding up the change itself."""
    task = asyncio.create_task(_notify_tools_changed())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _notify_tools_changed() -> None:
    try:
        await _subscriptions.publish(ToolsListChanged())
    except Exception as e:
        logger.warning("Could not publish the tool list change: %s", _reason(e))
    for connection in list(_listening_connections):
        try:
            with anyio.fail_after(NOTIFY_TIMEOUT_S):
                await connection.outbound.notify(
                    "notifications/tools/list_changed", None
                )
        except Exception:
            _listening_connections.discard(connection)  # gone, or not reading


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


async def _proxy_call(prefixed_name: str, arguments: dict) -> types.CallToolResult:
    entry = _tool_registry[prefixed_name]
    merged = {**entry.default_args, **arguments}

    if entry.transport in _POOLED_TRANSPORTS:
        # Never a one-off connection: a stdio backend's state (e.g. what a
        # "connect" tool set up) lives in its pooled process.
        state = _states.get(entry.backend_name)
        for attempt in (1, 2):
            if state is not None and state.session is None:
                with anyio.move_on_after(POOL_WAIT_S):
                    await state.session_ready.wait()
            session = state.session if state is not None else None
            if session is None:
                raise RuntimeError(
                    f"backend '{entry.backend_name}' is reconnecting; try again shortly"
                )
            try:
                return await session.call_tool(
                    entry.original_name, merged, read_timeout_seconds=CALL_TIMEOUT_S
                )
            except MCPError as e:
                # A restarted backend: without this, every call failed with
                # "Session not found" until the aggregator restarted (#8).
                if not _session_lost(e) or attempt == 2:
                    raise
                if state.session is session:
                    state.session = None
                    state.session_ready.clear()
                    state.reconnect.set()

    async with _make_client(
        entry.transport,
        url=entry.url,
        timeout=5,
        read_timeout=CALL_TIMEOUT_S,
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(entry.original_name, merged)


def _is_read_only(tool: types.Tool) -> bool:
    """True only when the backend explicitly declares readOnlyHint.

    Unannotated tools count as writes, matching the MCP spec defaults.
    """
    return bool(tool.annotations and tool.annotations.read_only_hint)


def _read_only_mode() -> bool:
    """AGGREGATOR_READ_ONLY=1 hides and refuses every tool not marked read-only."""
    return os.environ.get("AGGREGATOR_READ_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


async def _handle_list_tools(ctx, params) -> types.ListToolsResult:
    if ctx is not None:
        _remember_listener(ctx.session)
    if _read_only_mode():
        return types.ListToolsResult(
            tools=[t for t in _tool_list if _tool_registry[t.name].read_only]
        )
    return types.ListToolsResult(tools=list(_tool_list))


async def _handle_call_tool(
    ctx, params: types.CallToolRequestParams
) -> types.CallToolResult:
    name = params.name
    arguments = params.arguments or {}
    if name not in _tool_registry:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
            isError=True,
        )
    if _read_only_mode() and not _tool_registry[name].read_only:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f"Refused: {name} can change the system and this "
                    "aggregator is running read-only (AGGREGATOR_READ_ONLY).",
                )
            ],
            isError=True,
        )
    try:
        return await _proxy_call(name, arguments)
    except Exception as e:
        reason = _reason(e)
        logger.error("Tool call failed — %s: %s", name, reason)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Backend error: {reason}")],
            isError=True,
        )


class _AggregatorServer(Server):
    """Advertises tools.listChanged to clients on the initialize handshake.

    Every transport builds its initialize result from
    create_initialization_options(), which otherwise leaves the flag off.
    """

    def create_initialization_options(self, notification_options=None, *args, **kwargs):
        return super().create_initialization_options(
            notification_options or NotificationOptions(tools_changed=True),
            *args,
            **kwargs,
        )


def build_server() -> Server:
    return _AggregatorServer(
        "mcp-aggregator",
        on_list_tools=_handle_list_tools,
        on_call_tool=_handle_call_tool,
        on_subscriptions_listen=ListenHandler(_subscriptions),
    )


# ---------------------------------------------------------------------------
# Management API handlers
# ---------------------------------------------------------------------------


def _iso(ts: float | None) -> str | None:
    return (
        datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        if ts
        else None
    )


async def handle_backends(request: Request) -> JSONResponse:
    if request.method == "GET":
        result = [
            {
                "name": name,
                "source": s.source,
                "status": s.status,
                "transport": s.transport,
                "url": s.config.get("url"),
                "command": s.config.get("command"),
                "tools": s.tools,
                "pooled": s.session is not None,
                "last_error": s.last_error,
                "last_attempt": _iso(s.last_attempt),
                "last_attempt_s": (
                    round(s.last_attempt_s, 2) if s.last_attempt_s is not None else None
                ),
            }
            for name, s in _states.items()
        ]
        return JSONResponse({"backends": result, "total_tools": len(_tool_registry)})

    # POST — add a new backend
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type != "application/json":
        return JSONResponse(
            {"error": "Content-Type must be application/json"}, status_code=415
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    # A stdio backend is a command the aggregator runs with its own account
    # (LocalSystem as a service), so only the backends file may define one.
    if body.get("transport") == "stdio":
        return JSONResponse(
            {"error": "stdio backends can only be added through the backends file"},
            status_code=403,
        )
    try:
        check_backend(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    name = body["name"]
    if name in _states:
        return JSONResponse(
            {"error": f"Backend '{name}' already exists — remove it first"},
            status_code=409,
        )

    state = _add(body, "api")
    if not await _activate(state):
        error = state.last_error
        await _remove(name)
        return JSONResponse({"error": f"Discovery failed: {error}"}, status_code=502)

    logger.info("Management: added backend '%s' (%d tools)", name, state.tools)
    return JSONResponse({"added": state.tools}, status_code=201)


async def handle_remove_backend(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    if name not in _states:
        return JSONResponse({"error": f"Backend '{name}' not found"}, status_code=404)

    removed = await _remove(name)
    logger.info("Management: removed backend '%s' (%d tools)", name, removed)
    return JSONResponse({"removed": removed})


async def handle_refresh_backend(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    state = _states.get(name)
    if state is None:
        return JSONResponse({"error": f"Backend '{name}' not found"}, status_code=404)
    result = await _refresh(state)
    logger.info("Management: refreshed backend '%s': %s", name, result)
    return JSONResponse(result, status_code=502 if "error" in result else 200)


async def handle_refresh_all(request: Request) -> JSONResponse:
    results = await asyncio.gather(*(_refresh(s) for s in list(_states.values())))
    logger.info("Management: refreshed all backends: %s", results)
    return JSONResponse({"backends": results, "total_tools": len(_tool_registry)})


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
    try:
        check_backends(new_backends)
    except ValueError as e:
        # Nothing changes: a half-applied bad file would drop good backends.
        return JSONResponse(
            {"error": f"Backends file not loaded: {e}"}, status_code=400
        )

    return JSONResponse(await _reload(new_backends))


# ---------------------------------------------------------------------------
# Starlette app
# ---------------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_network_settings() -> tuple[str, str | None]:
    """Return (bind_host, token). Exit if an external bind has no token."""
    host = os.environ.get("AGGREGATOR_BIND_HOST", "127.0.0.1").strip()
    token = os.environ.get("AGGREGATOR_AUTH_TOKEN", "").strip() or None
    if not _is_loopback(host) and token is None:
        raise SystemExit(
            f"Refusing to listen on {host} without AGGREGATOR_AUTH_TOKEN. Set a "
            "token, or unset AGGREGATOR_BIND_HOST to stay on 127.0.0.1."
        )
    return host, token


def require_bearer_token(app, token: str | None):
    """ASGI wrapper that rejects HTTP requests without the bearer token."""
    if token is None:
        return app
    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope.get("type") == "http":
            supplied = next(
                (v for k, v in scope.get("headers", []) if k == b"authorization"), b""
            )
            if not hmac.compare_digest(supplied, expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await app(scope, receive, send)

    return guarded


def load_allowed_origins() -> set[str]:
    """Browser origins allowed to call the aggregator (none by default)."""
    raw = os.environ.get("AGGREGATOR_ALLOWED_ORIGINS", "")
    return {o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip()}


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def refuse_browser_requests(app, bind_host: str, allowed_origins: set[str]):
    """ASGI wrapper that refuses requests a web page made on someone's behalf.

    MCP clients and curl send no Origin and no Sec-Fetch-Site, so they pass.
    A browser sends Origin on every cross-origin POST, even a "no-cors" one
    that skips the CORS preflight, and Sec-Fetch-Site on every request. With
    no token on loopback, this is what stops a page in a local browser from
    calling tools or the management API. On a loopback bind, the Host check
    also stops DNS rebinding, where a page's own hostname resolves to
    127.0.0.1 so its requests count as same-origin.
    """
    check_host = _is_loopback(bind_host)

    def refusal(scope) -> str | None:
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin", b"").decode("latin-1").rstrip("/").lower()
        if origin:
            return None if origin in allowed_origins else f"origin {origin} not allowed"
        fetch_site = headers.get(b"sec-fetch-site", b"").decode("latin-1")
        if fetch_site in ("cross-site", "same-site"):
            return f"{fetch_site} browser request"
        host = headers.get(b"host", b"").decode("latin-1").lower()
        if check_host and host:
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            if name not in _LOOPBACK_HOSTS:
                return f"host {host} not allowed"
        return None

    async def guarded(scope, receive, send):
        if scope.get("type") == "http":
            reason = refusal(scope)
            if reason:
                logger.warning(
                    "Refused %s %s: %s", scope.get("method"), scope.get("path"), reason
                )
                response = JSONResponse({"error": "forbidden"}, status_code=403)
                await response(scope, receive, send)
                return
        await app(scope, receive, send)

    return guarded


def build_starlette_app(server: Server) -> Starlette:
    # --- Streamable HTTP transport (Claude Desktop, modern clients) ---
    # stateless=False kept deliberately, not left over from the mcp 1.x port:
    # StreamableHTTPSessionManager's constructor is unchanged in mcp 2.0 (same
    # param, same default), and every piece of this aggregator's own state
    # (_tool_registry, _states, etc.) is a module-level global, not scoped to
    # any particular client's MCP session — there is no app-level session
    # affinity here for a stateless mode to threaten. A stateful session is
    # also what lets the aggregator send tools/list_changed outside a request.
    # If a future mcp SDK release removes this parameter entirely (the spec's
    # long-term direction per SEP-2567), revisit then; nothing here forces an
    # earlier change.
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
        tasks = [s.pool_task for s in _states.values() if s.pool_task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            Route("/backends/refresh", endpoint=handle_refresh_all, methods=["POST"]),
            Route(
                "/backends/{name}/refresh",
                endpoint=handle_refresh_backend,
                methods=["POST"],
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

    # Validate before discovery so a bad config fails fast.
    bind_host, auth_token = load_network_settings()

    backends_file = os.environ.get("BACKENDS_FILE", "backends.json")
    _backends_path = (
        backends_file
        if os.path.isabs(backends_file)
        else os.path.join(os.path.dirname(__file__), backends_file)
    )
    with open(_backends_path) as f:
        backends = json.load(f)
    try:
        check_backends(backends)
    except ValueError as e:
        raise SystemExit(f"{_backends_path}: {e}") from None

    failed = await _activate_all([_add(b, "file") for b in backends])
    logger.info(
        "Ready — %d tool(s) aggregated from %d backend(s); %d pending: %s",
        len(_tool_list),
        len(backends) - len(failed),
        len(failed),
        ", ".join(s.name for s in failed) or "none",
    )

    server = build_server()

    if "--stdio" in sys.argv:
        from mcp.server.stdio import stdio_server

        logger.info("Starting in stdio mode")
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    else:
        app = refuse_browser_requests(
            require_bearer_token(build_starlette_app(server), auth_token),
            bind_host,
            load_allowed_origins(),
        )
        port = int(os.environ.get("AGGREGATOR_PORT", 8100))
        config = uvicorn.Config(app, host=bind_host, port=port, log_level="info")
        userver = uvicorn.Server(config)
        logger.info(
            "Starting aggregator on %s:%d (auth %s)",
            bind_host,
            port,
            "on" if auth_token else "off",
        )
        await userver.serve()


if __name__ == "__main__":
    asyncio.run(main())
