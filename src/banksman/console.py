"""What `status` and `watch` show: every resource, who holds it, and when it can be free.

The functions here work on plain data and print nothing, so that `status`, `watch`, and the
JSON show the same thing.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from banksman.discovery import ENDS, FactValue, handle_of, serial_of
from banksman.history import ACQUIRE, FORCE_RELEASE, QUARANTINE, RELEASE, VOID, Event
from banksman.lease import DRAINING, VOID_REASONS, Lease, User, free_by
from banksman.request import Search
from banksman.sanitize import clean
from banksman.store import Snapshot

FREE = "free"
# An allowed instance that discovery does not find now, for example an unplugged phone.
ABSENT = "absent"
UNREADABLE = "unreadable"
# The facts that the console shows. The others are for requests.
SHOWN_FACTS = ("form", "api")
NONE = "-"


@dataclass(frozen=True)
class Resource:
    name: str
    kind: str | None
    state: str
    present: bool
    facts: Mapping[str, FactValue] = field(default_factory=dict)
    lease: Lease | None = None
    # Why the lease file cannot be read.
    error: str | None = None
    # The serial in the lease, or for a resource without a lease, the one that discovery found.
    serial: str | None = None
    # Each use costs money.
    paid: bool = False
    # The id of the reservation of a remote device, from the lease or from discovery.
    handle: str | None = None
    # When that reservation ends, in wall-clock time, when discovery knows it.
    ends: float | None = None


@dataclass(frozen=True)
class Clock:
    """Now, in awake time and in wall-clock time.

    Lease deadlines count awake time. A deadline is shown as the time of day at which it comes
    if the machine does not sleep before then.
    """

    awake: float
    wall: float

    def wall_time(self, awake: float) -> float:
        return self.wall + (awake - self.awake)


def resources(snapshot: Snapshot, found: Search, paid: Collection[str] = ()) -> list[Resource]:
    """Return every resource that agents may use or that has a lease file.

    `found` is a search with one part without clauses: every permitted resource that is present
    now. A resource that discovery finds but that the operator does not permit is never shown,
    because it can be personal, also to the agents that read the JSON. `paid` names the kinds
    whose use costs money.
    """
    present = {candidate.resource: candidate for candidate in found.candidates[0]}
    absent = {name: kind for kind, name in found.absent}
    shown: dict[str, Resource] = {}
    for lease in snapshot.leases:
        candidate = present.get(lease.resource)
        facts = candidate.facts if candidate is not None else {}
        shown[lease.resource] = Resource(
            lease.resource,
            lease.kind,
            lease.state,
            present=candidate is not None,
            facts=_shown(facts),
            lease=lease,
            serial=lease.serial,
            paid=lease.kind in paid,
            handle=lease.handle or handle_of(facts),
            ends=_ends(facts),
        )
    for entry in snapshot.unreadable:
        candidate = present.get(entry.resource)
        facts = candidate.facts if candidate is not None else {}
        kind = candidate.kind if candidate is not None else absent.get(entry.resource)
        shown[entry.resource] = Resource(
            entry.resource,
            kind,
            UNREADABLE,
            present=candidate is not None,
            facts=_shown(facts),
            error=entry.error,
            serial=serial_of(facts),
            paid=kind in paid,
            handle=handle_of(facts),
            ends=_ends(facts),
        )
    for name, candidate in present.items():
        if name not in shown:
            shown[name] = Resource(
                name,
                candidate.kind,
                FREE,
                True,
                _shown(candidate.facts),
                serial=serial_of(candidate.facts),
                paid=candidate.paid,
                handle=handle_of(candidate.facts),
                ends=_ends(candidate.facts),
            )
    for name, kind in absent.items():
        if name not in shown:
            shown[name] = Resource(name, kind, ABSENT, False, paid=kind in paid)
    return sorted(shown.values(), key=lambda each: (each.kind is None, each.kind or "", each.name))


def _shown(facts: Mapping[str, FactValue]) -> dict[str, FactValue]:
    return {name: facts[name] for name in SHOWN_FACTS if name in facts}


def _ends(facts: Mapping[str, FactValue]) -> float | None:
    # A discover hook can set the fact to any text, so only a valid time counts.
    text = facts.get(ENDS)
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    value = moment.timestamp()
    return value if math.isfinite(value) else None


def owner_pids(items: Sequence[Resource]) -> list[int]:
    return sorted(
        {
            each.lease.owner_pid
            for each in items
            if each.lease is not None and each.lease.owner_pid is not None
        }
    )


def status_json(
    items: Sequence[Resource],
    notes: Sequence[str],
    clock: Clock,
    running: Mapping[int, str],
    *,
    verbose: bool,
) -> dict[str, object]:
    return {
        "resources": [_resource_json(each, clock, running, verbose) for each in items],
        "notes": list(notes),
    }


def _resource_json(
    item: Resource, clock: Clock, running: Mapping[int, str], verbose: bool
) -> dict[str, object]:
    shown: dict[str, object] = {
        "resource": item.name,
        "kind": item.kind,
        "state": item.state,
        "present": item.present,
        "paid": item.paid,
        "serial": item.serial,
        "handle": item.handle,
        # When the reservation of a remote device ends. "in" counts wall-clock time, because
        # the service ends the reservation also while the machine sleeps.
        "ends": None
        if item.ends is None
        else {"at": utc(item.ends), "in": round(item.ends - clock.wall)},
        "facts": dict(item.facts),
        "lease": None if item.lease is None else lease_json(item.lease, clock, running, verbose),
    }
    if item.error is not None:
        shown["error"] = item.error
    return shown


def lease_json(
    lease: Lease, clock: Clock, running: Mapping[int, str], verbose: bool = False
) -> dict[str, object]:
    ends = free_by(lease, running)
    shown: dict[str, object] = {
        "resource": lease.resource,
        "kind": lease.kind,
        "state": lease.state,
        "owner": lease.owner,
        "owner_pid": lease.owner_pid,
        "agent": lease.agent,
        "issue": lease.issue,
        "session": lease.session,
        "serial": lease.serial,
        "handle": lease.handle,
        "acquired_at": utc(lease.acquired_at),
        "touched_at": utc(lease.touched_at),
        "free_by": None
        if ends is None
        else {
            "expected": _moment(ends.expected, clock),
            "latest": _moment(ends.latest, clock),
            "abandoned": _moment(ends.abandoned, clock),
        },
        # A draining lease is freed when its scripts end, and quarantined when they still run
        # at this time.
        "drain_deadline": _moment(lease.drain_deadline, clock)
        if lease.state == DRAINING
        else None,
        "void_reason": lease.void_reason,
        "users": users_json(lease.users),
        "accounts": list(lease.accounts),
    }
    # The purpose is the only free text in a lease, and other agents wrote it. JSON is what
    # agents read, so it carries the purpose only when the caller asks for it.
    if verbose:
        shown["purpose"] = lease.purpose
    return shown


def _moment(awake: float | None, clock: Clock) -> dict[str, object] | None:
    # "in" is negative when the time has passed, for example an expected end that is overdue.
    if awake is None:
        return None
    return {"at": utc(clock.wall_time(awake)), "in": round(awake - clock.awake)}


def users_json(users: Sequence[User]) -> list[dict[str, object]]:
    return [{"pid": user.pid, "pgid": user.pgid} for user in users]


def processes_text(items: Sequence[object]) -> str:
    """Return, for example, "pid 51234 in group 51240, pid 51300" for scripts or processes."""
    return ", ".join(
        f"pid {item.pid}"  # type: ignore[attr-defined]
        + ("" if item.pgid is None else f" in group {item.pgid}")  # type: ignore[attr-defined]
        for item in items
    )


STATUS_HEADER = (
    "RESOURCE",
    "KIND",
    "SERIAL",
    "FORM",
    "API",
    "STATE",
    "SINCE",
    "LAST USE",
    "EXPECTED",
    "LATEST",
    "IF ABANDONED",
    "ACCOUNTS",
    "HOLDER",
)


def status_rows(
    items: Sequence[Resource], clock: Clock, running: Mapping[int, str]
) -> list[list[str]]:
    rows = []
    for item in items:
        lease = item.lease
        facts = [str(item.facts[name]) if name in item.facts else NONE for name in SHOWN_FACTS]
        serial = item.serial or NONE
        kind = item.kind or NONE
        if item.paid:
            kind = f"{kind} (paid)"
        if lease is None:
            holder = NONE if item.error is None else f"lease file: {item.error}"
            rows.append([item.name, kind, serial, *facts, item.state, *[NONE] * 6, holder])
            continue
        ends = free_by(lease, running)
        times = [NONE, NONE, NONE]
        if ends is not None:
            times = [
                NONE if ends.expected is None else _time(clock.wall_time(ends.expected), clock),
                _time(clock.wall_time(ends.latest), clock),
                _time(clock.wall_time(ends.abandoned), clock),
            ]
        rows.append(
            [
                item.name,
                kind,
                serial,
                *facts,
                lease.state,
                _time(lease.acquired_at, clock),
                _time(lease.touched_at, clock),
                *times,
                ",".join(lease.accounts) or NONE,
                holder_label(lease),
            ]
        )
    return rows


def holder_label(lease: Lease | Event) -> str:
    # For example "codex · #123 · verify the tablet layout". Without an issue id, the name of
    # the worktree tells the holders apart.
    owner = lease.owner or ""
    where = lease.issue or os.path.basename(owner) or owner
    return " · ".join(part for part in (lease.agent, where, lease.purpose) if part) or NONE


def _time(wall: float, clock: Clock) -> str:
    moment = time.localtime(wall)
    if time.strftime("%Y-%m-%d", moment) == time.strftime("%Y-%m-%d", time.localtime(clock.wall)):
        return time.strftime("%H:%M", moment)
    return time.strftime("%m-%d %H:%M", moment)


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    cells = [[clean(cell) for cell in row] for row in [header, *rows]]
    widths = [max(len(row[column]) for row in cells) for column in range(len(header))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in cells
    )


def utc(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def span(seconds: float) -> str:
    """Return a duration such as 45s, 26m, or 1h 20m."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


def void_text(reason: str | None) -> str:
    return VOID_REASONS.get(reason or "", "the lease was void")


LOG_HEADER = ("TIME", "EVENT", "RESOURCE", "KIND", "DETAILS", "HOLDER")


def log_rows(events: Sequence[Event]) -> list[list[str]]:
    return [
        [
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.at)),
            event.event,
            event.resource,
            event.kind or NONE,
            _details(event),
            NONE if event.owner is None else holder_label(event),
        ]
        for event in events
    ]


def event_json(event: Event, *, verbose: bool) -> dict[str, object]:
    shown = event.to_json()
    del shown["schema"]
    shown["at"] = utc(event.at)
    # The purpose is untrusted text from another agent, as in status.
    if not verbose:
        del shown["purpose"]
    return shown


def _details(event: Event) -> str:
    if event.event == ACQUIRE:
        return f"with the accounts {','.join(event.accounts)}" if event.accounts else NONE
    if event.event == RELEASE:
        drains = ""
        if event.running:
            drains = f"it drains while its scripts run: {processes_text(event.running)}"
        elif event.drained:
            drains = "it drains while another banksman process resets its instance"
        return "; ".join(part for part in (_held(event), drains) if part) or NONE
    if event.event == VOID:
        return "; ".join(part for part in (void_text(event.reason), _held(event)) if part)
    if event.event == QUARANTINE:
        if event.problem is not None:
            return event.problem
        stopping = "on" if event.stopping else "off"
        return (
            f"its scripts still ran at the drain deadline: {processes_text(event.running)};"
            f" stopping was {stopping}"
        )
    if event.event == FORCE_RELEASE:
        if event.problem is not None:
            return event.problem
        text = f"a person released the {event.state} lease"
        if event.running:
            text += f"; its scripts still ran: {processes_text(event.running)}"
        return text
    # A reap frees the resource of a void or released lease.
    return f"the resource is free again ({void_text(event.reason)})"


def _held(event: Event) -> str:
    parts = []
    if event.held is not None:
        parts.append(f"held {span(event.held)}")
    if event.longest_quiet is not None:
        parts.append(f"longest time without a touch {span(event.longest_quiet)}")
    return ", ".join(parts)
