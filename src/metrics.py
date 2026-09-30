"""Structured system metrics from the ``Buff Usage Monitor`` online report.

The appliance samples its own health about once a minute and exposes it
through the REST ``online_report`` resource. Every value comes back as a
string; this module turns the rows into typed, grouped JSON and flags
values that usually need attention.
"""

from __future__ import annotations

from typing import Any

REPORT_NAME = "Buff Usage Monitor"
MAX_SAMPLES = 60

# (group, output key, report column). Only columns whose meaning and unit are
# certain are curated; everything else is available via include_raw.
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("system", "cpu_load_pct", "System Cpu Load"),
    ("system", "memory_usage_pct", "System Memory Usage"),
    ("system", "root_disk_usage_pct", "System Root Disk Usage"),
    ("system", "var_disk_usage_pct", "System Var Disk Usage"),
    ("system", "uptime_seconds", "System Uptime"),
    ("sniffer", "cpu_pct", "% CPU Sniffer"),
    ("sniffer", "mem_pct", "% Mem Sniffer"),
    ("sniffer", "free_buffer_space_pct", "Free Buffer Space"),
    ("sniffer", "analyzer_rate", "Analyzer Rate"),
    ("sniffer", "logger_rate", "Logger Rate"),
    ("sniffer", "analyzer_queue_length", "Analyzer Queue Length"),
    ("sniffer", "logger_queue_length", "Logger Queue Length"),
    ("sniffer", "session_queue_length", "Session Queue Length"),
    ("sniffer", "analyzer_queue_drops", "Analyzer Queue Drops"),
    ("sniffer", "priority_queue_drops", "Priority Queue Drops"),
    ("mysql", "cpu_pct", "% CPU Mysql"),
    ("mysql", "mem_pct", "% Mem Mysql"),
    ("mysql", "disk_usage_pct", "Mysql Disk Usage"),
)

# Compact per-sample view used for the trend (history).
_HISTORY_KEYS = (
    ("cpu_load_pct", "system", "cpu_load_pct"),
    ("memory_usage_pct", "system", "memory_usage_pct"),
    ("var_disk_usage_pct", "system", "var_disk_usage_pct"),
    ("free_buffer_space_pct", "sniffer", "free_buffer_space_pct"),
    ("sniffer_cpu_pct", "sniffer", "cpu_pct"),
)

# (group, key, comparator, threshold). "gt"/"ge": warn when value is above;
# "le": warn when value is at or below. These are rules of thumb, not limits
# defined by Guardium.
_THRESHOLDS: tuple[tuple[str, str, str, float], ...] = (
    ("system", "cpu_load_pct", "ge", 85),
    ("system", "memory_usage_pct", "ge", 90),
    ("system", "root_disk_usage_pct", "ge", 80),
    ("system", "var_disk_usage_pct", "ge", 80),
    ("mysql", "disk_usage_pct", "ge", 80),
    ("sniffer", "free_buffer_space_pct", "le", 20),
    ("sniffer", "analyzer_queue_drops", "gt", 0),
    ("sniffer", "priority_queue_drops", "gt", 0),
)


def _to_number(value: Any) -> int | float | None:
    """Convert a report cell to a number; None for blank or non-numeric."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def humanize_uptime(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}d {hours}h {minutes}m"


def parse_sample(row: dict[str, Any]) -> dict[str, Any]:
    """Group one report row into system / sniffer / mysql sections."""
    sample: dict[str, Any] = {
        "timestamp": row.get("Timestamp"),
        "system": {},
        "sniffer": {},
        "mysql": {},
    }
    for group, key, column in _FIELDS:
        sample[group][key] = _to_number(row.get(column))
    sample["system"]["uptime"] = humanize_uptime(sample["system"]["uptime_seconds"])
    return sample


def find_warnings(sample: dict[str, Any]) -> list[str]:
    """Return human-readable warnings for values past the rule-of-thumb limits."""
    warnings = []
    for group, key, comparator, threshold in _THRESHOLDS:
        value = sample[group][key]
        if value is None:
            continue
        if comparator == "ge" and value >= threshold:
            warnings.append(f"{group}.{key} = {value} (>= {threshold:g})")
        elif comparator == "gt" and value > threshold:
            warnings.append(f"{group}.{key} = {value} (> {threshold:g})")
        elif comparator == "le" and value <= threshold:
            warnings.append(f"{group}.{key} = {value} (<= {threshold:g})")
    return warnings


def build_metrics(
    rows: list[dict[str, Any]], include_raw: bool = False
) -> dict[str, Any]:
    """Build the tool result from report rows (newest first)."""
    samples = [parse_sample(r) for r in rows]
    latest = samples[0]
    warnings = find_warnings(latest)
    result: dict[str, Any] = {
        "source": f"{REPORT_NAME} online report",
        "timestamp": latest["timestamp"],
        "status": "warning" if warnings else "ok",
        "warnings": warnings,
        "system": latest["system"],
        "sniffer": latest["sniffer"],
        "mysql": latest["mysql"],
    }
    if len(samples) > 1:
        result["history"] = [
            {"timestamp": s["timestamp"]}
            | {name: s[group][key] for name, group, key in _HISTORY_KEYS}
            for s in samples
        ]
    if include_raw:
        result["raw"] = rows[0]
    return result


async def fetch_system_metrics(
    client, samples: int = 1, include_raw: bool = False
) -> dict[str, Any]:
    """Fetch the latest *samples* rows of the report and build the metrics.

    Raises RuntimeError when the appliance rejects the report or returns no
    rows in the time window.
    """
    samples = max(1, min(int(samples), MAX_SAMPLES))
    body = {
        "reportName": REPORT_NAME,
        "indexFrom": "1",
        "fetchSize": samples,
        "sortColumn": "Timestamp",
        "sortType": "DESC",
        "reportParameter": {
            "QUERY_FROM_DATE": f"NOW -{max(15, samples * 2)} MINUTE",
            "QUERY_TO_DATE": "NOW",
            "REMOTE_SOURCE": "%",
            "SHOW_ALIASES": "TRUE",
        },
    }
    data = await client.request("POST", "online_report", body)
    if isinstance(data, dict):
        if data.get("ErrorCode"):
            raise RuntimeError(data.get("ErrorMessage") or f"ErrorCode {data['ErrorCode']}")
        raise RuntimeError(f"Unexpected report response: {str(data)[:200]}")
    if not data:
        raise RuntimeError(
            f"'{REPORT_NAME}' returned no rows in the last {max(15, samples * 2)} minutes; "
            "the appliance may not be recording health samples."
        )
    return build_metrics(data, include_raw=include_raw)
