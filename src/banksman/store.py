"""The lease store: one lease file per resource, changed only under one file lock.

The lock is held for milliseconds, never while a resource is in use.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pwd
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from banksman import SCHEMA_VERSION, fencing
from banksman.errors import BanksmanError
from banksman.lease import (
    BOOTING,
    DRAINING,
    HELD,
    QUARANTINED,
    READY,
    REBOOTED,
    RELEASE,
    VOID_REASONS,
    Holder,
    Lease,
    LeaseFormatError,
    Timeouts,
    User,
    check_names,
    check_resource,
    void_reason,
)
from banksman.system import OUTSIDE_SANDBOX, System

STATE_DIR_ENV = "BANKSMAN_STATE_DIR"
QUARANTINE_DIR_ENV = "BANKSMAN_QUARANTINE_DIR"
LOCK_WAIT_SECONDS = 10.0
RELEASED = "released"
# A take-back that this many reapers started and none finished is quarantined.
TAKE_BACK_ATTEMPTS = 3

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
class Reaped:
    lease: Lease
    outcome: str  # RELEASED, DRAINING, or QUARANTINED
    # The scripts that still ran: the lease waits for them, or is quarantined because of them.
    running: tuple[User, ...] = ()
    # Why the lease is quarantined, when the reaper itself found the reason.
    problem: str | None = None


# Ends the instance of a void lease, for example with the on_void hook of its kind. Returns
# False when that cannot be confirmed; the lease is then quarantined. Only one reaper at a time
# runs it for a lease, but a reaper that ended in the middle leaves the lease to the next one,
# so it must be safe to run again.
TakeBack = Callable[[Lease], bool]
# Whether the reaper may signal the scripts of a void lease.
Stopping = Callable[[Lease], bool]


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
    # boot, but a quarantine lasts until a person releases it. The home directory comes from
    # the user database, as for the configuration file, so that every caller of one user finds
    # the same quarantines.
    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise StoreError(
            f"cannot find the home directory of user {os.geteuid()}; set {QUARANTINE_DIR_ENV}"
        ) from exc
    return Path(home) / ".local" / "state" / "banksman" / "quarantine"


def _take_back_nothing(lease: Lease) -> bool:
    return True


def _stop_nothing(lease: Lease) -> bool:
    return False


class Store:
    def __init__(
        self,
        directory: Path,
        system: System,
        *,
        take_back: TakeBack = _take_back_nothing,
        stopping: Stopping = _stop_nothing,
        quarantine_dir: Path | None = None,
        lock_wait: float = LOCK_WAIT_SECONDS,
    ) -> None:
        self.directory = Path(directory)
        self.quarantine_dir = (
            default_quarantine_dir() if quarantine_dir is None else Path(quarantine_dir)
        )
        self.system = system
        self._take_back = take_back
        self._stopping = stopping
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

    def ready(self, resource: str, lease_id: str) -> Lease:
        with self._lock():
            lease = self._held(resource, lease_id)
            if lease.state == READY:
                return lease
            return self._write(replace(self._touched(lease, None), state=READY, boot_deadline=None))

    def touch(
        self,
        resource: str,
        owner: str,
        owner_pid: int | None = None,
        *,
        expect: float | None = None,
    ) -> Lease:
        with self._lock():
            lease = self._touched(self._owned(resource, owner), owner_pid)
            if expect is not None:
                lease = replace(lease, expected=self.system.clock() + expect)
            return self._write(lease)

    def enter(self, resource: str, lease_id: str, pid: int, pgid: int | None = None) -> Lease:
        """Check the lease, touch it, and register a script as a user of the resource."""
        with self._lock():
            lease = self._held(resource, lease_id)
            table = self.system.process_table()
            user = fencing.register(table, lease, pid, pgid)
            # A new record replaces the one of the same process and group, and the records of
            # scripts that have ended go. Another group of the same process can still run.
            users = [
                other
                for other in lease.users
                if (other.pid, other.started, other.pgid) != (user.pid, user.started, user.pgid)
                and fencing.runs(other, table)
            ]
            return self._write(replace(self._touched(lease, None), users=(*users, user)))

    def check(self, resource: str, lease_id: str) -> Lease:
        """Touch the lease while the caller may still use the resource, or raise NotHeld."""
        with self._lock():
            return self._write(self._touched(self._held(resource, lease_id), None))

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

    def release(self, resource: str, owner: str) -> Lease | None:
        """Give the resource back. Return the lease while it drains, or None when it is free."""
        with self._lock():
            lease = self._read(resource)
            if (
                not isinstance(lease, Lease)
                or lease.resource != resource
                or lease.owner != owner
                or lease.state not in HELD
            ):
                raise NotHeld(f"{resource} is not held by this owner")
            running = self._running_users(lease)
            if not running:
                self._delete(resource)
                return None
            # The scripts must end before the resource is handed on, as for a void lease. The
            # holder gives the instance back as it is, so no hook ends it.
            drained = _drained(replace(lease, users=running), RELEASE, self.system.clock())
            return self._write(drained)

    def force_release(self, resource: str) -> tuple[Lease | Unreadable, tuple[User, ...]]:
        """Remove the lease on a resource in any state, without a hook and without a signal.

        Return what was removed, and the registered scripts that still run.
        """
        with self._lock():
            current = self._read(resource)
            if current is None:
                raise BanksmanError(f"{resource} has no lease")
            # A take-back that still runs could end the instance of the next holder.
            if (
                isinstance(current, Lease)
                and current.reaper_pid is not None
                and current.boot_id == self.system.boot_id()
                and self.system.running([current.reaper_pid]).get(current.reaper_pid)
                == current.reaper_started
            ):
                raise BanksmanError(
                    f"banksman process {current.reaper_pid} takes {resource} back now; run this"
                    " command again when that process has ended"
                )
            running = self._running_users(current) if isinstance(current, Lease) else ()
            self._delete(resource)
            return current, running

    def snapshot(self) -> Snapshot:
        with self._lock():
            entries = self._scan()
        return Snapshot(
            leases=[entry for entry in entries if isinstance(entry, Lease)],
            unreadable=[entry for entry in entries if isinstance(entry, Unreadable)],
        )

    def reap(self) -> list[Reaped]:
        """Take back the resources of void leases, and delete those leases."""
        reaped = []
        with self._lock():
            leases = [entry for entry in self._scan() if isinstance(entry, Lease)]
            if not leases:
                return []
            boot_id = self.system.boot_id()
            now = self.system.clock()
            table = self.system.process_table()
            running = {pid: process.started for pid, process in table.items()}
            me = self._me(running)
            mine = []
            for found in leases:
                lease = found
                if lease.state in HELD:
                    reason = void_reason(lease, boot_id=boot_id, now=now, running=running)
                    if reason is None:
                        continue
                    lease = _drained(lease, reason, now)
                elif lease.boot_id != boot_id:
                    lease = _restarted(lease, now)
                elif lease.state != DRAINING or _other_reaper_runs(lease, running, me):
                    continue
                # The lease records this boot from now on: with the boot id of an earlier boot,
                # other reapers would read a claim as the claim of a reaper from that boot, and
                # take the lease over at once. A quarantine then also belongs to this boot, so
                # that later commands do not run a failed take-back again.
                lease = replace(lease, boot_id=boot_id)
                if not lease.ended and lease.attempts >= TAKE_BACK_ATTEMPTS:
                    # Every reaper so far ended before it finished, for example because its
                    # caller stopped it. A take-back in every command would block every command.
                    problem = f"{TAKE_BACK_ATTEMPTS} take-backs started and none finished"
                    quarantined = self._write(_quarantined(lease))
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
        for lease in mine:
            reaped.extend(self._end(lease, me))
        return reaped

    def _end(self, lease: Lease, me: tuple[int, str]) -> list[Reaped]:
        # Taking a resource back can take seconds, for example to end an emulator or to wait
        # for processes to end, so it runs outside the lock. No ownership check passes a
        # draining lease.
        freed = lease.void_reason == RELEASE or self._take_back(lease)
        if lease.users and self._stopping(lease):
            # A script that left meanwhile no longer uses the resource, so it is not stopped.
            fencing.stop(replace(lease, users=self._users_now(lease)), self.system)
        with self._lock():
            current = self._read(lease.resource)
            if (
                not isinstance(current, Lease)
                or current.lease_id != lease.lease_id
                or current.state != DRAINING
                or (current.reaper_pid, current.reaper_started) != me
            ):
                return []
            if not freed:
                return [Reaped(self._write(_quarantined(current)), QUARANTINED)]
            ended = replace(current, ended=True, reaper_pid=None, reaper_started=None)
            table = self.system.process_table() if ended.users else {}
            outcome, users = _settled(ended, table, self.system.clock())
            if outcome == DRAINING:
                return [Reaped(self._write(replace(ended, users=users)), DRAINING, users)]
            return [self._finish(ended, outcome, users)]

    def _finish(self, lease: Lease, outcome: str, users: tuple[User, ...]) -> Reaped:
        if outcome == RELEASED:
            self._delete(lease.resource)
            return Reaped(lease, RELEASED)
        return Reaped(self._write(_quarantined(replace(lease, users=users))), QUARANTINED, users)

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
                if (
                    current.resource == resource
                    and current.owner == holder.owner
                    and current.state in HELD
                    and self._void_reason(current) is None
                ):
                    return self._write(self._touched(current, holder.owner_pid))
                raise Busy(f"{resource} is {current.state}, held by {current.owner}")
            now = self.system.clock()
            wall = self.system.wall_clock()
            return self._write(
                Lease(
                    lease_id=uuid.uuid4().hex,
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
                    boot_deadline=now + timeouts.boot_timeout if state == BOOTING else None,
                    hard_deadline=now + timeouts.hard_cap,
                    idle_timeout=timeouts.idle_timeout,
                    owner_grace=timeouts.owner_grace,
                    drain_timeout=timeouts.drain_timeout,
                )
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

    def _owned(self, resource: str, owner: str) -> Lease:
        lease = self._read(resource)
        if isinstance(lease, Unreadable):
            raise NotHeld(f"{resource} has a lease file that cannot be read")
        if lease is None or lease.resource != resource:
            raise NotHeld(f"{resource} has no lease")
        if lease.owner != owner:
            raise NotHeld(f"{resource} is held by another owner")
        return self._valid(lease)

    def _valid(self, lease: Lease) -> Lease:
        if lease.state not in HELD:
            raise NotHeld(f"the lease on {lease.resource} is {lease.state}")
        reason = self._void_reason(lease)
        if reason is not None:
            raise NotHeld(f"the lease on {lease.resource} is void: {VOID_REASONS[reason]}")
        return lease

    def _touched(self, lease: Lease, owner_pid: int | None) -> Lease:
        touched = replace(lease, touched=self.system.clock(), touched_at=self.system.wall_clock())
        if owner_pid is None:
            return touched
        # A restarted agent runs under a new pid. Recording it keeps the owner rule true.
        return replace(touched, owner_pid=owner_pid, owner_started=self._start_time(owner_pid))

    def _start_time(self, pid: int | None) -> str | None:
        if pid is None:
            return None
        started = self.system.running([pid]).get(pid) if pid > 0 else None
        if started is None:
            raise BanksmanError(f"process {pid} is not running")
        return started

    def _void_reason(self, lease: Lease) -> str | None:
        pids = [lease.owner_pid] if lease.owner_pid is not None else []
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
            return self._load(path)
        except FileNotFoundError:
            pass
        # A cleaner of temporary files can delete a quarantined lease after this command has
        # restored the missing ones. Its record still keeps the resource out of use.
        try:
            return self._load(self.quarantine_dir / path.name)
        except FileNotFoundError:
            return None

    def _scan(self) -> list[Lease | Unreadable]:
        entries: list[Lease | Unreadable] = []
        for name in sorted(os.listdir(self.directory)):
            if name.startswith(".") or not name.endswith(_LEASE_SUFFIX):
                continue
            resource = name[: -len(_LEASE_SUFFIX)]
            try:
                entry = self._load(self.directory / name)
            except FileNotFoundError:
                continue
            if isinstance(entry, Lease) and entry.resource != resource:
                entry = Unreadable(resource, "the file name does not match the resource in the file")
            entries.append(entry)
        return entries

    def _load(self, path: Path) -> Lease | Unreadable:
        resource = path.name[: -len(_LEASE_SUFFIX)]
        raw = _read_file(path)
        try:
            data = json.loads(raw)
        except ValueError:
            return Unreadable(resource, "the file is not valid JSON")
        schema = data.get("schema") if isinstance(data, dict) else None
        if isinstance(schema, int) and not isinstance(schema, bool) and schema > SCHEMA_VERSION:
            raise StoreError(
                f"{path} has lease schema {schema}, but this banksman knows only schema "
                f"{SCHEMA_VERSION}. Two banksman versions must not share one state directory."
            )
        try:
            return Lease.from_json(data)
        except LeaseFormatError as exc:
            return Unreadable(resource, str(exc))

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

    def _delete(self, resource: str) -> None:
        name = self._path(resource).name
        # The record outside the state directory first: a command that ends between the two
        # steps leaves a lease file without its record, never a released quarantine that the
        # next command restores.
        for directory in (self.quarantine_dir, self.directory):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(directory / name)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        # flock belongs to an open file, so a second lock in this process would wait for
        # the first one.
        if self._locked:
            raise RuntimeError("the store lock is not re-entrant")
        self._ensure_directory()
        fd = self._take_lock()
        self._locked = True
        try:
            _remove_temp_files(self.directory)
            self._restore_quarantines()
            yield
        finally:
            self._locked = False
            os.close(fd)

    def _take_lock(self) -> int:
        path = self.directory / _LOCK_FILE
        give_up = time.monotonic() + self._lock_wait
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
                    f"for more than {self._lock_wait:g} s; it can be stopped or hung"
                )
            time.sleep(pause)
            pause = min(pause * 2, 0.05)

    def _ensure_directory(self) -> None:
        try:
            os.mkdir(self.directory, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise StoreError(
                f"cannot create the state directory {self.directory}: {exc.strerror}"
            ) from exc
        _check_directory(self.directory)

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
