"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from banksman import SCHEMA_VERSION, __version__, hooks
from banksman.config import Config, load_config
from banksman.errors import BanksmanError
from banksman.lease import QUARANTINED, VOID_REASONS, Lease, User
from banksman.sanitize import clean
from banksman.store import NotHeld, Reaped, Stopping, Store, TakeBack, default_state_dir
from banksman.system import Machine

VERSION_LINE = f"banksman {__version__} (schema {SCHEMA_VERSION})"
# A script that gets this exit status has lost its lease, and must stop using the resource.
EXIT_LOST = 3


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

    enter = commands.add_parser(
        "enter",
        help="check a lease, touch it, and register a script as a user of the resource",
        allow_abbrev=False,
    )
    _lease_arguments(enter)
    enter.add_argument("--pid", type=_pid, required=True, help="the process of the script")
    enter.add_argument(
        "--pgid", type=_pid, help="a process group that the script created for its work"
    )

    check = commands.add_parser(
        "check",
        help="touch a lease and exit 0 while it is yours; exit 3 when it is lost",
        allow_abbrev=False,
    )
    _lease_arguments(check)

    leave = commands.add_parser(
        "leave", help="remove the record of a script from a lease", allow_abbrev=False
    )
    _lease_arguments(leave)
    leave.add_argument("--pid", type=_pid, required=True, help="the process of the script")

    admin = commands.add_parser("admin", help="commands for the operator", allow_abbrev=False)
    admin_commands = admin.add_subparsers(
        dest="admin_command", metavar="<command>", required=True
    )
    release = admin_commands.add_parser(
        "release",
        help="remove a lease in any state, also a quarantined one",
        allow_abbrev=False,
    )
    release.add_argument(
        "--force",
        action="store_true",
        required=True,
        help="take the resource from its holder; its scripts are not stopped",
    )
    release.add_argument("--resource", required=True, help="the resource")
    return parser


def _lease_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--resource", required=True, help="the leased resource")
    parser.add_argument(
        "--lease", required=True, metavar="ID", help="the id of the lease that the script uses"
    )


def _pid(text: str) -> int:
    # 0 and negative numbers name groups of processes in kill(2), never one process.
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"not a process id: {text!r}")
    return int(text)


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


def _open_store(config: Config) -> Store:
    return Store(
        default_state_dir(),
        Machine(),
        take_back=_take_back(config),
        stopping=_stopping(config),
    )


def _take_back(config: Config) -> TakeBack:
    def take_back(lease: Lease) -> bool:
        failure = hooks.take_back(config.kinds, lease)
        if failure is not None:
            print(clean(f"banksman: cannot take back {lease.resource}: {failure}"), file=sys.stderr)
        return failure is None

    return take_back


def _stopping(config: Config) -> Stopping:
    # Read from the current configuration, like the on_void hook: when the operator turns
    # stopping off, it is off at once, also for void leases that are not taken back yet.
    def stopping(lease: Lease) -> bool:
        kind = config.kinds.get(lease.kind)
        return kind is not None and kind.stop

    return stopping


def _reaped_store() -> tuple[Store, list[Reaped]]:
    """Open the store and reap first, as every command that reads or changes leases does."""
    config = load_config()
    store = _open_store(config)
    reaped = store.reap()
    for item in reaped:
        if item.outcome == QUARANTINED and item.running:
            print(clean(_still_running(item, config)), file=sys.stderr)
        elif item.problem is not None:
            message = f"banksman: quarantined {item.lease.resource}: {item.problem}"
            print(clean(message), file=sys.stderr)
    return store, reaped


def _still_running(item: Reaped, config: Config) -> str:
    resource = item.lease.resource
    kind = config.kinds.get(item.lease.kind)
    stopping = (
        ""
        if kind is not None and kind.stop
        else f" Stopping is off for kind {item.lease.kind}, so banksman sent them no signal."
    )
    return (
        f"banksman: quarantined {resource}: its scripts still run after the drain timeout:"
        f" {_processes(item.running)}.{stopping} When they have ended, run:"
        f" banksman admin release --force --resource {resource}"
    )


def _processes(users: Sequence[User]) -> str:
    return ", ".join(
        f"pid {user.pid}" + ("" if user.pgid is None else f" in group {user.pgid}")
        for user in users
    )


def _users_json(users: Sequence[User]) -> list[dict[str, object]]:
    return [{"pid": user.pid, "pgid": user.pgid} for user in users]


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
        "users": _users_json(lease.users),
    }


def _reaped_json(reaped: Reaped) -> dict[str, object]:
    return {
        "resource": reaped.lease.resource,
        "kind": reaped.lease.kind,
        "owner": reaped.lease.owner,
        "void_reason": reaped.lease.void_reason,
        "outcome": reaped.outcome,
        "running": _users_json(reaped.running),
    }


def _cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        _print_json({"schema": SCHEMA_VERSION, "version": __version__})
    else:
        print(VERSION_LINE)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store, _ = _reaped_store()
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
    _, reaped = _reaped_store()
    if args.json:
        _print_json({"schema": SCHEMA_VERSION, "reaped": [_reaped_json(item) for item in reaped]})
        return 0
    for item in reaped:
        reason = VOID_REASONS.get(item.lease.void_reason or "", "the lease was void")
        line = f"{item.lease.resource}: {item.outcome}, because {reason}"
        if item.running:
            line += f"; its scripts still run: {_processes(item.running)}"
        print(clean(line))
    if not reaped:
        print("Nothing to reap.")
    return 0


def _cmd_enter(args: argparse.Namespace) -> int:
    store, _ = _reaped_store()
    store.enter(args.resource, args.lease, args.pid, args.pgid)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    store, _ = _reaped_store()
    store.check(args.resource, args.lease)
    return 0


def _cmd_leave(args: argparse.Namespace) -> int:
    store, _ = _reaped_store()
    store.leave(args.resource, args.lease, args.pid)
    return 0


def _cmd_admin_release(args: argparse.Namespace) -> int:
    store, _ = _reaped_store()
    removed, running = store.force_release(args.resource)
    if isinstance(removed, Lease):
        print(clean(f"Released {args.resource}: its lease was {removed.state}."))
    else:
        print(clean(f"Released {args.resource}: its lease file could not be read."))
    if running:
        print(
            clean(f"banksman: scripts of {args.resource} still run: {_processes(running)}"),
            file=sys.stderr,
        )
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "status": _cmd_status,
    "reap": _cmd_reap,
    "enter": _cmd_enter,
    "check": _cmd_check,
    "leave": _cmd_leave,
    "admin release": _cmd_admin_release,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        return _cmd_version(args)
    name = args.command if args.command != "admin" else f"admin {args.admin_command}"
    command = _COMMANDS.get(name)
    if command is None:
        parser.print_help()
        return 2
    try:
        return command(args)
    except NotHeld as exc:
        print(f"banksman: the lease is lost: {clean(str(exc))}", file=sys.stderr)
        return EXIT_LOST
    except (BanksmanError, OSError) as exc:
        print(f"banksman: {clean(str(exc))}", file=sys.stderr)
        return 1
