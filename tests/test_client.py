"""Tests for persistent HTTP client reuse in GDPClient / GDPAuth."""

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
