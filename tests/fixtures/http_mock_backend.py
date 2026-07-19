"""Same tool surface as stdio_mock_backend.py, served over sse or
streamable-http, for aggregator regression tests against those transports.
Spawned as a subprocess by tests, never run directly by a human."""

import argparse

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("http-mock-backend")


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

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport", choices=["sse", "streamable-http"], required=True
    )
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    mcp.settings.port = args.port
    mcp.run(args.transport)
