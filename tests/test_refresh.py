"""Listing a backend's tools again without restarting the aggregator (#8).

A backend restarted with new tools used to keep its old list until the
aggregator restarted. Now POST /backends/{name}/refresh (or /backends/refresh)
lists it again. A pooled backend is listed again when its session
reconnects, such as after a call finds the backend restarted, and when it
sends tools/list_changed. A backend that doesn't
answer keeps the tools it had.
"""

import asyncio
import json
import subprocess
import sys

import anyio
import pytest
from starlette.requests import Request

import server as agg
from conftest import connect
from test_http_backends import FIXTURE, _free_port, _wait_for_port

pytestmark = pytest.mark.anyio


class Backend:
    """The mock backend as a process the test can restart on the same port."""

    def __init__(self, transport: str):
        self.transport, self.port, self.proc = transport, _free_port(), None
        path = "/sse" if transport == "sse" else "/mcp"
        self.config = {"name": "live", "url": f"http://127.0.0.1:{self.port}{path}"}
        if transport != "sse":
            self.config["transport"] = transport

    def start(self, *flags: str) -> None:
        arg = "sse" if self.transport == "sse" else "streamable-http"
        self.proc = subprocess.Popen(
            [sys.executable, str(FIXTURE), "--transport", arg, "--port", str(self.port), *flags],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_port(self.port)

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait(timeout=5)
            self.proc = None


@pytest.fixture
def backend(request):
    b = Backend(getattr(request, "param", "sse"))
    b.start()
    yield b
    b.stop()


async def _post(path: str):
    """POST to a refresh route. TestClient would run the handler in a loop
    of its own; the aggregator's state lives in this one."""
    name = path.split("/")[2] if path.count("/") == 3 else None
    scope = {"type": "http", "method": "POST", "path": path, "headers": [],
             "path_params": {"name": name} if name else {}}
    handler = agg.handle_refresh_backend if name else agg.handle_refresh_all
    response = await handler(Request(scope))
    return response.status_code, json.loads(response.body)


def _names() -> set[str]:
    return {t.name for t in agg._tool_list}


async def test_a_restarted_backends_new_tool_appears_on_refresh(backend):
    await connect(backend.config)
    assert _names() == {"live__echo", "live__call_count"}
    backend.stop()
    backend.start("--extra")
    status, body = await _post("/backends/live/refresh")
    assert status == 200
    assert body == {"backend": "live", "tools": 3, "added": ["live__extra"], "removed": []}
    assert "live__extra" in _names()
    result = await agg._handle_call_tool(None, agg.types.CallToolRequestParams(name="live__extra"))
    assert not result.is_error


async def test_a_backend_that_is_down_keeps_its_tools(backend):
    await connect(backend.config)
    backend.stop()
    status, body = await _post("/backends/live/refresh")
    assert status == 502 and body["tools"] == 2 and body["error"]
    assert _names() == {"live__echo", "live__call_count"}
    assert agg._states["live"].status == "ready"


async def test_an_unchanged_refresh_tells_no_client(backend, monkeypatch):
    await connect(backend.config)
    told = []
    monkeypatch.setattr(agg, "_tools_changed", lambda: told.append(1))
    status, body = await _post("/backends/live/refresh")
    assert status == 200 and body["added"] == body["removed"] == []
    assert told == []


async def test_a_changed_refresh_tells_clients(backend, monkeypatch):
    await connect(backend.config)
    backend.stop()
    backend.start("--extra")
    told = []
    monkeypatch.setattr(agg, "_tools_changed", lambda: told.append(1))
    await _post("/backends/live/refresh")
    assert told == [1]


async def test_refresh_all_and_an_unknown_backend(backend):
    await connect(backend.config)
    status, body = await _post("/backends/refresh")
    assert status == 200 and body["total_tools"] == 2
    assert body["backends"] == [{"backend": "live", "tools": 2, "added": [], "removed": []}]
    status, body = await _post("/backends/nope/refresh")
    assert status == 404


async def test_refresh_activates_a_pending_backend(backend):
    backend.stop()
    state = agg._add(backend.config, "file")
    assert not await agg._activate(state)
    backend.start()
    status, body = await _post("/backends/live/refresh")
    assert status == 200 and body["added"] == ["live__call_count", "live__echo"]
    assert state.status == "ready"


async def _until(condition, timeout=20.0):
    with anyio.fail_after(timeout):
        while not condition():
            await asyncio.sleep(0.1)


@pytest.mark.parametrize("backend", ["streamable_http"], indirect=True)
async def test_a_restarted_pooled_backend_is_reconnected_and_listed_again(backend):
    # The restarted backend doesn't know the pool's session. Every call used
    # to fail with "Session not found" until the aggregator restarted.
    state = await connect(backend.config)
    old = state.session
    backend.stop()
    backend.start("--extra")
    result = await agg._proxy_call("live__echo", {"text": "hi"})
    assert not result.is_error and result.content[0].text == "echo: hi"
    assert state.session is not old
    await _until(lambda: "live__extra" in _names())


@pytest.mark.parametrize("backend", ["streamable_http"], indirect=True)
async def test_a_pooled_backends_list_changed_notice_is_honoured(backend):
    backend.stop()
    backend.start("--grow")
    await connect(backend.config)
    assert "live__extra" not in _names()
    result = await agg._proxy_call("live__grow", {})
    assert not result.is_error
    await _until(lambda: "live__extra" in _names(), timeout=10)
