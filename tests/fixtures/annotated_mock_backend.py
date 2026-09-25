"""MCP server over stdio with one read-only, one destructive and one
unannotated tool, for the annotation-forwarding and read-only-mode tests.
Spawned as a subprocess by tests, never run directly by a human.
"""

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("annotated-mock-backend")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_value(tag: str) -> str:
    """Read a value without changing anything."""
    return f"value of {tag}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def delete_thing(tag: str) -> str:
    """Pretend to delete something."""
    return f"deleted {tag}"


@mcp.tool()
def unlabelled(tag: str) -> str:
    """No annotations: must be treated as a write."""
    return f"touched {tag}"


if __name__ == "__main__":
    mcp.run("stdio")
