"""Tests for the pooled (persistent) Guard CLI SSH session."""

import paramiko
import pytest

from src.cli import _MAX_IDLE_TTL, GDPCLIClient
from src.config import GDPConfig


class FakeTime:
    """Replaces the ``time`` module inside src.cli with a controllable clock."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    monotonic = perf_counter = time

    def sleep(self, seconds):
        self.now += seconds


class FakeChan:
    def __init__(self, mode="normal"):
        self.mode = mode          # normal | stale | closed_after_send | hang
        self.closed = False
        self.sent = []
        self._queue = []

    def settimeout(self, _):
        pass

    def send(self, data):
        if self.mode == "stale":
            raise OSError("Socket is closed")
        command = data.strip()
        self.sent.append(command)
        if self.mode == "normal":
            self._queue.append(f"{command}\r\nout-{command}\r\nok\r\ngdp> ".encode())
        elif self.mode == "hang":
            self._queue.append(f"{command}\r\npartial".encode())
        # closed_after_send: nothing queued, recv() reports EOF

    def recv_ready(self):
        return bool(self._queue)

    def recv(self, _):
        if self._queue:
            return self._queue.pop(0)
        if self.mode == "closed_after_send":
            return b""
        raise TimeoutError

    def close(self):
        self.closed = True


class FakeTransport:
    def is_active(self):
        return True


class FakeClient:
    def __init__(self):
        self.closed = False

    def get_transport(self):
        return FakeTransport()

    def close(self):
        self.closed = True


@pytest.fixture
def clock(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr("src.cli.time", fake)
    return fake


def make(clock, modes=("normal",), **cfg):
    """Build a client whose _open_session hands out scripted fake sessions."""
    config = GDPConfig(host="h", cli_host="h", cli_pass="x", **cfg)
    client = GDPCLIClient(config)
    opened = []
    script = list(modes)

    def fake_open(timeout):
        mode = script.pop(0) if len(script) > 1 else script[0]
        pair = (FakeClient(), FakeChan(mode))
        opened.append(pair)
        return pair

    client._open_session = fake_open
    return client, opened


def test_session_is_reused_between_commands(clock):
    client, opened = make(clock)
    assert client._ssh_exec("show build", 60) == "out-show build\nok"
    assert client._ssh_exec("show unit type", 60) == "out-show unit type\nok"
    assert len(opened) == 1
    assert opened[0][1].sent == ["show build", "show unit type"]


def test_session_recycled_after_idle_ttl(clock):
    client, opened = make(clock, cli_idle_ttl=180)
    client._ssh_exec("show build", 60)
    clock.now += 181
    client._ssh_exec("show build", 60)
    assert len(opened) == 2
    assert opened[0][0].closed and opened[0][1].closed   # old one closed by us


def test_session_kept_within_idle_ttl(clock):
    client, opened = make(clock, cli_idle_ttl=180)
    client._ssh_exec("show build", 60)
    clock.now += 179
    client._ssh_exec("show build", 60)
    assert len(opened) == 1


def test_idle_ttl_is_capped_below_server_timeout(clock):
    client, _ = make(clock, cli_idle_ttl=9999)
    assert client._idle_ttl == _MAX_IDLE_TTL < 240


def test_stale_session_is_replaced_and_command_retried(clock):
    client, opened = make(clock, modes=("normal", "normal"))
    client._ssh_exec("show build", 60)
    opened[0][1].mode = "stale"          # server dropped it; send() now fails
    assert client._ssh_exec("show build", 60) == "out-show build\nok"
    assert len(opened) == 2


def test_command_already_sent_is_not_resent_when_session_dies(clock):
    client, opened = make(clock, modes=("closed_after_send", "normal"))
    assert client._ssh_exec("show build", 60) == "(no output)"
    assert opened[0][1].sent == ["show build"]     # sent exactly once
    assert client._chan is None                    # broken session dropped
    assert client._ssh_exec("show build", 60) == "out-show build\nok"
    assert len(opened) == 2


def test_timeout_without_prompt_drops_the_session(clock):
    client, opened = make(clock, modes=("hang", "normal"))
    assert client._ssh_exec("show build", 60) == "partial"
    assert opened[0][1].closed
    assert client._ssh_exec("show build", 60) == "out-show build\nok"
    assert len(opened) == 2


def test_connect_failure_returns_message_and_caches_nothing(clock):
    client, _ = make(clock)

    def boom(timeout):
        raise paramiko.AuthenticationException("bad")

    client._open_session = boom
    assert "authentication failed" in client._ssh_exec("show build", 60)
    assert client._chan is None and client._client is None


def test_persistence_can_be_disabled(clock):
    client, opened = make(clock, cli_persistent=False)
    client._ssh_exec("show build", 60)
    client._ssh_exec("show build", 60)
    assert len(opened) == 2
    assert all(c.closed and ch.closed for c, ch in opened)


def test_close_shuts_the_session_and_is_idempotent(clock):
    client, opened = make(clock)
    client._ssh_exec("show build", 60)
    client.close()
    client.close()
    assert opened[0][0].closed and opened[0][1].closed
    assert client._chan is None


def test_config_defaults_and_env(monkeypatch):
    monkeypatch.delenv("GDP_CLI_PERSISTENT", raising=False)
    monkeypatch.delenv("GDP_CLI_IDLE_TTL", raising=False)
    cfg = GDPConfig(host="h")
    assert cfg.cli_persistent is True and cfg.cli_idle_ttl == 180
    monkeypatch.setenv("GDP_CLI_PERSISTENT", "false")
    monkeypatch.setenv("GDP_CLI_IDLE_TTL", "60")
    assert GDPConfig(host="h").cli_persistent is False
    assert GDPConfig(host="h").cli_idle_ttl == 60
    monkeypatch.setenv("GDP_OCI_CLI_PERSISTENT", "true")
    assert GDPConfig.from_prefix("GDP_OCI").cli_persistent is True
