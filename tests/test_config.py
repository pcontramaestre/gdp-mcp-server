"""Tests for discovery cache location (GDP_CACHE_DIR)."""

from src.config import _DEFAULT_CACHE_DIR, GDPConfig


def test_cache_dir_defaults_to_project_root(monkeypatch):
    monkeypatch.delenv("GDP_CACHE_DIR", raising=False)
    cfg = GDPConfig(host="h")
    assert cfg.cache_path.parent == _DEFAULT_CACHE_DIR
    assert cfg.cache_path_for("oci").parent == _DEFAULT_CACHE_DIR


def test_cache_dir_override_is_created_and_used(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "cache"
    monkeypatch.setenv("GDP_CACHE_DIR", str(target))
    cfg = GDPConfig(host="h")
    assert cfg.cache_path == target / "gdp_discovery_with_params.json"
    assert cfg.cache_path_for("aws") == target / "gdp_discovery_aws.json"
    assert target.is_dir()


def test_blank_cache_dir_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("GDP_CACHE_DIR", "  ")
    assert GDPConfig(host="h").cache_path.parent == _DEFAULT_CACHE_DIR
