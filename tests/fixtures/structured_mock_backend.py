"""MCP server over stdio with a tool that returns readable text for the model
and structured data for a UI in the same result, for the structured-output
tests. Spawned as a subprocess by tests, never run directly by a human.
"""

from typing import Annotated

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
# pydantic needs typing_extensions.TypedDict to build a schema on Python < 3.12.
from typing_extensions import TypedDict

mcp = MCPServer("structured-mock-backend")


class Row(TypedDict):
    tagname: str
    status: str


class View(TypedDict):
    rows: list[Row]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def deployment_view(area: str) -> Annotated[CallToolResult, View]:
    """List instances in an area with their status."""
    rows = [{"tagname": f"{area}_Pump_01", "status": "deployed"}]
    text = "\n".join(f"{r['tagname']} | status={r['status']}" for r in rows)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"rows": rows},
    )


if __name__ == "__main__":
    mcp.run("stdio")
