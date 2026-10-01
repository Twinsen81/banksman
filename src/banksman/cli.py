"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone

from banksman import SCHEMA_VERSION, __version__, hooks
from banksman.config import Config, load_config
from banksman.errors import BanksmanError
from banksman.lease import QUARANTINED, VOID_REASONS, Lease
from banksman.sanitize import clean
from banksman.store import Reaped, Store, TakeBack, default_state_dir
from banksman.system import Machine

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
        "status", help="show every lease and who holds it", allow_abbrev=False
    )
    status.add_argument("--json", action="store_true", help="print JSON")

    reap = commands.add_parser(
        "reap", help="take back the resources of void leases now", allow_abbrev=False
    )
    reap.add_argument("--json", action="store_true", help="print JSON")
    return parser


def _print_json(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _print_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    cells = [[clean(cell) for cell in row] for row in [header, *rows]]
    widths = [max(len(row[column]) for row in cells) for column in range(len(header))]
    for row in cells:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _utc(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _local(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _open_store() -> Store:
    return Store(default_state_dir(), Machine(), take_back=_take_back(load_config()))


def _take_back(config: Config) -> TakeBack:
    def take_back(lease: Lease) -> bool:
        failure = hooks.take_back(config.kinds, lease)
        if failure is not None:
            print(clean(f"banksman: cannot take back {lease.resource}: {failure}"), file=sys.stderr)
        return failure is None

    return take_back


def _lease_json(lease: Lease) -> dict[str, object]:
    return {
        "resource": lease.resource,
        "kind": lease.kind,
        "state": lease.state,
        "owner": lease.owner,
        "owner_pid": lease.owner_pid,
        "acquired_at": _utc(lease.acquired_at),
        "touched_at": _utc(lease.touched_at),
        "void_reason": lease.void_reason,
    }


def _reaped_json(reaped: Reaped) -> dict[str, object]:
    return {
        "resource": reaped.lease.resource,
        "kind": reaped.lease.kind,
        "owner": reaped.lease.owner,
        "void_reason": reaped.lease.void_reason,
        "outcome": reaped.outcome,
    }


def _cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        _print_json({"schema": SCHEMA_VERSION, "version": __version__})
    else:
        print(VERSION_LINE)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store = _open_store()
    store.reap()
    snapshot = store.snapshot()
    if args.json:
        _print_json(
            {
                "schema": SCHEMA_VERSION,
                "leases": [_lease_json(lease) for lease in snapshot.leases],
                "unreadable": [
                    {"resource": entry.resource, "error": entry.error}
                    for entry in snapshot.unreadable
                ],
            }
        )
        return 0
    rows = [
        [lease.resource, lease.kind, lease.state, lease.owner, _local(lease.acquired_at)]
        for lease in snapshot.leases
    ]
    rows += [
        [entry.resource, "?", QUARANTINED, f"unreadable lease file: {entry.error}", "?"]
        for entry in snapshot.unreadable
    ]
    if rows:
        _print_table(["RESOURCE", "KIND", "STATE", "OWNER", "SINCE"], rows)
    else:
        print("No leases.")
    return 0


def _cmd_reap(args: argparse.Namespace) -> int:
    reaped = _open_store().reap()
    if args.json:
        _print_json({"schema": SCHEMA_VERSION, "reaped": [_reaped_json(item) for item in reaped]})
        return 0
    for item in reaped:
        reason = VOID_REASONS.get(item.lease.void_reason or "", "the lease was void")
        print(clean(f"{item.lease.resource}: {item.outcome}, because {reason}"))
    if not reaped:
        print("Nothing to reap.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        return _cmd_version(args)
    try:
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "reap":
            return _cmd_reap(args)
    except (BanksmanError, OSError) as exc:
        print(f"banksman: {clean(str(exc))}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2

