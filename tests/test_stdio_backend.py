"""fieldworks-core#20 — stdio backend transport support.

Verifies list_tools/call_tool round-trip through the aggregator against a
real stdio subprocess, both without pooling (fresh connection per call,
mirroring the SSE fallback path) and with the persistent pool (proving
calls share one subprocess rather than respawning it each time)."""

import asyncio
import sys
from pathlib import Path

import pytest

import server as agg

FIXTURE = Path(__file__).parent / "fixtures" / "stdio_mock_backend.py"

STDIO_BACKEND = {
    "name": "stdiomock",
    "transport": "stdio",
    "command": sys.executable,
    "args": [str(FIXTURE)],
}


def _text(result: agg.types.CallToolResult) -> str:
    return next(b.text for b in result.content if hasattr(b, "text"))


@pytest.mark.anyio
async def test_stdio_discovery_registers_tools():
    count = await agg._discover_backend(STDIO_BACKEND)
    assert count == 2
    entry = agg._tool_registry["stdiomock__echo"]
    assert entry.transport == "stdio"
    assert entry.url is None
    assert entry.command == sys.executable
    assert entry.args == [str(FIXTURE)]


@pytest.mark.anyio
async def test_stdio_call_without_pool_round_trips():
    await agg._discover_backend(STDIO_BACKEND)
    result = await agg._proxy_call("stdiomock__echo", {"text": "hello"})
    assert _text(result) == "echo: hello"


@pytest.mark.anyio
async def test_stdio_persistent_pool_reuses_subprocess():
    await agg._discover_backend(STDIO_BACKEND)
    task = agg.asyncio.create_task(agg._run_persistent_pool("stdiomock", STDIO_BACKEND))
    agg._pool_tasks["stdiomock"] = task

    for _ in range(100):
        if "stdiomock" in agg._session_pool:
            break
        await agg.asyncio.sleep(0.05)
    assert "stdiomock" in agg._session_pool

    try:
        first = await agg._proxy_call("stdiomock__call_count", {})
        second = await agg._proxy_call("stdiomock__call_count", {})
        # Same subprocess handling both calls -> counter increments (1, then 2).
        # A fresh subprocess per call would return "1" both times.
        assert _text(first) == "1"
        assert _text(second) == "2"
    finally:
        # Cancel in this test's own event loop — see conftest.py's note on
        # why the autouse fixture can't safely do this from teardown.
        del agg._pool_tasks["stdiomock"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
