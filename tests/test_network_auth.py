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
