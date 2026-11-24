from ipaddress import IPv4Address
from unittest.mock import Mock

import pytest

from se.config import ConfigError, read_networks
from se.probe import SshProbe, classify_ping

HEADER = b"PING host (192.0.2.1) 56(84) bytes of data.\n"
SUMMARY = b"\n--- host ping statistics ---\n"


@pytest.mark.parametrize(
    ("code", "extra", "counts", "stderr", "state"),
    [
        (
            1,
            b"",
            b"1 packets transmitted, 0 received, 100% packet loss, time 0ms\n",
            b"",
            "no_reply",
        ),
        (
            1,
            b"From gateway Destination Host Unreachable\n",
            b"1 packets transmitted, 0 received\n",
            b"",
            "error",
        ),
        (
            1,
            b"",
            b"1 packets transmitted, 0 received\n",
            b"Network is unreachable",
            "error",
        ),
        (2, b"", b"1 packets transmitted, 0 received\n", b"", "error"),
        (1, b"", b"", b"", "error"),
        (
            0,
            b"64 bytes from host: icmp_seq=1 ttl=64 time=0.5 ms\n",
            b"1 packet transmitted, 1 received, 0% packet loss\n",
            b"",
            "used",
        ),
    ],
)
def test_linux_ping_classification(code, extra, counts, stderr, state):
    assert classify_ping(code, HEADER + extra + SUMMARY + counts, stderr).state == state


@pytest.mark.parametrize(
    ("dotenv", "override", "expected"),
    [
        ("IPALLOC_SSH_PASSWORD=00123", None, "00123"),
        ("IPALLOC_SSH_PASSWORD=' pa#ss ' # comment", None, " pa#ss "),
        ("IPALLOC_SSH_PASSWORD=pa#ss", None, "pa#ss"),
        ("OTHER_KEY=ignored", None, None),
        ("IPALLOC_SSH_PASSWORD=local", "override", "override"),
        ("IPALLOC_SSH_PASSWORD=local", "", None),
        (None, "override", "override"),
        (None, None, None),
    ],
)
def test_ssh_password_sources(tmp_path, monkeypatch, dotenv, override, expected):
    config = tmp_path / "jump_host.yaml"
    config.write_text(
        "host: jump.example.invalid\nuser: operator\ninterfaces: {}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv + "\n", encoding="utf-8")
    if override is None:
        monkeypatch.delenv("IPALLOC_SSH_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("IPALLOC_SSH_PASSWORD", override)
    client = Mock()
    monkeypatch.setattr("se.probe.paramiko.SSHClient", lambda: client)

    if expected is None:
        with pytest.raises(ConfigError, match="Set IPALLOC_SSH_PASSWORD"):
            SshProbe(config, {}).connect()
        client.connect.assert_not_called()
        return

    SshProbe(config, {}).connect()

    connection = client.connect.call_args.kwargs
    assert connection["username"] == "operator"
    assert connection["password"] == expected
    assert connection["allow_agent"] is False
    assert connection["look_for_keys"] is False


def test_legacy_yaml_password_points_to_dotenv(tmp_path):
    config = tmp_path / "jump_host.yaml"
    config.write_text(
        "host: jump.example.invalid\nuser: operator\npassword: old-value\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError,
        match=r"^Move password from jump_host.yaml to \.env as IPALLOC_SSH_PASSWORD$",
    ):
        SshProbe(config, {}).connect()


def test_malformed_dotenv_password_reports_only_the_line(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "OTHER_KEY=ignored\nIPALLOC_SSH_PASSWORD='unclosed\n", encoding="utf-8"
    )
    config = tmp_path / "jump_host.yaml"
    config.write_text(
        "host: jump.example.invalid\nuser: operator\ninterfaces: {}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IPALLOC_SSH_PASSWORD", raising=False)

    with pytest.raises(
        ConfigError, match=r"^Invalid password entry in \.env at line 2$"
    ):
        SshProbe(config, {}).connect()


def test_probe_uses_jump_host_mapping_and_excludes_its_address(tmp_path, monkeypatch):
    network_config = tmp_path / "networks.yaml"
    network_config.write_text(
        "networks:\n  om:\n    cidr: 192.0.2.0/24\n"
        "    usable_start: 192.0.2.20\n    usable_end: 192.0.2.99\n",
        encoding="utf-8",
    )
    jump_config = tmp_path / "jump_host.yaml"
    jump_config.write_text(
        "host: jump.example.invalid\nuser: operator\ninterfaces:\n  om: ens192\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IPALLOC_SSH_PASSWORD", "fixture-password")
    monkeypatch.setattr("se.probe.paramiko.SSHClient", Mock())
    networks = read_networks(network_config)
    probe = SshProbe(jump_config, networks)
    probe.run = Mock(
        side_effect=[
            (0, b"2: ens192 inet 192.0.2.20/24 scope global ens192\n", b""),
            (
                1,
                HEADER
                + SUMMARY
                + b"1 packets transmitted, 0 received, 100% packet loss\n",
                b"",
            ),
        ]
    )

    assert probe.observe(networks["om"], IPv4Address("192.0.2.20")).state == "used"
    probe.run.assert_called_once_with("ip", "-4", "-o", "addr", "show", "dev", "ens192")
    assert probe.observe(networks["om"], IPv4Address("192.0.2.21")).state == "no_reply"
    probe.run.assert_called_with(
        "env",
        "LC_ALL=C",
        "ping",
        "-4",
        "-n",
        "-I",
        "ens192",
        "-c",
        "1",
        "-W",
        "1",
        "192.0.2.21",
    )
