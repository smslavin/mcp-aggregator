"""A backend that isn't listening yet at startup gets its tools once it is.

As Windows services, the aggregator can start before a backend listens (a
service counts as started before its process opens its port), and discovery
used to run once, so that backend's tools stayed missing until a restart."""

import subprocess
import sys

import pytest

import server as agg
from test_http_backends import FIXTURE, _free_port, _wait_for_port


def _spawn_sse_backend(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(FIXTURE), "--transport", "sse", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.anyio
async def test_late_backend_is_discovered_on_retry(monkeypatch):
    monkeypatch.setattr(agg, "_RETRY_MIN_S", 0.1)
    port = _free_port()
    backend = {"name": "late", "url": f"http://127.0.0.1:{port}/sse"}
    agg._file_backend_names.add("late")

    failed = await agg.discover_all([backend])
    assert failed == [backend]
    assert "late__echo" not in agg._tool_registry

    agg._queue_retry(failed)
    task = agg._retry_task
    proc = _spawn_sse_backend(port)
    try:
        _wait_for_port(port)
        await agg.asyncio.wait_for(task, timeout=30)
        assert "late__echo" in agg._tool_registry
        assert not agg._pending_discovery
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except agg.asyncio.CancelledError:
                pass
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.anyio
async def test_backend_dropped_from_file_stops_retrying(monkeypatch):
    monkeypatch.setattr(agg, "_RETRY_MIN_S", 0.05)
    backend = {"name": "gone", "url": f"http://127.0.0.1:{_free_port()}/sse"}
    agg._file_backend_names.add("gone")
    agg._queue_retry([backend])
    task = agg._retry_task

    # What reload does when the name leaves the backends file.
    agg._file_backend_names.discard("gone")
    del agg._pending_discovery["gone"]

    await agg.asyncio.wait_for(task, timeout=10)
    assert "gone" not in agg._backends


@pytest.mark.anyio
async def test_rediscovery_replaces_tools_instead_of_duplicating():
    port = _free_port()
    proc = _spawn_sse_backend(port)
    try:
        _wait_for_port(port)
        backend = {"name": "twice", "url": f"http://127.0.0.1:{port}/sse"}
        first = await agg._discover_backend(backend)
        second = await agg._discover_backend(backend)
        names = [t.name for t in agg._tool_list if t.name.startswith("twice__")]
        assert first == second == len(names) == len(set(names))
    finally:
        proc.kill()
        proc.wait(timeout=5)
