import argparse
import os
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from .allocation import plan_allocation, publish_workbook
from .config import ConfigError, read_networks
from .probe import SshProbe
from .scene import SceneError, read_scene, replacements


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="se", description="Probe and fill IPv4 addresses in an Excel workbook"
    )
    commands = root.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect", help="show workbook slots and validation issues"
    )
    inspect.add_argument("workbook", type=Path)
    apply = commands.add_parser(
        "apply", help="probe addresses, review the plan, and write an Excel copy"
    )
    apply.add_argument("workbook", type=Path)
    apply.add_argument("--config-dir", type=Path, default=Path("config"))
    apply.add_argument("--output", type=Path, help="path for the filled Excel copy")
    return root


def execute(args) -> int:
    source = args.workbook.resolve()
    if args.workbook.suffix.lower() != ".xlsx" or not source.is_file():
        raise ConfigError("Specify an existing .xlsx workbook")
    contents = source.read_bytes()
    scene = read_scene(contents)
    if scene.issues:
        raise SceneError("\n".join(scene.issues))
    if args.command == "inspect":
        print(
            f"{len(scene.slots)} slots, {len(scene.resources)} unique IP requirements"
        )
        for plane, count in sorted(
            Counter(slot.column.plane for slot in scene.slots).items()
        ):
            print(f"  {plane}: {count} slots")
        return 0
    if not scene.slots:
        print("No addresses to fill.")
        return 0

    requested = args.output or source.with_name(f"{source.stem}_filled.xlsx")
    output = requested.parent.resolve() / requested.name
    if output.suffix.lower() != ".xlsx" or output == source or os.path.lexists(output):
        raise ConfigError("Specify a new .xlsx output path")
    if not output.parent.is_dir():
        raise ConfigError("Output directory must exist")

    networks = read_networks(args.config_dir / "networks.yaml")
    print("Checking candidate addresses through the jump host…", flush=True)
    with closing(SshProbe(args.config_dir / "jump_host.yaml", networks)) as probe:
        plan = plan_allocation(scene, networks, probe)

    for (plane, detail), count in sorted(plan.errors.items()):
        print(f"{plane}: {count} probes skipped ({detail})", file=sys.stderr)
    if plan.shortages:
        for plane, count in sorted(plan.shortages.items()):
            print(f"{plane}: {count} more addresses needed", file=sys.stderr)
        return 1

    print(f"Plan for {len(scene.slots)} slots")
    for slot in scene.slots:
        label = f"{slot.sheet}!{slot.cell}"
        if slot.span is not None:
            label += f"[{slot.resource[-1]}@{slot.span[0]}]"
        outcome = "existing VIP" if slot.resource in plan.reused else "no reply"
        print(
            f"  {label}  {slot.node}  {slot.column.plane}  {plan.addresses[slot.resource]}  ({outcome})"
        )
    print(f"Output: {output}")
    print(
        "No reply means no conflict was detected by ping. Review the candidates before writing."
    )
    try:
        approved = input("Write this plan? [y/N] ").strip().casefold() == "y"
    except EOFError:
        approved = False
    if not approved:
        print("Cancelled.")
        return 0
    publish_workbook(source, output, contents, replacements(scene, plan.addresses))
    print(f"Wrote {output}")
    return 0


def run(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except (SceneError, ConfigError, OSError) as error:
        print(f"se: {error}", file=sys.stderr)
        return 1
    except BadZipFile, InvalidFileException, ParseError, KeyError, ValueError:
        print("se: Workbook is not a readable .xlsx file", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
