"""OAuth2 password-grant token management for GDP REST API."""

import logging
import time

import httpx

from .config import GDPConfig

logger = logging.getLogger(__name__)


class GDPAuth:
    """Acquires and caches OAuth2 tokens using the password grant flow."""

    def __init__(self, config: GDPConfig) -> None:
        self._config = config
        self._token: str | None = None
        self._expires_at: float = 0
        self._http: httpx.AsyncClient | None = None

    def _get_http(self) -> httpx.AsyncClient:
        """Return the persistent HTTP client, creating it on first use."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                verify=self._config.verify_ssl, timeout=30.0
            )
        return self._http

    async def aclose(self) -> None:
        """Close the persistent HTTP client (call on server shutdown)."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get_token(self) -> str:
        """Return a valid Bearer token, refreshing if expired."""
        if self._token and time.time() < self._expires_at:
            return self._token
        return await self._acquire_token()

    async def _acquire_token(self) -> str:
        """Request a new token from the GDP OAuth endpoint."""
        logger.info("Requesting OAuth token from %s", self._config.token_url)
        http = self._get_http()
        if self._config.api_key:
            resp = await http.post(
                self._config.token_url,
                headers={
                    "Authorization": f"HOBA {self._config.api_key.strip()}",
                    "Content-Type": "application/json",
                },
            )
        else:
            resp = await http.post(
                self._config.token_url,
                data={
                    "grant_type": "password",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "username": self._config.username,
                    "password": self._config.password,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        resp.raise_for_status()
        data = resp.json()

        expires_in = data.get("expires_in", 300)
        self._token = data["access_token"]
        self._expires_at = time.time() + expires_in - 30  # refresh 30s early
        logger.info("OAuth token acquired (expires in %ds)", expires_in)
        return self._token

    def invalidate(self) -> None:
        """Force token refresh on next call."""
        self._token = None
        self._expires_at = 0
