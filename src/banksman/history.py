"""The log: an append-only history of what happened to the leases.

It answers who held a resource at a given time, and it gives the data for tuning the idle
timeout and the hard cap. The file has one JSON object for each event on each line. It is kept
outside /tmp, because the history must outlive restarts and cleaners of temporary files.

Every command that runs as the user can change the file, so it is an aid for a person, not
evidence. Each event is checked when it is read, as a lease file is, because other agents read
the log.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from banksman import SCHEMA_VERSION
from banksman.errors import BanksmanError
from banksman.lease import (
    ISSUE,
    KIND_NAME,
    PROGRAM_NAME,
    RESOURCE_NAME,
    SESSION,
    STATES,
    VOID_REASONS,
    Lease,
    User,
    is_owner,
    is_purpose,
    is_wall_time,
)

ACQUIRE = "acquire"
RELEASE = "release"
VOID = "void"
REAP = "reap"
QUARANTINE = "quarantine"
FORCE_RELEASE = "force_release"
EVENTS = (ACQUIRE, RELEASE, VOID, REAP, QUARANTINE, FORCE_RELEASE)
# The file starts again at this size, and the earlier file is kept next to it, so that the log
# never fills the disk.
MAX_LOG_BYTES = 10 * 1024 * 1024
MAX_PROBLEM_LENGTH = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class LogError(BanksmanError):
    """The log file cannot be used safely."""


@dataclass(frozen=True)
class Process:
    """A registered script that still ran."""

    pid: int
    pgid: int | None = None


@dataclass(frozen=True)
class Event:
    at: float
    event: str
    resource: str
    kind: str | None = None
    lease_id: str | None = None
    holding: str | None = None
    # The state of the lease before a forced release.
    state: str | None = None
    owner: str | None = None
    owner_pid: int | None = None
    agent: str | None = None
    issue: str | None = None
    session: str | None = None
    # Text that one agent writes and other agents read. It is untrusted.
    purpose: str | None = None
    accounts: tuple[str, ...] = ()
    # Why the lease became void, or, for a reap, why it was taken back.
    reason: str | None = None
    # Seconds of awake time from the grant to the release or the void.
    held: float | None = None
    longest_quiet: float | None = None
    # For a release: whether scripts still ran, so that the lease drains first.
    drained: bool | None = None
    running: tuple[Process, ...] = ()
    # For a quarantine because of scripts that still ran: whether stopping was on for the kind.
    stopping: bool | None = None
    problem: str | None = None

    @classmethod
    def of(cls, event: str, lease: Lease, at: float, **details: Any) -> Event:
        return cls(
            at=at,
            event=event,
            resource=lease.resource,
            kind=lease.kind,
            lease_id=lease.lease_id,
            holding=lease.holding,
            owner=lease.owner,
            owner_pid=lease.owner_pid,
            agent=lease.agent,
            issue=lease.issue,
            session=lease.session,
            purpose=lease.purpose,
            accounts=lease.accounts,
            **details,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "at": self.at,
            "event": self.event,
            "resource": self.resource,
            "kind": self.kind,
            "lease_id": self.lease_id,
            "holding": self.holding,
            "state": self.state,
            "owner": self.owner,
            "owner_pid": self.owner_pid,
            "agent": self.agent,
            "issue": self.issue,
            "session": self.session,
            "purpose": self.purpose,
            "accounts": list(self.accounts),
            "reason": self.reason,
            "held": self.held,
            "longest_quiet": self.longest_quiet,
            "drained": self.drained,
            "running": [{"pid": each.pid, "pgid": each.pgid} for each in self.running],
            "stopping": self.stopping,
            "problem": self.problem,
        }

    @classmethod
    def from_json(cls, data: object) -> Event:
        """Return the event of a line of the log, or raise ValueError."""
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"not an event of schema {SCHEMA_VERSION}")
        running = data.get("running")
        if not isinstance(running, list) or not all(
            isinstance(each, dict)
            and _is_pid(each.get("pid"))
            and (each.get("pgid") is None or _is_pid(each.get("pgid")))
            for each in running
        ):
            raise ValueError("running is not valid")
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or not all(_is_name(each) for each in accounts):
            raise ValueError("accounts is not valid")
        return cls(
            at=_get(data, "at", is_wall_time),
            event=_get(data, "event", lambda value: value in EVENTS),
            resource=_get(data, "resource", _is_name),
            kind=_get(data, "kind", _optional(_matches(KIND_NAME))),
            lease_id=_get(data, "lease_id", _optional(_is_name)),
            holding=_get(data, "holding", _optional(_is_name)),
            state=_get(data, "state", _optional(lambda value: value in STATES)),
            owner=_get(data, "owner", _optional(is_owner)),
            owner_pid=_get(data, "owner_pid", _optional(_is_pid)),
            agent=_get(data, "agent", _optional(_matches(PROGRAM_NAME))),
            issue=_get(data, "issue", _optional(_matches(ISSUE))),
            session=_get(data, "session", _optional(_matches(SESSION))),
            purpose=_get(data, "purpose", _optional(is_purpose)),
            accounts=tuple(accounts),
            reason=_get(data, "reason", _optional(lambda value: value in VOID_REASONS)),
            held=_get(data, "held", _optional(_is_seconds)),
            longest_quiet=_get(data, "longest_quiet", _optional(_is_seconds)),
            drained=_get(data, "drained", _optional(lambda value: isinstance(value, bool))),
            running=tuple(Process(each["pid"], each.get("pgid")) for each in running),
            stopping=_get(data, "stopping", _optional(lambda value: isinstance(value, bool))),
            problem=_get(data, "problem", _optional(_is_problem)),
        )


def processes(users: Sequence[User]) -> tuple[Process, ...]:
    return tuple(Process(user.pid, user.pgid) for user in users)


class History:
    def __init__(self, path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes

    @property
    def earlier(self) -> Path:
        return self.path.with_name(f"{self.path.name}.1")

    def append(self, event: Event) -> None:
        """Add an event at the end of the log.

        The lease store calls this under its lock, so two lines never mix, and only one process
        at a time starts the file again.
        """
        line = (json.dumps(event.to_json(), separators=(",", ":")) + "\n").encode()
        self._ensure_directory()
        try:
            size = os.lstat(self.path).st_size
        except FileNotFoundError:
            size = 0
        if size + len(line) > self.max_bytes and size > 0:
            os.replace(self.path, self.earlier)
        fd = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            _check_file(self.path, os.fstat(fd))
            os.write(fd, line)
        finally:
            os.close(fd)

    def read(self) -> tuple[list[Event], int]:
        """Return the events in the order in which they happened, and how many lines were left
        out because they are not valid events of this schema."""
        events: list[Event] = []
        skipped = 0
        for path in (self.earlier, self.path):
            try:
                raw = _read_file(path)
            except FileNotFoundError:
                continue
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    events.append(Event.from_json(json.loads(line)))
                except ValueError:
                    skipped += 1
        return events, skipped

    def _ensure_directory(self) -> None:
        directory = self.path.parent
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700, parents=True)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise LogError(f"{directory} is not a directory")
        _check_owner(directory, info)


def _read_file(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise LogError(f"cannot open {path}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as file:
        _check_file(path, os.fstat(file.fileno()))
        return file.read()


def _check_file(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise LogError(f"{path} is not a regular file")
    _check_owner(path, info)


def _check_owner(path: Path, info: os.stat_result) -> None:
    # Other agents read the log, so a log that another user can change could tell them anything.
    if info.st_uid != os.geteuid():
        raise LogError(f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise LogError(f"group or others can write to {path}")


def _get(data: dict[str, Any], key: str, check: Any) -> Any:
    value = data.get(key)
    if not check(value):
        raise ValueError(f"{key} is not valid")
    return value


def _optional(check: Any) -> Any:
    return lambda value: value is None or check(value)


def _matches(pattern: re.Pattern[str]) -> Any:
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


_is_name = _matches(RESOURCE_NAME)


def _is_pid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_seconds(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _is_problem(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PROBLEM_LENGTH
        and _CONTROL.search(value) is None
    )
