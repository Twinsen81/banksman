"""The lease store: one lease file per resource, changed only under one file lock.

The lock is held for milliseconds, never while a resource is in use.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import pwd
import re
import stat
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from banksman import LEASE_SCHEMA, fencing, trace
from banksman.assign import Option, Part, assign
from banksman.errors import BanksmanError
from banksman.history import (
    ACQUIRE,
    FORCE_RELEASE,
    QUARANTINE,
    REAP,
    RELEASE as RELEASE_EVENT,
    VOID,
    Event,
    problem_text,
    processes,
)
from banksman.lease import (
    BOOTING,
    DRAINING,
    FREE_TIMEOUT,
    HELD,
    ISSUE,
    LEASE_VARIABLE,
    QUARANTINED,
    READY,
    REBOOTED,
    RELEASE,
    RELEASED_END,
    RESOURCE_NAME,
    VOID_REASONS,
    Holder,
    Lease,
    LeaseFormatError,
    SerialSeen,
    Timeouts,
    User,
    check_names,
    check_resource,
    checked_pids,
    is_label,
    is_owner,
    is_wall_time,
    void_reason,
)
from banksman.system import OUTSIDE_SANDBOX, System

STATE_DIR_ENV = "BANKSMAN_STATE_DIR"
QUARANTINE_DIR_ENV = "BANKSMAN_QUARANTINE_DIR"
LOG_ENV = "BANKSMAN_LOG"
ENDED_ENV = "BANKSMAN_ENDED"
TRACE_ENV = "BANKSMAN_TRACE"
LOCK_WAIT_SECONDS = 10.0
RELEASED = "released"
# A take-back that this many reapers started and none finished is quarantined.
TAKE_BACK_ATTEMPTS = 3
# The owner of the lease that the reaper writes for a free instance that it ends.
REAPER_OWNER = "banksman"

# The version of the record of lease ends. No lease file changes with it, so it is not the lease
# schema. A record of another version counts as empty, and the next end replaces it.
ENDS_SCHEMA = 2
# How a lease ended, as the events of the log name it.
ENDS = (RELEASE_EVENT, VOID, FORCE_RELEASE)

_LOCK_FILE = ".lock"
_TEMP_PREFIX = ".tmp-"
_LEASE_SUFFIX = ".json"


class StoreError(BanksmanError):
    """The state directory or a lease file cannot be used safely."""


class LockTimeout(StoreError):
    """Another process held the lock for too long."""


class Busy(BanksmanError):
    """Another holder has the resource."""


class NotHeld(BanksmanError):
    """The caller does not hold a valid lease on the resource."""


class Outdated(BanksmanError):
    """A lease on a resource that the request could get ended after its discovery started."""


@dataclass(frozen=True)
class Unreadable:
    """A lease file that cannot be read. Its resource stays out of use."""

    resource: str
    error: str


@dataclass(frozen=True)
class Snapshot:
    leases: list[Lease]
    unreadable: list[Unreadable]


@dataclass(frozen=True)
class Choice:
    """A resource that a request may get, with the lease that it would get."""

    resource: str
    kind: str
    timeouts: Timeouts = Timeouts()
    # The caller resets the instance before the holder uses it. Until then the lease is
    # booting, and it names the caller, so that no other banksman process acts on the instance
    # while the reset runs.
    reset: bool = False
    # The allowed accounts that are signed in on the instance now, in the order of choice.
    accounts: tuple[str, ...] = ()
    # The caller starts the instance, which does not run, before the holder uses it. The lease
    # is booting until then, as for a reset.
    start: bool = False
    # Each use costs money, so only a request that acknowledges the cost gets the resource,
    # unless the caller keeps a lease on it.
    paid: bool = False
    # A person or a program outside banksman uses the instance, so it is not granted, as one with
    # a lease, unless the caller keeps a lease on it.
    in_use: bool = False


@dataclass(frozen=True)
class Need:
    """One part of a request: the resources that it may get, and how many accounts on its
    resource it needs."""

    choices: tuple[Choice, ...]
    accounts: int = 0


@dataclass(frozen=True)
class LastUse:
    """The last lease of a resource that a holder could use, and what banksman did to the
    instance after it."""

    # The worktree of the holder.
    owner: str
    # The boot on which the lease ended, and the wall time of its end.
    boot_id: str
    ended_at: float
    # How it ended: by a release, a void, or a forced release. A forced release of a later lease
    # that no holder used counts as a forced release too.
    how: str
    # A take-back ran a hook of the kind at its end, or at the end of a later lease that no holder
    # used, such as the reaper's lease of a free emulator. banksman does not know what the hook did
    # to the instance.
    hook: bool = False
    reason: str | None = None
    label: str | None = None
    issue: str | None = None
    # The reservation of a remote device. Another reservation is another device.
    handle: str | None = None

    def left_as_is(self, owner: str, boot_id: str, handle: str | None) -> bool:
        """Whether `owner` gave the instance back with a plain release on this boot, banksman ran
        nothing on it since, and it is the same instance."""
        return (
            self.owner == owner
            and self.how == RELEASE_EVENT
            and not self.hook
            and self.boot_id == boot_id
            and self.handle == handle
        )

    def to_json(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "boot_id": self.boot_id,
            "ended_at": self.ended_at,
            "how": self.how,
            "reason": self.reason,
            "hook": self.hook,
            "label": self.label,
            "issue": self.issue,
            "handle": self.handle,
        }

    @classmethod
    def from_json(cls, data: object) -> LastUse | None:
        if not isinstance(data, dict):
            return None
        use = cls(
            owner=data.get("owner"),  # type: ignore[arg-type]
            boot_id=data.get("boot_id"),  # type: ignore[arg-type]
            ended_at=data.get("ended_at"),  # type: ignore[arg-type]
            how=data.get("how"),  # type: ignore[arg-type]
            hook=data.get("hook"),  # type: ignore[arg-type]
            reason=data.get("reason"),
            label=data.get("label"),
            issue=data.get("issue"),
            handle=data.get("handle"),
        )
        valid = (
            is_owner(use.owner)
            and _is_text(use.boot_id)
            and is_wall_time(use.ended_at)
            and use.how in ENDS
            and isinstance(use.hook, bool)
            and (use.reason is None or (isinstance(use.reason, str) and use.reason in VOID_REASONS))
            and (use.label is None or is_label(use.label))
            and (use.issue is None or _matches(ISSUE, use.issue))
            and (use.handle is None or _matches(RESOURCE_NAME, use.handle))
        )
        return use if valid else None


@dataclass(frozen=True)
class Ended:
    """The last end of a lease on a resource."""

    boot_id: str
    # Awake time on that boot.
    at: float
    # The lease before it that a holder could use, if any.
    last_use: LastUse | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "boot_id": self.boot_id,
            "at": self.at,
            "last_use": None if self.last_use is None else self.last_use.to_json(),
        }

    @classmethod
    def from_json(cls, data: object) -> Ended | None:
        if not isinstance(data, dict):
            return None
        boot_id, at, use = data.get("boot_id"), data.get("at"), data.get("last_use")
        last_use = None if use is None else LastUse.from_json(use)
        if not _is_text(boot_id) or not _is_number(at) or (use is not None and last_use is None):
            return None
        return cls(boot_id, at, last_use)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Granted:
    lease: Lease
    # The accounts that the part got. A kept lease can list more.
    accounts: tuple[str, ...] = ()
    # The caller already held this lease, and passed its id to keep it.
    kept: bool = False
    # The last lease of the resource that a holder could use, before this new lease.
    previous: LastUse | None = None


@dataclass(frozen=True)
class Unused:
    """A free instance that the reaper ends: banksman started it and no lease of it has existed
    for the free timeout of its kind, or nobody has used it for the unused timeout of its
    kind."""

    resource: str
    kind: str
    # Its address, for `status` and the adb guard while the reaper ends it.
    serial: str | None = None
    # The void reason of the reaper's lease: FREE_TIMEOUT or UNUSED.
    reason: str = FREE_TIMEOUT


@dataclass(frozen=True)
class Reaped:
    lease: Lease
    outcome: str  # RELEASED, DRAINING, or QUARANTINED
    # The scripts that still ran: the lease waits for them, or is quarantined because of them.
    running: tuple[User, ...] = ()
    # Why the lease is quarantined, when the reaper itself found the reason.
    problem: str | None = None


# Ends the instance of a void lease, for example with the on_void hook of its kind. Returns
# why that cannot be confirmed, or None; with a reason, the lease is quarantined, and the log
# records the reason. Only one reaper at a time runs it for a lease, but a reaper that ended in
# the middle leaves the lease to the next one, so it must be safe to run again.
TakeBack = Callable[[Lease], str | None]
# Whether the reaper may signal the scripts of a void lease.
Stopping = Callable[[Lease], bool]
# Returns the free instances that the reaper ends now. It gets the resources that have a lease
# file in any state, and the awake time. It runs under the lock, so that no request gets an
# instance before the reaper has written its own lease of it, and it must not raise.
FindUnused = Callable[[frozenset[str], float], Sequence[Unused]]
# Adds an event to the log. It runs under the lock, and it must not raise: a lease that changed
# stays changed when its event cannot be written.
Record = Callable[[Event], None]
# Whether the take-back of a void lease runs a hook of its kind.
Hooked = Callable[[Lease], bool]
# Tells the user about a problem that does not stop the command.
Warn = Callable[[str], None]


def default_state_dir() -> Path:
    # A fixed path, not one below $TMPDIR: the agents, sandboxes, and scheduled jobs of one
    # user can each have a different $TMPDIR, and callers that use different directories do
    # not exclude each other.
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path(f"/tmp/banksman-{os.geteuid()}")


def default_quarantine_dir() -> Path:
    override = os.environ.get(QUARANTINE_DIR_ENV)
    if override:
        return Path(override)
    # Every command restores the quarantines into its state directory, so a state directory
    # for tests keeps its own quarantines, and never mixes them with the real ones.
    state_dir = os.environ.get(STATE_DIR_ENV)
    if state_dir:
        return Path(state_dir).with_name(f"{Path(state_dir).name}-quarantine")
    # Outside /tmp: cleaners of temporary files delete old files there, and macOS empties it at
    # boot, but a quarantine lasts until a person releases it.
    return lasting_dir(QUARANTINE_DIR_ENV) / "quarantine"


def default_log_path() -> Path:
    override = os.environ.get(LOG_ENV)
    if override:
        return Path(override)
    state_dir = os.environ.get(STATE_DIR_ENV)
    if state_dir:
        return Path(state_dir).with_name(f"{Path(state_dir).name}-log.jsonl")
    # Outside /tmp, as the quarantines: the history must outlive restarts and cleaners of
    # temporary files.
    return lasting_dir(LOG_ENV) / "log.jsonl"


def default_ended_path() -> Path:
    override = os.environ.get(ENDED_ENV)
    if override:
        return Path(override)
    state_dir = os.environ.get(STATE_DIR_ENV)
    if state_dir:
        return Path(state_dir).with_name(f"{Path(state_dir).name}-ended.json")
    # Outside /tmp: the disk of an AVD and the state of a device outlive a restart, and so must
    # the record of who used them last.
    return lasting_dir(ENDED_ENV) / "ended.json"


def default_trace_path() -> Path:
    override = os.environ.get(TRACE_ENV)
    if override:
        return Path(override)
    state_dir = os.environ.get(STATE_DIR_ENV)
    if state_dir:
        return Path(state_dir).with_name(f"{Path(state_dir).name}-trace.jsonl")
    # Next to the log: a trace that the operator turned on for a test must outlive a restart.
    return lasting_dir(TRACE_ENV) / "trace.jsonl"


def lasting_dir(variable: str) -> Path:
    # The home directory comes from the user database, as for the configuration file, so that
    # every caller of one user finds the same files.
    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise StoreError(
            f"cannot find the home directory of user {os.geteuid()}; set {variable}"
        ) from exc
    return Path(home) / ".local" / "state" / "banksman"


def _take_back_nothing(lease: Lease) -> str | None:
    return None


def _stop_nothing(lease: Lease) -> bool:
    return False


def _record_nothing(event: Event) -> None:
    pass


def _hook_nothing(lease: Lease) -> bool:
    return False


def _warn_nothing(text: str) -> None:
    pass


def _unused_nothing(leased: frozenset[str], now: float) -> Sequence[Unused]:
    return ()


class Store:
    def __init__(
        self,
        directory: Path,
        system: System,
        *,
        take_back: TakeBack = _take_back_nothing,
        stopping: Stopping = _stop_nothing,
        record: Record = _record_nothing,
        unused: FindUnused = _unused_nothing,
        hooked: Hooked = _hook_nothing,
        warn: Warn = _warn_nothing,
        quarantine_dir: Path | None = None,
        ended_path: Path | None = None,
        lock_wait: float = LOCK_WAIT_SECONDS,
    ) -> None:
        self.directory = Path(directory)
        self.quarantine_dir = (
            default_quarantine_dir() if quarantine_dir is None else Path(quarantine_dir)
        )
        self.ended_path = default_ended_path() if ended_path is None else Path(ended_path)
        self.system = system
        self._take_back = take_back
        self._stopping = stopping
        self._record = record
        self._unused = unused
        self._hooked = hooked
        self._warn = warn
        self._lock_wait = lock_wait
        self._locked = False

    def reserve(
        self,
        resource: str,
        kind: str,
        holder: Holder,
        *,
        timeouts: Timeouts = Timeouts(),
        expect: float | None = None,
    ) -> Lease:
        """Write a `booting` lease, before the resource is started.

        `expect` is how long the holder expects to keep the resource, in seconds. It is only a
        hint for other callers.
        """
        return self._grant(resource, kind, holder, BOOTING, timeouts, expect)

    def acquire(
        self,
        resource: str,
        kind: str,
        holder: Holder,
        *,
        timeouts: Timeouts = Timeouts(),
        expect: float | None = None,
    ) -> Lease:
        """Write a `ready` lease, for a resource that needs no start."""
        return self._grant(resource, kind, holder, READY, timeouts, expect)

    def grant(
        self,
        needs: Sequence[Need],
        holder: Holder,
        *,
        keep: str | None = None,
        expect: float | None = None,
        reset_time: float = 0.0,
        seen: Iterable[SerialSeen] = (),
        paid: bool = False,
        label: str | None = None,
        since: float | None = None,
    ) -> list[Granted] | None:
        """Grant a resource for every need, all or nothing, or return None while that cannot be
        done because resources or accounts are in use.

        `seen` is what the discovery of the request found about serials, as for `observe`. The
        held leases get it first, and each new lease gets the serial of its instance.

        Each new lease gets its own id, and the new leases form one holding. With `keep`, the
        id of a lease that the caller holds, the resources of its holding come first: such a
        lease is touched, records the holder's owner process, and can get more accounts on its
        resource. While that holding has a held lease, the new leases join it. Otherwise a held
        resource is never granted, also not to the same owner, because several agents can work
        in one worktree.

        With `label` instead of `keep`, the holding with that label whose owner process is the
        holder's owner process comes first in the same way. Without one, the new leases start a
        holding with that label.

        When a choice needs a reset or a start, every new lease of the request is booting and
        names this process until the caller marks it ready, so that no other process frees it
        while the resets and the starts run. They run one after another, and `reset_time` is
        the longest that one reset takes, so each boot deadline also counts that time for every
        reset of the request, and the boot timeout of every other start.

        A paid choice is granted only with `paid`, the acknowledgement of the cost, or when the
        caller keeps a lease on it.

        A holding that has a booting lease is not kept, and the request waits as for a resource
        in use: the acquire that starts or resets its instance has not given it to anybody yet,
        and can have ended before it was done.

        `since` is the awake time at which the discovery of the request started. A resource whose
        lease ended at that time or later counts as in use: its holder or its take-back can have
        changed the instance after discovery saw it. When only such resources keep the request
        from being met, this raises Outdated, and the caller runs discovery again.

        Each new lease comes with the last lease of its resource that a holder could use.
        """
        for need in needs:
            for choice in need.choices:
                check_names(choice.resource, choice.kind, holder)
        if label is not None:
            _check_label(label, holder.owner_pid)
        seen = tuple(seen)
        with self._lock():
            entries, serials = self._see(self._scan(), seen)
            held = {entry.resource for entry in entries}
            # An account is in use while a lease in any state lists it. The accounts of a lease
            # file that cannot be read are not known; the file keeps only its own resource out
            # of use.
            taken = {
                account
                for entry in entries
                if isinstance(entry, Lease)
                for account in entry.accounts
            }
            # The label is looked up under the lock, so that two requests of one agent process with
            # the same label never start two holdings.
            if keep is not None:
                kept = self._holding(entries, keep)
            elif label is not None:
                kept = self._labelled(entries, holder.owner_pid, label)  # type: ignore[arg-type]
            else:
                kept = {}
            if any(lease.state == BOOTING for lease in kept.values()):
                _trace_grant(needs, entries, (), taken, kept, paid, "booting")
                return None
            ends = self._ends()
            boot_id = self.system.boot_id()
            ended = (
                set()
                if since is None
                else {
                    resource
                    for resource, end in ends.items()
                    if end.boot_id == boot_id and end.at >= since
                }
                - held
            )
            picks = assign([_part(need, held | ended, taken, kept, paid) for need in needs])
            if picks is None:
                parts = [_part(need, held, taken, kept, paid) for need in needs]
                outdated = bool(ended) and assign(parts) is not None
                _trace_grant(
                    needs, entries, ended, taken, kept, paid, "outdated" if outdated else "busy"
                )
                if outdated:
                    raise Outdated(
                        f"a lease on {', '.join(sorted(ended))} ended after the discovery of the"
                        " request started"
                    )
                return None
            # A request that keeps no lease starts a holding of its own. The new leases of a kept
            # holding get its label.
            if kept:
                first = next(iter(kept.values()))
                holding, label = first.holding, first.label
            else:
                holding = uuid.uuid4().hex
            choices = [
                next(each for each in need.choices if each.resource == pick.resource)
                for need, pick in zip(needs, picks)
            ]
            new = [choice for choice in choices if choice.resource not in kept]
            resets = sum(choice.reset for choice in new)
            starts = [choice for choice in new if choice.start]
            granted: list[tuple[str, tuple[str, ...], bool]] = []
            for choice, pick in zip(choices, picks):
                current = kept.get(pick.resource)
                if current is not None:
                    lease = replace(
                        self._touched(current, holder.owner_pid),
                        accounts=tuple(dict.fromkeys((*current.accounts, *pick.accounts))),
                    )
                    if expect is not None:
                        lease = replace(lease, expected=lease.touched + expect)
                    serials[lease.resource] = self._write(lease)
                    granted.append((lease.resource, pick.accounts, True))
                    continue
                state = BOOTING if resets or starts else READY
                lease = self._new_lease(
                    choice.resource,
                    choice.kind,
                    holder,
                    state,
                    choice.timeouts,
                    expect,
                    holding,
                    label,
                )
                lease = replace(lease, accounts=pick.accounts)
                if resets or starts:
                    me = os.getpid()
                    others = sum(each.timeouts.boot_timeout for each in starts if each != choice)
                    lease = replace(
                        lease,
                        boot_deadline=lease.touched
                        + choice.timeouts.boot_timeout
                        + others
                        + resets * reset_time,
                        reaper_pid=me,
                        reaper_started=self._start_time(me),
                    )
                serials[lease.resource] = lease
                changed = set()
                for each in seen:
                    if each.resource == lease.resource:
                        changed |= _see_serial(serials, each)
                for resource in changed - {lease.resource}:
                    self._write(serials[resource])
                lease = self._write(serials[lease.resource])
                granted.append((lease.resource, pick.accounts, False))
                self._log(ACQUIRE, lease)
            _trace_grant(
                needs,
                entries,
                ended,
                taken,
                kept,
                paid,
                "granted",
                [resource for resource, _, _ in granted],
            )
            # A later lease of the request can take the serial of an earlier one.
            return [
                Granted(
                    serials[resource],
                    accounts,
                    kept=kept,
                    previous=None if kept or resource not in ends else ends[resource].last_use,
                )
                for resource, accounts, kept in granted
            ]

    def observe(self, seen: Iterable[SerialSeen]) -> dict[str, Lease]:
        """Record what a discovery found about serials in the held leases of this boot.

        Each result applies to the lease of its resource and kind, whatever its lease id: the
        serial belongs to the instance. A result is not applied when the lease records a
        discovery that started later, so a slow discovery never undoes a newer one. A serial
        belongs to one held lease at most: a serial that discovery finds on an instance, also on
        one without a lease, leaves every other lease. A booting lease keeps its serial, because
        a start can know it before the instance answers. Recording a serial is not a touch.

        Return the held leases of this boot by resource, as they are now.
        """
        seen = tuple(seen)
        with self._lock():
            _, serials = self._see(self._scan(), seen)
        return serials

    def ends(self) -> dict[str, Ended]:
        """Return the last end of a lease on each resource, also of an earlier boot."""
        with self._lock():
            return self._ends()

    def holding(self, lease_id: str) -> dict[str, Lease]:
        """Return the valid held leases of the holding of this lease, by resource."""
        with self._lock():
            return self._holding(self._scan(), lease_id)

    def labelled(self, owner_pid: int, label: str) -> dict[str, Lease]:
        """Return the valid held leases of the holding with this label whose owner process is
        `owner_pid`, by resource."""
        _check_label(label, owner_pid)
        with self._lock():
            return self._labelled(self._scan(), owner_pid, label)

    def held_by(self, owner_pid: int) -> list[Lease]:
        """Return the valid held leases whose owner process is `owner_pid`."""
        with self._lock():
            return self._owned(self._scan(), owner_pid)

    def started(
        self,
        resource: str,
        lease_id: str,
        *,
        handle: str | None = None,
        serial: str | None = None,
    ) -> Lease:
        """Record what the start of the instance of a lease found: the id of its reservation as
        soon as the start knows it, and then its serial.

        The id is recorded in any state of the lease, so that the reaper can end the
        reservation also when the lease became void during the start. The serial is recorded
        only while the lease is held, as a discovery records it.
        """
        with self._lock():
            lease = self._current(resource, lease_id)
            if handle is not None:
                lease = self._write(replace(lease, handle=handle))
            if serial is None or lease.state not in HELD:
                return lease
            seen = SerialSeen(resource, lease.kind, serial, self.system.clock(), lease.handle)
            _, serials = self._see(self._scan(), [seen])
            return serials.get(resource, lease)

    def claim_serial(self, resource: str, lease_id: str, serials: Sequence[str]) -> Lease:
        """Record in a held lease the first of `serials` that no other held lease of this boot
        records, before the start of its instance, and return the lease.

        Two starts that choose from the same free addresses, such as the console ports of
        emulators, then never get the same one, and the serial is known from the start.
        """
        with self._lock():
            lease = self._held(resource, lease_id)
            boot_id = self.system.boot_id()
            taken = {
                entry.serial
                for entry in self._scan()
                if isinstance(entry, Lease)
                and entry.resource != resource
                and entry.state in HELD
                and entry.boot_id == boot_id
            }
            serial = next((each for each in serials if each not in taken), None)
            if serial is None:
                raise BanksmanError(
                    f"every free address for {resource} is recorded by another lease, whose start"
                    " has not taken it yet"
                )
            return self._write(replace(lease, serial=serial, serial_seen=self.system.clock()))

    def hand_over(self, leases: Sequence[tuple[str, str]], before: tuple[int, str]) -> list[Lease]:
        """Name this process in booting leases, given by their resources and ids, in place of the
        process `before`, by pid and start time: this process starts or resets their instances
        from now on, and marks them ready.

        Every lease must be valid, booting, and name `before`, or none changes. When `before` has
        ended before it handed the leases over, they are void.
        """
        with self._lock():
            me = os.getpid()
            started = self._start_time(me)
            current = [self._held(resource, lease_id) for resource, lease_id in leases]
            for lease in current:
                if lease.state != BOOTING or (lease.reaper_pid, lease.reaper_started) != before:
                    raise NotHeld(
                        f"the lease on {lease.resource} is not booting for process {before[0]}"
                    )
            return [
                self._write(replace(lease, reaper_pid=me, reaper_started=started))
                for lease in current
            ]

    def still_booting(self, resource: str, lease_id: str) -> bool:
        """Whether the file of a resource still holds this lease in state booting.

        It reads without the lock, as the adb guard does, because a start asks it every second
        while it waits for a boot, and only to decide whether to go on. A lease that is released,
        or that a reaper found void, is no longer booting. A file that cannot be read gives no
        reason to stop.
        """
        try:
            current = self._read(resource)
        except (OSError, BanksmanError):
            return True
        if isinstance(current, Unreadable):
            return True
        return current is not None and current.lease_id == lease_id and current.state == BOOTING

    def ready(self, resource: str, lease_id: str, seen: Iterable[SerialSeen] = ()) -> Lease:
        """End the boot or the reset of an instance: the holder may use it now.

        `seen` is what a discovery after a reset found about serials, as for `observe`. It
        applies in the same lock, also where a booting lease would keep its serial, so that
        nobody sees the lease ready with a serial from before the reset.
        """
        seen = tuple(seen)
        with self._lock():
            lease = self._held(resource, lease_id)
            if lease.state == READY:
                return lease
            ready = replace(
                self._touched(lease, None),
                state=READY,
                boot_deadline=None,
                reaper_pid=None,
                reaper_started=None,
            )
            if not seen:
                return self._write(ready)
            entries = [
                ready if isinstance(entry, Lease) and entry.resource == resource else entry
                for entry in self._scan()
            ]
            _, serials = self._see(entries, seen)
            return self._write(serials.get(resource, ready))

    def touch(
        self,
        resource: str,
        lease_id: str,
        owner_pid: int | None = None,
        *,
        expect: float | None = None,
    ) -> Lease:
        """Touch a lease and the other leases of its holding, and record `owner_pid` as their
        owner process, for example after the agent restarted."""
        with self._lock():
            lease = self._held(resource, lease_id)
            return self._touch_holding(lease, owner_pid, expect)[0]

    def touch_holding(
        self, lease_id: str, owner_pid: int | None = None, *, expect: float | None = None
    ) -> list[Lease]:
        """Touch every held lease of the holding of this lease, as `touch` does."""
        with self._lock():
            leases = list(self._holding(self._scan(), lease_id).values())
            if not leases:
                raise NotHeld(f"the holding of the lease {lease_id} has no held lease")
            return self._touch_holding(leases[0], owner_pid, expect)

    def touch_seen(self, seen: Sequence[Lease]) -> list[Lease]:
        """Touch the holdings of leases that the adb guard read without the lock, as `touch`
        does, but record no owner process. Return the leases that it touched.

        A lease that is no longer held, that is void, or that another process changed since the
        guard read it, for example with a touch, is left as it is. The lock comes without the
        clean-up of a change, as for an event of the log, because the guard runs before an adb
        call.
        """
        with self._lock(clean=False):
            touched: dict[str, Lease] = {}
            for lease in seen:
                if lease.resource in touched:
                    continue
                held = self._held_leases(self._scan(), lease.holding)
                if held.get(lease.resource) != lease:
                    continue
                for each in held.values():
                    touched[each.resource] = self._write(self._touched(each, None))
            return list(touched.values())

    def enter(self, resource: str, lease_id: str, pid: int, pgid: int | None = None) -> Lease:
        """Check the lease, touch it, and register a script as a user of the resource."""
        return self.enter_leases([(resource, lease_id)], pid, pgid)[0]

    def enter_leases(
        self, leases: Sequence[tuple[str, str]], pid: int, pgid: int | None = None
    ) -> list[Lease]:
        """Enter each lease, given by its resource and its id, as `enter` does: every one, or
        none when one of them is not held."""
        with self._lock():
            held = [self._held(resource, lease_id) for resource, lease_id in leases]
            table = self.system.process_table()
            entered = []
            for lease in held:
                user = fencing.register(table, lease, pid, pgid)
                # A new record replaces the one of the same process and group, and the records
                # of scripts that have ended go. Another group of the same process can still
                # run.
                users = [
                    other
                    for other in lease.users
                    if (other.pid, other.started, other.pgid) != (user.pid, user.started, user.pgid)
                    and fencing.runs(other, table)
                ]
                entered.append(replace(lease, users=(*users, user)))
            for lease in entered:
                self._write(lease)
            return self._touch_held(entered)

    def check(self, resource: str, lease_id: str) -> Lease:
        """Touch the lease and its holding while the caller may still use the resource, or
        raise NotHeld."""
        return self.check_leases([(resource, lease_id)])[0]

    def check_leases(self, leases: Sequence[tuple[str, str]]) -> list[Lease]:
        """Check and touch each lease, given by its resource and its id, as `check` does, or
        raise NotHeld for the first one that is not held."""
        with self._lock():
            return self._touch_held(
                [self._held(resource, lease_id) for resource, lease_id in leases]
            )

    def leave(self, resource: str, lease_id: str, pid: int) -> None:
        """Remove the record of a script that no longer uses the resource."""
        with self._lock():
            lease = self._current(resource, lease_id)
            if not any(user.pid == pid for user in lease.users):
                return
            table = self.system.process_table()
            # The script runs this command itself, so banksman and the processes above it do
            # not count. Any other process of the script still uses the resource.
            caller = {os.getpid(), *fencing.ancestors(table, os.getpid())}
            for user in lease.users:
                still = fencing.processes_of(user, table) - caller if user.pid == pid else set()
                if still:
                    raise BanksmanError(
                        f"process {min(still)} of the script still runs; stop the processes of the"
                        " script before it leaves"
                    )
            others = tuple(user for user in lease.users if user.pid != pid)
            self._write(replace(lease, users=others))

    def release(self, resource: str, lease_id: str, *, end: bool = False) -> Lease | None:
        """Give the resource back. Return the lease while it drains or after it was quarantined,
        or None when it is free.

        With `end`, the release also takes the instance back, as for a void lease, after the
        scripts of the lease have ended. The caller passes it only for an instance that banksman
        started.
        """
        with self._lock():
            lease = self._current(resource, lease_id)
            if lease.state not in HELD:
                raise NotHeld(f"the lease on {resource} is {lease.state}")
            given = self._give_back(lease, end)
        return self._end_released(given)

    def release_holding(
        self, lease_id: str, *, end: Collection[str] = ()
    ) -> list[tuple[Lease, Lease | None]]:
        """Give back every held lease of the holding of this lease, and take back the instances
        of the resources in `end`, as `release` does.

        Return each lease, and the lease while it drains or after it was quarantined, or None
        when its resource is free.
        """
        with self._lock():
            leases = list(self._holding(self._scan(), lease_id).values())
            if not leases:
                raise NotHeld(f"the holding of the lease {lease_id} has no held lease")
            given = [(lease, self._give_back(lease, lease.resource in end)) for lease in leases]
        return [(lease, self._end_released(after)) for lease, after in given]

    def release_all(
        self, owner_pid: int, *, end: Collection[str] = ()
    ) -> list[tuple[Lease, Lease | None]]:
        """Give back every held lease whose owner process is `owner_pid`, and take back the
        instances of the resources in `end`, as `release` does.

        Return each lease, and the lease while it drains or after it was quarantined, or None
        when its resource is free.
        """
        with self._lock():
            started = self._start_time(owner_pid)
            mine = [
                entry
                for entry in self._scan()
                if isinstance(entry, Lease)
                and entry.state in HELD
                and (entry.owner_pid, entry.owner_started) == (owner_pid, started)
            ]
            given = [(lease, self._give_back(lease, lease.resource in end)) for lease in mine]
        return [(lease, self._end_released(after)) for lease, after in given]

    def force_release(self, resource: str) -> tuple[Lease | Unreadable, tuple[User, ...]]:
        """Remove the lease on a resource in any state, without a hook and without a signal.

        Return what was removed, and the registered scripts that still run.
        """
        with self._lock():
            current = self._read(resource)
            if current is None:
                raise BanksmanError(f"{resource} has no lease")
            # A take-back or a reset that still runs could act on the instance of the next holder.
            if (
                isinstance(current, Lease)
                and current.reaper_pid is not None
                and current.boot_id == self.system.boot_id()
                and self.system.running([current.reaper_pid]).get(current.reaper_pid)
                == current.reaper_started
            ):
                raise BanksmanError(
                    f"banksman process {current.reaper_pid} takes {resource} back or resets it"
                    " now; run this command again when that process has ended"
                )
            running = self._running_users(current) if isinstance(current, Lease) else ()
            self._delete(resource, current if isinstance(current, Lease) else None, FORCE_RELEASE)
            if isinstance(current, Lease):
                self._log(FORCE_RELEASE, current, state=current.state, running=processes(running))
            else:
                self._record(
                    Event(
                        self.system.wall_clock(),
                        FORCE_RELEASE,
                        resource,
                        problem=problem_text(f"the lease file could not be read: {current.error}"),
                    )
                )
            return current, running

    def snapshot(self) -> Snapshot:
        with self._lock():
            entries = self._scan()
        return Snapshot(
            leases=[entry for entry in entries if isinstance(entry, Lease)],
            unreadable=[entry for entry in entries if isinstance(entry, Unreadable)],
        )

    def reap(self) -> list[Reaped]:
        """Take back the resources of void leases, and delete those leases. End the free
        instances that `unused` returns."""
        reaped = []
        with self._lock():
            entries = self._scan()
            leases = [entry for entry in entries if isinstance(entry, Lease)]
            leased = frozenset(entry.resource for entry in entries)
            now = self.system.clock()
            # One lease for each resource, also when two kinds name it.
            ending = {
                each.resource: each
                for each in self._unused(leased, now)
                if each.resource not in leased
            }
            if not leases and not ending:
                return []
            boot_id = self.system.boot_id()
            table = self.system.process_table()
            running = {pid: process.started for pid, process in table.items()}
            me = self._me(running)
            # The reaper ends a free instance under a lease of its own, as a take-back: a request
            # waits for the instance meanwhile, and only one reaper ends it.
            mine = [self._write(self._ending(each, me, now)) for each in ending.values()]
            checked: list[dict[str, object]] = []
            for found in leases:
                lease = found
                if lease.state in HELD:
                    reason = void_reason(lease, boot_id=boot_id, now=now, running=running)
                    checked.append(
                        {
                            "resource": lease.resource,
                            "lease_id": lease.lease_id,
                            "state": lease.state,
                            "void": reason,
                        }
                    )
                    if reason is None:
                        continue
                    self._log(VOID, lease, reason=reason, **self._spent(lease, final=False))
                    lease = _drained(lease, reason, now)
                    if _other_reaper_runs(lease, running, me):
                        # The acquire that resets the instance still runs. The lease drains, so
                        # that no ownership check passes, and it is taken back when that
                        # process has ended, so that a take-back never runs next to a reset.
                        self._write(lease)
                        continue
                elif lease.boot_id != boot_id:
                    if lease.state == QUARANTINED:
                        self._log(VOID, lease, reason=REBOOTED)
                    lease = _restarted(lease, now)
                elif lease.state != DRAINING or _other_reaper_runs(lease, running, me):
                    continue
                # The lease records this boot from now on: with the boot id of an earlier boot,
                # other reapers would read a claim as the claim of a reaper from that boot, and
                # take the lease over at once. A quarantine then also belongs to this boot, so
                # that later commands do not run a failed take-back again.
                lease = replace(lease, boot_id=boot_id)
                if not lease.ended and lease.void_reason == RELEASED_END and lease.users:
                    # The holder released the lease while its scripts ran, so its instance ends
                    # only after them, and they can still use it meanwhile.
                    outcome, users = _settled(lease, table, now)
                    if outcome == QUARANTINED:
                        reaped.append(self._finish(lease, outcome, users))
                        continue
                    if outcome == DRAINING:
                        if replace(lease, users=users) != found:
                            self._write(replace(lease, users=users))
                        continue
                    lease = replace(lease, users=())
                if not lease.ended and lease.attempts >= TAKE_BACK_ATTEMPTS:
                    # Every reaper so far ended before it finished, for example because its
                    # caller stopped it. A take-back in every command would block every command.
                    problem = f"{TAKE_BACK_ATTEMPTS} take-backs started and none finished"
                    quarantined = self._write(_quarantined(lease))
                    self._log(QUARANTINE, quarantined, problem=problem)
                    reaped.append(Reaped(quarantined, QUARANTINED, problem=problem))
                    continue
                if not lease.ended:
                    # Only one reaper takes a lease back, and another one takes it over only
                    # after that reaper has ended. So a late take-back never acts on a newer
                    # lease.
                    claimed = replace(
                        lease, attempts=lease.attempts + 1, reaper_pid=me[0], reaper_started=me[1]
                    )
                    mine.append(self._write(claimed))
                    continue
                outcome, users = _settled(lease, table, now)
                if outcome != DRAINING:
                    reaped.append(self._finish(lease, outcome, users))
                elif replace(lease, users=users) != found:
                    self._write(replace(lease, users=users))
            if trace.active():
                trace.step(
                    "reap",
                    leases=checked,
                    ending=[
                        {"resource": each.resource, "kind": each.kind, "reason": each.reason}
                        for each in ending.values()
                    ],
                )
        for lease in mine:
            reaped.extend(self._end(lease, me))
        return reaped

    def _ending(self, unused: Unused, me: tuple[int, str], now: float) -> Lease:
        holder = Holder(REAPER_OWNER)
        check_names(unused.resource, unused.kind, holder)
        lease = self._new_lease(unused.resource, unused.kind, holder, READY, Timeouts(), None)
        lease = _drained(replace(lease, serial=unused.serial), unused.reason, now)
        return replace(lease, attempts=1, reaper_pid=me[0], reaper_started=me[1])

    def _end(self, lease: Lease, me: tuple[int, str]) -> list[Reaped]:
        # Taking a resource back can take seconds, for example to end an emulator or to wait
        # for processes to end, so it runs outside the lock. No ownership check passes a
        # draining lease.
        if lease.void_reason == RELEASED_END and lease.users:
            # The holder gave the lease back while its scripts ran: they end first, and the
            # instance only after them.
            self._stop(lease)
            with self._lock():
                current = self._claimed(lease, me)
                if current is None:
                    return []
                table = self.system.process_table()
                outcome, users = _settled(current, table, self.system.clock())
                if outcome == QUARANTINED:
                    return [self._finish(current, outcome, users)]
                if outcome == DRAINING:
                    # A later reaper takes the instance back when the scripts have ended. This
                    # claim did not start a take-back.
                    waiting = replace(
                        current,
                        users=users,
                        attempts=current.attempts - 1,
                        reaper_pid=None,
                        reaper_started=None,
                    )
                    return [Reaped(self._write(waiting), DRAINING, users)]
                lease = self._write(replace(current, users=()))
        failure = None if lease.void_reason == RELEASE else self._take_back(lease)
        self._stop(lease)
        with self._lock():
            current = self._claimed(lease, me)
            if current is None:
                return []
            if failure is not None:
                quarantined = self._write(_quarantined(current))
                problem = problem_text(f"its instance could not be taken back: {failure}")
                self._log(QUARANTINE, quarantined, problem=problem)
                return [Reaped(quarantined, QUARANTINED)]
            ended = replace(current, ended=True, reaper_pid=None, reaper_started=None)
            table = self.system.process_table() if ended.users else {}
            outcome, users = _settled(ended, table, self.system.clock())
            if outcome == DRAINING:
                return [Reaped(self._write(replace(ended, users=users)), DRAINING, users)]
            return [self._finish(ended, outcome, users)]

    def _stop(self, lease: Lease) -> None:
        if lease.users and self._stopping(lease):
            # A script that left meanwhile no longer uses the resource, so it is not stopped.
            fencing.stop(replace(lease, users=self._users_now(lease)), self.system)

    def _claimed(self, lease: Lease, me: tuple[int, str]) -> Lease | None:
        """Return the lease that this reaper claimed, as its file holds it now, or None when the
        file holds another lease or another claim."""
        current = self._read(lease.resource)
        if (
            not isinstance(current, Lease)
            or current.lease_id != lease.lease_id
            or current.state != DRAINING
            or (current.reaper_pid, current.reaper_started) != me
        ):
            return None
        return current

    def _finish(self, lease: Lease, outcome: str, users: tuple[User, ...]) -> Reaped:
        if outcome == RELEASED:
            how = RELEASE_EVENT if lease.void_reason in (RELEASE, RELEASED_END) else VOID
            self._delete(lease.resource, lease, how)
            self._log(REAP, lease, reason=lease.void_reason)
            return Reaped(lease, RELEASED)
        quarantined = self._write(_quarantined(replace(lease, users=users)))
        # While stopping is off, the log shows what the reaper would have stopped, so that the
        # operator can decide whether to turn it on.
        self._log(
            QUARANTINE, quarantined, running=processes(users), stopping=self._stopping(lease)
        )
        return Reaped(quarantined, QUARANTINED, users)

    def _users_now(self, lease: Lease) -> tuple[User, ...]:
        with self._lock():
            current = self._read(lease.resource)
        if isinstance(current, Lease) and current.lease_id == lease.lease_id:
            return current.users
        return ()

    def _running_users(self, lease: Lease) -> tuple[User, ...]:
        # The pids of an earlier boot name other processes now, if any.
        if not lease.users or lease.boot_id != self.system.boot_id():
            return ()
        table = self.system.process_table()
        return tuple(user for user in lease.users if fencing.runs(user, table))

    def _give_back(self, lease: Lease, end: bool = False) -> Lease | None:
        running = self._running_users(lease)
        spent = self._spent(lease, final=True)
        resetting = self._reset_runs(lease)
        if not resetting:
            lease = replace(lease, reaper_pid=None, reaper_started=None)
            if not running and not end:
                self._delete(lease.resource, lease, RELEASE_EVENT)
                self._log(RELEASE_EVENT, lease, drained=False, end=False, **spent)
                return None
        # The scripts, and a reset by another banksman process, must end before the resource is
        # handed on, as for a void lease. The holder gives the instance back as it is, so no
        # hook ends it, unless the holder asks banksman to end it.
        reason = RELEASED_END if end else RELEASE
        drained = _drained(replace(lease, users=running), reason, self.system.clock())
        if end and not resetting:
            # This command takes the instance back itself, as a reaper that has claimed the
            # lease: no request gets the instance meanwhile, and no other reaper acts on it.
            me = os.getpid()
            drained = replace(
                drained, attempts=1, reaper_pid=me, reaper_started=self._start_time(me)
            )
        self._log(
            RELEASE_EVENT,
            lease,
            drained=bool(running) or resetting,
            end=end,
            running=processes(running),
            **spent,
        )
        return self._write(drained)

    def _end_released(self, lease: Lease | None) -> Lease | None:
        """Take back, outside the lock, the instance of a lease that this command released and
        claimed. Return the lease while it drains or after it was quarantined, or None when its
        resource is free."""
        if lease is None or lease.void_reason != RELEASED_END or lease.reaper_pid != os.getpid():
            return lease
        assert lease.reaper_started is not None
        for item in self._end(lease, (lease.reaper_pid, lease.reaper_started)):
            return None if item.outcome == RELEASED else item.lease
        return None

    def _see(
        self, entries: list[Lease | Unreadable], seen: Sequence[SerialSeen]
    ) -> tuple[list[Lease | Unreadable], dict[str, Lease]]:
        """Apply the results of discovery to the held leases of this boot, and write those that
        change. Return the entries as they are now, and the held leases of this boot."""
        boot_id = self.system.boot_id()
        serials = {
            entry.resource: entry
            for entry in entries
            if isinstance(entry, Lease) and entry.state in HELD and entry.boot_id == boot_id
        }
        changed: set[str] = set()
        for each in seen:
            changed |= _see_serial(serials, each)
        for resource in sorted(changed):
            self._write(serials[resource])
        now = [
            serials.get(entry.resource, entry) if isinstance(entry, Lease) else entry
            for entry in entries
        ]
        return now, serials

    def _log(self, event: str, lease: Lease, **details: object) -> None:
        self._record(Event.of(event, lease, self.system.wall_clock(), **details))

    def _spent(self, lease: Lease, *, final: bool) -> dict[str, float | None]:
        """Return how long a lease was held, and its longest time without a touch.

        With `final`, the holder ends the lease itself, so the time since the last touch was
        a quiet time of the run. When the lease becomes void, that time is how long the holder
        was gone.
        """
        if lease.boot_id != self.system.boot_id():
            return {"held": None, "longest_quiet": lease.longest_quiet}
        now = self.system.clock()
        quiet = max(lease.longest_quiet, now - lease.touched) if final else lease.longest_quiet
        return {"held": now - lease.acquired, "longest_quiet": quiet}

    def _reset_runs(self, lease: Lease) -> bool:
        """Whether another banksman process still resets the instance of a held lease."""
        # This process resets nothing while it gives a lease back: its own hook has ended.
        if lease.reaper_pid is None or lease.reaper_pid == os.getpid():
            return False
        pid = lease.reaper_pid
        return self.system.running([pid]).get(pid) == lease.reaper_started

    def _grant(
        self,
        resource: str,
        kind: str,
        holder: Holder,
        state: str,
        timeouts: Timeouts,
        expect: float | None,
    ) -> Lease:
        check_names(resource, kind, holder)
        with self._lock():
            current = self._read(resource)
            if isinstance(current, Unreadable):
                raise Busy(f"{resource} has a lease file that cannot be read")
            if current is not None:
                # Also for the same owner: several agents can work in one worktree, so only the
                # lease id tells a holding apart.
                raise Busy(f"{resource} is {current.state}, held by {current.owner}")
            lease = self._write(self._new_lease(resource, kind, holder, state, timeouts, expect))
            self._log(ACQUIRE, lease)
            return lease

    def _new_lease(
        self,
        resource: str,
        kind: str,
        holder: Holder,
        state: str,
        timeouts: Timeouts,
        expect: float | None,
        holding: str | None = None,
        label: str | None = None,
    ) -> Lease:
        now = self.system.clock()
        wall = self.system.wall_clock()
        lease_id = uuid.uuid4().hex
        return Lease(
            lease_id=lease_id,
            # A lease that is granted alone is a holding of its own.
            holding=lease_id if holding is None else holding,
            resource=resource,
            kind=kind,
            state=state,
            owner=holder.owner,
            owner_pid=holder.owner_pid,
            owner_started=self._start_time(holder.owner_pid),
            issue=holder.issue,
            agent=holder.agent,
            session=holder.session,
            purpose=holder.purpose,
            expected=None if expect is None else now + expect,
            boot_id=self.system.boot_id(),
            acquired_at=wall,
            touched_at=wall,
            touched=now,
            acquired=now,
            boot_deadline=now + timeouts.boot_timeout if state == BOOTING else None,
            hard_deadline=now + timeouts.hard_cap,
            idle_timeout=timeouts.idle_timeout,
            owner_grace=timeouts.owner_grace,
            drain_timeout=timeouts.drain_timeout,
            label=label,
        )

    def _current(self, resource: str, lease_id: str) -> Lease:
        # A script passes the id of the lease that it started under, so a script of an earlier
        # lease, for example one of the same worktree, never acts on a newer lease.
        lease = self._read(resource)
        if isinstance(lease, Unreadable):
            raise NotHeld(f"{resource} has a lease file that cannot be read")
        if lease is None or lease.resource != resource:
            raise NotHeld(f"{resource} has no lease")
        if lease.lease_id != lease_id:
            raise NotHeld(f"{resource} has another lease now")
        return lease

    def _held(self, resource: str, lease_id: str) -> Lease:
        return self._valid(self._current(resource, lease_id))

    def _valid(self, lease: Lease) -> Lease:
        if lease.state not in HELD:
            raise NotHeld(f"the lease on {lease.resource} is {lease.state}")
        reason = self._void_reason(lease)
        if reason is not None:
            raise NotHeld(f"the lease on {lease.resource} is void: {VOID_REASONS[reason]}")
        return lease

    def _holding(self, entries: Sequence[Lease | Unreadable], lease_id: str) -> dict[str, Lease]:
        """Return the valid held leases of the holding of the lease with this id, by resource.

        The lease itself can be in any state: its id names its holding while its file exists.
        """
        named = next(
            (entry for entry in entries if isinstance(entry, Lease) and entry.lease_id == lease_id),
            None,
        )
        return {} if named is None else self._held_leases(entries, named.holding)

    def _labelled(
        self, entries: Sequence[Lease | Unreadable], owner_pid: int, label: str
    ) -> dict[str, Lease]:
        """Return the valid held leases of the holding with this label whose owner process is
        `owner_pid`, by resource."""
        leases = [lease for lease in self._owned(entries, owner_pid) if lease.label == label]
        holdings = sorted({lease.holding for lease in leases})
        if len(holdings) > 1:
            # For example after a restart of the agent, when a touch with a lease id moved an
            # earlier holding with the same label to the new agent process. banksman does not
            # choose one of them for the caller.
            ids = ", ".join(
                next(lease.lease_id for lease in leases if lease.holding == holding)
                for holding in holdings
            )
            raise BanksmanError(
                f"process {owner_pid} has {len(holdings)} holdings with the label {label}, with"
                f" the leases {ids}: give the id of one of them with --lease instead"
            )
        return {lease.resource: lease for lease in leases}

    def _owned(self, entries: Sequence[Lease | Unreadable], owner_pid: int) -> list[Lease]:
        """Return the valid held leases whose owner process is `owner_pid`."""
        started = self._start_time(owner_pid)
        owned = [
            entry
            for entry in entries
            if isinstance(entry, Lease)
            and entry.state in HELD
            and (entry.owner_pid, entry.owner_started) == (owner_pid, started)
        ]
        return self._valid_ones(owned, {owner_pid: started})

    def _held_leases(
        self, entries: Sequence[Lease | Unreadable], holding: str
    ) -> dict[str, Lease]:
        """Return the valid held leases of a holding, by resource."""
        leases = [
            entry
            for entry in entries
            if isinstance(entry, Lease) and entry.holding == holding and entry.state in HELD
        ]
        return {lease.resource: lease for lease in self._valid_ones(leases)}

    def _valid_ones(
        self, leases: Sequence[Lease], known: Mapping[int, str] | None = None
    ) -> list[Lease]:
        """Return the leases that are not void. `known` gives the start times of processes
        that the caller has just read."""
        if not leases:
            return []
        known = {} if known is None else known
        pids = sorted({pid for lease in leases for pid in checked_pids(lease)} - set(known))
        running = {**known, **(self.system.running(pids) if pids else {})}
        boot_id = self.system.boot_id()
        now = self.system.clock()
        return [
            lease
            for lease in leases
            if void_reason(lease, boot_id=boot_id, now=now, running=running) is None
        ]

    def _touch_holding(
        self, lease: Lease, owner_pid: int | None, expect: float | None = None
    ) -> list[Lease]:
        # The leases that one acquire granted form a holding, and the holder uses them together.
        # A script that checks one of them keeps the others too, so that a tablet does not time
        # out while a long test runs on the phone of the same holding.
        others = self._held_leases(self._scan(), lease.holding)
        others.pop(lease.resource, None)
        touched = []
        for each in (lease, *others.values()):
            each = self._touched(each, owner_pid)
            if expect is not None:
                each = replace(each, expected=each.touched + expect)
            touched.append(self._write(each))
        return touched

    def _touch_held(self, leases: Sequence[Lease]) -> list[Lease]:
        """Touch held leases and their holdings, each holding once. Return the leases as they
        are now."""
        touched: dict[str, Lease] = {}
        for lease in leases:
            if lease.resource not in touched:
                touched.update((each.resource, each) for each in self._touch_holding(lease, None))
        return [touched[lease.resource] for lease in leases]

    def _touched(self, lease: Lease, owner_pid: int | None) -> Lease:
        now = self.system.clock()
        touched = replace(
            lease,
            touched=now,
            touched_at=self.system.wall_clock(),
            longest_quiet=max(lease.longest_quiet, now - lease.touched),
        )
        if owner_pid is None or (owner_pid != lease.owner_pid and self._owner_started_me(lease)):
            return touched
        # A restarted agent runs under a new pid. Recording it keeps the owner rule true.
        return replace(touched, owner_pid=owner_pid, owner_started=self._start_time(owner_pid))

    def _owner_started_me(self, lease: Lease) -> bool:
        """Whether the owner process of the lease runs and started this process: it is an
        ancestor, or this process has the lease id that `banksman run` gives its command, of a
        lease of the same holding.

        The caller then works within the life of that owner, for example a script that banksman
        run runs with the lease that it took for its command, so the lease keeps its owner. A
        script that has left the process tree of the run still has the lease id, and its agent
        process can come from the pid variable of the agent.
        """
        if lease.owner_pid is None:
            return False
        table = self.system.process_table()
        owner = table.get(lease.owner_pid)
        if owner is None or owner.started != lease.owner_started:
            return False
        return (
            lease.owner_pid in fencing.ancestors(table, os.getpid())
            or self._given_holding() == lease.holding
        )

    def _given_holding(self) -> str | None:
        token = os.environ.get(LEASE_VARIABLE)
        if not token:
            return None
        return next(
            (
                entry.holding
                for entry in self._scan()
                if isinstance(entry, Lease) and entry.lease_id == token
            ),
            None,
        )

    def _start_time(self, pid: int | None) -> str | None:
        if pid is None:
            return None
        started = self.system.running([pid]).get(pid) if pid > 0 else None
        if started is None:
            raise BanksmanError(f"process {pid} is not running")
        return started

    def _void_reason(self, lease: Lease) -> str | None:
        pids = checked_pids(lease)
        return void_reason(
            lease,
            boot_id=self.system.boot_id(),
            now=self.system.clock(),
            running=self.system.running(pids) if pids else {},
        )

    @staticmethod
    def _me(running: dict[int, str]) -> tuple[int, str]:
        started = running.get(os.getpid())
        if started is None:
            raise StoreError("cannot read the start time of this banksman process")
        return os.getpid(), started

    def _path(self, resource: str) -> Path:
        check_resource(resource)
        return self.directory / f"{resource}{_LEASE_SUFFIX}"

    def _read(self, resource: str) -> Lease | Unreadable | None:
        path = self._path(resource)
        try:
            return _load(path)
        except FileNotFoundError:
            pass
        # A cleaner of temporary files can delete a quarantined lease after this command has
        # restored the missing ones. Its record still keeps the resource out of use.
        try:
            return _load(self.quarantine_dir / path.name)
        except FileNotFoundError:
            return None

    def _scan(self) -> list[Lease | Unreadable]:
        return _scan(self.directory)

    def _write(self, lease: Lease) -> Lease:
        name = self._path(lease.resource).name
        data = (json.dumps(lease.to_json(), indent=2) + "\n").encode()
        if lease.state == QUARANTINED:
            # A quarantine lasts until a person releases it, but cleaners of temporary files
            # delete old files in the state directory, and macOS empties /tmp at boot. So a
            # quarantined lease is also kept outside it, and every command first restores a
            # lease file that has gone.
            self._ensure_quarantine_dir()
            try:
                _replace_file(self.quarantine_dir, name, data)
            except OSError as exc:
                raise StoreError(
                    f"cannot write {self.quarantine_dir / name}: {exc.strerror}. {OUTSIDE_SANDBOX}"
                ) from exc
        _replace_file(self.directory, name, data)
        return lease

    def _delete(self, resource: str, lease: Lease | None, how: str) -> None:
        """Delete the lease on a resource, which ended `how`. `lease` is None for a lease file
        that cannot be read."""
        name = self._path(resource).name
        # The end first: a command that ends between the two steps leaves an end without its
        # deletion, which costs a request one more discovery, never a deletion that a request
        # does not see.
        self._record_end(resource, lease, how)
        # The record outside the state directory first: a command that ends between the two
        # steps leaves a lease file without its record, never a released quarantine that the
        # next command restores.
        for directory in (self.quarantine_dir, self.directory):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(directory / name)

    def _record_end(self, resource: str, lease: Lease | None, how: str) -> None:
        ends = self._ends()
        boot_id = self.system.boot_id()
        hook = (
            how != FORCE_RELEASE
            and lease is not None
            and _taken_back(lease)
            and self._hooked(lease)
        )
        before = ends[resource].last_use if resource in ends else None
        if lease is None:
            # Nobody knows who held a lease file that cannot be read.
            last_use = None
        elif _used(lease):
            last_use = LastUse(
                lease.owner,
                boot_id,
                self.system.wall_clock(),
                how,
                hook,
                lease.void_reason if how == VOID else None,
                lease.label,
                lease.issue,
                lease.handle,
            )
        elif before is not None and (hook or how == FORCE_RELEASE):
            # No holder could use the instance under this lease, so the holder before it stays
            # the last one. But its end can have changed the instance.
            last_use = replace(before, hook=before.hook or hook, how=_forced(how, before.how))
        else:
            last_use = before
        ends[resource] = Ended(boot_id, self.system.clock(), last_use)
        record = {
            "schema": ENDS_SCHEMA,
            "resources": {name: end.to_json() for name, end in sorted(ends.items())},
        }
        try:
            _ensure_parent(self.ended_path)
            _replace_file(
                self.ended_path.parent,
                self.ended_path.name,
                (json.dumps(record, indent=2) + "\n").encode(),
            )
        except (StoreError, OSError) as exc:
            # The lease ends all the same. Without the record, the next request can use facts
            # from before this end, and gets no preference for the instance that it used last. The
            # record goes, so that it does not name the holder before this lease as the last one.
            with contextlib.suppress(OSError):
                os.unlink(self.ended_path)
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            self._warn(f"the end of the lease on {resource} is not recorded: {reason}")

    def _ends(self) -> dict[str, Ended]:
        """Return the last end of a lease on each resource, also of an earlier boot.

        The record holds one entry for each resource, so it does not grow with the number of
        leases.
        """
        try:
            data = json.loads(_read_file(self.ended_path))
        except (FileNotFoundError, ValueError, StoreError):
            # Also a file that others can write to: it is not used, and the next end replaces it
            # with a file of this user.
            return {}
        if (
            not isinstance(data, dict)
            or data.get("schema") != ENDS_SCHEMA
            or not isinstance(data.get("resources"), dict)
        ):
            return {}
        ends = {}
        for resource, entry in data["resources"].items():
            end = Ended.from_json(entry)
            if end is not None and _matches(RESOURCE_NAME, resource):
                ends[resource] = end
        return ends

    @contextmanager
    def _lock(self, *, clean: bool = True) -> Iterator[None]:
        # flock belongs to an open file, so a second lock in this process would wait for
        # the first one.
        if self._locked:
            raise RuntimeError("the store lock is not re-entrant")
        _ensure_directory(self.directory)
        fd = _take_lock(self.directory / _LOCK_FILE, self._lock_wait)
        self._locked = True
        try:
            if clean:
                _remove_temp_files(self.directory)
                self._restore_quarantines()
            yield
        finally:
            self._locked = False
            os.close(fd)

    def _ensure_quarantine_dir(self) -> None:
        try:
            self.quarantine_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.quarantine_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise StoreError(
                f"cannot create {self.quarantine_dir}: {exc.strerror}. {OUTSIDE_SANDBOX}"
            ) from exc
        _check_directory(self.quarantine_dir)

    def _restore_quarantines(self) -> None:
        if not os.path.lexists(self.quarantine_dir):
            return
        _check_directory(self.quarantine_dir)
        _remove_temp_files(self.quarantine_dir)
        for name in os.listdir(self.quarantine_dir):
            if name.startswith(".") or not name.endswith(_LEASE_SUFFIX):
                continue
            if os.path.lexists(self.directory / name):
                continue
            with contextlib.suppress(FileNotFoundError):
                _replace_file(self.directory, name, _read_file(self.quarantine_dir / name))


def _part(
    need: Need, held: set[str], taken: set[str], kept: dict[str, Lease], paid: bool
) -> Part:
    """Return the options of a need that the caller can get now: its own kept leases first,
    then the resources that have no lease. A paid resource without a kept lease needs `paid`."""
    options = []
    for choice in need.choices:
        current = kept.get(choice.resource)
        if current is None:
            continue
        # The accounts of the kept lease first; it can also get free accounts on its resource.
        own = [account for account in choice.accounts if account in current.accounts]
        free = [account for account in choice.accounts if account not in taken]
        options.append(Option(choice.resource, (*own, *free)))
    options.extend(
        Option(
            choice.resource,
            tuple(account for account in choice.accounts if account not in taken),
        )
        for choice in need.choices
        if choice.resource not in held and not choice.in_use and (paid or not choice.paid)
    )
    return Part(tuple(options), need.accounts)


def _trace_grant(
    needs: Sequence[Need],
    entries: Sequence[Lease | Unreadable],
    ended: Collection[str],
    taken: Collection[str],
    kept: Mapping[str, Lease],
    paid: bool,
    outcome: str,
    granted: Sequence[str] = (),
) -> None:
    """Record in the trace the resources that matched each part of a request, and why the
    request could not get each one, as `_part` decides."""
    if not trace.active():
        return
    found = {entry.resource: entry for entry in entries}
    parts = [
        [_option(choice, need.accounts, found, ended, taken, kept, paid) for choice in need.choices]
        for need in needs
    ]
    trace.step("grant", outcome=outcome, granted=list(granted), parts=parts)


def _option(
    choice: Choice,
    accounts: int,
    found: Mapping[str, Lease | Unreadable],
    ended: Collection[str],
    taken: Collection[str],
    kept: Mapping[str, Lease],
    paid: bool,
) -> dict[str, object]:
    entry = found.get(choice.resource)
    current = kept.get(choice.resource)
    free = [
        account
        for account in choice.accounts
        if account not in taken or (current is not None and account in current.accounts)
    ]
    lease = None
    skipped: str | None = None
    if current is not None:
        if current.state == BOOTING:
            skipped = BOOTING
    elif isinstance(entry, Unreadable):
        skipped = "unreadable"
    elif entry is not None:
        skipped, lease = "in_use", entry
    elif choice.resource in ended:
        skipped = "ended"
    elif choice.in_use:
        skipped = "unmanaged"
    elif choice.paid and not paid:
        skipped = "paid"
    if skipped is None and len(free) < accounts:
        skipped = "accounts"
    return {
        "resource": choice.resource,
        "kept": current is not None,
        "skipped": skipped,
        "lease_id": None if lease is None else lease.lease_id,
        "state": None if lease is None else lease.state,
    }


def _used(lease: Lease) -> bool:
    """Whether a holder could use the instance under the lease: it was ready, and it is not the
    reaper's lease of a free instance."""
    # A booting lease has a boot deadline until it is ready, also after it became void. The
    # reaper's lease gets the void reason rebooted when it is taken back after a restart, so its
    # owner tells it apart: the owner of a holder is the path of a worktree.
    return lease.boot_deadline is None and lease.owner != REAPER_OWNER


def _taken_back(lease: Lease) -> bool:
    """Whether the end of a lease took its instance back: ended it, and ran the hook of its kind.

    Only a release without an end gives the instance back as it is.
    """
    return lease.void_reason not in (None, RELEASE)


def _forced(how: str, before: str) -> str:
    return FORCE_RELEASE if how == FORCE_RELEASE else before


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _check_label(label: str, owner_pid: int | None) -> None:
    if not is_label(label):
        raise BanksmanError(
            f"not a valid label: {label!r}. A label starts with a letter or a digit, has only"
            " letters, digits, '.', '_', and '-', and has at most 64 characters"
        )
    if owner_pid is None:
        # Without an owner process, every caller of the user would share the label.
        raise BanksmanError("a label needs an owner process: the agent process, or --owner-pid")


def satisfiable(needs: Sequence[Need]) -> bool:
    """Whether the needs could be met together if no resource and no account were in use."""
    parts = [
        Part(
            tuple(Option(choice.resource, choice.accounts) for choice in need.choices),
            need.accounts,
        )
        for need in needs
    ]
    return assign(parts) is not None


def _see_serial(leases: dict[str, Lease], seen: SerialSeen) -> set[str]:
    """Apply one result of discovery to held leases by resource. Return the resources whose
    lease changed.

    The results of one discovery have the same time, and each can change the leases that
    another one changes, for example when two emulators exchange their ports. So a result of
    the discovery that the lease records is applied too.
    """
    lease = leases.get(seen.resource)
    if lease is not None and (
        lease.kind != seen.kind
        or (lease.serial_seen is not None and seen.at < lease.serial_seen)
    ):
        return set()
    if seen.serial is None:
        if lease is None or lease.state == BOOTING:
            return set()
        leases[lease.resource] = replace(lease, serial=None, serial_seen=seen.at)
        return {lease.resource}
    others = [
        other
        for other in leases.values()
        if other.resource != seen.resource and other.serial == seen.serial
    ]
    # A newer discovery found the serial on another instance, so this result is out of date.
    if any(other.serial_seen is not None and other.serial_seen >= seen.at for other in others):
        return set()
    # Another lease with the serial would make a command of its holder act on this instance,
    # also when this instance has no lease.
    changed = set()
    for other in others:
        leases[other.resource] = replace(other, serial=None, serial_seen=seen.at)
        changed.add(other.resource)
    if lease is not None:
        # A handle stays when discovery does not report one: the reservation can outlive the
        # connection of its device.
        handle = seen.handle if seen.handle is not None else lease.handle
        leases[lease.resource] = replace(
            lease, serial=seen.serial, serial_seen=seen.at, handle=handle
        )
        changed.add(lease.resource)
    return changed


def _drained(lease: Lease, reason: str, now: float) -> Lease:
    # The scripts of an earlier boot have ended, and their pids can name new processes now.
    users = () if reason == REBOOTED else lease.users
    return replace(
        lease,
        state=DRAINING,
        void_reason=reason,
        users=users,
        ended=False,
        attempts=0,
        drain_deadline=now + lease.drain_timeout,
    )


def _restarted(lease: Lease, now: float) -> Lease:
    """Return a draining or quarantined lease of an earlier boot, to take back on this boot."""
    # No process of an earlier boot runs now. A quarantined instance, such as a physical
    # device, can outlive a restart, so it is ended again before it is handed on.
    if lease.state == QUARANTINED:
        return _drained(lease, REBOOTED, now)
    return replace(lease, users=(), drain_deadline=now + lease.drain_timeout)


def _settled(
    lease: Lease, table: fencing.Table, now: float
) -> tuple[str, tuple[User, ...]]:
    """Decide a draining lease whose instance has ended.

    It is freed when its scripts have ended, quarantined when they still run at its drain
    deadline, and otherwise it drains on.
    """
    users = tuple(user for user in lease.users if fencing.runs(user, table))
    if not users:
        return RELEASED, users
    if lease.drain_deadline is None or now >= lease.drain_deadline:
        return QUARANTINED, users
    return DRAINING, users


def _quarantined(lease: Lease) -> Lease:
    return replace(lease, state=QUARANTINED, reaper_pid=None, reaper_started=None)


def _other_reaper_runs(lease: Lease, running: dict[int, str], me: tuple[int, str]) -> bool:
    claim = (lease.reaper_pid, lease.reaper_started)
    if lease.reaper_pid is None or claim == me:
        return False
    return running.get(lease.reaper_pid) == lease.reaper_started


def peek(directory: Path, quarantine_dir: Path) -> Snapshot:
    """Read the lease files without the lock, and change nothing: no reaping, no restore.

    Every write replaces a whole file with a rename, so each file that this reads is whole. But
    the files can come from different moments, so the result is good for a check that refuses or
    allows a command, never for a change to a lease.
    """
    # A quarantined lease whose file a cleaner of temporary files or a restart deleted still
    # keeps its resource out of use, also when the whole state directory has gone. The file in the
    # state directory comes last, so it wins.
    places = [place for place in (quarantine_dir, directory) if os.path.lexists(place)]
    for place in places:
        _check_directory(place)
    entries = {entry.resource: entry for place in places for entry in _scan(place)}
    found = [entries[resource] for resource in sorted(entries)]
    return Snapshot(
        leases=[entry for entry in found if isinstance(entry, Lease)],
        unreadable=[entry for entry in found if isinstance(entry, Unreadable)],
    )


def _scan(directory: Path) -> list[Lease | Unreadable]:
    entries: list[Lease | Unreadable] = []
    for name in sorted(os.listdir(directory)):
        if name.startswith(".") or not name.endswith(_LEASE_SUFFIX):
            continue
        resource = name[: -len(_LEASE_SUFFIX)]
        try:
            entry = _load(directory / name)
        except FileNotFoundError:
            continue
        if isinstance(entry, Lease) and entry.resource != resource:
            entry = Unreadable(resource, "the file name does not match the resource in the file")
        entries.append(entry)
    return entries


def _load(path: Path) -> Lease | Unreadable:
    resource = path.name[: -len(_LEASE_SUFFIX)]
    raw = _read_file(path)
    try:
        data = json.loads(raw)
    except ValueError:
        return Unreadable(resource, "the file is not valid JSON")
    schema = data.get("schema") if isinstance(data, dict) else None
    if isinstance(schema, int) and not isinstance(schema, bool) and schema > LEASE_SCHEMA:
        raise StoreError(
            f"{path} has lease schema {schema}, but this banksman knows only schema "
            f"{LEASE_SCHEMA}. Two banksman versions must not share one state directory."
        )
    try:
        return Lease.from_json(data)
    except LeaseFormatError as exc:
        return Unreadable(resource, str(exc))


def _check_ownership(path: Path, info: os.stat_result) -> None:
    # The reaper can signal the processes that a lease names, so a lease that another user can
    # change could make it signal the owner's processes.
    if info.st_uid != os.geteuid():
        raise StoreError(f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise StoreError(f"group or others can write to {path}")


def _check_directory(path: Path) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StoreError(f"{path} is not a directory")
    _check_ownership(path, info)


def _read_file(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StoreError(f"cannot open {path}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise StoreError(f"{path} is not a regular file")
        _check_ownership(path, info)
        return file.read()


def _replace_file(directory: Path, name: str, data: bytes) -> None:
    import tempfile  # Not at the top: the adb guard imports this module for every adb call.

    fd, temp = tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=directory)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        # rename is atomic: a reader sees the old file or the new one, never a part.
        os.replace(temp, directory / name)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


def _remove_temp_files(directory: Path) -> None:
    # Every write ends while the lock is held, so a temporary file that exists now was left by
    # a process that ended in the middle of a write.
    for name in os.listdir(directory):
        if name.startswith(_TEMP_PREFIX):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(directory / name)


@contextmanager
def log_lock(directory: Path, wait: float) -> Iterator[None]:
    """Hold the lock of the lease store to add to the log, for a process that changes no lease.

    Unlike the lock of a change, it neither cleans up nor restores the quarantines, so that it
    stays short for the adb guard.
    """
    _ensure_directory(directory)
    fd = _take_lock(directory / _LOCK_FILE, wait)
    try:
        yield
    finally:
        os.close(fd)


def _take_lock(path: Path, wait: float) -> int:
    give_up = time.monotonic() + wait
    pause = 0.002
    while True:
        fd = _open_lock_file(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A lock on a file that was deleted meanwhile, for example by a cleaner of
            # temporary files, excludes nobody.
            if _same_file(fd, path):
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n".encode())
                return fd
        except BlockingIOError:
            pass
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        # The kernel frees the lock when its holder ends, but not while the holder is
        # stopped (for example with Ctrl-Z) or hung.
        if time.monotonic() >= give_up:
            raise LockTimeout(
                f"another banksman process (pid {_lock_holder(path)}) has held {path} "
                f"for more than {wait:g} s; it can be stopped or hung"
            )
        time.sleep(pause)
        pause = min(pause * 2, 0.05)


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise StoreError(f"cannot create {path.parent}: {exc.strerror}") from exc
    _check_directory(path.parent)


def _ensure_directory(directory: Path) -> None:
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise StoreError(f"cannot create the state directory {directory}: {exc.strerror}") from exc
    _check_directory(directory)


def _open_lock_file(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise StoreError(f"cannot open {path}: {exc.strerror}") from exc
    try:
        _check_ownership(path, os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _same_file(fd: int, path: Path) -> bool:
    try:
        on_disk = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (on_disk.st_dev, on_disk.st_ino) == (held.st_dev, held.st_ino)


def _lock_holder(path: Path) -> str:
    try:
        content = path.read_text().strip()
    except OSError:
        return "unknown"
    return content if content.isdigit() else "unknown"
