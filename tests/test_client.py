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
