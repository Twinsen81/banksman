"""Command-line entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib import resources

from banksman import SCHEMA_VERSION, __version__, console, hooks
from banksman.assign import MAX_ACCOUNTS, MAX_PARTS
from banksman.config import Config, Kind, load_config, parse_duration
from banksman.discovery import Found, Instance, discover, serial_now, serials_seen
from banksman.errors import BanksmanError
from banksman.history import Event, History
from banksman.identity import find_agent, find_holder
from banksman.inventory import (
    Decisions,
    Inventory,
    decide,
    default_inventory_path,
    load_inventory,
    matches,
    preselected,
    save_inventory,
)
from banksman.lease import QUARANTINED, READY, VOID_REASONS, Lease, SerialSeen, User
from banksman.request import (
    PART_NAME,
    Candidate,
    Clause,
    Part,
    check_kinds,
    parse_clause,
    search,
)
from banksman.run import Outcome
from banksman.run import run as run_command
from banksman.sanitize import clean
from banksman.store import (
    Busy,
    Choice,
    Granted,
    Need,
    NotHeld,
    Reaped,
    Record,
    Stopping,
    Store,
    TakeBack,
    default_log_path,
    default_state_dir,
    satisfiable,
)
from banksman.system import Machine

VERSION_LINE = f"banksman {__version__} (schema {SCHEMA_VERSION})"
# A script that gets this exit status has lost its lease, and must stop using the resource.
EXIT_LOST = 3
# Every resource that matches is in use, also after the wait. A caller can treat this as a busy
# machine, not as a failure of its own work.
EXIT_BUSY = 4
# How often a waiting acquire looks again. Each look runs discovery, for example adb.
POLL_SECONDS = 5.0
# How often watch shows the table again by default. Each refresh also runs discovery.
WATCH_SECONDS = 5
# The Gradle scripts in this package: the init script, and the build-slot part that it applies
# on Gradle 7.4 and later to take a build slot for every build.
GRADLE_INIT_SCRIPT = "gradle-init.gradle"
GRADLE_BUILD_SLOT_SCRIPT = "build-slot.gradle"


def _build_parser() -> argparse.ArgumentParser:
    # Abbreviated flags are refused, so a permission rule that matches a flag in an agent's
    # command text always sees the flag's full name.
    parser = argparse.ArgumentParser(
        prog="banksman",
        description="Lease shared devices and emulators to parallel coding agents.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=VERSION_LINE)
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    version = commands.add_parser(
        "version", help="print the version and the schema version", allow_abbrev=False
    )
    version.add_argument("--json", action="store_true", help="print JSON")

    status = commands.add_parser(
        "status",
        help="show every resource, who holds it, and when it can be free",
        allow_abbrev=False,
    )
    status.add_argument("--json", action="store_true", help="print JSON")
    _verbose_argument(status)

    watch = commands.add_parser(
        "watch", help="show the table of status again and again", allow_abbrev=False
    )
    watch.add_argument(
        "--interval",
        type=_interval,
        default=WATCH_SECONDS,
        metavar="DURATION",
        help=f"how often to show it again, such as 10s; by default {WATCH_SECONDS}s",
    )

    log = commands.add_parser(
        "log",
        help="show the history of acquire, release, void, reap, and quarantine events",
        allow_abbrev=False,
    )
    log.add_argument(
        "--since",
        type=_since,
        metavar="TIME",
        help="only events from this time on: a duration back from now, such as 3h, or a local"
        " date and time, such as 2026-10-02T03:00",
    )
    log.add_argument("--resource", help="only the events of this resource")
    log.add_argument("--json", action="store_true", help="print JSON")
    _verbose_argument(log)

    whoami = commands.add_parser(
        "whoami",
        help="show the holder that banksman would record for a lease taken from here",
        allow_abbrev=False,
    )
    whoami.add_argument("--json", action="store_true", help="print JSON")

    reap = commands.add_parser(
        "reap", help="take back the resources of void leases now", allow_abbrev=False
    )
    reap.add_argument("--json", action="store_true", help="print JSON")

    acquire = commands.add_parser(
        "acquire",
        help="lease permitted resources that match the request, and print them as KEY=value",
        allow_abbrev=False,
    )
    acquire.set_defaults(parts=None)
    acquire.add_argument(
        "--as",
        action=_PartAction,
        type=_part_name,
        metavar="NAME",
        help="start a part of the request: one more resource that the run needs at the same"
        " time, such as --as phone; the --where and --accounts after it belong to this part",
    )
    acquire.add_argument(
        "--where",
        action=_WhereAction,
        type=_clause,
        metavar="CLAUSE",
        help="a property that the resource must have, such as form=tablet or 'api>=33'; repeat"
        " it for more",
    )
    acquire.add_argument(
        "--accounts",
        action=_AccountsAction,
        type=_account_count,
        metavar="N",
        help="also lease N allowed accounts that are signed in on the resource, such as 1",
    )
    acquire.add_argument(
        "--for",
        dest="purpose",
        metavar="TEXT",
        help="why the resource is needed; people and other agents read it",
    )
    acquire.add_argument(
        "--expect",
        type=_duration,
        metavar="DURATION",
        help="how long the resource is needed, such as 20m; only a hint for others",
    )
    acquire.add_argument(
        "--wait",
        type=_duration,
        default=0,
        metavar="DURATION",
        help="how long to wait while every matching resource is in use, such as 15m",
    )
    acquire.add_argument(
        "--lease",
        metavar="ID",
        help="the id of a lease to keep: its resource is chosen first if it still matches",
    )
    acquire.add_argument("--issue", help="the issue id; by default it comes from the branch name")
    acquire.add_argument(
        "--owner-pid",
        type=_pid,
        help="the process whose end frees the lease; by default the agent process",
    )
    acquire.add_argument("--json", action="store_true", help="print JSON")

    touch = commands.add_parser(
        "touch",
        help="touch the leases of a holding, and record this agent process as their owner process",
        allow_abbrev=False,
    )
    touch.add_argument(
        "--lease", required=True, metavar="ID", help="the id of the lease, from acquire"
    )
    touch.add_argument("--resource", help="a resource of the lease, to check that it is held")
    touch.add_argument(
        "--expect",
        type=_duration,
        metavar="DURATION",
        help="how long the resource is still needed, such as 20m; only a hint for others",
    )
    touch.add_argument(
        "--owner-pid", type=_pid, help="the owner process; by default the agent process"
    )

    give_back = commands.add_parser(
        "release",
        help="give back the resources of a lease, or every lease of this agent process",
        allow_abbrev=False,
    )
    give_back.add_argument(
        "--lease", metavar="ID", help="the id of the lease: gives back all its resources"
    )
    give_back.add_argument("--resource", help="with --lease: give back only this resource")
    give_back.add_argument(
        "--all",
        action="store_true",
        help="every lease whose owner process is this agent process",
    )
    give_back.add_argument(
        "--owner-pid",
        type=_pid,
        help="with --all: the owner process; by default the agent process",
    )

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

    run = commands.add_parser(
        "run",
        help="run a command in a process group of its own under a lease, and stop the group when"
        " the lease is lost",
        allow_abbrev=False,
    )
    run.set_defaults(parts=None)
    run.add_argument(
        "--lease", metavar="ID", help="the id of a lease that the caller holds, from acquire"
    )
    run.add_argument("--resource", help="with --lease: the leased resource")
    run.add_argument(
        "--where",
        action=_WhereAction,
        type=_clause,
        metavar="CLAUSE",
        help="instead of --lease: lease a resource that matches for the command, and give it"
        " back after the command; repeat it for more",
    )
    run.add_argument(
        "--accounts",
        action=_AccountsAction,
        type=_account_count,
        metavar="N",
        help="with --where: also lease N allowed accounts that are signed in on the resource",
    )
    run.add_argument(
        "--for",
        dest="purpose",
        metavar="TEXT",
        help="with --where: why the resource is needed; people and other agents read it",
    )
    run.add_argument(
        "--expect",
        type=_duration,
        metavar="DURATION",
        help="with --where: how long the resource is needed, such as 20m; only a hint for others",
    )
    run.add_argument(
        "--wait",
        type=_duration,
        metavar="DURATION",
        help="with --where: how long to wait while every matching resource is in use",
    )
    run.add_argument(
        "--issue", help="with --where: the issue id; by default it comes from the branch name"
    )
    run.add_argument(
        "--owner-pid",
        type=_pid,
        help="with --where: the process whose end frees the lease; by default this run",
    )
    run.add_argument(
        "command_line",
        nargs=argparse.REMAINDER,
        metavar="-- COMMAND",
        help="the command and its arguments, after --",
    )

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
    discover_command = admin_commands.add_parser(
        "discover",
        help="find the instances of the discovered kinds, and choose which agents may use",
        allow_abbrev=False,
    )
    discover_command.add_argument("--kind", help="discover only this kind")
    discover_command.add_argument(
        "--all",
        action="store_true",
        help="select every instance and account at first, except the ones refused before",
    )
    discover_command.add_argument(
        "--yes",
        action="store_true",
        help="take the selection without questions, and write the inventory",
    )
    discover_command.add_argument(
        "--json", action="store_true", help="print JSON; without --yes, write nothing"
    )
    gradle_init_command = admin_commands.add_parser(
        "gradle-init",
        help="print the optional Gradle init script that makes every build hold a build slot;"
        " save it in ~/.gradle/init.d/",
        allow_abbrev=False,
    )
    gradle_init_command.add_argument(
        "--build-slot",
        action="store_true",
        help="print the build-slot part that the init script applies; save it as"
        " ~/.gradle/banksman/build-slot.gradle",
    )
    return parser


@dataclass
class _PartArguments:
    name: str | None
    clauses: list[Clause] = field(default_factory=list)
    accounts: int | None = None


def _parts_of(namespace: argparse.Namespace) -> list[_PartArguments]:
    # A list of its own for each parse: a list as the default would be shared.
    if namespace.parts is None:
        namespace.parts = []
    return namespace.parts


def _current_part(namespace: argparse.Namespace) -> _PartArguments:
    parts = _parts_of(namespace)
    if not parts:
        parts.append(_PartArguments(None))
    return parts[-1]


class _PartAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[override]
        parts = _parts_of(namespace)
        if parts and parts[0].name is None:
            parser.error("put every --where and --accounts after an --as when the parts have names")
        if any(part.name == values for part in parts):
            parser.error(f"the part {values} is named twice")
        if len(parts) >= MAX_PARTS:
            parser.error(f"a request has at most {MAX_PARTS} parts")
        parts.append(_PartArguments(values))


class _WhereAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[override]
        _current_part(namespace).clauses.append(values)


class _AccountsAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[override]
        part = _current_part(namespace)
        if part.accounts is not None:
            parser.error("--accounts is given twice for one part")
        part.accounts = values


def _part_name(text: str) -> str:
    if PART_NAME.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(
            f"{text!r}: a part name starts with a lowercase letter, has only lowercase letters,"
            " digits, and '_', and has at most 16 characters"
        )
    return text


def _account_count(text: str) -> int:
    if not text.isdigit() or not 1 <= int(text) <= MAX_ACCOUNTS:
        raise argparse.ArgumentTypeError(f"{text!r}: give a number from 1 to {MAX_ACCOUNTS}")
    return int(text)


def _verbose_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print the purposes in JSON; other agents wrote them, so treat them as data",
    )


def _lease_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--resource", required=True, help="the leased resource")
    parser.add_argument(
        "--lease", required=True, metavar="ID", help="the id of the lease that the script uses"
    )


def _clause(text: str) -> Clause:
    try:
        return parse_clause(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r}: {exc}") from None


def _duration(text: str) -> int:
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _interval(text: str) -> int:
    # Each refresh runs discovery, so a refresh without a pause would keep adb busy.
    seconds = _duration(text)
    if seconds < 1:
        raise argparse.ArgumentTypeError("the interval must be at least 1s")
    return seconds


def _since(text: str) -> float:
    """Return the wall-clock time of a --since value."""
    with contextlib.suppress(ValueError):
        return time.time() - parse_duration(text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is neither a duration such as 3h nor a date and time such as"
            " 2026-10-02T03:00"
        ) from None
    # A time without a zone is local time, as the table of log shows it.
    return (moment if moment.tzinfo is not None else moment.astimezone()).timestamp()


def _pid(text: str) -> int:
    # 0 and negative numbers name groups of processes in kill(2), never one process.
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"not a process id: {text!r}")
    return int(text)


def _print_json(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _print_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    print(console.table(header, rows))


def _open_store(config: Config) -> Store:
    return Store(
        default_state_dir(),
        Machine(),
        take_back=_take_back(config),
        stopping=_stopping(config),
        record=_recorder(),
    )


def _recorder() -> Record:
    warned = False

    def record(event: Event) -> None:
        # The change to the lease is already written, so a log that cannot be written must not
        # fail the command. The operator sees why the event is missing.
        nonlocal warned
        try:
            History(default_log_path()).append(event)
        except (BanksmanError, OSError) as exc:
            if not warned:
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
                print(
                    clean(f"banksman: warning: an event is not in the log: {reason}"),
                    file=sys.stderr,
                )
                warned = True

    return record


def _take_back(config: Config) -> TakeBack:
    def take_back(lease: Lease) -> bool:
        kind = config.kinds.get(lease.kind)
        if kind is not None and kind.on_void is not None:
            # The hook can end the instance by its serial. The serial in the lease can be old,
            # and an emulator takes the lowest free port, so another instance can have it now.
            lease = replace(lease, serial=_serial_found(config, kind, lease))
        failure = hooks.take_back(config.kinds, lease)
        if failure is not None:
            print(clean(f"banksman: cannot take back {lease.resource}: {failure}"), file=sys.stderr)
        return failure is None

    return take_back


def _serial_found(config: Config, kind: Kind, lease: Lease) -> str | None:
    """Return the serial that discovery finds now for the instance of a lease, or None."""
    seen = serial_now(kind, config, lease.resource, lease.serial, Machine().clock)
    return next((each.serial for each in seen if each.resource == lease.resource), None)


def _stopping(config: Config) -> Stopping:
    # Read from the current configuration, like the on_void hook: when the operator turns
    # stopping off, it is off at once, also for void leases that are not taken back yet.
    def stopping(lease: Lease) -> bool:
        kind = config.kinds.get(lease.kind)
        return kind is not None and kind.stop

    return stopping


def _reaped_store(config: Config | None = None) -> tuple[Store, list[Reaped]]:
    """Open the store and reap first, as every command that reads or changes leases does."""
    config = load_config() if config is None else config
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
    return console.processes_text(users)


def _reaped_json(reaped: Reaped) -> dict[str, object]:
    return {
        "resource": reaped.lease.resource,
        "kind": reaped.lease.kind,
        "owner": reaped.lease.owner,
        "void_reason": reaped.lease.void_reason,
        "outcome": reaped.outcome,
        "running": console.users_json(reaped.running),
    }


def _cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        _print_json({"schema": SCHEMA_VERSION, "version": __version__})
    else:
        print(VERSION_LINE)
    return 0


@dataclass(frozen=True)
class _Status:
    resources: list[console.Resource]
    notes: tuple[str, ...]
    clock: console.Clock
    # The owner processes of the leases that run now, for the time at which a lease is void.
    running: Mapping[int, str]


def _status(config: Config) -> _Status:
    store, _ = _reaped_store(config)
    # Discovery finds the free resources and their facts. It runs before the leases are read,
    # because it can take seconds. What it finds about serials goes into the leases first.
    found = search(config, load_inventory(), [Part()], clock=store.system.clock)
    store.observe(found.serials)
    resources = console.resources(store.snapshot(), found)
    machine = store.system
    pids = console.owner_pids(resources)
    return _Status(
        resources,
        found.notes,
        console.Clock(machine.clock(), machine.wall_clock()),
        machine.running(pids) if pids else {},
    )


def _status_text(status: _Status) -> str:
    if not status.resources:
        text = (
            "No resources. The configuration declares the kinds, and banksman admin discover"
            " allows the instances that agents may use."
        )
    else:
        text = console.table(
            console.STATUS_HEADER,
            console.status_rows(status.resources, status.clock, status.running),
        )
    return "\n".join([text, *(clean(f"note: {note}") for note in status.notes)])


def _cmd_status(args: argparse.Namespace) -> int:
    status = _status(load_config())
    if args.json:
        shown = console.status_json(
            status.resources, status.notes, status.clock, status.running, verbose=args.verbose
        )
        _print_json({"schema": SCHEMA_VERSION, **shown})
    else:
        print(_status_text(status))
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    try:
        while True:
            # The configuration is read again on every refresh, as by a waiting acquire, so that
            # the reaper uses the current hooks. An error is shown, and the next refresh tries
            # again, for example after the operator fixed the configuration.
            try:
                text = _status_text(_status(load_config()))
            except (BanksmanError, OSError) as exc:
                text = clean(f"banksman: {exc}")
            if sys.stdout.isatty():
                # This command's own output, so the escape sequence is its own to write.
                sys.stdout.write("\x1b[H\x1b[2J")
            now = time.strftime("%H:%M:%S")
            sys.stdout.write(
                f"{text}\n\n{now}: shown every {console.span(args.interval)}. Press Ctrl-C to"
                " stop.\n"
            )
            sys.stdout.flush()
            _pause(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _cmd_log(args: argparse.Namespace) -> int:
    # The reaper first, so that the log also has the leases that became void just now.
    _reaped_store()
    events, skipped = History(default_log_path()).read()
    events = [
        event
        for event in events
        if (args.since is None or event.at >= args.since)
        and (args.resource is None or event.resource == args.resource)
    ]
    if args.json:
        _print_json(
            {
                "schema": SCHEMA_VERSION,
                "events": [console.event_json(event, verbose=args.verbose) for event in events],
                "skipped": skipped,
            }
        )
        return 0
    if events:
        _print_table(console.LOG_HEADER, console.log_rows(events))
    else:
        print("No events.")
    if skipped:
        print(f"note: {skipped} lines of the log are not valid events, so they are left out")
    return 0


def _cmd_whoami(args: argparse.Namespace) -> int:
    config = load_config()
    holder = find_holder(Machine().process_table(), config.holder)
    if args.json:
        _print_json(
            {
                "schema": SCHEMA_VERSION,
                "owner": holder.owner,
                "owner_pid": holder.owner_pid,
                "agent": holder.agent,
                "issue": holder.issue,
                "session": holder.session,
            }
        )
        return 0
    agent = (
        "none: only a touch keeps a lease, and the owner process cannot end it"
        if holder.agent is None
        else f"{holder.agent} (pid {holder.owner_pid})"
    )
    rows = [
        ["owner", holder.owner],
        ["agent", agent],
        ["issue", holder.issue or "none"],
        ["session", holder.session or "none"],
    ]
    _print_table(["FIELD", "VALUE"], rows)
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


# When the command ends by one of these signals, banksman ends by the same signal, so that the
# shell that runs banksman sees the same end; for example, a script stops on Ctrl-C. For a
# signal that dumps core, banksman exits with 128 and the number of the signal instead.
_PASSED_ON = (signal.SIGHUP, signal.SIGINT, signal.SIGKILL, signal.SIGPIPE, signal.SIGTERM)


def _cmd_run(args: argparse.Namespace) -> int:
    command = args.command_line
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise BanksmanError(
            "give the command after --, for example: banksman run --lease <id> --resource <name>"
            " -- ./run-tests"
        )
    request = args.parts is not None
    if request == (args.lease is not None):
        raise BanksmanError("run needs --lease and --resource, or --where, but not both")
    if not request:
        if args.resource is None:
            raise BanksmanError("run --lease needs --resource, the leased resource")
        given = [
            flag
            for flag, value in (
                ("--for", args.purpose),
                ("--expect", args.expect),
                ("--wait", args.wait),
                ("--issue", args.issue),
                ("--owner-pid", args.owner_pid),
            )
            if value is not None
        ]
        if given:
            raise BanksmanError(f"run {given[0]} needs --where")
        config = load_config()
        store, _ = _reaped_store(config)
        # A lease that is lost runs no discovery.
        lease = store.check(args.resource, args.lease)
        kind = config.kinds.get(lease.kind)
        seen: tuple[SerialSeen, ...] = ()
        if kind is not None and kind.discovered:
            # The holder can have started or restarted the instance since the grant.
            clock = store.system.clock
            seen = serial_now(kind, config, lease.resource, lease.serial, clock)
            store.observe(seen)
            lease = store.check(args.resource, args.lease)
        # The command acts on the serial, so it gets only one that discovery confirms now: a
        # discovery that fails cannot tell whether another instance has the serial by now.
        if lease.resource not in {each.resource for each in seen if each.serial is not None}:
            lease = replace(lease, serial=None)
        command = _command_line(command, lease)
        outcome = run_command(
            store, lease.resource, lease.lease_id, command, _command_env(lease, kind), _warn
        )
        return _run_status(outcome, command)
    if args.resource is not None:
        raise BanksmanError("run --resource needs --lease; with --where, banksman chooses it")
    parts = _parts(args)
    grant = _acquire(
        parts,
        purpose=args.purpose,
        expect=args.expect,
        wait=args.wait or 0,
        keep=None,
        issue=args.issue,
        # The lease belongs to this run, so it ends with it, also when something kills it.
        owner_pid=os.getpid() if args.owner_pid is None else args.owner_pid,
    )
    (lease,) = grant.leases
    try:
        command = _command_line(command, lease)
        outcome = run_command(
            grant.store,
            lease.resource,
            lease.lease_id,
            command,
            _command_env(lease, grant.config.kinds.get(lease.kind)),
            _warn,
        )
    finally:
        _give_back_after_run(lease)
    return _run_status(outcome, command)


# The arguments of a command that run replaces. The shell of the caller cannot expand a variable
# that banksman sets only for the command, so a placeholder is the short way to pass a value.
SERIAL_PLACEHOLDER = "{serial}"
RESOURCE_PLACEHOLDER = "{resource}"


def _command_line(command: Sequence[str], lease: Lease) -> list[str]:
    """Replace each argument that is a placeholder, as a whole, with its value."""
    if SERIAL_PLACEHOLDER in command and lease.serial is None:
        raise BanksmanError(
            f"the serial of {lease.resource} is not known, so {SERIAL_PLACEHOLDER} cannot be"
            " replaced: the instance does not run, or discovery does not find its serial. Start"
            " the instance, and run the command again"
        )
    values = {SERIAL_PLACEHOLDER: lease.serial, RESOURCE_PLACEHOLDER: lease.resource}
    return [values.get(argument) or argument for argument in command]


def _command_env(lease: Lease, kind: Kind | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in hooks.VARIABLES}
    android = hooks.is_android(kind)
    env.update(hooks.variables(lease, android=android), BANKSMAN_LEASE=lease.lease_id)
    if lease.accounts:
        env["BANKSMAN_ACCOUNTS"] = ",".join(lease.accounts)
    if android and lease.serial is None:
        _warn(
            f"the serial of {lease.resource} is not known, so {hooks.ANDROID_SERIAL} names no"
            f" device ({hooks.NO_SERIAL}): adb without -s, and the connected tests of Gradle,"
            " find no device"
        )
    return env


def _give_back_after_run(lease: Lease) -> None:
    # The reaper first, as for banksman release, so that a lease that became void is taken back
    # with the hooks of the current configuration. The lease can be lost already.
    try:
        store, _ = _reaped_store()
        for each, drained in store.release_holding(lease.lease_id):
            if drained is not None:
                _warn(_released(each.resource, drained))
    except NotHeld:
        pass
    except (BanksmanError, OSError) as exc:
        _warn(f"cannot give back {lease.resource}: {exc}")


def _run_status(outcome: Outcome, command: Sequence[str]) -> int:
    if outcome.lost is not None:
        if outcome.running:
            pids = ", ".join(f"pid {pid}" for pid in outcome.running)
            stopped = (
                f"processes of the command still run: {pids}. The resource is handed on only"
                " after they have ended"
            )
        else:
            stopped = "the command was stopped"
        _warn(f"the lease is lost: {outcome.lost}; {stopped}")
        return EXIT_LOST
    if outcome.error is not None:
        _warn(f"cannot run {command[0]}: {outcome.error}")
    returncode = outcome.returncode
    assert returncode is not None
    if outcome.terminal and returncode in (-signal.SIGINT, 128 + signal.SIGINT):
        # Ctrl-C reached only the group of the command, which had the terminal. Without
        # banksman, the script that runs the command would get it too. A program such as a JVM
        # exits with 130 after Ctrl-C instead of ending by the signal.
        _end_by(signal.SIGINT, os.getpgrp())
    if returncode >= 0:
        return returncode
    signum = -returncode
    if signum in _PASSED_ON:
        _end_by(signum, None)
    return 128 + signum


def _end_by(signum: int, group: int | None) -> None:
    """End banksman by a signal, together with its process group when one is given."""
    sys.stdout.flush()
    sys.stderr.flush()
    if signum != signal.SIGKILL:
        signal.signal(signum, signal.SIG_DFL)
    if group is not None:
        os.killpg(group, signum)
    else:
        os.kill(os.getpid(), signum)


def _warn(text: str) -> None:
    print(clean(f"banksman: {text}"), file=sys.stderr)


def _cmd_acquire(args: argparse.Namespace) -> int:
    parts = _parts(args)
    grant = _acquire(
        parts,
        purpose=args.purpose,
        expect=args.expect,
        wait=args.wait,
        keep=args.lease,
        issue=args.issue,
        owner_pid=args.owner_pid,
    )
    _print_grant(parts, grant.granted, grant.leases, args.json)
    return 0


def _parts(args: argparse.Namespace) -> list[Part]:
    return [
        Part(tuple(each.clauses), each.name, each.accounts or 0)
        for each in args.parts or [_PartArguments(None)]
    ]


@dataclass(frozen=True)
class _Grant:
    store: Store
    config: Config
    granted: list[Granted]
    leases: list[Lease]


def _acquire(
    parts: Sequence[Part],
    *,
    purpose: str | None,
    expect: int | None,
    wait: int,
    keep: str | None,
    issue: str | None,
    owner_pid: int | None,
) -> _Grant:
    config = load_config()
    check_kinds([clause for part in parts for clause in part.clauses], config)
    machine = Machine()
    holder = find_holder(
        machine.process_table(),
        config.holder,
        issue=issue,
        owner_pid=owner_pid,
        purpose=purpose,
    )
    give_up = machine.clock() + wait
    waiting = False
    owner_started = None
    while True:
        # The configuration, the inventory, and the machine are read again on every look. The
        # reaper must use the hooks of the current configuration, so that a hook that the
        # operator removes stops at once, also for a caller that waits.
        config = load_config()
        store, _ = _reaped_store(config)
        found = search(config, load_inventory(), parts, clock=machine.clock)
        needs = [
            Need(tuple(_choice(candidate, config) for candidate in candidates), part.accounts)
            for part, candidates in zip(parts, found.candidates)
        ]
        # Waiting helps only when the request could be met if the resources in use were free.
        if not satisfiable(needs):
            _print_notes(
                [
                    *found.notes,
                    *(
                        f"{name} (kind {kind}) is allowed but not present now"
                        for kind, name in found.absent
                    ),
                ]
            )
            raise BanksmanError(_no_match(parts, found.candidates))
        granted = store.grant(
            needs,
            holder,
            keep=keep,
            expect=expect,
            reset_time=hooks.HOOK_TIMEOUT_SECONDS,
            seen=found.serials,
        )
        if granted is not None:
            break
        left = give_up - machine.clock()
        if left <= 0:
            raise Busy(_busy(parts, found.candidates, still=bool(wait)))
        owner_started = _owner_still_runs(machine, holder.owner_pid, owner_started)
        if not waiting:
            # A caller that waits, such as a build, shows why it does not go on.
            busy = _busy(parts, found.candidates, still=False)
            print(
                clean(
                    f"banksman: {busy}; waiting up to {console.span(wait)}. banksman status"
                    " shows who holds them"
                ),
                file=sys.stderr,
            )
            waiting = True
        machine.sleep(min(POLL_SECONDS, left))
    leases = _prepare(store, config, granted)
    reset = [
        lease
        for grant, lease in zip(granted, leases)
        if not grant.kept and config.kinds[lease.kind].on_acquire is not None
    ]
    if reset:
        # The hook can restart the instance, and a restarted emulator can get another serial.
        seen: list[SerialSeen] = []
        for name in sorted({lease.kind for lease in reset}):
            kind = config.kinds[name]
            if not kind.discovered:
                continue
            started = machine.clock()
            found = serials_seen(discover(kind, config), started)
            seen.extend(found)
            # The serial from before the reset can name another instance now, so an instance
            # that discovery does not find with a serial now, also because it fails, has none.
            known = {each.resource for each in found if each.serial is not None}
            seen.extend(
                SerialSeen(lease.resource, name, None, started)
                for lease in reset
                if lease.kind == name and lease.resource not in known
            )
        now = store.observe(seen)
        leases = [
            now[lease.resource]
            if lease.resource in now and now[lease.resource].lease_id == lease.lease_id
            else lease
            for lease in leases
        ]
    return _Grant(store, config, granted, leases)


def _owner_still_runs(machine: Machine, owner_pid: int | None, started: str | None) -> str | None:
    """Return the start time of the owner process of a request that waits, or raise when that
    process has ended.

    Nothing would use what an ended owner gets, and the grant would end at the next reap. A
    Gradle daemon that stops while its build waits for a slot leaves its acquire behind.
    """
    if owner_pid is None:
        return None
    now = machine.running([owner_pid]).get(owner_pid)
    # The start time guards against a pid that the system has given to a new process.
    if now is None or started not in (None, now):
        raise BanksmanError(
            f"the owner process {owner_pid} has ended, so the request stops waiting"
        )
    return now


def _some(candidates: Sequence[Candidate], shown: int = 10) -> str:
    names = ", ".join(candidate.resource for candidate in candidates[:shown])
    more = len(candidates) - shown
    return f"{names}, and {more} more" if more > 0 else names


def _choice(candidate: Candidate, config: Config) -> Choice:
    kind = config.kinds[candidate.kind]
    # banksman starts nothing: the holder starts an instance that does not run. A lease is
    # booting only while the on_acquire hook resets its instance, so that a caller that dies in
    # the middle loses the lease at the boot deadline.
    return Choice(
        candidate.resource,
        candidate.kind,
        kind.timeouts,
        reset=kind.on_acquire is not None,
        accounts=candidate.accounts or (),
    )


def _prepare(store: Store, config: Config, granted: Sequence[Granted]) -> list[Lease]:
    """Reset the instances of the new leases with the on_acquire hooks of their kinds, and then
    mark every new lease ready.

    A request gets all its parts or none: if a reset fails, or a new lease is lost meanwhile,
    every new lease of the request is given back.
    """
    leases = [grant.lease for grant in granted]
    new = [index for index, grant in enumerate(granted) if not grant.kept]
    if all(leases[index].state == READY for index in new):
        return leases
    given_back = "the lease is" if len(granted) == 1 else "the new leases are"
    try:
        for index in new:
            lease = leases[index]
            if config.kinds[lease.kind].on_acquire is None:
                continue
            failure = hooks.reset(config.kinds, lease)
            if failure is not None:
                # A failed reset does not make the instance unsafe for the next holder, as a
                # failed take-back does, so the lease is given back, not quarantined.
                raise BanksmanError(
                    f"cannot reset {lease.resource}: {failure}; {given_back} given back"
                )
            # The resets run one after another. A check keeps the other leases of the holding
            # from becoming idle meanwhile.
            store.check(lease.resource, lease.lease_id)
        for index in new:
            leases[index] = store.ready(leases[index].resource, leases[index].lease_id)
    except NotHeld as exc:
        for grant in granted:
            if not grant.kept:
                _give_back(store, grant.lease)
        raise BanksmanError(f"{exc} while the request was reset; {given_back} given back") from None
    except BaseException:
        for grant in granted:
            if not grant.kept:
                _give_back(store, grant.lease)
        raise
    return leases


def _give_back(store: Store, lease: Lease) -> None:
    with contextlib.suppress(BanksmanError):
        store.release(lease.resource, lease.lease_id)


def _no_match(parts: Sequence[Part], candidates: Sequence[Sequence[Candidate]]) -> str:
    empty = next((part for part, matching in zip(parts, candidates) if not matching), None)
    if empty is None:
        found = (
            "the permitted resources that are present now cannot meet all parts of the request"
            " at the same time"
        )
    else:
        request = ", ".join(str(clause) for clause in empty.clauses)
        accounts = f" with {empty.accounts} allowed accounts signed in" if empty.accounts else ""
        found = (
            f"no permitted resource that is present now matches {request}{accounts}"
            if request
            else f"no permitted resource{accounts} is present now"
        )
        if empty.name is not None:
            found = f"part {empty.name}: {found}"
    return (
        f"{found}. The operator decides what agents may use, in the configuration and with"
        " banksman admin discover"
    )


def _busy(parts: Sequence[Part], candidates: Sequence[Sequence[Candidate]], *, still: bool) -> str:
    now = " still" if still else ""
    if len(parts) == 1:
        accounts = ", or the accounts on it," if parts[0].accounts else ""
        return f"every matching resource{accounts} is{now} in use: {_some(candidates[0])}"
    matches = "; ".join(f"{part.name}: {_some(each)}" for part, each in zip(parts, candidates))
    return (
        f"the parts of the request cannot all be granted, because resources or accounts that"
        f" they need are{now} in use: {matches}"
    )


def _print_notes(notes: Sequence[str]) -> None:
    for note in notes:
        print(clean(f"banksman: note: {note}"), file=sys.stderr)


def _print_grant(
    parts: Sequence[Part],
    granted: Sequence[Granted],
    leases: Sequence[Lease],
    as_json: bool,
) -> None:
    # A serial in a lease has only the characters of a resource name, as every value here, so
    # that a shell script can use it as it is. Account names are resource names too.
    serials = [lease.serial for lease in leases]
    if as_json:
        _print_json(
            {
                "schema": SCHEMA_VERSION,
                "parts": [
                    {
                        "part": part.name,
                        "lease_id": lease.lease_id,
                        "resource": lease.resource,
                        "kind": lease.kind,
                        "state": lease.state,
                        "serial": serial,
                        "accounts": list(grant.accounts),
                        "kept": grant.kept,
                    }
                    for part, grant, lease, serial in zip(parts, granted, leases, serials)
                ],
            }
        )
        return
    if parts[0].name is None:
        lines = [
            ("RESOURCE", leases[0].resource),
            ("KIND", leases[0].kind),
            ("LEASE", leases[0].lease_id),
            ("STATE", leases[0].state),
            ("KEPT", _flag(granted[0].kept)),
        ]
        lines += [("SERIAL", serials[0])] if serials[0] is not None else []
        lines += [("ACCOUNTS", ",".join(granted[0].accounts))] if parts[0].accounts else []
    else:
        lines = []
        for part, grant, lease, serial in zip(parts, granted, leases, serials):
            prefix = f"{part.name.upper()}_"  # type: ignore[union-attr]
            lines += [
                (f"{prefix}RESOURCE", lease.resource),
                (f"{prefix}KIND", lease.kind),
                (f"{prefix}LEASE", lease.lease_id),
                (f"{prefix}STATE", lease.state),
                (f"{prefix}KEPT", _flag(grant.kept)),
            ]
            lines += [(f"{prefix}SERIAL", serial)] if serial is not None else []
            lines += [(f"{prefix}ACCOUNTS", ",".join(grant.accounts))] if part.accounts else []
    for key, value in lines:
        print(f"{key}={value}")


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _cmd_touch(args: argparse.Namespace) -> int:
    config = load_config()
    store, _ = _reaped_store(config)
    owner = _owner_process(config, args.owner_pid)
    # A touch keeps every lease of the holding, so the resource only checks that it is held.
    if args.resource is None:
        store.touch_holding(args.lease, owner, expect=args.expect)
    else:
        store.touch(args.resource, args.lease, owner, expect=args.expect)
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    if args.all and (args.lease is not None or args.resource is not None):
        raise BanksmanError("release --all takes no --lease and no --resource")
    if not args.all and args.lease is None:
        raise BanksmanError("release needs --lease, the id of the lease, or --all")
    if not args.all and args.owner_pid is not None:
        raise BanksmanError("release --owner-pid needs --all")
    config = load_config()
    store, _ = _reaped_store(config)
    if args.resource is not None:
        print(clean(_released(args.resource, store.release(args.resource, args.lease))))
        return 0
    if not args.all:
        for lease, drained in store.release_holding(args.lease):
            print(clean(_released(lease.resource, drained)))
        return 0
    owner = _owner_process(config, args.owner_pid)
    if owner is None:
        raise BanksmanError(
            "release --all needs the agent process, and none was found: pass --owner-pid, or"
            " release each lease with --resource and --lease"
        )
    released = store.release_all(owner)
    for lease, drained in released:
        print(clean(_released(lease.resource, drained)))
    if not released:
        print(f"Process {owner} holds no lease.")
    return 0


def _owner_process(config: Config, owner_pid: int | None) -> int | None:
    # The nearest agent process, as for a new lease: after a restart of the agent, a touch
    # records its new process, so that the lease does not end with the earlier one.
    if owner_pid is not None:
        return owner_pid
    return find_agent(Machine().process_table(), config.holder.agents, os.getpid())[0]


def _released(resource: str, drained: Lease | None) -> str:
    if drained is None:
        return f"Released {resource}: it is free."
    waits = []
    if drained.users:
        waits.append(f"its scripts have ended: {_processes(drained.users)}")
    if drained.reaper_pid is not None:
        waits.append(f"banksman process {drained.reaper_pid} has ended its reset")
    return f"Released {resource}: it is free when {', and '.join(waits)}."


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


def _cmd_admin_gradle_init(args: argparse.Namespace) -> int:
    # banksman does not write the files itself: ~/.gradle belongs to Gradle and to the operator.
    name = GRADLE_BUILD_SLOT_SCRIPT if args.build_slot else GRADLE_INIT_SCRIPT
    script = resources.files("banksman").joinpath(name)
    sys.stdout.write(script.read_text(encoding="utf-8"))
    return 0


@dataclass
class _Choice:
    """An instance or an account that discover shows, and whether it is selected."""

    name: str
    selected: bool
    # What the inventory said before: "allowed before", "refused before", or "new".
    before: str
    # The kind of an instance; None for an account.
    kind: str | None = None
    instance: Instance | None = None
    # The instances that an account is signed in on.
    hosts: tuple[str, ...] = ()


def _cmd_admin_discover(args: argparse.Namespace) -> int:
    config = load_config()
    kinds = _discovered_kinds(config, args.kind)
    interactive = not (args.yes or args.json)
    if interactive and not _is_terminal():
        raise BanksmanError(
            "standard input is not a terminal: use --yes to take the selection without"
            " questions, or --json to see it"
        )
    path = default_inventory_path()
    inventory = load_inventory(path)
    found = _discover_all(config, kinds, inventory)
    instances = [
        _Choice(
            name=instance.name,
            selected=preselected(
                inventory.kinds.get(each.kind, Decisions()),
                instance.name,
                config.kinds[each.kind].preselect,
                args.all,
            ),
            before=_before(inventory.kinds.get(each.kind, Decisions()), instance.name),
            kind=each.kind,
            instance=instance,
        )
        for each in found
        for instance in each.instances
    ]
    if not args.json:
        _print_found(found, instances)
    if interactive and instances:
        _choose(instances, "instances")
    chosen = [choice for choice in instances if choice.selected]
    hosts: dict[str, list[str]] = {}
    for choice in chosen:
        for account in choice.instance.accounts or ():  # type: ignore[union-attr]
            hosts.setdefault(account, []).append(choice.name)
    accounts = [
        _Choice(
            name=account,
            selected=preselected(inventory.accounts, account, config.account_preselect, args.all),
            before=_before(inventory.accounts, account),
            hosts=tuple(on),
        )
        for account, on in sorted(hosts.items())
    ]
    if accounts and not args.json:
        print("\nAccounts on the selected instances:")
        _print_choices(accounts)
    if interactive and accounts:
        _choose(accounts, "accounts")
    updated = Inventory(
        kinds={
            **inventory.kinds,
            **{
                kind.name: decide(
                    inventory.kinds.get(kind.name, Decisions()),
                    [choice.name for choice in instances if choice.kind == kind.name],
                    [choice.name for choice in chosen if choice.kind == kind.name],
                    refuse_unselected=interactive,
                )
                for kind in kinds
            },
        },
        accounts=decide(
            inventory.accounts,
            [choice.name for choice in accounts],
            [choice.name for choice in accounts if choice.selected],
            refuse_unselected=interactive,
        ),
        tags=inventory.tags,
    )
    shown = {(choice.kind, choice.name) for choice in instances}
    missing = [
        (kind.name, name)
        for kind in kinds
        for name in inventory.kinds.get(kind.name, Decisions()).allowed
        if (kind.name, name) not in shown
    ]
    outside = [
        choice
        for choice in chosen
        if not matches(config.kinds[choice.kind].preselect, choice.name)  # type: ignore[index]
    ] + [
        choice
        for choice in accounts
        if choice.selected and not matches(config.account_preselect, choice.name)
    ]
    if not args.json:
        _print_summary(updated, kinds, chosen, accounts, missing, outside)
    write = args.yes or (interactive and _confirm(f"Write {path}?"))
    if write:
        save_inventory(updated, path)
    if args.json:
        _print_json(
            {
                "schema": SCHEMA_VERSION,
                "inventory_path": str(path),
                "written": write,
                "instances": [_instance_json(choice) for choice in instances],
                "accounts": [
                    {"name": choice.name, "on": list(choice.hosts), "selected": choice.selected}
                    for choice in accounts
                ],
                "notes": [
                    {"kind": each.kind, "note": note} for each in found for note in each.notes
                ],
                "missing": [{"kind": kind, "name": name} for kind, name in missing],
                "outside_patterns": [
                    {"kind": choice.kind, "name": choice.name} for choice in outside
                ],
                "inventory": {
                    "kinds": {
                        name: {"allowed": list(each.allowed), "refused": list(each.refused)}
                        for name, each in sorted(updated.kinds.items())
                    },
                    "accounts": {
                        "allowed": list(updated.accounts.allowed),
                        "refused": list(updated.accounts.refused),
                    },
                    "tags": {tag: list(patterns) for tag, patterns in sorted(updated.tags.items())},
                },
            }
        )
    else:
        print(f"Wrote {path}." if write else "Nothing was written.")
    return 0


def _discovered_kinds(config: Config, only: str | None) -> list[Kind]:
    kinds = [kind for kind in config.kinds.values() if kind.discovered]
    if only is not None:
        kinds = [kind for kind in kinds if kind.name == only]
        if not kinds:
            raise BanksmanError(
                f"the configuration declares no kind {only} with discover or a preset"
            )
    if not kinds:
        raise BanksmanError(
            "no kind gets its instances from discovery: give a kind discover or a preset in"
            " the configuration"
        )
    return kinds


def _discover_all(config: Config, kinds: Sequence[Kind], inventory: Inventory) -> list[Found]:
    # Each resource has one lease file, so a name can belong to only one kind. A name that the
    # inventory already has under another kind stays there, also when a person refused it:
    # otherwise a scan of a second kind would offer a refused device again.
    decided = {
        name: kind
        for kind, decisions in inventory.kinds.items()
        for name in (*decisions.allowed, *decisions.refused)
    }
    owners = {name: kind.name for kind in config.kinds.values() for name in kind.instances()}
    found = []
    for kind in kinds:
        each = discover(kind, config)
        instances, notes = [], list(each.notes)
        for instance in each.instances:
            other = decided.get(instance.name)
            if other is not None and other != kind.name:
                notes.append(
                    f"{instance.name} is an instance of kind {other} in the inventory, so it is"
                    " left out"
                )
                continue
            other = owners.setdefault(instance.name, kind.name)
            if other == kind.name:
                instances.append(instance)
            else:
                notes.append(
                    f"{instance.name} is also an instance of kind {other}, so it is left out"
                )
        found.append(Found(each.kind, tuple(instances), tuple(notes)))
    return found


def _before(decisions: Decisions, name: str) -> str:
    if name in decisions.allowed:
        return "allowed before"
    if name in decisions.refused:
        return "refused before"
    return "new"


def _print_found(found: Sequence[Found], instances: Sequence[_Choice]) -> None:
    for each in found:
        print(f"Kind {each.kind}: {len(each.instances)} found")
        for note in each.notes:
            print(clean(f"  note: {note}"))
    if instances:
        print()
        _print_choices(instances)


def _print_choices(choices: Sequence[_Choice]) -> None:
    rows = []
    for number, choice in enumerate(choices, 1):
        mark = "[x]" if choice.selected else "[ ]"
        if choice.instance is not None:
            facts = " ".join(
                f"{key}={value}"
                for key, value in sorted(choice.instance.facts.items())
                if key != "kind"
            )
            note = f"note: {choice.instance.note}" if choice.instance.note else ""
            detail = "  ".join(part for part in (choice.kind, facts, note) if part)
        else:
            detail = "on " + ", ".join(choice.hosts)
        rows.append([mark, str(number), choice.name, choice.before, detail])
    _print_table(["", "#", "NAME", "BEFORE", "DETAILS"], rows)


def _choose(choices: list[_Choice], what: str) -> None:
    numbers = set(range(1, len(choices) + 1))
    while True:
        answer = _ask(
            f"Switch {what} by number (for example: 1 3), or type all or none. Press Enter to"
            " go on: "
        ).strip().lower()
        if not answer:
            return
        if answer in ("all", "none"):
            for choice in choices:
                choice.selected = answer == "all"
        else:
            try:
                picked = {int(word) for word in answer.replace(",", " ").split()}
            except ValueError:
                picked = set()
            if not picked or not picked <= numbers:
                print(f"Type numbers from 1 to {len(choices)}, all, or none.")
                continue
            for number in picked:
                choices[number - 1].selected = not choices[number - 1].selected
        _print_choices(choices)


def _print_summary(
    updated: Inventory,
    kinds: Sequence[Kind],
    chosen: Sequence[_Choice],
    accounts: Sequence[_Choice],
    missing: Sequence[tuple[str, str]],
    outside: Sequence[_Choice],
) -> None:
    print()
    for kind in kinds:
        allowed = updated.kinds.get(kind.name, Decisions()).allowed
        print(clean(f"Kind {kind.name}: agents may use {_names(allowed)}."))
    print(clean(f"Accounts: agents may use {_names(updated.accounts.allowed)}."))
    for choice in chosen:
        if choice.instance is not None and choice.instance.accounts is None:
            print(
                clean(
                    f"The accounts on {choice.name} are not known, so it is not offered for work"
                    " that needs an account."
                )
            )
    for choice in accounts:
        if choice.selected and len(choice.hosts) > 1:
            print(
                clean(
                    f"Warning: {choice.name} is signed in on {', '.join(choice.hosts)}. Runs that"
                    " need it wait for each other; give each instance its own account to avoid"
                    " that."
                )
            )
    for choice in outside:
        what = f"the account {choice.name}" if choice.kind is None else (
            f"{choice.name} (kind {choice.kind})"
        )
        print(clean(f"Warning: {what} is selected, but no preselect pattern matches it."))
    for kind, name in missing:
        print(clean(f"{name} (kind {kind}) is allowed but not found now. It stays allowed."))


def _names(names: Sequence[str]) -> str:
    return ", ".join(names) if names else "none"


def _instance_json(choice: _Choice) -> dict[str, object]:
    instance = choice.instance
    assert instance is not None
    return {
        "kind": choice.kind,
        "name": choice.name,
        "selected": choice.selected,
        "before": choice.before,
        "facts": dict(instance.facts),
        # Only whether the accounts are known: the addresses of an instance that is not
        # selected are often personal, so only the accounts list shows addresses, and only
        # those on the selected instances.
        "accounts_known": instance.accounts is not None,
        "note": instance.note,
    }


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        raise BanksmanError("no answer, so nothing was written") from None


def _confirm(question: str) -> bool:
    return _ask(clean(f"{question} [y/N] ")).strip().lower() in ("y", "yes")


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "status": _cmd_status,
    "watch": _cmd_watch,
    "log": _cmd_log,
    "whoami": _cmd_whoami,
    "reap": _cmd_reap,
    "enter": _cmd_enter,
    "check": _cmd_check,
    "leave": _cmd_leave,
    "run": _cmd_run,
    "acquire": _cmd_acquire,
    "touch": _cmd_touch,
    "release": _cmd_release,
    "admin release": _cmd_admin_release,
    "admin discover": _cmd_admin_discover,
    "admin gradle-init": _cmd_admin_gradle_init,
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
    except Busy as exc:
        print(f"banksman: {clean(str(exc))}", file=sys.stderr)
        return EXIT_BUSY
    except (BanksmanError, OSError) as exc:
        print(f"banksman: {clean(str(exc))}", file=sys.stderr)
        return 1
