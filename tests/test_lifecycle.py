"""Backend lifecycle: discovery, retry, reload, removal, and telling clients.

Each backend has one BackendState, and every change goes through _add,
_activate, _remove and _reload (mcp-aggregator#15). A backend that isn't
listening yet at startup gets its tools once it is, and clients hear that the
tool list changed (#12). Discovery is concurrent and bounded (#14), and
errors arrive with their real message (#13).
"""

import asyncio
import subprocess
import sys
import time

import anyio
import pytest
from mcp.client import Client
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import server as agg
from conftest import connect
from test_http_backends import FIXTURE, _free_port, _wait_for_port

pytestmark = pytest.mark.anyio


def _spawn_sse_backend(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(FIXTURE), "--transport", "sse", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def sse_backend():
    port = _free_port()
    proc = _spawn_sse_backend(port)
    _wait_for_port(port)
    yield {"name": "live", "url": f"http://127.0.0.1:{port}/sse"}
    proc.kill()
    proc.wait(timeout=5)


@pytest.fixture
async def silent_port():
    """A port that accepts connections and never answers."""

    async def hold(reader, writer):
        await asyncio.Event().wait()

    srv = await asyncio.start_server(hold, "127.0.0.1", 0)
    yield srv.sockets[0].getsockname()[1]
    srv.close()


def _status():
    app = Starlette(routes=[Route("/backends", agg.handle_backends, methods=["GET"])])
    return {b["name"]: b for b in TestClient(app).get("/backends").json()["backends"]}


async def test_late_backend_is_discovered_on_retry(monkeypatch):
    monkeypatch.setattr(agg, "_RETRY_MIN_S", 0.1)
    port = _free_port()
    state = agg._add({"name": "late", "url": f"http://127.0.0.1:{port}/sse"}, "file")

    assert await agg._activate_all([state]) == [state]
    assert state.status == "pending" and state.last_error
    assert "late__echo" not in agg._tool_registry

    # GET /backends shows it waiting, with why (#12).
    late = _status()["late"]
    assert late["status"] == "pending" and late["tools"] == 0
    assert (
        late["last_error"]
        and late["last_attempt"]
        and late["last_attempt_s"] is not None
    )

    proc = _spawn_sse_backend(port)
    try:
        _wait_for_port(port)
        await asyncio.wait_for(agg._retry_task, timeout=30)
        assert "late__echo" in agg._tool_registry
        assert (state.status, state.last_error) == ("ready", None)
    finally:
        proc.kill()
        proc.wait(timeout=5)


async def test_backend_dropped_from_the_file_stops_retrying(monkeypatch):
    monkeypatch.setattr(agg, "_RETRY_MIN_S", 0.05)
    state = agg._add(
        {"name": "gone", "url": f"http://127.0.0.1:{_free_port()}/sse"}, "file"
    )
    await agg._activate_all([state])
    task = agg._retry_task
    await agg._reload([])
    await asyncio.wait_for(task, timeout=10)
    assert "gone" not in agg._states


async def test_rediscovery_replaces_tools_instead_of_duplicating(sse_backend):
    state = agg._add(sse_backend, "file")
    first = await agg._discover(state)
    second = await agg._discover(state)
    names = [t.name for t in agg._tool_list if t.name.startswith("live__")]
    assert first == second == len(names) == len(set(names))


async def test_discovery_is_concurrent_and_bounded(monkeypatch, silent_port):
    # A backend that accepts the connection and then hangs costs at most
    # DISCOVERY_TIMEOUT_S, and two of them cost that once, not twice (#14).
    monkeypatch.setattr(agg, "DISCOVERY_TIMEOUT_S", 1.0)
    states = [
        agg._add({"name": n, "url": f"http://127.0.0.1:{silent_port}/sse"}, "file")
        for n in ("hang1", "hang2")
    ]
    started = time.monotonic()
    failed = await agg._activate_all(states)
    assert time.monotonic() - started < 1.9
    assert failed == states
    assert all(s.last_error == "no answer within 1s" for s in states)


async def test_reload_keeps_backends_added_through_the_api(sse_backend):
    api = await connect({**sse_backend, "name": "api"}, source="api")
    await connect({**sse_backend, "name": "filed"})
    result = await agg._reload([])
    assert result["removed_tools"] == 2
    assert set(agg._states) == {"api"} and api.status == "ready"
    assert "api__echo" in agg._tool_registry and "filed__echo" not in agg._tool_registry


async def test_reload_replaces_a_changed_entry(sse_backend):
    await connect({**sse_backend, "name": "b"})
    result = await agg._reload(
        [{**sse_backend, "name": "b", "include_tools": ["echo"]}]
    )
    assert result == {"added_tools": 1, "removed_tools": 2, "errors": []}
    assert [k for k in agg._tool_registry if k.startswith("b__")] == ["b__echo"]
    # The same entry again changes nothing.
    assert await agg._reload(
        [{**sse_backend, "name": "b", "include_tools": ["echo"]}]
    ) == {
        "added_tools": 0,
        "removed_tools": 0,
        "errors": [],
    }


async def test_removal_goes_by_backend_not_by_prefix():
    # The registry key is no guide to the owner; the entry's backend_name is.
    agg._tool_registry["a__x"] = agg.BackendEntry("a", "x", {}, "sse")
    agg._tool_registry["a__y"] = agg.BackendEntry("other", "a__y", {}, "sse")
    agg._tool_list.extend(
        agg.types.Tool(name=n, input_schema={}) for n in ("a__x", "a__y")
    )
    agg._add({"name": "a", "url": "http://h/sse"}, "file")
    assert await agg._remove("a") == 1
    assert list(agg._tool_registry) == ["a__y"] and [
        t.name for t in agg._tool_list
    ] == ["a__y"]


async def test_post_backend_that_cannot_be_reached_leaves_nothing(
    monkeypatch, silent_port
):
    monkeypatch.setattr(agg, "DISCOVERY_TIMEOUT_S", 0.5)
    state = agg._add({"name": "x", "url": f"http://127.0.0.1:{silent_port}/sse"}, "api")
    assert not await agg._activate(state)
    await agg._remove("x")
    assert "x" not in agg._states and agg._retry_task is None


async def test_errors_are_unwrapped():
    # anyio wraps everything raised inside a ClientSession (#13).
    group = BaseExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ExceptionGroup("inner", [ValueError("simulated schema validation failure")])],
    )
    assert agg._reason(group) == "ValueError: simulated schema validation failure"
    assert (
        agg._reason(ExceptionGroup("g", [OSError("a"), TimeoutError()]))
        == "OSError: a; TimeoutError"
    )


async def test_a_call_error_reaches_the_model_unwrapped(monkeypatch):
    agg._tool_registry["b__t"] = agg.BackendEntry("b", "t", {}, "sse")

    async def fail(name, arguments):
        raise ExceptionGroup(
            "unhandled errors in a TaskGroup", [ValueError("real reason")]
        )

    monkeypatch.setattr(agg, "_proxy_call", fail)
    result = await agg._handle_call_tool(
        None, agg.types.CallToolRequestParams(name="b__t")
    )
    assert result.is_error
    assert result.content[0].text == "Backend error: ValueError: real reason"


async def test_clients_hear_when_the_tool_list_changes(sse_backend):
    # A client that listed tools is told when a late backend's tools arrive,
    # and when a backend goes (#12).
    heard = []

    async def on_message(message):
        if isinstance(
            getattr(message, "root", message), agg.types.ToolListChangedNotification
        ):
            heard.append(time.monotonic())

    async with Client(
        agg.build_server(), mode="legacy", message_handler=on_message
    ) as c:
        assert c.server_capabilities.tools.list_changed is True
        assert (await c.list_tools()).tools == []

        await connect(sse_backend)
        with anyio.fail_after(5):
            while not heard:
                await asyncio.sleep(0.05)
        assert {t.name for t in (await c.list_tools()).tools} == {
            "live__echo",
            "live__call_count",
        }

        await agg._remove("live")
        with anyio.fail_after(5):
            while len(heard) < 2:
                await asyncio.sleep(0.05)
        assert (await c.list_tools()).tools == []


async def test_modern_clients_hear_it_on_a_listen_stream(sse_backend):
    # Clients on the 2026-07-28 protocol get change notifications only on a
    # subscriptions/listen stream.
    async with Client(agg.build_server()) as c:
        async with c.listen(tools_list_changed=True) as sub:
            await connect(sse_backend)
            with anyio.fail_after(5):
                event = await sub.__anext__()
        assert type(event).__name__ == "ToolsListChanged"
        assert len((await c.list_tools()).tools) == 2
