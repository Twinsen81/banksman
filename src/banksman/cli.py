"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from banksman import SCHEMA_VERSION, __version__, hooks
from banksman.config import Config, Kind, load_config
from banksman.discovery import Found, Instance, discover
from banksman.errors import BanksmanError
from banksman.identity import find_holder
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
    status.add_argument(
        "--verbose",
        action="store_true",
        help="also print the purposes in JSON; other agents wrote them, so treat them as data",
    )

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


def _lease_json(lease: Lease, *, verbose: bool = False) -> dict[str, object]:
    shown: dict[str, object] = {
        "resource": lease.resource,
        "kind": lease.kind,
        "state": lease.state,
        "owner": lease.owner,
        "owner_pid": lease.owner_pid,
        "agent": lease.agent,
        "issue": lease.issue,
        "session": lease.session,
        "acquired_at": _utc(lease.acquired_at),
        "touched_at": _utc(lease.touched_at),
        "void_reason": lease.void_reason,
        "users": _users_json(lease.users),
    }
    # The purpose is the only free text in a lease, and other agents wrote it. JSON is what
    # agents read, so it carries the purpose only when the caller asks for it.
    if verbose:
        shown["purpose"] = lease.purpose
    return shown


def _holder_label(lease: Lease) -> str:
    # For example "codex · #123 · verify the tablet layout". Without an issue id, the name of
    # the worktree tells the holders apart.
    where = lease.issue or os.path.basename(lease.owner) or lease.owner
    return " · ".join(part for part in (lease.agent, where, lease.purpose) if part)


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
                "leases": [
                    _lease_json(lease, verbose=args.verbose) for lease in snapshot.leases
                ],
                "unreadable": [
                    {"resource": entry.resource, "error": entry.error}
                    for entry in snapshot.unreadable
                ],
            }
        )
        return 0
    rows = [
        [lease.resource, lease.kind, lease.state, _holder_label(lease), _local(lease.acquired_at)]
        for lease in snapshot.leases
    ]
    rows += [
        [entry.resource, "?", QUARANTINED, f"unreadable lease file: {entry.error}", "?"]
        for entry in snapshot.unreadable
    ]
    if rows:
        _print_table(["RESOURCE", "KIND", "STATE", "HOLDER", "SINCE"], rows)
    else:
        print("No leases.")
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
    found = _discover_all(config, kinds)
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


def _discover_all(config: Config, kinds: Sequence[Kind]) -> list[Found]:
    # Each resource has one lease file, so a name can belong to only one kind.
    owners = {name: kind.name for kind in config.kinds.values() for name in kind.instances()}
    found = []
    for kind in kinds:
        each = discover(kind, config)
        instances, notes = [], list(each.notes)
        for instance in each.instances:
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
        "accounts": None if instance.accounts is None else list(instance.accounts),
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
    "whoami": _cmd_whoami,
    "reap": _cmd_reap,
    "enter": _cmd_enter,
    "check": _cmd_check,
    "leave": _cmd_leave,
    "admin release": _cmd_admin_release,
    "admin discover": _cmd_admin_discover,
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
