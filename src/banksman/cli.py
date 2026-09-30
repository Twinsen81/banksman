"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from banksman import SCHEMA_VERSION, __version__

VERSION_LINE = f"banksman {__version__} (schema {SCHEMA_VERSION})"


def _build_parser() -> argparse.ArgumentParser:
    # Abbreviated flags are refused, so a permission rule that matches a flag in an agent's
    # command text always sees the flag's full name.
    parser = argparse.ArgumentParser(
        prog="banksman",
        description="Lease shared devices, emulators, and build slots to parallel coding agents.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=VERSION_LINE)
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    version = commands.add_parser(
        "version", help="print the version and the schema version", allow_abbrev=False
    )
    version.add_argument("--json", action="store_true", help="print JSON")

    status = commands.add_parser(
        "status", help="show every resource and who holds it", allow_abbrev=False
    )
    status.add_argument("--json", action="store_true", help="print JSON")
    return parser


def _print_json(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        _print_json({"schema": SCHEMA_VERSION, "version": __version__})
    else:
        print(VERSION_LINE)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    # There is no lease store yet, so the pool is always empty.
    leases: list[dict[str, object]] = []
    if args.json:
        _print_json({"schema": SCHEMA_VERSION, "leases": leases})
    else:
        print("No leases.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        return _cmd_version(args)
    if args.command == "status":
        return _cmd_status(args)
    parser.print_help()
    return 2

