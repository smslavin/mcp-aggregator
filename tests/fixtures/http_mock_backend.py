"""Same tool surface as stdio_mock_backend.py, served over sse or
streamable-http, for aggregator regression tests against those transports.
Spawned as a subprocess by tests, never run directly by a human."""

import argparse

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

mcp = MCPServer("http-mock-backend")


@mcp.tool()
def echo(text: str) -> str:
    """Return the given text unchanged, prefixed to prove a real round trip."""
    return f"echo: {text}"


@mcp.tool()
def call_count() -> int:
    """Return how many times this tool has been called in this process
    (proves pooled calls share one connection instead of a fresh backend
    process each time — here there's one process either way, but the
    counter still confirms session reuse over reconnects)."""
    call_count.calls += 1
    return call_count.calls


call_count.calls = 0


def extra() -> str:
    """A tool this backend has only when started with --extra, or after grow."""
    return "extra"


async def grow(ctx: Context) -> str:
    """Add the extra tool and tell the client its tool list changed."""
    mcp.add_tool(extra)
    await ctx.session.send_tool_list_changed()
    return "grown"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport", choices=["sse", "streamable-http"], required=True
    )
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--extra", action="store_true", help="start with the extra tool"
    )
    parser.add_argument(
        "--grow", action="store_true", help="offer grow, which adds it later"
    )
    args = parser.parse_args()
    if args.extra:
        mcp.add_tool(extra)
    if args.grow:
        mcp.add_tool(grow)
    mcp.run(args.transport, port=args.port)
