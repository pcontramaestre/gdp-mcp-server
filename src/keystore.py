"""GDP MCP Server — API Key Store.

Manages per-user API keys with persistent JSON storage.
Keys are stored as SHA-256 hashes — raw keys are never persisted.
Admin operations (create/list/revoke) are exposed via localhost-only endpoints.
"""

import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path

from . import (
    config as _config,  # noqa: F401  (loads .env before KEY_STORE_PATH is read)
)

logger = logging.getLogger("gdp_mcp.keystore")

# Default key store path — can be overridden via GDP_MCP_KEY_STORE_PATH env var
_DEFAULT_KEY_STORE_PATH = "/data/keys.json"
KEY_STORE_PATH = os.environ.get("GDP_MCP_KEY_STORE_PATH", _DEFAULT_KEY_STORE_PATH)

# Thread lock for concurrent access
_lock = threading.Lock()


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash a raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _key_prefix(raw_key: str) -> str:
    """First 8 characters of the raw key — used as human-readable identifier."""
    return raw_key[:8]


class KeyStoreError(RuntimeError):
    """The key store exists but cannot be read; never treated as empty."""


def _load_store() -> dict:
    """Load the key store from disk. Returns an empty store only if the file
    does not exist; an unreadable or corrupt file raises KeyStoreError so a
    later write cannot silently wipe the existing keys."""
    path = Path(KEY_STORE_PATH)
    if not path.exists():
        return {"keys": {}}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Failed to read key store at %s: %s", KEY_STORE_PATH, e)
        raise KeyStoreError(f"Cannot read key store at {KEY_STORE_PATH}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("keys", {}), dict):
        raise KeyStoreError(f"Key store at {KEY_STORE_PATH} has an unexpected format")
    data.setdefault("keys", {})
    return data


def _save_store(store: dict) -> None:
    """Persist the key store atomically, created with mode 0600 from the start."""
    path = Path(KEY_STORE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.error("Failed to write key store at %s: %s", KEY_STORE_PATH, e)
        raise


def generate_key(user: str) -> dict:
    """Generate a new API key for a user.

    Returns dict with the raw key (shown once), user, created timestamp,
    and key_prefix for future reference.
    """
    raw_key = secrets.token_hex(32)  # 64-char hex string
    hashed = _hash_key(raw_key)
    prefix = _key_prefix(raw_key)
    now = datetime.now(UTC).isoformat()

    with _lock:
        store = _load_store()
        store["keys"][hashed] = {
            "user": user,
            "created": now,
            "key_prefix": prefix,
        }
        _save_store(store)

    logger.info("Generated API key for user '%s' (prefix: %s)", user, prefix)

    return {
        "key": raw_key,
        "user": user,
        "created": now,
        "key_prefix": prefix,
    }


def validate_key(raw_key: str) -> dict | None:
    """Validate a raw API key against the store.

    Returns the key metadata (user, created, key_prefix) if valid, None otherwise.
    """
    if not raw_key:
        return None

    hashed = _hash_key(raw_key)

    try:
        with _lock:
            store = _load_store()
    except KeyStoreError:
        return None  # fail closed: nobody is authenticated by an unreadable store

    entry = store["keys"].get(hashed)
    if entry:
        logger.debug("Key validated for user '%s' (prefix: %s)", entry["user"], entry["key_prefix"])
    return entry


def list_keys() -> list:
    """List all active keys (masked — no raw keys or hashes exposed)."""
    with _lock:
        store = _load_store()

    return [
        {
            "key_prefix": meta["key_prefix"],
            "user": meta["user"],
            "created": meta["created"],
        }
        for meta in store["keys"].values()
    ]


def revoke_key(key_prefix: str) -> dict | None:
    """Revoke a key by its prefix. Returns the revoked key metadata, or None if not found."""
    with _lock:
        store = _load_store()
        target_hash = None
        target_meta = None
        for hashed, meta in store["keys"].items():
            if meta["key_prefix"] == key_prefix:
                target_hash = hashed
                target_meta = meta
                break

        if target_hash is None:
            return None

        del store["keys"][target_hash]
        _save_store(store)

    logger.info("Revoked API key for user '%s' (prefix: %s)", target_meta["user"], key_prefix)
    return {"status": "revoked", "key_prefix": key_prefix, "user": target_meta["user"]}


def has_any_keys() -> bool:
    """Check if any keys exist in the store."""
    with _lock:
        store = _load_store()
    return len(store["keys"]) > 0
