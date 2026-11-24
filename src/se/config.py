from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from itertools import combinations
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Network:
    name: str
    cidr: IPv4Network
    start: IPv4Address
    end: IPv4Address
    reserved: frozenset[IPv4Address]
    gateway: IPv4Address | None

    def permits(self, address: IPv4Address) -> bool:
        return (
            self.start <= address <= self.end
            and address != self.gateway
            and address not in self.reserved
        )


def read_yaml(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            result = yaml.safe_load(stream)
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise ConfigError(f"Cannot read configuration {path}") from error
    if not isinstance(result, dict):
        raise ConfigError(f"Expected a configuration mapping in {path}")
    return result


def read_networks(path: Path) -> dict[str, Network]:
    document = read_yaml(path).get("networks")
    if not isinstance(document, dict) or not document:
        raise ConfigError("networks.yaml must define at least one network")
    networks = {}
    for name, options in document.items():
        if not isinstance(name, str) or not name or not isinstance(options, dict):
            raise ConfigError(
                "Each network must have a name and a configuration mapping"
            )
        if unknown := options.keys() - {
            "cidr",
            "usable_start",
            "usable_end",
            "gateway",
            "reserved",
        }:
            fields = ", ".join(sorted(map(str, unknown)))
            raise ConfigError(f"Unsupported fields for network {name!r}: {fields}")
        try:
            cidr = IPv4Network(options["cidr"])
            start = IPv4Address(options["usable_start"])
            end = IPv4Address(options["usable_end"])
            gateway_value = options.get("gateway")
            gateway = IPv4Address(gateway_value) if gateway_value is not None else None
            exclusions = options.get("reserved", [])
            if not isinstance(exclusions, list):
                raise ValueError  # noqa: TRY004
            reserved = frozenset(map(IPv4Address, exclusions))
            if not cidr.network_address < start <= end < cidr.broadcast_address:
                raise ValueError
            if (
                gateway is not None
                and not cidr.network_address < gateway < cidr.broadcast_address
            ):
                raise ValueError
            if any(address not in cidr for address in reserved):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigError(
                f"Invalid CIDR, usable range, gateway, or reserved addresses for {name!r}"
            ) from error
        networks[name] = Network(name, cidr, start, end, reserved, gateway)
    for first, second in combinations(networks.values(), 2):
        if first.cidr.overlaps(second.cidr):
            raise ConfigError(f"Network CIDRs overlap: {first.name}, {second.name}")
    return networks
