"""/health must be public but reveal nothing; details need the admin token."""

import pytest
from starlette.testclient import TestClient

from src import keystore, server


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(keystore, "KEY_STORE_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setenv("GDP_APPLIANCES", "oci")
    monkeypatch.setenv("GDP_OCI_HOST", "10.9.8.7")
    monkeypatch.setenv("GDP_OCI_PORT", "8443")
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "admin-secret")
    # No `with`: lifespan (endpoint discovery) is not started.
    return TestClient(server._create_http_app())


def test_public_health_reveals_nothing(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert "10.9.8.7" not in resp.text


def test_admin_health_requires_token(client):
    assert client.get("/admin/health").status_code == 403
    bad = client.get("/admin/health", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 403


def test_admin_health_returns_details_with_token(client):
    resp = client.get(
        "/admin/health", headers={"Authorization": "Bearer admin-secret"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["appliances"] == {"oci": "10.9.8.7:8443"}
    assert body["active_keys"] == 0


def test_admin_health_disabled_without_admin_token(client, monkeypatch):
    monkeypatch.delenv("MCP_ADMIN_TOKEN")
    resp = client.get("/admin/health", headers={"Authorization": "Bearer "})
    assert resp.status_code == 403
