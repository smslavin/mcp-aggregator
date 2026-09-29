"""Structured tool results reach clients (smslavin/graccess-mcp#26).

A backend tool can return text for the model and structuredContent for a UI
in one result. The aggregator must forward the tool's outputSchema as well
as the result, so clients know the shape and can validate it.
"""

import sys
from pathlib import Path

import pytest

import server as agg

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "structured_mock_backend.py"
BACKEND = {
    "name": "mock",
    "transport": "stdio",
    "command": sys.executable,
    "args": [str(FIXTURE)],
}


async def test_output_schema_is_forwarded():
    await agg._discover_backend(BACKEND)
    tools = {t.name: t for t in (await agg._handle_list_tools(None, None)).tools}

    schema = tools["mock__deployment_view"].output_schema
    assert schema is not None
    assert schema["required"] == ["rows"]


async def test_result_keeps_text_and_structured_content():
    await agg._discover_backend(BACKEND)
    params = agg.types.CallToolRequestParams(
        name="mock__deployment_view", arguments={"area": "WTP"}
    )
    result = await agg._handle_call_tool(None, params)

    assert not result.is_error
    assert result.content[0].text == "WTP_Pump_01 | status=deployed"
    assert result.structured_content == {
        "rows": [{"tagname": "WTP_Pump_01", "status": "deployed"}]
    }
