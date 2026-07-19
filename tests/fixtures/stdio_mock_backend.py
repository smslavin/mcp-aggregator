"""Trivial MCP server over stdio — stand-in for a real stdio adapter
(e.g. fieldworks-adapters' mqtt-mcp) in aggregator stdio-transport tests.
Spawned as a subprocess by tests, never run directly by a human.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stdio-mock-backend")


@mcp.tool()
def echo(text: str) -> str:
    """Return the given text unchanged, prefixed to prove a real round trip."""
    return f"echo: {text}"


@mcp.tool()
def call_count() -> int:
    """Return how many times this tool has been called in this process
    (proves pooled calls share one subprocess instead of spawning fresh
    ones each time)."""
    call_count.calls += 1
    return call_count.calls


call_count.calls = 0


if __name__ == "__main__":
    mcp.run("stdio")
