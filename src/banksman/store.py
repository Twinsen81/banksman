"""The lease store: one lease file per resource, changed only under one file lock.

The lock is held for milliseconds, never while a resource is in use.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from banksman import SCHEMA_VERSION
from banksman.errors import BanksmanError
from banksman.lease import (
    BOOTING,
    DRAINING,
    HELD,
    QUARANTINED,
    READY,
    REBOOTED,
    VOID_REASONS,
    Lease,
    LeaseFormatError,
    Timeouts,
    check_names,
    void_reason,
)
from banksman.system import System

STATE_DIR_ENV = "BANKSMAN_STATE_DIR"
LOCK_WAIT_SECONDS = 10.0
RELEASED = "released"

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
    outcome: str  # RELEASED or QUARANTINED


# Frees the resource of a void lease. Returns False when that cannot be confirmed; the lease
# is then quarantined. It must be safe to run twice for one lease: two reapers can take the
# same lease back at the same time, and a reaper that died leaves its lease to the next one.
TakeBack = Callable[[Lease], bool]


def default_state_dir() -> Path:
    # A fixed path, not one below $TMPDIR: the agents, sandboxes, and scheduled jobs of one
    # user can each have a different $TMPDIR, and callers that use different directories do
    # not exclude each other.
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path(f"/tmp/banksman-{os.geteuid()}")


def _take_back_nothing(lease: Lease) -> bool:
    return True


class Store:
    def __init__(
        self,
        directory: Path,
        system: System,
        *,
        timeouts: Timeouts = Timeouts(),
        take_back: TakeBack = _take_back_nothing,
        lock_wait: float = LOCK_WAIT_SECONDS,
    ) -> None:
        self.directory = Path(directory)
        self.system = system
        self.timeouts = timeouts
        self._take_back = take_back
        self._lock_wait = lock_wait
        self._locked = False

    def reserve(self, resource: str, kind: str, owner: str, owner_pid: int | None = None) -> Lease:
        """Write a `booting` lease, before the resource is started."""
        return self._grant(resource, kind, owner, owner_pid, BOOTING)

    def acquire(self, resource: str, kind: str, owner: str, owner_pid: int | None = None) -> Lease:
        """Write a `ready` lease, for a resource that needs no start."""
        return self._grant(resource, kind, owner, owner_pid, READY)

    def ready(self, resource: str, owner: str) -> Lease:
        with self._lock():
            lease = self._held(resource, owner)
            if lease.state == READY:
                return lease
            return self._write(replace(self._touched(lease, None), state=READY, boot_deadline=None))

    def touch(self, resource: str, owner: str, owner_pid: int | None = None) -> Lease:
        with self._lock():
            return self._write(self._touched(self._held(resource, owner), owner_pid))

    def release(self, resource: str, owner: str) -> None:
        with self._lock():
            lease = self._read(resource)
            if (
                not isinstance(lease, Lease)
                or lease.resource != resource
                or lease.owner != owner
                or lease.state not in HELD
            ):
                raise NotHeld(f"{resource} is not held by this owner")
            self._delete(resource)

    def snapshot(self) -> Snapshot:
        with self._lock():
            entries = self._scan()
        return Snapshot(
            leases=[entry for entry in entries if isinstance(entry, Lease)],
            unreadable=[entry for entry in entries if isinstance(entry, Unreadable)],
        )

    def reap(self) -> list[Reaped]:
        """Take back the resources of void leases, and delete those leases."""
        with self._lock():
            boot_id = self.system.boot_id()
            now = self.system.clock()
            leases = [entry for entry in self._scan() if isinstance(entry, Lease)]
            running = self._running_owners(leases, boot_id)
            draining = []
            for lease in leases:
                reason = self._reap_reason(lease, boot_id, now, running)
                if reason is not None:
                    lease = self._write(replace(lease, state=DRAINING, void_reason=reason))
                if lease.state == DRAINING:
                    draining.append(lease)
        reaped = []
        for lease in draining:
            # Taking a resource back can take seconds, for example to wait for processes to
            # end, so it runs outside the lock. No ownership check passes a draining lease.
            freed = self._take_back(lease)
            with self._lock():
                current = self._read(lease.resource)
                # Another reaper can have finished this lease, and the resource can have a
                # new lease already.
                if (
                    not isinstance(current, Lease)
                    or current.lease_id != lease.lease_id
                    or current.state != DRAINING
                ):
                    continue
                if freed:
                    self._delete(lease.resource)
                    reaped.append(Reaped(current, RELEASED))
                else:
                    quarantined = self._write(replace(current, state=QUARANTINED))
                    reaped.append(Reaped(quarantined, QUARANTINED))
        return reaped

    def _grant(
        self, resource: str, kind: str, owner: str, owner_pid: int | None, state: str
    ) -> Lease:
        check_names(resource, kind, owner)
        with self._lock():
            current = self._read(resource)
            if isinstance(current, Unreadable):
                raise Busy(f"{resource} has a lease file that cannot be read")
            if current is not None:
                if (
                    current.resource == resource
                    and current.owner == owner
                    and current.state in HELD
                    and self._void_reason(current) is None
                ):
                    return self._write(self._touched(current, owner_pid))
                raise Busy(f"{resource} is {current.state}, held by {current.owner}")
            now = self.system.clock()
            wall = self.system.wall_clock()
            return self._write(
                Lease(
                    lease_id=uuid.uuid4().hex,
                    resource=resource,
                    kind=kind,
                    state=state,
                    owner=owner,
                    owner_pid=owner_pid,
                    owner_started=self._start_time(owner_pid),
                    boot_id=self.system.boot_id(),
                    acquired_at=wall,
                    touched_at=wall,
                    touched=now,
                    boot_deadline=now + self.timeouts.boot if state == BOOTING else None,
                    hard_deadline=now + self.timeouts.hard_cap,
                    idle_timeout=self.timeouts.idle,
                    owner_grace=self.timeouts.owner_grace,
                )
            )

    def _held(self, resource: str, owner: str) -> Lease:
        lease = self._read(resource)
        if isinstance(lease, Unreadable):
            raise NotHeld(f"{resource} has a lease file that cannot be read")
        if lease is None or lease.resource != resource:
            raise NotHeld(f"{resource} has no lease")
        if lease.owner != owner:
            raise NotHeld(f"{resource} is held by another owner")
        if lease.state not in HELD:
            raise NotHeld(f"the lease on {resource} is {lease.state}")
        reason = self._void_reason(lease)
        if reason is not None:
            raise NotHeld(f"the lease on {resource} is void: {VOID_REASONS[reason]}")
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

    def _running_owners(self, leases: list[Lease], boot_id: str) -> dict[int, str]:
        # A pid from an earlier boot names a different process, if it names one at all.
        pids = [
            lease.owner_pid
            for lease in leases
            if lease.owner_pid is not None and lease.boot_id == boot_id and lease.state in HELD
        ]
        return self.system.running(pids) if pids else {}

    @staticmethod
    def _reap_reason(
        lease: Lease, boot_id: str, now: float, running: dict[int, str]
    ) -> str | None:
        if lease.state in HELD:
            return void_reason(lease, boot_id=boot_id, now=now, running=running)
        # No process of an earlier boot runs now, so a quarantined resource is safe again.
        if lease.state == QUARANTINED and lease.boot_id != boot_id:
            return REBOOTED
        return None

    def _path(self, resource: str) -> Path:
        return self.directory / f"{resource}{_LEASE_SUFFIX}"

    def _read(self, resource: str) -> Lease | Unreadable | None:
        try:
            return self._load(self._path(resource))
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
            raw = file.read()
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
        fd, temp = tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(lease.to_json(), file, indent=2)
                file.write("\n")
            # rename is atomic: a reader sees the old file or the new one, never a part.
            os.replace(temp, self._path(lease.resource))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
            raise
        return lease

    def _delete(self, resource: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._path(resource))

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
            self._remove_temp_files()
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
        info = os.lstat(self.directory)
        if not stat.S_ISDIR(info.st_mode):
            raise StoreError(f"{self.directory} is not a directory")
        _check_ownership(self.directory, info)

    def _remove_temp_files(self) -> None:
        # Every write ends while the lock is held, so a temporary file that exists now was
        # left by a process that ended in the middle of a write.
        for name in os.listdir(self.directory):
            if name.startswith(_TEMP_PREFIX):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.directory / name)


def _check_ownership(path: Path, info: os.stat_result) -> None:
    # The reaper will signal the processes that a lease names, so a lease that another user
    # can change could make it signal the owner's processes.
    if info.st_uid != os.geteuid():
        raise StoreError(f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise StoreError(f"group or others can write to {path}")


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

