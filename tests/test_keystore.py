import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dotenv_is_loaded_before_key_store_path_is_read():
    """Importing keystore first must still load .env (it reads its path at import)."""
    code = "import sys, src.keystore; print('src.config' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip().endswith("True")


# ── store hardening ─────────────────────────────────────────────

import os  # noqa: E402
import stat  # noqa: E402

import pytest  # noqa: E402

from src import keystore  # noqa: E402


@pytest.fixture
def store(monkeypatch, tmp_path):
    path = tmp_path / "keys.json"
    monkeypatch.setattr(keystore, "KEY_STORE_PATH", str(path))
    return path


def test_store_is_created_private_and_without_leftovers(store):
    keystore.generate_key("alice")
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600
    assert not store.with_name("keys.json.tmp").exists()


def test_generated_key_validates_and_revokes(store):
    k = keystore.generate_key("alice")
    assert keystore.validate_key(k["key"])["user"] == "alice"
    keystore.revoke_key(k["key_prefix"])
    assert keystore.validate_key(k["key"]) is None


def test_corrupt_store_is_never_overwritten(store):
    store.write_text("{ not json")
    with pytest.raises(keystore.KeyStoreError):
        keystore.generate_key("mallory")
    assert store.read_text() == "{ not json"


def test_unreadable_store_fails_closed(store):
    good = keystore.generate_key("alice")
    store.write_text("{ not json")
    assert keystore.validate_key(good["key"]) is None


def test_missing_store_is_simply_empty(store):
    assert keystore.list_keys() == []
    assert keystore.validate_key("whatever") is None
