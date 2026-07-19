"""Regression coverage for the sse/streamable_http transports — both existed
before fieldworks-core#20's stdio work, but were only ever checked by hand.
#20 refactored _make_client's signature and generalized pooling from
HTTP-only (_run_http_pool) to _run_persistent_pool, touching this code
directly, so it's worth locking down alongside the new stdio tests."""

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import server as agg

FIXTURE = Path(__file__).parent / "fixtures" / "http_mock_backend.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"mock backend never opened port {port}")


@pytest.fixture
def http_backend(request):
    """Spawn tests/fixtures/http_mock_backend.py, params: "sse" | "streamable_http"
    (backends.json's spelling — translated to FastMCP's "streamable-http" CLI flag
    below). Yields a backends.json-shaped dict. Subprocess teardown is a plain OS
    kill, not asyncio-task cancellation, so it isn't subject to the cross-event-loop
    hazard noted in conftest.py."""
    transport = request.param
    fastmcp_transport = (
        "streamable-http" if transport == "streamable_http" else transport
    )
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(FIXTURE),
            "--transport",
            fastmcp_transport,
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port)
        path = "/sse" if transport == "sse" else "/mcp"
        yield {
            "name": "httpmock",
            "transport": transport,
            "url": f"http://127.0.0.1:{port}{path}",
        }
    finally:
        proc.kill()
        proc.wait(timeout=5)


def _text(result: agg.types.CallToolResult) -> str:
    return next(b.text for b in result.content if hasattr(b, "text"))


@pytest.mark.anyio
@pytest.mark.parametrize("http_backend", ["sse", "streamable_http"], indirect=True)
async def test_http_discovery_and_call(http_backend):
    count = await agg._discover_backend(http_backend)
    assert count == 2
    entry = agg._tool_registry["httpmock__echo"]
    assert entry.transport == http_backend["transport"]
    assert entry.url == http_backend["url"]
    assert entry.command is None

    result = await agg._proxy_call("httpmock__echo", {"text": "hello"})
    assert _text(result) == "echo: hello"


@pytest.mark.anyio
@pytest.mark.parametrize("http_backend", ["streamable_http"], indirect=True)
async def test_streamable_http_persistent_pool_reuses_session(http_backend):
    await agg._discover_backend(http_backend)
    task = agg.asyncio.create_task(agg._run_persistent_pool("httpmock", http_backend))
    agg._pool_tasks["httpmock"] = task

    for _ in range(100):
        if "httpmock" in agg._session_pool:
            break
        await agg.asyncio.sleep(0.05)
    assert "httpmock" in agg._session_pool

    try:
        first = await agg._proxy_call("httpmock__call_count", {})
        second = await agg._proxy_call("httpmock__call_count", {})
        assert _text(first) == "1"
        assert _text(second) == "2"
    finally:
        # Cancel in this test's own event loop — see conftest.py's note.
        del agg._pool_tasks["httpmock"]
        task.cancel()
        try:
            await task
        except agg.asyncio.CancelledError:
            pass


@pytest.mark.anyio
@pytest.mark.parametrize("http_backend", ["sse"], indirect=True)
async def test_sse_backend_not_pooled(http_backend):
    """SSE backends still open a fresh connection per call — confirm
    _start_pool_tasks doesn't pool them (unchanged pre-#20 behavior)."""
    await agg._discover_backend(http_backend)
    await agg._start_pool_tasks([http_backend])
    assert "httpmock" not in agg._session_pool
    assert "httpmock" not in agg._pool_tasks
