"""Bind-address policy and bearer-token enforcement."""

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import server

# conftest's autouse state-reset fixture is async, so every test runs under anyio.
pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "host, token, allowed",
    [
        ("127.0.0.1", None, True),
        ("localhost", None, True),
        ("::1", None, True),
        ("0.0.0.0", None, False),
        ("10.1.2.3", None, False),
        ("0.0.0.0", "s3cret", True),
    ],
)
async def test_bind_policy(monkeypatch, host, token, allowed):
    monkeypatch.setenv("AGGREGATOR_BIND_HOST", host)
    if token:
        monkeypatch.setenv("AGGREGATOR_AUTH_TOKEN", token)
    else:
        monkeypatch.delenv("AGGREGATOR_AUTH_TOKEN", raising=False)

    if allowed:
        assert server.load_network_settings() == (host, token)
    else:
        with pytest.raises(SystemExit):
            server.load_network_settings()


async def test_default_bind_is_loopback(monkeypatch):
    monkeypatch.delenv("AGGREGATOR_BIND_HOST", raising=False)
    monkeypatch.delenv("AGGREGATOR_AUTH_TOKEN", raising=False)
    assert server.load_network_settings() == ("127.0.0.1", None)


def _app():
    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/backends", ok, methods=["GET", "POST"])])


async def test_no_token_passes_through():
    client = TestClient(server.require_bearer_token(_app(), None))
    assert client.get("/backends").status_code == 200


@pytest.mark.parametrize(
    "headers, status",
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": "s3cret"}, 401),
        ({"Authorization": "Bearer s3cret"}, 200),
    ],
)
async def test_token_enforced(headers, status):
    client = TestClient(server.require_bearer_token(_app(), "s3cret"))
    assert client.get("/backends", headers=headers).status_code == status
    assert client.post("/backends", headers=headers).status_code == status


# --- Requests a web page makes (#10) ---------------------------------------


def _guarded(bind_host="127.0.0.1", origins=()):
    return TestClient(
        server.refuse_browser_requests(_app(), bind_host, set(origins)),
        base_url="http://127.0.0.1:8100",
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, MCP clients
        {"Host": "localhost:8100"},
        {"Host": "[::1]:8100"},
        {"Sec-Fetch-Site": "none"},  # typed into the address bar
        {"Sec-Fetch-Site": "same-origin"},
    ],
)
async def test_non_browser_and_direct_requests_pass(headers):
    assert _guarded().get("/backends", headers=headers).status_code == 200
    assert _guarded().post("/backends", headers=headers).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        # A page's fetch(..., {mode: "no-cors", body: "..."}) — no preflight.
        {"Origin": "https://evil.example", "Content-Type": "text/plain"},
        {"Origin": "http://127.0.0.1:8080"},  # another local web app
        {"Origin": "null"},  # sandboxed iframe, file:// page
        {"Sec-Fetch-Site": "cross-site"},  # no-cors GET carries no Origin
        {"Sec-Fetch-Site": "same-site"},
        {"Host": "rebound.evil.example:8100"},  # DNS rebinding
    ],
)
async def test_browser_requests_refused(headers):
    assert _guarded().get("/backends", headers=headers).status_code == 403
    assert _guarded().post("/backends", headers=headers).status_code == 403


async def test_allowed_origin_passes():
    client = _guarded(origins={"http://localhost:6274"})
    headers = {"Origin": "http://localhost:6274/", "Sec-Fetch-Site": "same-site"}
    assert client.post("/backends", headers=headers).status_code == 200


async def test_host_not_checked_on_external_bind():
    client = _guarded(bind_host="0.0.0.0")
    assert client.get("/backends", headers={"Host": "mddemo:8100"}).status_code == 200


async def test_allowed_origins_from_env(monkeypatch):
    monkeypatch.setenv(
        "AGGREGATOR_ALLOWED_ORIGINS", " http://localhost:6274/ ,HTTP://X:1"
    )
    assert server.load_allowed_origins() == {"http://localhost:6274", "http://x:1"}
    monkeypatch.delenv("AGGREGATOR_ALLOWED_ORIGINS")
    assert server.load_allowed_origins() == set()


# --- POST /backends input rules (#10) --------------------------------------


def _management():
    return TestClient(
        Starlette(routes=[Route("/backends", server.handle_backends, methods=["POST"])])
    )


async def test_add_backend_refuses_stdio(monkeypatch):
    called = []
    monkeypatch.setattr(server, "_activate", lambda s: called.append(s))
    body = {"name": "x", "transport": "stdio", "command": "powershell", "args": []}
    response = _management().post("/backends", json=body)
    assert response.status_code == 403
    assert called == [] and "x" not in server._states


@pytest.mark.parametrize(
    "content, content_type, status",
    [
        ('{"name": "x", "url": "http://h/sse"}', "text/plain", 415),
        ('{"name": "x", "url": "http://h/sse"}', None, 415),
        ('["x"]', "application/json", 400),
        ("not json", "application/json", 400),
    ],
)
async def test_add_backend_requires_a_json_object(content, content_type, status):
    headers = {"Content-Type": content_type} if content_type else {}
    response = _management().post("/backends", content=content, headers=headers)
    assert response.status_code == status
