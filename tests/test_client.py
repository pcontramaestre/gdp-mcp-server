"""Tests for persistent HTTP client reuse in GDPClient / GDPAuth."""

import json

import httpx
import pytest

from src.auth import GDPAuth
from src.client import GDPClient
from src.config import GDPConfig


@pytest.fixture
def config():
    return GDPConfig(host="gdp.example.com", api_key="dummy")


def test_client_reuses_http_instance(config):
    client = GDPClient(config, GDPAuth(config))
    assert client._get_http() is client._get_http()


def test_auth_reuses_http_instance(config):
    auth = GDPAuth(config)
    assert auth._get_http() is auth._get_http()


@pytest.mark.asyncio
async def test_aclose_closes_and_allows_recreation(config):
    client = GDPClient(config, GDPAuth(config))
    first = client._get_http()
    await client.aclose()
    assert first.is_closed
    assert client._http is None
    assert client._get_http() is not first
    await client.aclose()


@pytest.mark.asyncio
async def test_aclose_is_noop_when_never_used(config):
    await GDPClient(config, GDPAuth(config)).aclose()
    await GDPAuth(config).aclose()


@pytest.mark.asyncio
async def test_health_check_ok_reports_latency(config):
    class FakeAuth:
        invalidated = False

        def invalidate(self):
            self.invalidated = True

        async def get_token(self):
            return "tok"

    auth = FakeAuth()
    info = await GDPClient(config, auth).health_check()
    assert auth.invalidated  # forces a real round trip, not the cache
    assert info["reachable"] and info["authenticated"]
    assert isinstance(info["latency_ms"], int)


@pytest.mark.asyncio
async def test_health_check_failure_reports_error(config):
    class FailingAuth:
        def invalidate(self):
            pass

        async def get_token(self):
            raise RuntimeError("boom")

    info = await GDPClient(config, FailingAuth()).health_check()
    assert info["reachable"] is False
    assert info["authenticated"] is False
    assert info["error"] == "boom"
    assert "latency_ms" in info


# ── GDPClient.request() against a mocked transport ──────────────

class FakeAuth:
    """Hands out tok-1, tok-2, ... and records invalidations."""

    def __init__(self):
        self.count = 0
        self.invalidations = 0

    def invalidate(self):
        self.invalidations += 1

    async def get_token(self):
        if self.invalidations >= self.count:
            self.count += 1
        return f"tok-{self.count}"


def make_client(config, handler):
    auth = FakeAuth()
    client = GDPClient(config, auth)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, auth


@pytest.mark.asyncio
async def test_get_sends_query_params_and_bearer(config):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client, _ = make_client(config, handler)
    result = await client.request("GET", "some_api", {"a": "1"})
    assert result == {"ok": True}
    assert seen[0].method == "GET"
    assert seen[0].url.params["a"] == "1"
    assert seen[0].headers["Authorization"] == "Bearer tok-1"
    assert str(seen[0].url).startswith(f"{config.base_url}/some_api")


@pytest.mark.asyncio
async def test_post_sends_json_body(config):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client, _ = make_client(config, handler)
    await client.request("post", "some_api", {"x": 2})
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {"x": 2}


@pytest.mark.asyncio
async def test_401_refreshes_token_and_retries_once(config):
    tokens = []

    def handler(request):
        tokens.append(request.headers["Authorization"])
        if len(tokens) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    client, auth = make_client(config, handler)
    result = await client.request("GET", "some_api")
    assert result == {"ok": True}
    assert auth.invalidations == 1
    assert tokens == ["Bearer tok-1", "Bearer tok-2"]


@pytest.mark.asyncio
async def test_persistent_401_raises_after_single_retry(config):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    client, _ = make_client(config, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.request("GET", "some_api")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_http_error_status_raises(config):
    client, _ = make_client(config, lambda r: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await client.request("GET", "some_api")


@pytest.mark.asyncio
async def test_204_returns_success_dict(config):
    client, _ = make_client(config, lambda r: httpx.Response(204))
    assert await client.request("DELETE", "some_api") == {
        "status": "success",
        "http_code": 204,
    }


@pytest.mark.asyncio
async def test_non_json_body_is_wrapped_and_truncated(config):
    client, _ = make_client(config, lambda r: httpx.Response(200, text="x" * 5000))
    result = await client.request("GET", "some_api")
    assert result["status"] == "success"
    assert result["http_code"] == 200
    assert len(result["body"]) == 2000
