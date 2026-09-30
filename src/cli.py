"""Guard CLI client — execute GDP CLI commands over SSH.

The Guard CLI uses a forced ``cli_wrapper`` login shell that only accepts
commands via an interactive PTY (``invoke_shell``).  ``exec_command`` always
fails with "Incorrect number of arguments / Usage: cli_wrapper".
"""

import asyncio
import logging
import os
import re
import threading
import time

import paramiko

from .config import GDPConfig

logger = logging.getLogger("gdp_mcp.cli")

# Strip ANSI escape sequences from CLI output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Commands that modify system state — blocked unless confirm_destructive=True.
# Includes Guardium mutation verbs (store, halt, set, …), not only stop/delete.
_DESTRUCTIVE_PATTERNS = re.compile(
    r"\b(restart|reboot|shutdown|delete|remove|drop|restore|"
    r"reset|purge|truncate|kill|stop|disable|decommission|"
    r"uninstall|format|wipe|"
    r"store|set|create|add|modify|update|enable|install|upgrade|"
    r"apply|deploy|halt|clear|abort|suspend|revoke|unregister)\b",
    re.IGNORECASE,
)

# Guard CLI prompt pattern — e.g. "guardiumdpdp.ibm.com> "
_PROMPT_RE = re.compile(r"[\w.\-]+>\s*$")

# Commands that require interactive input — cannot be automated via paramiko.
# Categories:
#   TUI:        diag, iptraf (curses menu)
#   WIZARD:     configure_*, backup system, restore backup, import file
#   PASSWORD:   change_cli_password, store user password,
#               store alerter smtp authentication password
#   PASTE:      store certificate … console, store cert_key … console,
#               store stap certificate, store certificate rsa_securid console
#   MENU:       store ssl_configuration (cipher toggle)
#   LONG-RUN:   fileserver (blocks until duration expires)
_INTERACTIVE_CMDS = re.compile(
    r"^\s*("
    r"diag|iptraf"
    r"|change_cli_password"
    r"|store\s+user\s+password"
    r"|store\s+alerter\s+smtp\s+authentication\s+password"
    r"|store\s+certificate\s+.*\bconsole\b"
    r"|store\s+cert_key\s+.*\bconsole\b"
    r"|store\s+stap\s+certificate"
    r"|store\s+certificate\s+rsa_securid\s+console"
    r"|store\s+ssl_configuration"
    r"|configure_archive"
    r"|configure_export"
    r"|configure_purge"
    r"|configure_results_archive"
    r"|configure_cold_storage"
    r"|configure_cold_storage_data_streaming"
    r"|backup\s+system"
    r"|restore\s+backup"
    r"|import\s+file"
    r"|fileserver"
    r")\b",
    re.IGNORECASE,
)


# The Guard CLI closes a session that is idle for ~4 minutes; never hold a
# pooled session longer than this, whatever GDP_CLI_IDLE_TTL says.
_MAX_IDLE_TTL = 210.0


class GDPCLIClient:
    """SSH client for the Guard CLI (port and user from GDP_CLI_PORT / GDP_CLI_USER)."""

    def __init__(self, config: GDPConfig) -> None:
        self._config = config
        self._available: bool | None = None
        # Pooled session state — guarded by _lock (one command at a time).
        self._persistent = config.cli_persistent
        self._idle_ttl = min(max(config.cli_idle_ttl, 0.0), _MAX_IDLE_TTL)
        self._clock = time.monotonic
        self._lock = threading.Lock()
        self._client: paramiko.SSHClient | None = None
        self._chan: paramiko.Channel | None = None
        self._last_used = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._config.cli_pass or self._config.cli_key_file)

    async def check_reachable(self, timeout: float = 5.0) -> dict:
        """TCP-probe the CLI SSH port (no login, no banner wait).

        Returns reachability and connect latency in ms; a failed probe
        includes an ``error`` message.
        """
        host, port = self._config.cli_host, self._config.cli_port
        start = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except (TimeoutError, OSError) as exc:
            return {
                "reachable": False,
                "latency_ms": round((time.perf_counter() - start) * 1000),
                "error": str(exc) or type(exc).__name__,
            }
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return {
            "reachable": True,
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }

    def close(self) -> None:
        """Close the pooled SSH session, if any."""
        with self._lock:
            self._drop_session()

    async def execute(
        self,
        command: str,
        confirm_destructive: bool = False,
        timeout: int = 60,
    ) -> str:
        """Execute a Guard CLI command over SSH.

        Args:
            command: The CLI command (e.g. "show system hostname").
            confirm_destructive: Must be True for destructive commands.
            timeout: Total timeout in seconds (banner + command).

        Returns:
            Command output as a string.
        """
        if not self.configured:
            return (
                "Guard CLI is not configured. Set GDP_CLI_PASS or GDP_CLI_KEY_FILE in your environment. "
                "Optional: GDP_CLI_HOST (defaults to GDP_HOST), "
                "GDP_CLI_PORT (defaults to 22), GDP_CLI_USER (defaults to cli)."
            )

        command = command.strip()
        if not command:
            return "No command provided."

        if _INTERACTIVE_CMDS.match(command):
            return (
                f"⚠️ '{command}' requires interactive input (password prompt, "
                f"paste dialog, wizard, or TUI menu) and cannot be automated "
                f"over SSH. Run it manually via: "
                f"ssh {self._config.cli_user}@{self._config.cli_host} "
                f"-p {self._config.cli_port}"
            )

        if _DESTRUCTIVE_PATTERNS.search(command) and not confirm_destructive:
            return (
                f"⚠️ BLOCKED: '{command}' appears destructive.\n"
                f"This command may modify system state. "
                f"To proceed, call gdp_guard_cli with confirm_destructive=True.\n"
                f"Ask the user for confirmation first."
            )

        return await asyncio.to_thread(
            self._ssh_exec, command, timeout
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_response(chan: paramiko.Channel, timeout: float) -> tuple[str, bool, bool]:
        """Read from *chan* until the Guard CLI prompt or *timeout*.

        Returns ``(text, prompt_seen, channel_closed)``.
        """
        buf = b""
        prompt_seen = False
        closed = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = chan.recv(4096)
            except TimeoutError:
                time.sleep(0.3)
                continue
            except (OSError, EOFError):
                closed = True
                break
            if not chunk:
                closed = True
                break
            buf += chunk
            # Strip ANSI escapes before checking — the CLI often appends
            # control sequences (e.g. \x1b[K) after the prompt, which
            # breaks the $ anchor in _PROMPT_RE.
            clean_text = _ANSI_RE.sub("", buf.decode("utf-8", errors="replace"))
            if _PROMPT_RE.search(clean_text):
                prompt_seen = True
                break
        return buf.decode("utf-8", errors="replace"), prompt_seen, closed

    @staticmethod
    def _drain(chan: paramiko.Channel) -> None:
        """Discard any unread bytes so they are not mistaken for new output."""
        try:
            while chan.recv_ready():
                chan.recv(4096)
        except (OSError, EOFError):
            pass

    @staticmethod
    def _clean(raw: str, command: str) -> str:
        """Strip ANSI codes, the echoed command, and the trailing prompt."""
        text = _ANSI_RE.sub("", raw).replace("\r", "")
        # Remove the echoed command line
        lines = text.split("\n")
        cleaned: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == command.strip():
                continue
            # Remove trailing prompt line
            if _PROMPT_RE.match(stripped):
                continue
            cleaned.append(line)
        # Trim leading/trailing blank lines
        result = "\n".join(cleaned).strip()
        return result or "(no output)"

    def _error_message(self, exc: Exception) -> str:
        host, port, user = self._config.cli_host, self._config.cli_port, self._config.cli_user
        if isinstance(exc, paramiko.AuthenticationException):
            return (
                f"SSH authentication failed for {user}@{host}:{port}. "
                f"Check GDP_CLI_USER, GDP_CLI_PASS, or GDP_CLI_KEY_FILE."
            )
        if isinstance(exc, paramiko.SSHException):
            return f"SSH error connecting to {host}:{port}: {exc}"
        return f"Cannot reach {host}:{port}: {exc}"

    def _open_session(self, timeout: int) -> tuple[paramiko.SSHClient, paramiko.Channel]:
        """Connect, open the interactive Guard CLI shell and wait for its prompt."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        host = self._config.cli_host
        port = self._config.cli_port
        user = self._config.cli_user
        password = self._config.cli_pass
        key_file = self._config.cli_key_file

        try:
            logger.info("SSH %s@%s:%d — opening session", user, host, port)
            connect_kwargs = {
                "hostname": host,
                "port": port,
                "username": user,
                "timeout": 15,
                "look_for_keys": False,
                "allow_agent": False,
            }
            if key_file:
                connect_kwargs["key_filename"] = os.path.expanduser(key_file)
            if password:
                connect_kwargs["password"] = password

            client.connect(**connect_kwargs)
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)

            chan = client.invoke_shell(width=200, height=50)
            chan.settimeout(3)

            # Wait for the CLI banner + prompt (can take 10-15 s)
            banner, _, _ = self._read_response(chan, min(timeout * 0.5, 30))
            logger.debug("CLI banner: %s", banner[:200])
            return client, chan
        except BaseException:
            client.close()
            raise

    def _session_usable(self) -> bool:
        """True if the pooled session is open and was used recently enough."""
        if self._client is None or self._chan is None or self._chan.closed:
            return False
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            return False
        return self._clock() - self._last_used < self._idle_ttl

    def _drop_session(self) -> None:
        """Close and forget the pooled session (caller holds the lock)."""
        chan, client = self._chan, self._client
        self._chan = self._client = None
        for closer in (chan, client):
            if closer is not None:
                try:
                    closer.close()
                except Exception:  # best effort
                    logger.debug("Error closing CLI session", exc_info=True)

    def _ssh_exec(self, command: str, timeout: int) -> str:
        """Run *command* in the Guard CLI and return its cleaned output."""
        if not self._persistent:
            return self._ssh_exec_oneshot(command, timeout)
        with self._lock:
            return self._ssh_exec_pooled(command, timeout)

    def _ssh_exec_oneshot(self, command: str, timeout: int) -> str:
        """Open a fresh session for this single command, then close it."""
        client = None
        try:
            client, chan = self._open_session(timeout)
            logger.info("SSH command: %s", command)
            chan.send(command + "\n")
            cmd_timeout = max(timeout - min(timeout * 0.5, 30), 15)
            raw, _, _ = self._read_response(chan, cmd_timeout)
            chan.close()
            return self._clean(raw, command)
        except (paramiko.SSHException, OSError) as exc:
            return self._error_message(exc)
        finally:
            if client is not None:
                client.close()

    def _ssh_exec_pooled(self, command: str, timeout: int) -> str:
        """Run *command* on the pooled session, (re)connecting when needed.

        A session that turns out to be dead *before* the command is sent is
        replaced and the command is retried once. A command that was already
        sent is never re-sent, so it cannot run twice.
        """
        for attempt in (1, 2):
            reused = self._session_usable()
            try:
                if not reused:
                    self._drop_session()
                    self._client, self._chan = self._open_session(timeout)
                self._drain(self._chan)
                logger.info("SSH command (%s session): %s", "reused" if reused else "new", command)
                self._chan.send(command + "\n")
            except (paramiko.SSHException, OSError, EOFError) as exc:
                self._drop_session()
                if reused and attempt == 1:
                    logger.info("Pooled CLI session was stale (%s); reconnecting", exc)
                    continue
                return self._error_message(exc)
            break

        # A reused session has no banner to wait for.
        cmd_timeout = timeout if reused else max(timeout - min(timeout * 0.5, 30), 15)
        raw, prompt_seen, closed = self._read_response(self._chan, cmd_timeout)
        if closed or not prompt_seen:
            # Timed out or the CLI ended the session: its state is unknown, so
            # the next command must start from a clean session.
            self._drop_session()
        else:
            self._last_used = self._clock()
        return self._clean(raw, command)
