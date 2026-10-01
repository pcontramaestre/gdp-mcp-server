"""REST API call classification and resource-name validation."""

import json
from pathlib import Path

import pytest

from src.safety import api_requires_confirmation, valid_resource_name


@pytest.mark.parametrize(
    "name, verb, needs",
    [
        ("list_group", "GET", False),
        ("display_stap_config", "GET", False),
        ("gim_list_registered_clients", "GET", False),
        ("online_report", "POST", False),
        ("list_something", "POST", False),
        ("create_datasource", "POST", True),
        ("delete_user", "DELETE", True),
        ("update_group", "PUT", True),
        ("run_report_by_name", "POST", True),
        # a read verb does not excuse a mutating name
        ("restart_stap", "GET", True),
        ("list_delete_candidates_and_delete_them", "POST", True),
    ],
)
def test_api_requires_confirmation(name, verb, needs):
    assert api_requires_confirmation(name, verb) is needs


@pytest.mark.parametrize("name", ["unit_data", "datasource", "report/online", "list-x_1"])
def test_valid_resource_names(name):
    assert valid_resource_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "../etc/passwd", "a/../b", "unit_data?x=1", "unit_data#f", "/abs", "a//b",
     "https://evil.example/x", "a b", "a%2e%2e", ".hidden"],
)
def test_invalid_resource_names(name):
    assert not valid_resource_name(name)


def test_every_discovered_resource_name_is_valid():
    """The validation must not reject real endpoints (cached discovery files)."""
    root = Path(__file__).resolve().parents[1]
    files = list(root.glob("gdp_discovery_*.json"))
    if not files:
        pytest.skip("no discovery cache present")
    for f in files:
        for ep in json.loads(f.read_text()):
            assert valid_resource_name(ep["resourceName"]), ep["resourceName"]


# ── confirmation flow ───────────────────────────────────────────

from types import SimpleNamespace  # noqa: E402

from src.tools import _confirm_mutation, _param_names  # noqa: E402


class FakeCtx:
    def __init__(self, action="accept", confirm=True, raises=False):
        self._result = SimpleNamespace(action=action, data=SimpleNamespace(confirm=confirm))
        self._raises = raises
        self.messages = []

    async def log(self, *a, **k):
        pass

    async def elicit(self, message, schema):
        if self._raises:
            raise RuntimeError("elicitation not supported")
        self.messages.append(message)
        return self._result


@pytest.mark.asyncio
async def test_confirm_mutation_approved():
    assert await _confirm_mutation(FakeCtx(), "oci", "delete_user", "x") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ctx", [FakeCtx(action="decline"), FakeCtx(confirm=False)])
async def test_confirm_mutation_declined(ctx):
    out = await _confirm_mutation(ctx, "oci", "delete_user", "x")
    assert out and out.startswith("Cancelled")


@pytest.mark.asyncio
async def test_confirm_mutation_blocks_when_client_cannot_ask():
    out = await _confirm_mutation(FakeCtx(raises=True), "oci", "delete_user", "x")
    assert out and "BLOCKED" in out


def test_param_names_never_include_values():
    text = _param_names({"user": "bob", "password": "hunter2"})
    assert "password" in text and "hunter2" not in text and "bob" not in text
