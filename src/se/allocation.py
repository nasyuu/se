import os
from collections import Counter
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import ConfigError, Network
from .probe import Probe
from .scene import Scene, SceneError
from .workbook import write_workbook


@dataclass
class Plan:
    addresses: dict[tuple[str, ...], str] = field(default_factory=dict)
    reused: set[tuple[str, ...]] = field(default_factory=set)
    shortages: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)


def validate_scene(scene: Scene, networks: dict[str, Network]):
    needed = {resource[0] for resource in scene.resources} | {
        key[0] for key in scene.existing_vips
    }
    if missing := needed - networks.keys():
        raise ConfigError(
            f"Missing network configuration: {', '.join(sorted(missing))}"
        )
    issues = list(scene.issues)
    for (plane, *_), addresses in scene.existing_vips.items():
        for address, locations in addresses.items():
            if address not in networks[plane].cidr:
                issues.append(
                    f"{', '.join(sorted(set(locations)))}: filled VIP is outside {plane}"
                )
    if issues:
        raise SceneError("\n".join(issues))


def candidates(network: Network, existing: set[IPv4Address]):
    for number in range(int(network.start), int(network.end) + 1):
        address = IPv4Address(number)
        if network.permits(address) and address not in existing:
            yield address


def plan_allocation(scene: Scene, networks: dict[str, Network], probe: Probe) -> Plan:
    validate_scene(scene, networks)
    result = Plan()
    by_plane = {
        plane: candidates(network, scene.existing)
        for plane, network in networks.items()
    }
    for resource in sorted(scene.resources, key=lambda key: key[1] == "vm_ip"):
        plane = resource[0]
        if filled := scene.existing_vips.get(resource):
            result.addresses[resource] = str(next(iter(filled)))
            result.reused.add(resource)
            continue
        for address in by_plane[plane]:
            observation = probe.observe(networks[plane], address)
            if observation.state == "error":
                result.errors[plane, observation.detail] += 1
            if observation.state == "no_reply":
                result.addresses[resource] = str(address)
                break
        else:
            result.shortages[plane] += 1
    return result


def publish_workbook(
    source: Path, output: Path, contents: bytes, changes: dict[str, dict[str, str]]
):
    with NamedTemporaryFile(dir=output.parent, prefix=".se-", suffix=".xlsx") as stage:
        write_workbook(contents, changes, stage)
        stage.flush()
        os.fsync(stage.fileno())
        if source.read_bytes() != contents:
            raise SceneError(
                "Source workbook changed while the plan was being prepared"
            )
        os.link(stage.name, output)
