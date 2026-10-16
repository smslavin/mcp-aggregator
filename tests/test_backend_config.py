"""The backends file is checked before anything starts (#11)."""

import json

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import server

pytestmark = pytest.mark.anyio

GOOD = [
    {"name": "mes", "url": "http://127.0.0.1:8007/sse"},
    {"name": "local", "transport": "stdio", "command": "python", "args": ["x.py"]},
    {"name": "http", "transport": "streamable_http", "url": "http://h/mcp"},
]


async def test_a_good_file_passes():
    server.check_backends(GOOD)


@pytest.mark.parametrize(
    "entries, problem",
    [
        ({"name": "x"}, "JSON list"),
        (["x"], "must be an object"),
        ([{"url": "http://h/sse"}], "no name"),
        ([{"name": "a__b", "url": "http://h/sse"}], "may not contain"),
        ([{"name": "a_", "url": "http://h/sse"}], "may not contain"),
        (
            [{"name": "a", "transport": "websocket", "url": "ws://h"}],
            "unknown transport",
        ),
        ([{"name": "a", "transport": "sse"}], "needs a url"),
        ([{"name": "a", "transport": "stdio", "url": "http://h"}], "needs a command"),
        (
            [{"name": "a", "url": "http://h/1"}, {"name": "a", "url": "http://h/2"}],
            "listed twice",
        ),
    ],
)
async def test_bad_files_are_refused(entries, problem):
    with pytest.raises(ValueError, match=problem):
        server.check_backends(entries)


async def test_the_shipped_examples_are_valid():
    root = server.os.path.dirname(server.__file__)
    for name in ("backends.json", "backends.production.example.json"):
        with open(server.os.path.join(root, name)) as f:
            server.check_backends(json.load(f))


async def test_reload_with_a_bad_file_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "backends.json"
    path.write_text(
        json.dumps([{"name": "a", "transport": "websocket", "url": "ws://h"}])
    )
    monkeypatch.setattr(server, "_backends_path", str(path))
    removed = []
    monkeypatch.setattr(server, "_remove_backend", lambda name: removed.append(name))
    server._backends["kept"] = {"name": "kept", "url": "http://h/sse"}
    app = Starlette(
        routes=[
            Route("/backends/reload", server.handle_reload_backends, methods=["POST"])
        ]
    )
    response = TestClient(app).post("/backends/reload")
    assert (
        response.status_code == 400 and "unknown transport" in response.json()["error"]
    )
    assert removed == [] and "kept" in server._backends


async def test_adding_a_backend_checks_it_too():
    app = Starlette(
        routes=[Route("/backends", server.handle_backends, methods=["POST"])]
    )
    response = TestClient(app).post(
        "/backends", json={"name": "a__b", "url": "http://h/sse"}
    )
    assert response.status_code == 400 and "may not contain" in response.json()["error"]
