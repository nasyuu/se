import os
import re
import shlex
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path
from typing import Literal, Protocol

import paramiko

from .config import ConfigError, Network, read_yaml


@dataclass(frozen=True)
class Observation:
    state: Literal["used", "no_reply", "error"]
    detail: str


class Probe(Protocol):
    def observe(self, network: Network, address: IPv4Address) -> Observation: ...


SUMMARY = re.compile(
    rb"^1 packets? transmitted,\s*(\d+) (?:packets? )?received\b", re.MULTILINE
)
IFACE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.:-]{0,14}")
PASSWORD_KEY = "IPALLOC_SSH_PASSWORD"
PASSWORD_ENTRY = re.compile(rf"^\s*(?:export\s+)?{PASSWORD_KEY}\s*=(.*)$")


def ssh_password() -> str | None:
    if PASSWORD_KEY in os.environ:
        return os.environ[PASSWORD_KEY]
    password = None
    try:
        with Path(".env").open(encoding="utf-8-sig") as stream:
            for number, line in enumerate(stream, start=1):
                match = PASSWORD_ENTRY.match(line)
                if match is None:
                    continue
                value = match[1].lstrip()
                if not value.startswith(("'", '"')):
                    password = re.split(r"\s+#", match[1], maxsplit=1)[0].strip()
                    continue
                try:
                    tokens = shlex.split(value, comments=True)
                except ValueError:
                    tokens = []
                if len(tokens) != 1:
                    raise ConfigError(
                        f"Invalid password entry in .env at line {number}"
                    )
                password = tokens[0]
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as error:
        raise ConfigError("Cannot read .env") from error
    return password


def classify_ping(status: int, stdout: bytes, stderr: bytes) -> Observation:
    counts = SUMMARY.findall(stdout)
    if not stderr.strip() and counts == [b"1"] and status == 0:
        return Observation("used", "ICMP echo reply")
    if not stderr.strip() and counts == [b"0"] and status == 1:
        # Unreachable diagnostics (which can appear on stdout) do not establish a valid probe.
        lines = [line for line in stdout.splitlines() if line.strip()]
        if (
            len(lines) in (3, 4)
            and lines[0].startswith(b"PING ")
            and lines[1].startswith(b"--- ")
        ):
            return Observation("no_reply", "completed ICMP check without a reply")
    return Observation("error", f"unexpected ping diagnostics (exit {status})")


class SshProbe:
    def __init__(self, config_path: Path, networks: dict[str, Network]):
        self.path = config_path
        self.networks = networks
        self.client = None
        self.interfaces: dict[str, str] = {}
        self.interface_addresses: dict[str, set[IPv4Address]] = {}
        self.timeout = 0
        self.wait_seconds = 0

    def close(self):
        if self.client is not None:
            self.client.close()

    def connect(self):
        options = read_yaml(self.path)
        if "password" in options:
            raise ConfigError(
                f"Move password from {self.path.name} to .env as {PASSWORD_KEY}"
            )
        if unknown := options.keys() - {
            "host",
            "user",
            "port",
            "known_hosts",
            "interfaces",
            "timeout_seconds",
            "ping_wait_seconds",
        }:
            fields = ", ".join(sorted(map(str, unknown)))
            raise ConfigError(f"Unsupported fields in {self.path.name}: {fields}")
        base = self.path.parent
        try:
            host = options["host"]
            user = options["user"]
            port = options.get("port", 22)
            self.timeout = options.get("timeout_seconds", 8)
            self.wait_seconds = options.get("ping_wait_seconds", 1)
            known_hosts = options.get("known_hosts", "known_hosts")
            if (
                not isinstance(host, str)
                or not host
                or not isinstance(user, str)
                or not user
            ):
                raise ValueError
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError
            if type(self.wait_seconds) is not int or type(self.timeout) is not int:
                raise ValueError
            if not 1 <= self.wait_seconds < self.timeout <= 60:
                raise ValueError
            if not isinstance(known_hosts, str):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigError(f"Invalid SSH configuration in {self.path}") from error

        mappings = options.get("interfaces")
        if not isinstance(mappings, dict):
            raise ConfigError(
                "jump_host.yaml must map network planes to interface names in interfaces"
            )
        interfaces = {}
        for plane in self.networks:
            interface = mappings.get(plane)
            if not isinstance(interface, str) or not IFACE.fullmatch(interface):
                raise ConfigError(
                    f"Invalid or missing interface for {plane} in jump_host.yaml"
                )
            interfaces[plane] = interface
        if len(set(interfaces.values())) != len(interfaces):
            raise ConfigError(
                "Each network plane must have a distinct interface in jump_host.yaml"
            )
        password = ssh_password()
        if not password:
            raise ConfigError(f"Set {PASSWORD_KEY} in .env or the environment")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.load_host_keys(str(base / known_hosts))
            client.connect(
                hostname=host,
                port=port,
                username=user,
                password=password,
                allow_agent=False,
                look_for_keys=False,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                channel_timeout=self.timeout,
            )
        except (OSError, ValueError, paramiko.SSHException) as error:
            client.close()
            kind = type(error).__name__
            raise ConfigError(
                f"SSH connection or verified host key failed ({kind})"
            ) from error
        self.client = client
        self.interfaces = interfaces

    def run(self, *argv: str) -> tuple[int, bytes, bytes]:
        stdin, stdout, stderr = self.client.exec_command(
            shlex.join(argv), timeout=self.timeout
        )
        try:
            return_code = stdout.channel
            output = stdout.read()
            diagnostics = stderr.read()
            return return_code.recv_exit_status(), output, diagnostics
        finally:
            stdin.close()
            stdout.channel.close()

    def observe(self, network: Network, address: IPv4Address) -> Observation:
        if not network.permits(address):
            raise ValueError("Probe destination is outside the configured usable range")
        try:
            if self.client is None:
                self.connect()
            interface = self.interfaces[network.name]
            if network.name not in self.interface_addresses:
                code, output, stderr = self.run(
                    "ip", "-4", "-o", "addr", "show", "dev", interface
                )
                addresses = set()
                if code == 0 and not stderr.strip():
                    for prefix in re.findall(
                        rb"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", output
                    ):
                        assigned = IPv4Interface(prefix.decode())
                        if assigned.network == network.cidr:
                            addresses.add(assigned.ip)
                self.interface_addresses[network.name] = addresses
            assigned_addresses = self.interface_addresses[network.name]
            if not assigned_addresses:
                return Observation(
                    "error",
                    "interface address or prefix does not match the configured plane",
                )
            if address in assigned_addresses:
                return Observation(
                    "used", "IPv4 address is assigned to the jump host interface"
                )
            result = self.run(
                "env",
                "LC_ALL=C",
                "ping",
                "-4",
                "-n",
                "-I",
                interface,
                "-c",
                "1",
                "-W",
                str(self.wait_seconds),
                str(address),
            )
        except OSError, paramiko.SSHException:
            return Observation("error", "SSH probe command failed or timed out")
        return classify_ping(*result)
