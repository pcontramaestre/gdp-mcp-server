"""Tests for structured system metrics (Buff Usage Monitor)."""

import json

import httpx
import pytest

from src.client import GDPClient
from src.config import GDPConfig
from src.metrics import (
    build_metrics,
    fetch_system_metrics,
    find_warnings,
    humanize_uptime,
    parse_sample,
)


def row(**over):
    base = {
        "Timestamp": "2026-09-30 17:09:53",
        "% CPU Sniffer": "0",
        "% Mem Sniffer": "6",
        "% CPU Mysql": "1",
        "% Mem Mysql": "17",
        "Free Buffer Space": "100",
        "Analyzer Rate": "0",
        "Logger Rate": "0",
        "Analyzer Queue Length": "0",
        "Logger Queue Length": "0",
        "Session Queue Length": "166",
        "Analyzer Queue Drops": "0",
        "Priority Queue Drops": "0",
        "Mysql Disk Usage": "2",
        "System Cpu Load": "2",
        "System Memory Usage": "79",
        "System Root Disk Usage": "29",
        "System Var Disk Usage": "9",
        "System Uptime": "87661",
        "Mysql Is Up": "0",
    }
    base.update(over)
    return base


def test_parse_sample_groups_and_types():
    s = parse_sample(row())
    assert s["timestamp"] == "2026-09-30 17:09:53"
    assert s["system"]["cpu_load_pct"] == 2
    assert s["system"]["memory_usage_pct"] == 79
    assert s["system"]["var_disk_usage_pct"] == 9
    assert s["system"]["uptime"] == "1d 0h 21m"
    assert s["sniffer"]["free_buffer_space_pct"] == 100
    assert s["sniffer"]["session_queue_length"] == 166
    assert s["mysql"]["disk_usage_pct"] == 2


def test_parse_sample_tolerates_blank_missing_and_junk():
    s = parse_sample({"Timestamp": "t", "System Cpu Load": "", "System Memory Usage": "n/a"})
    assert s["system"]["cpu_load_pct"] is None
    assert s["system"]["memory_usage_pct"] is None
    assert s["system"]["var_disk_usage_pct"] is None
    assert s["system"]["uptime"] is None


def test_parse_sample_accepts_decimals():
    assert parse_sample(row(**{"System Cpu Load": "12.5"}))["system"]["cpu_load_pct"] == 12.5


def test_humanize_uptime():
    assert humanize_uptime(0) == "0d 0h 0m"
    assert humanize_uptime(1451718) == "16d 19h 15m"
    assert humanize_uptime(None) is None


def test_healthy_sample_has_no_warnings():
    assert find_warnings(parse_sample(row())) == []


def test_warnings_fire_on_each_rule():
    bad = row(
        **{
            "System Cpu Load": "90",
            "System Memory Usage": "95",
            "System Root Disk Usage": "80",
            "System Var Disk Usage": "85",
            "Mysql Disk Usage": "81",
            "Free Buffer Space": "20",
            "Analyzer Queue Drops": "3",
            "Priority Queue Drops": "1",
        }
    )
    warnings = find_warnings(parse_sample(bad))
    assert len(warnings) == 8
    assert "system.var_disk_usage_pct = 85 (>= 80)" in warnings
    assert "sniffer.free_buffer_space_pct = 20 (<= 20)" in warnings
    assert "sniffer.analyzer_queue_drops = 3 (> 0)" in warnings


def test_missing_values_never_warn():
    assert find_warnings(parse_sample({"Timestamp": "t"})) == []


def test_build_metrics_single_sample():
    result = build_metrics([row()])
    assert result["status"] == "ok"
    assert result["warnings"] == []
    assert "history" not in result and "raw" not in result
    assert result["timestamp"] == "2026-09-30 17:09:53"


def test_build_metrics_history_and_raw():
    rows = [row(Timestamp="t2", **{"System Cpu Load": "7"}), row(Timestamp="t1")]
    result = build_metrics(rows, include_raw=True)
    assert result["system"]["cpu_load_pct"] == 7           # latest = first row
    assert [h["timestamp"] for h in result["history"]] == ["t2", "t1"]
    assert result["history"][0]["cpu_load_pct"] == 7
    assert result["raw"]["Mysql Is Up"] == "0"


def test_build_metrics_status_warning():
    result = build_metrics([row(**{"System Var Disk Usage": "91"})])
    assert result["status"] == "warning"
    assert result["warnings"] == ["system.var_disk_usage_pct = 91 (>= 80)"]


# ── fetch_system_metrics against a mocked transport ─────────────


class _Auth:
    def invalidate(self):
        pass

    async def get_token(self):
        return "tok"


def client_for(handler):
    config = GDPConfig(host="gdp.example.com", api_key="dummy")
    client = GDPClient(config, _Auth())
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_fetch_sends_expected_report_request():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[row()])

    result = await fetch_system_metrics(client_for(handler))
    body = json.loads(seen[0].content)
    assert seen[0].method == "POST" and seen[0].url.path.endswith("/online_report")
    assert body["reportName"] == "Buff Usage Monitor"
    assert body["fetchSize"] == 1 and body["sortType"] == "DESC"
    assert body["reportParameter"]["QUERY_FROM_DATE"] == "NOW -15 MINUTE"
    assert body["reportParameter"]["SHOW_ALIASES"] == "TRUE"
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_fetch_clamps_samples_and_widens_window():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=[row()])

    await fetch_system_metrics(client_for(handler), samples=500)
    await fetch_system_metrics(client_for(handler), samples=0)
    assert seen[0]["fetchSize"] == 60
    assert seen[0]["reportParameter"]["QUERY_FROM_DATE"] == "NOW -120 MINUTE"
    assert seen[1]["fetchSize"] == 1


@pytest.mark.asyncio
async def test_fetch_raises_on_api_error():
    client = client_for(
        lambda r: httpx.Response(200, json={"ErrorCode": "23", "ErrorMessage": "bad report"})
    )
    with pytest.raises(RuntimeError, match="bad report"):
        await fetch_system_metrics(client)


@pytest.mark.asyncio
async def test_fetch_raises_when_no_rows():
    with pytest.raises(RuntimeError, match="no rows"):
        await fetch_system_metrics(client_for(lambda r: httpx.Response(200, json=[])))
