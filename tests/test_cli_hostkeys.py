"""SSH host key handling for the Guard CLI client."""

import os
import stat

import paramiko

from src.cli import GDPCLIClient
from src.config import GDPConfig


def make(tmp_path, mode="tofu", host="gdp.example.com", port=22):
    kh = tmp_path / "sub" / "known_hosts"
    cfg = GDPConfig(
        cli_host=host, cli_port=port, cli_pass="x",
        cli_host_key_check=mode, cli_known_hosts=str(kh),
    )
    return GDPCLIClient(cfg), kh


def policy_of(client):
    return type(client._policy).__name__


def test_default_mode_is_tofu():
    assert GDPConfig(cli_pass="x").cli_host_key_check == "tofu"


def test_tofu_learns_and_persists_new_key(tmp_path):
    cli, kh = make(tmp_path)
    ssh = paramiko.SSHClient()
    cli._apply_host_key_policy(ssh)
    assert policy_of(ssh) == "AutoAddPolicy"

    key = paramiko.RSAKey.generate(1024)
    ssh.get_host_keys().add("[gdp.example.com]:22", "ssh-rsa", key)
    cli._remember_host_keys(ssh)

    assert kh.exists()
    assert stat.S_IMODE(os.stat(kh).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(kh.parent).st_mode) == 0o700

    # a later session loads the remembered key
    ssh2 = paramiko.SSHClient()
    cli._apply_host_key_policy(ssh2)
    stored = ssh2.get_host_keys().lookup("[gdp.example.com]:22")
    assert stored is not None and stored["ssh-rsa"] == key


def test_strict_rejects_unknown_hosts_and_does_not_write(tmp_path):
    cli, kh = make(tmp_path, mode="strict")
    ssh = paramiko.SSHClient()
    cli._apply_host_key_policy(ssh)
    assert policy_of(ssh) == "RejectPolicy"
    cli._remember_host_keys(ssh)
    assert not kh.exists()


def test_off_accepts_anything_and_does_not_persist(tmp_path):
    cli, kh = make(tmp_path, mode="off")
    ssh = paramiko.SSHClient()
    cli._apply_host_key_policy(ssh)
    assert policy_of(ssh) == "AutoAddPolicy"
    cli._remember_host_keys(ssh)
    assert not kh.exists()


def test_changed_key_message_explains_the_risk(tmp_path):
    cli, kh = make(tmp_path)
    exc = paramiko.BadHostKeyException("gdp.example.com", paramiko.RSAKey.generate(1024),
                                       paramiko.RSAKey.generate(1024))
    msg = cli._error_message(exc)
    assert "does NOT match" in msg and str(kh) in msg


def test_strict_rejection_message_points_to_known_hosts(tmp_path):
    cli, kh = make(tmp_path, mode="strict")
    msg = cli._error_message(paramiko.SSHException("Server 'x' not found in known_hosts"))
    assert "strict" in msg and str(kh) in msg


def test_unwritable_known_hosts_does_not_break_the_connection(tmp_path):
    cli, _ = make(tmp_path)
    blocker = tmp_path / "sub"
    blocker.write_text("a file where the directory should be")
    ssh = paramiko.SSHClient()
    cli._remember_host_keys(ssh)  # must only log a warning
