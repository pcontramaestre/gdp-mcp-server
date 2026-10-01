"""GDP MCP tool definitions — all @mcp.tool() functions live here.

Targets MCP spec 2025-11-25:
  - ToolAnnotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint)
  - Human-readable title fields
  - Elicitation for destructive CLI commands (ctx.elicit)
  - Progress notifications via ctx.report_progress() — visible in Claude, VS Code, Cursor
  - MCP logging via ctx.log — visible in Claude, VS Code, Cline
  - Structured error responses with error codes and suggestions
  - Lifespan API — tools pull deps from ctx.request_context.lifespan_context
  - Multi-appliance — optional 'appliance' param routes to the right context
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .cli import redact_command, requires_confirmation
from .metrics import fetch_system_metrics

if TYPE_CHECKING:
    from .server import AppContext, ApplianceContext

logger = logging.getLogger("gdp_mcp")


def _get_app(ctx: Context) -> AppContext:
    """Extract the AppContext from the MCP request context."""
    return ctx.request_context.lifespan_context


def _resolve_appliance(ctx: Context, appliance: str | None) -> ApplianceContext:
    """Resolve an appliance by name, falling back to the default."""
    app = _get_app(ctx)
    return app.get(appliance)


async def _ensure_discovered(ctx: Context, appl: ApplianceContext) -> None:
    """Lazy-load endpoints on first tool call if not already loaded."""
    if not appl.discovery.loaded:
        await ctx.log("info", f"Discovering GDP API endpoints on '{appl.name}'…")
        await ctx.info(f"Discovering GDP API endpoints on '{appl.name}'…")
        cache = appl.config.cache_path_for(appl.name) if _get_app(ctx).is_multi else appl.config.cache_path
        count = await appl.discovery.discover(cache_path=cache, prefer_cache=True)
        if count == 0:
            logger.error("No GDP endpoints on '%s' — check connectivity and OAuth config", appl.name)
            await ctx.log("error", f"No GDP endpoints on '{appl.name}' — check connectivity")
        else:
            await ctx.log("info", f"Discovered {count} endpoints on '{appl.name}'")
            await ctx.info(f"Discovered {count} endpoints on '{appl.name}'")


def _error_response(code: str, message: str, suggestion: str = "") -> str:
    """Return a structured JSON error response."""
    error = {"error": True, "code": code, "message": message}
    if suggestion:
        error["suggestion"] = suggestion
    return json.dumps(error, indent=2)


# ── Elicitation schema for destructive CLI commands ─────────────

class DestructiveConfirmation(BaseModel):
    """User confirmation for destructive CLI operations."""
    confirm: bool = Field(description="Set to true to proceed with the destructive command")


def register_tools(mcp) -> None:
    """Register all GDP MCP tools on the given FastMCP server instance.

    Tools pull runtime deps (config, discovery, cli_client) from
    ctx.request_context.lifespan_context at call time — no globals needed.

    Every tool accepts an optional ``appliance`` parameter. When multiple
    appliances are configured (via GDP_APPLIANCES), the AI can target a
    specific one. Omitting it routes to the default appliance.
    """

    # ── Tool 1: Search APIs ─────────────────────────────────────

    @mcp.tool(
        title="Search GDP APIs",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def gdp_search_apis(
        query: str,
        category: str | None = None,
        verb: str | None = None,
        appliance: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Search GDP API endpoints by keyword.

        Use this to find available API operations. Returns matching endpoint names
        with HTTP methods, descriptions, and required parameters.

        Args:
            query: Search keyword (e.g. "datasource", "policy", "group", "report",
                   "vulnerability", "stap", "inspection")
            category: Optional — filter by category name
                      (e.g. "Datasource Builder", "Policy Builder", "Group Builder")
            verb: Optional — filter by HTTP method (GET, POST, PUT, DELETE)
            appliance: Optional — target appliance name (omit for default)
        """
        appl = _resolve_appliance(ctx, appliance)
        await ctx.report_progress(progress=0, total=3)
        await ctx.log("info", f"gdp_search_apis: query='{query}' category={category} verb={verb} appliance={appl.name}")
        await _ensure_discovered(ctx, appl)
        await ctx.report_progress(progress=1, total=3)
        results = appl.discovery.search(query, category=category, verb=verb)
        await ctx.report_progress(progress=2, total=3)
        if not results:
            cats = ", ".join(list(appl.discovery.categories.keys())[:10])
            return _error_response(
                "NO_RESULTS",
                f"No endpoints matching '{query}' on '{appl.name}'.",
                f"Try broader terms or use gdp_list_categories. Sample categories: {cats}",
            )

        await ctx.log("info", f"Found {len(results)} endpoint(s) matching '{query}'")
        await ctx.info(f"Found {len(results)} matching endpoint(s) on '{appl.name}'")

        lines = [f"Found {len(results)} GDP API endpoint(s) on '{appl.name}':\n"]
        for ep in results:
            req = ", ".join(ep.required_params) if ep.required_params else "none"
            lines.append(
                f"  {ep.verb:6s}  {ep.function_name}\n"
                f"         {ep.description[:120]}\n"
                f"         required params: {req}\n"
            )
        await ctx.report_progress(progress=3, total=3)
        return "\n".join(lines)

    # ── Tool 2: List Categories ─────────────────────────────────

    @mcp.tool(
        title="Browse API Categories",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def gdp_list_categories(
        appliance: str | None = None,
        ctx: Context = None,
    ) -> str:
        """List all GDP API categories with endpoint counts.

        Use this to understand the scope of the GDP API and find the right
        category to search within.

        Args:
            appliance: Optional — target appliance name (omit for default)
        """
        appl = _resolve_appliance(ctx, appliance)
        await ctx.report_progress(progress=0, total=2)
        await ctx.log("info", f"gdp_list_categories: appliance={appl.name}")
        await _ensure_discovered(ctx, appl)
        await ctx.report_progress(progress=1, total=2)
        cats = appl.discovery.categories
        total = sum(cats.values())

        app = _get_app(ctx)
        header = f"GDP API on '{appl.name}': {total} endpoints across {len(cats)} categories"
        if app.is_multi:
            header += f"\nConfigured appliances: {', '.join(app.names)} (default: {app.default_name})"
        lines = [header, ""]
        for cat, count in cats.items():
            lines.append(f"  {count:4d}  {cat}")
        await ctx.log("info", f"Listed {len(cats)} categories with {total} total endpoints")
        await ctx.report_progress(progress=2, total=2)
        return "\n".join(lines)

    # ── Tool 3: Get API Details ─────────────────────────────────

    @mcp.tool(
        title="Inspect API Parameters",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def gdp_get_api_details(
        api_function_name: str,
        appliance: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Get full parameter details for a specific GDP API endpoint.

        Call this before gdp_execute_api to understand what parameters are
        needed and what values are valid.

        Args:
            api_function_name: Exact function name (e.g. "list_group",
                               "create_datasource", "run_report_by_name")
            appliance: Optional — target appliance name (omit for default)
        """
        appl = _resolve_appliance(ctx, appliance)
        await ctx.report_progress(progress=0, total=3)
        await ctx.log("info", f"gdp_get_api_details: function={api_function_name} appliance={appl.name}")
        await _ensure_discovered(ctx, appl)
        await ctx.report_progress(progress=1, total=3)
        ep = appl.discovery.endpoints.get(api_function_name)
        if not ep:
            matches = [
                name
                for name in appl.discovery.endpoints
                if api_function_name.lower() in name.lower()
            ]
            if matches:
                return _error_response(
                    "NOT_FOUND",
                    f"Endpoint '{api_function_name}' not found on '{appl.name}'.",
                    f"Similar: {', '.join(matches[:8])}",
                )
            return _error_response(
                "NOT_FOUND",
                f"Endpoint '{api_function_name}' not found on '{appl.name}'.",
                "Use gdp_search_apis to find endpoints.",
            )

        await ctx.report_progress(progress=2, total=3)
        await ctx.log("info", f"Resolved {ep.verb} /restAPI/{ep.resource_name}")
        await ctx.info(f"Resolving {ep.verb} /restAPI/{ep.resource_name} on '{appl.name}'")

        info = {
            "function_name": ep.function_name,
            "http_method": ep.verb,
            "resource_path": f"/restAPI/{ep.resource_name}",
            "category": ep.category,
            "version": ep.version,
            "description": ep.description,
            "appliance": appl.name,
            "parameters": [],
        }
        for p in ep.parameters:
            param_info: dict = {
                "name": p["parameterName"],
                "type": p["parameterType"].rsplit(".", 1)[-1],
                "required": p.get("isRequired", False),
                "description": p.get("parameterDescription", ""),
            }
            if p.get("parameterValues"):
                param_info["valid_values"] = p["parameterValues"]
            info["parameters"].append(param_info)

        await ctx.report_progress(progress=3, total=3)
        return json.dumps(info, indent=2)

    # ── Tool 4: Execute API ─────────────────────────────────────

    @mcp.tool(
        title="Execute GDP API",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def gdp_execute_api(
        api_function_name: str,
        parameters: dict | None = None,
        appliance: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Execute a GDP REST API endpoint.

        Flow: gdp_search_apis → gdp_get_api_details → gdp_execute_api

        Args:
            api_function_name: Exact function name (e.g. "list_group")
            parameters: Dict of parameter key-value pairs for the API call.
                        Check gdp_get_api_details for required params.
            appliance: Optional — target appliance name (omit for default)
        """
        appl = _resolve_appliance(ctx, appliance)
        await ctx.report_progress(progress=0, total=4)
        await ctx.log("info", f"gdp_execute_api: function={api_function_name} appliance={appl.name}")
        await _ensure_discovered(ctx, appl)
        await ctx.report_progress(progress=1, total=4)
        ep = appl.discovery.endpoints.get(api_function_name)
        if not ep:
            # Fallback inteligente: permitir ejecución directa del recurso REST (ej. unit_data, stap, datasource)
            verb = "GET"
            call_params = dict(parameters) if parameters else {}
            if "method" in call_params:
                verb = call_params.pop("method").upper()
            elif "reportName" in call_params or "reportParameter" in call_params:
                verb = "POST"

            await ctx.log("info", f"Executing direct REST resource {verb} /restAPI/{api_function_name} on '{appl.name}'")
            await ctx.info(f"Direct calling GDP ({appl.name}): {verb} /restAPI/{api_function_name}")
            try:
                result = await appl.client.request(verb, api_function_name, params=call_params or None)
                await ctx.report_progress(progress=3, total=4)
                text = json.dumps(result, indent=2, default=str)
                if len(text) > 30_000:
                    count = len(result) if isinstance(result, list) else "N/A"
                    text = (
                        f"Response truncated ({len(text):,} chars, {count} items). "
                        f"Showing first 30,000 chars:\n\n{text[:30_000]}\n\n"
                        f"... [truncated — use parameters to filter results]"
                    )
                await ctx.report_progress(progress=4, total=4)
                return text
            except Exception as exc:
                return _error_response(
                    "NOT_FOUND",
                    f"Endpoint '{api_function_name}' not found in discovery index on '{appl.name}' and direct invocation failed: {exc}",
                    "Use gdp_search_apis to find endpoints or verify the endpoint path.",
                )

        await ctx.log("info", f"Calling {ep.verb} /restAPI/{ep.resource_name} on '{appl.name}'")
        await ctx.info(f"Calling GDP ({appl.name}): {ep.verb} /restAPI/{ep.resource_name}")
        await ctx.report_progress(progress=2, total=4)

        try:
            result = await appl.client.request(ep.verb, ep.resource_name, params=parameters)
            await ctx.report_progress(progress=3, total=4)
            text = json.dumps(result, indent=2, default=str)

            await ctx.log("info", f"Response: {len(text):,} chars from '{appl.name}'")
            await ctx.info(f"Got response ({len(text):,} chars) from '{appl.name}'")

            # Truncate very large responses to avoid overwhelming the LLM context
            if len(text) > 30_000:
                count = len(result) if isinstance(result, list) else "N/A"
                text = (
                    f"Response truncated ({len(text):,} chars, {count} items). "
                    f"Showing first 30,000 chars:\n\n{text[:30_000]}\n\n"
                    f"... [truncated — use parameters to filter results]"
                )
                await ctx.log("warning", f"Response truncated from {len(text):,} chars")
            await ctx.report_progress(progress=4, total=4)
            return text
        except Exception as exc:
            await ctx.log("error", f"API call failed on '{appl.name}': {exc}")
            return _error_response(
                "API_CALL_FAILED",
                f"API call failed on '{appl.name}': {exc}",
                f"Endpoint: {ep.verb} /restAPI/{ep.resource_name}. "
                f"Parameters: {parameters}. "
                f"Check: Is the GDP appliance reachable? Is the OAuth client registered?",
            )

    # ── Tool 5: Guard CLI ───────────────────────────────────────

    @mcp.tool(
        title="Run Guard CLI Command",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def gdp_guard_cli(
        command: str,
        appliance: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Execute a Guard CLI command on the GDP appliance over SSH.

        Use this for system-level operations that the REST API cannot perform:
        network configuration, backup/restore, diagnostics, inspection engine
        control, certificate management, patch management.

        Destructive commands (restart, delete, restore, stop, store, halt, set, etc.) will prompt
        the user for confirmation via MCP elicitation before executing.

        **Read the `gdp://cli/reference` resource for the full list of ~600 commands.**

        Common safe (read-only) commands:
          System:   show system hostname | clock | cpu profile | domain | patch installed | patch available | service_status all
          Network:  show network interface all | routes static | routes operational | dhcp | arp-table | verify
          Build:    show build | show os version | show openssh version | show openssl version
          Buffer:   show buffer
          Alerter:  show alerter state operational | smtp relay | smtp port | delay | poll
          Certs:    show certificate all | summary | warn_expired | gui | sniffer | keystore all
          Engines:  list inspection-engines | show inspection-engines all | show ignored port list
          Config:   show license | show gui port | show stap approval | show full-bypass
          Security: show installed security policy | show security policies
          Support:  support show hardware-info | support show db-status free % | support show top cpu
          Users:    show auth | show account lockout | show password expiration

        NOT supported — opens interactive TUI (cannot automate):
          diag (SQLGuard Diagnostics menu)

        Args:
            command: Guard CLI command (e.g. "show system hostname")
            appliance: Optional — target appliance name (omit for default)
        """
        appl = _resolve_appliance(ctx, appliance)
        await ctx.report_progress(progress=0, total=3)
        await ctx.log("info", f"gdp_guard_cli: command='{redact_command(command)}' appliance={appl.name}")
        if appl.cli_client is None:
            return _error_response(
                "CLI_NOT_CONFIGURED",
                f"Guard CLI is not available on '{appl.name}' — GDPCLIClient was not initialized.",
                f"Set GDP_CLI_PASS or GDP_CLI_KEY_FILE (or GDP_{appl.name.upper()}_CLI_PASS / GDP_{appl.name.upper()}_CLI_KEY_FILE) to enable CLI access.",
            )

        command = command.strip()
        if not command:
            return "No command provided."

        # Destructive command detection with MCP elicitation
        if requires_confirmation(command):
            await ctx.log("warning", f"Non-read-only command on '{appl.name}': '{redact_command(command)}'")
            await ctx.warning(f"Non-read-only command on '{appl.name}': '{redact_command(command)}'")
            await ctx.report_progress(progress=1, total=3)
            try:
                result = await ctx.elicit(
                    message=(
                        f"⚠️ The command '{command}' is not a known read-only command and may modify system state on '{appl.name}'.\n"
                        f"Do you want to proceed?"
                    ),
                    schema=DestructiveConfirmation,
                )
                if result.action != "accept" or not result.data or not result.data.confirm:
                    return f"Cancelled: '{command}' was not executed on '{appl.name}'."
            except Exception:
                # Client doesn't support elicitation — fall back to blocking
                return (
                    f"⚠️ BLOCKED: '{command}' appears destructive (appliance: {appl.name}).\n"
                    f"This command may modify system state. "
                    f"Your MCP client does not support elicitation for confirmation.\n"
                    f"Use a client that supports MCP spec 2025-11-25 elicitation, "
                    f"or run the command manually via SSH."
                )

        await ctx.report_progress(progress=2, total=3)
        await ctx.log("info", f"Executing CLI on '{appl.name}': {redact_command(command)}")
        await ctx.info(f"Executing CLI on '{appl.name}': {redact_command(command)}")

        result = await appl.cli_client.execute(command, confirm_destructive=True)
        await ctx.report_progress(progress=3, total=3)
        await ctx.log("info", f"CLI result: {len(result)} chars from '{appl.name}'")
        return result

    # ── Tool 6: List appliances + health ────────────────────────

    @mcp.tool(
        title="List GDP Appliances",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def gdp_list_appliances(ctx: Context = None) -> str:
        """List the configured GDP appliances, what they are, and whether they are reachable.

        Returns JSON with, per appliance: name, whether it is the default,
        REST API status (reachable, authenticated, latency_ms, error), the
        appliance identity from the REST API (unit.hostname, ip, unit_type,
        version, online) and Guard CLI status (configured, reachable,
        latency_ms, error). All appliances are probed concurrently. The CLI
        check is a TCP connect to the SSH port only — it does not log in.

        ``unit.unit_type`` is the raw Guardium value (e.g. "StandaloneNetInsp",
        the same information as the CLI command ``show unit type``), returned
        as-is without interpretation.

        Call this first to find out which appliances exist, what type they
        are and which are online, instead of trying a command to find out.
        """
        app = _get_app(ctx)
        await ctx.log("info", f"gdp_list_appliances: probing {len(app.appliances)} appliance(s)")

        async def probe(appl: ApplianceContext) -> dict:
            cfg = appl.config
            cli: dict = {"configured": appl.cli_client is not None}
            if appl.cli_client is not None:
                cli.update(host=cfg.cli_host, port=cfg.cli_port, user=cfg.cli_user)
            try:
                rest, cli_probe = await asyncio.gather(
                    asyncio.wait_for(appl.client.health_check(), timeout=20),
                    appl.cli_client.check_reachable()
                    if appl.cli_client is not None
                    else asyncio.sleep(0),
                )
            except TimeoutError:
                rest = {"host": cfg.host, "port": cfg.port, "reachable": False,
                        "authenticated": False, "error": "REST health check timed out"}
                cli_probe = None
            if isinstance(cli_probe, dict):
                cli.update(cli_probe)

            unit = None
            if rest.get("authenticated"):
                try:
                    unit = await asyncio.wait_for(appl.client.get_unit_info(), timeout=15)
                except Exception as exc:
                    unit = {"error": str(exc) or type(exc).__name__}
            return {
                "name": appl.name,
                "is_default": appl.name == app.default_name,
                "rest": rest,
                "unit": unit,
                "cli": cli,
            }

        results = await asyncio.gather(*(probe(a) for a in app.appliances.values()))
        return json.dumps(
            {"default": app.default_name, "appliances": list(results)}, indent=2
        )

    # ── Tool 7: System metrics ──────────────────────────────────

    @mcp.tool(
        title="Get GDP System Metrics",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def gdp_get_system_metrics(
        appliance: str | None = None,
        samples: int = 1,
        include_raw: bool = False,
        ctx: Context = None,
    ) -> str:
        """Get structured health metrics of a GDP appliance (REST, JSON).

        Reads the appliance's own health samples (the "Buff Usage Monitor"
        online report, recorded about once a minute) and returns typed JSON:
          - system: cpu_load_pct, memory_usage_pct, root_disk_usage_pct,
            var_disk_usage_pct, uptime_seconds / uptime
          - sniffer: cpu_pct, mem_pct, free_buffer_space_pct, analyzer/logger
            rates and queue lengths, analyzer/priority queue drops
          - mysql: cpu_pct, mem_pct, disk_usage_pct
          - status ("ok" | "warning") and warnings: values past rules of thumb
            (CPU >= 85, memory >= 90, / or /var or MySQL disk >= 80, free
            buffer <= 20, any queue drops). These are heuristics, not limits
            defined by Guardium.

        Args:
            appliance: Optional — target appliance name (omit for default)
            samples: Number of most recent samples (1-60). With more than one,
                a "history" list (newest first) shows the trend of CPU,
                memory, /var disk, free buffer and sniffer CPU.
            include_raw: Also return the complete unparsed report row of the
                latest sample (all columns, values as strings).
        """
        appl = _resolve_appliance(ctx, appliance)
        await ctx.log("info", f"gdp_get_system_metrics: appliance={appl.name} samples={samples}")
        try:
            metrics = await fetch_system_metrics(appl.client, samples, include_raw)
        except Exception as exc:
            await ctx.log("error", f"System metrics failed on '{appl.name}': {exc}")
            return _error_response(
                "METRICS_FAILED",
                f"Could not get system metrics from '{appl.name}': {exc}",
                "Check that the appliance is reachable (gdp_list_appliances) and that "
                "the API user can run online reports.",
            )
        return json.dumps({"appliance": appl.name, **metrics}, indent=2, default=str)
