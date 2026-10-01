"""Safety checks for REST API calls made through ``gdp_execute_api``."""

import re

from .cli import _DESTRUCTIVE_PATTERNS

# A REST resource is a plain path of letters, digits, "_", "-" and "/". This
# keeps "..", "?", "#", "%", spaces and absolute URLs out of the request URL.
_RESOURCE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-/]*$")

_READ_ONLY_PREFIXES = ("list_", "get_")
# Online reports are queried with POST but only read data.
_READ_ONLY_RESOURCES = {"online_report"}


def valid_resource_name(name: str) -> bool:
    return bool(_RESOURCE_RE.match(name)) and "//" not in name


def api_requires_confirmation(function_name: str, verb: str) -> bool:
    """True when the call may change the appliance and needs user approval.

    Reads (GET, ``list_*``/``get_*``, online reports) go through; any other
    verb, or a GET whose name carries a mutating verb, is confirmed first.
    """
    name = function_name.lower()
    mutating_name = _DESTRUCTIVE_PATTERNS.search(name) is not None
    if verb.upper() == "GET":
        return mutating_name
    if name in _READ_ONLY_RESOURCES:
        return False
    return mutating_name or not name.startswith(_READ_ONLY_PREFIXES)
