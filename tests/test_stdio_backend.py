"""fieldworks-core#20 — stdio backend transport support.

Verifies list_tools/call_tool round-trip through the aggregator against a
real stdio subprocess, through the persistent pool (proving calls share one
subprocess rather than respawning it each time), and that a call with no
pool session is refused rather than run in a fresh process."""

import asyncio
import sys
from pathlib import Path

import pytest

import server as agg
from conftest import connect

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
    state = await connect(STDIO_BACKEND)
    assert state.tools == 2
    entry = agg._tool_registry["stdiomock__echo"]
    assert entry.transport == "stdio"
    assert entry.url is None
    assert entry.command == sys.executable
    assert entry.args == [str(FIXTURE)]


@pytest.mark.anyio
async def test_stdio_call_without_a_pool_is_refused_not_respawned(monkeypatch):
    # A one-off process would lose the backend's state (what a "connect" tool
    # set up), so a call waits for the pool and then says it's reconnecting
    # (mcp-aggregator#14).
    monkeypatch.setattr(agg, "POOL_WAIT_S", 0.2)
    await agg._discover(agg._add(STDIO_BACKEND, "file"))  # tools, but no pool
    with pytest.raises(RuntimeError, match="'stdiomock' is reconnecting"):
        await agg._proxy_call("stdiomock__echo", {"text": "hello"})


@pytest.mark.anyio
async def test_stdio_call_waits_for_the_pool():
    state = agg._add(STDIO_BACKEND, "file")
    assert await agg._activate(
        state
    )  # starts the pool; the call doesn't wait for it here
    result = await agg._proxy_call("stdiomock__echo", {"text": "hello"})
    assert _text(result) == "echo: hello"


@pytest.mark.anyio
async def test_stdio_persistent_pool_reuses_subprocess():
    await connect(STDIO_BACKEND)
    first = await agg._proxy_call("stdiomock__call_count", {})
    second = await agg._proxy_call("stdiomock__call_count", {})
    # Same subprocess handling both calls -> counter increments (1, then 2).
    # A fresh subprocess per call would return "1" both times.
    assert _text(first) == "1"
    assert _text(second) == "2"
