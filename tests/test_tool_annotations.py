"""Tool annotations are forwarded, and AGGREGATOR_READ_ONLY enforces them
(smslavin/graccess-mcp#5)."""

import sys
from pathlib import Path

import pytest

import server as agg

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "annotated_mock_backend.py"
BACKEND = {
    "name": "mock",
    "transport": "stdio",
    "command": sys.executable,
    "args": [str(FIXTURE)],
}


def _text(result):
    return next(b.text for b in result.content if hasattr(b, "text"))


async def _tools():
    return {t.name: t for t in (await agg._handle_list_tools(None, None)).tools}


async def test_annotations_are_forwarded(monkeypatch):
    monkeypatch.delenv("AGGREGATOR_READ_ONLY", raising=False)
    await agg._discover_backend(BACKEND)
    tools = await _tools()

    assert tools["mock__read_value"].annotations.read_only_hint is True
    assert tools["mock__delete_thing"].annotations.destructive_hint is True
    assert tools["mock__unlabelled"].annotations is None


async def test_only_explicit_read_only_counts(monkeypatch):
    await agg._discover_backend(BACKEND)
    assert agg._tool_registry["mock__read_value"].read_only
    assert not agg._tool_registry["mock__delete_thing"].read_only
    assert not agg._tool_registry["mock__unlabelled"].read_only


async def test_full_mode_lists_and_calls_everything(monkeypatch):
    monkeypatch.delenv("AGGREGATOR_READ_ONLY", raising=False)
    await agg._discover_backend(BACKEND)
    assert set(await _tools()) == {
        "mock__read_value",
        "mock__delete_thing",
        "mock__unlabelled",
    }

    params = agg.types.CallToolRequestParams(
        name="mock__delete_thing", arguments={"tag": "x"}
    )
    result = await agg._handle_call_tool(None, params)
    assert not result.is_error
    assert _text(result) == "deleted x"


@pytest.mark.parametrize("value", ["1", "true", "YES"])
async def test_read_only_mode_hides_writes(monkeypatch, value):
    monkeypatch.setenv("AGGREGATOR_READ_ONLY", value)
    await agg._discover_backend(BACKEND)
    assert set(await _tools()) == {"mock__read_value"}


@pytest.mark.parametrize("tool", ["mock__delete_thing", "mock__unlabelled"])
async def test_read_only_mode_refuses_write_calls(monkeypatch, tool):
    monkeypatch.setenv("AGGREGATOR_READ_ONLY", "1")
    await agg._discover_backend(BACKEND)

    params = agg.types.CallToolRequestParams(name=tool, arguments={"tag": "x"})
    result = await agg._handle_call_tool(None, params)
    assert result.is_error
    assert "read-only" in _text(result)


async def test_read_only_mode_still_serves_reads(monkeypatch):
    monkeypatch.setenv("AGGREGATOR_READ_ONLY", "1")
    await agg._discover_backend(BACKEND)

    params = agg.types.CallToolRequestParams(
        name="mock__read_value", arguments={"tag": "t"}
    )
    result = await agg._handle_call_tool(None, params)
    assert not result.is_error
    assert _text(result) == "value of t"
