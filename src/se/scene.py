import json
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from io import BytesIO
from ipaddress import AddressValueError, IPv4Address

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter


class SceneError(ValueError):
    pass


class JSONObject(list):
    """Distinguish JSON objects from arrays and preserve duplicate keys for validation."""


@dataclass(frozen=True)
class Column:
    heading: str
    plane: str
    kind: str


SCENE_SHEETS = ("om", "tenant")
NETWORK_COLUMNS = {
    "OM-IP": "om",
    "OM-VIP": "om",
    "APP-OM-IP": "app-om",
    "APP-OM-VIP": "app-om",
    "Service-IP": "service",
    "Service-VIP": "service",
    "External-IP": "external",
    "External-VIP": "external",
    "Service-External-IP": "service-external",
}
SKIP = "N/A"
JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)


@dataclass(frozen=True)
class Slot:
    sheet: str
    cell: str
    node: str
    column: Column
    resource: tuple[str, ...]
    span: tuple[int, int] | None = None


@dataclass
class Scene:
    slots: list[Slot] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    existing: set[IPv4Address] = field(default_factory=set)
    existing_vips: dict[tuple[str, ...], dict[IPv4Address, list[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )
    originals: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def resources(self) -> dict[tuple[str, ...], Slot]:
        resources = {}
        for slot in self.slots:
            resources.setdefault(slot.resource, slot)
        return resources


def ipv4(value: object) -> IPv4Address | None:
    if isinstance(value, str):
        try:
            return IPv4Address(value.strip())
        except AddressValueError:
            pass
    return None


def json_vips(value: str) -> Iterator[tuple[str, str, tuple[int, int]]]:
    """Return keys, decoded values, and offsets into the original JSON text."""
    document = json.loads(value, object_pairs_hook=JSONObject)
    if not isinstance(document, JSONObject):
        raise SceneError("VIP JSON must be an object")
    strings = iter(JSON_STRING.finditer(value))
    seen = set()
    for key, values in document:
        if key in seen or not key.strip():
            raise SceneError("VIP JSON keys must be unique and nonempty")
        seen.add(key)
        next(strings)  # Key token
        if type(values) is not list or not all(
            isinstance(item, str) for item in values
        ):
            raise SceneError("VIP JSON values must be arrays of strings")
        for item in values:
            token = next(strings)
            yield key, item, (token.start() + 1, token.end() - 1)


def parse_value(scene: Scene, sheet: str, cell: str, node: str, column: Column, value):
    location = f"{sheet}!{cell}"
    if isinstance(value, str) and value.strip().upper() == SKIP:
        return
    if value is None or value == "":
        if column.kind != "vip_json":
            resource = (column.plane, column.kind, sheet, cell)
            scene.slots.append(Slot(sheet, cell, node, column, resource))
        return
    if column.kind != "vip_json":
        address = ipv4(value)
        if address is None:
            scene.issues.append(
                f"{location}: expected an IPv4 address, an empty cell, or N/A"
            )
        else:
            scene.existing.add(address)
        return
    if not isinstance(value, str):
        scene.issues.append(f"{location}: expected VIP JSON text")
        return
    try:
        items = list(json_vips(value))
    except SceneError, json.JSONDecodeError, RecursionError:
        scene.issues.append(f"{location}: invalid VIP JSON")
        return
    for key, item, span in items:
        resource = (column.plane, "vip_json", column.heading, key)
        if item == "":
            scene.originals[sheet, cell] = value
            scene.slots.append(Slot(sheet, cell, node, column, resource, span))
        elif (address := ipv4(item)) is not None:
            scene.existing.add(address)
            scene.existing_vips[resource][address].append(location)
        else:
            scene.issues.append(
                f"{location}: VIP JSON contains an invalid IPv4 address"
            )


def read_scene(contents: bytes) -> Scene:
    scene = Scene()
    with BytesIO(contents) as source:
        book = load_workbook(source, read_only=True)
        try:
            sheet_names = book.sheetnames
            if not any(name in sheet_names for name in SCENE_SHEETS):
                scene.issues.append("Expected at least one worksheet named om or tenant")
            for name in SCENE_SHEETS:
                count = sheet_names.count(name)
                if count == 0:
                    continue
                if count > 1:
                    scene.issues.append(f"{name}: duplicate worksheet")
                    continue
                sheet = book[name]
                sheet.reset_dimensions()
                header = next(sheet.iter_rows(max_row=1, values_only=True), ())
                positions = header_positions(scene, name, header)
                if positions is None or len(positions) == 1:
                    continue
                rows = sheet.iter_rows(
                    min_row=2, max_col=max(positions.values()), values_only=True
                )
                vip_kind = "vip_json" if name == "tenant" else "vip"
                ip_columns = [
                    (
                        positions[heading],
                        Column(
                            heading,
                            plane,
                            vip_kind if heading.endswith("-VIP") else "vm_ip",
                        ),
                    )
                    for heading, plane in NETWORK_COLUMNS.items()
                    if heading in positions
                ]
                node_index = positions["Node"] - 1
                for row_number, row in enumerate(rows, start=2):
                    node = row[node_index]
                    if node is None or not str(node).strip():
                        continue
                    for index, column in ip_columns:
                        cell = f"{get_column_letter(index)}{row_number}"
                        parse_value(scene, name, cell, str(node), column, row[index - 1])
        finally:
            book.close()
    for addresses in scene.existing_vips.values():
        if len(addresses) > 1:
            locations = sorted(
                {loc for entries in addresses.values() for loc in entries}
            )
            scene.issues.append(
                f"{', '.join(locations)}: conflicting filled addresses in one VIP group"
            )
    return scene


def header_positions(
    scene: Scene, sheet: str, header: tuple
) -> dict[str, int] | None:
    matches: dict[str, list[int]] = {"Node": []}
    for index, value in enumerate(header, start=1):
        if isinstance(value, str) and (value == "Node" or value in NETWORK_COLUMNS):
            matches.setdefault(value, []).append(index)

    positions = {}
    for heading, indices in matches.items():
        if not indices:
            scene.issues.append(f"{sheet}!1: missing header {heading}")
        elif len(indices) > 1:
            cells = ", ".join(f"{get_column_letter(index)}1" for index in indices)
            scene.issues.append(f"{sheet}!{cells}: duplicate header {heading}")
        else:
            positions[heading] = indices[0]
    return positions if len(positions) == len(matches) else None


def replacements(
    scene: Scene, addresses: dict[tuple[str, ...], str]
) -> dict[str, dict[str, str]]:
    changes: dict[str, dict[str, str]] = defaultdict(dict)
    spans: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)
    for slot in scene.slots:
        address = addresses[slot.resource]
        if slot.span is None:
            changes[slot.sheet][slot.cell] = address
        else:
            spans[slot.sheet, slot.cell].append((*slot.span, address))
    for (sheet, cell), edits in spans.items():
        value = scene.originals[sheet, cell]
        for start, end, address in sorted(edits, reverse=True):
            value = value[:start] + address + value[end:]
        changes[sheet][cell] = value
    return changes
