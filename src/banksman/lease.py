"""The lease record, its states, and the rules that make a lease void."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from banksman import SCHEMA_VERSION
from banksman.errors import BanksmanError

BOOTING = "booting"
READY = "ready"
DRAINING = "draining"
QUARANTINED = "quarantined"
STATES = (BOOTING, READY, DRAINING, QUARANTINED)
# The holder may use the resource only in these states.
HELD = (BOOTING, READY)

REBOOTED = "rebooted"
BOOT_TIMEOUT = "boot_timeout"
HARD_CAP = "hard_cap"
OWNER_GONE = "owner_gone"
IDLE = "idle"
RELEASE = "release"
VOID_REASONS = {
    REBOOTED: "the machine restarted after the lease was written",
    BOOT_TIMEOUT: "the resource was not ready by its boot deadline",
    HARD_CAP: "the lease reached its hard cap",
    OWNER_GONE: "the owner process ended, and nothing touched the lease for the grace period",
    IDLE: "nothing touched the lease for the idle timeout",
    RELEASE: "the holder released the lease while its scripts still ran",
}

RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}")
KIND_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# An issue id such as #123 or abc-123. It comes from the caller or from a branch name, and other
# agents read it, so it has only characters that cannot carry instructions or escape sequences.
ISSUE = re.compile(r"[A-Za-z0-9#][A-Za-z0-9._#/-]{0,63}")
# The last part of the path of an agent's executable, such as claude or codex.
PROGRAM_NAME = re.compile(r"[A-Za-z0-9._+-]{1,64}")
SESSION = re.compile(r"[A-Za-z0-9._:-]{1,128}")
MAX_PURPOSE_LENGTH = 200
# The most accounts that one lease can list.
MAX_LEASE_ACCOUNTS = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_OWNER_LENGTH = 1024
# The last second that a date can show: 9999-12-31 23:59:59 UTC.
_MAX_WALL_TIME = 253_402_300_799


class LeaseFormatError(BanksmanError):
    """A lease file does not have the format of the current schema."""


@dataclass(frozen=True)
class Timeouts:
    """How long a lease can live, in seconds of awake time.

    The field names are the keys of the configuration file. The defaults are starting values,
    to be tuned by measurement.
    """

    boot_timeout: float = 5 * 60
    owner_grace: float = 5 * 60
    idle_timeout: float | None = 20 * 60
    hard_cap: float = 3 * 60 * 60
    # How long a void lease waits for its scripts to end before it is quarantined.
    drain_timeout: float = 5 * 60


@dataclass(frozen=True)
class Holder:
    """Who holds a lease, and why."""

    # The worktree: the caller's git toplevel.
    owner: str
    # The agent process, or the process that holds the lease for a person. It decides liveness.
    owner_pid: int | None = None
    issue: str | None = None
    # The program name of the agent process.
    agent: str | None = None
    # An agent-specific id of the session, for display only.
    session: str | None = None
    # Text that one agent writes and other agents read. It is untrusted.
    purpose: str | None = None


@dataclass(frozen=True)
class User:
    """A script that uses the resource of a lease: its process, and its work group, if any."""

    pid: int
    started: str
    pgid: int | None = None
    # While the group's leader runs with this start time, the group is the one that the script
    # registered.
    leader_started: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started": self.started,
            "pgid": self.pgid,
            "leader_started": self.leader_started,
        }

    @classmethod
    def from_json(cls, data: object) -> User:
        if not isinstance(data, dict):
            raise LeaseFormatError("a user is not a JSON object")
        user = cls(
            pid=_get(data, "pid", _is_pid),
            started=_get(data, "started", _is_text),
            pgid=_get(data, "pgid", _optional(_is_pid)),
            leader_started=_get(data, "leader_started", _optional(_is_text)),
        )
        if (user.pgid is None) != (user.leader_started is None):
            raise LeaseFormatError("pgid and leader_started must both be set or both be null")
        return user


@dataclass(frozen=True)
class Lease:
    # Each lease has its own id: scripts pass it back as a token, so a script of an earlier
    # lease never acts on a newer lease of the same resource.
    lease_id: str
    # The leases that one acquire grants form one holding, and the holder uses them together.
    holding: str
    resource: str
    kind: str
    state: str
    owner: str
    owner_pid: int | None
    owner_started: str | None
    boot_id: str
    # Wall-clock times, for display only.
    acquired_at: float
    touched_at: float
    # Awake time on the boot that boot_id names. The rules that make a lease void use only
    # these, so a machine that sleeps does not age its leases.
    touched: float
    # When the lease was granted. With `longest_quiet`, it is the data for tuning the hard cap and
    # the idle timeout.
    acquired: float
    boot_deadline: float | None
    hard_deadline: float
    idle_timeout: float | None
    owner_grace: float
    drain_timeout: float
    # A draining lease is quarantined when its scripts still run at this time.
    drain_deadline: float | None = None
    users: tuple[User, ...] = ()
    void_reason: str | None = None
    # The reaper has ended the instance of a draining lease, and stopped its scripts if
    # stopping is on. It now waits for the scripts to end.
    ended: bool = False
    # How many reapers have started to take the lease back.
    attempts: int = 0
    # The banksman process that acts on the instance outside the lock: the acquire that resets
    # it with the on_acquire hook, or the reaper that takes a draining lease back. No other
    # banksman process acts on the instance while it runs.
    reaper_pid: int | None = None
    reaper_started: str | None = None
    issue: str | None = None
    agent: str | None = None
    session: str | None = None
    purpose: str | None = None
    # When the holder expects to give the resource back, in awake time. Only a hint.
    expected: float | None = None
    # The accounts on the instance that the holder got with it. An account is in use while a
    # lease in any state lists it: also while the lease drains, the scripts of its holder can
    # still use the account.
    accounts: tuple[str, ...] = ()
    # The longest time between two touches, in seconds of awake time.
    longest_quiet: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "lease_id": self.lease_id,
            "holding": self.holding,
            "resource": self.resource,
            "kind": self.kind,
            "state": self.state,
            "owner": self.owner,
            "owner_pid": self.owner_pid,
            "owner_started": self.owner_started,
            "issue": self.issue,
            "agent": self.agent,
            "session": self.session,
            "purpose": self.purpose,
            "boot_id": self.boot_id,
            "acquired_at": self.acquired_at,
            "touched_at": self.touched_at,
            "awake": {
                "acquired": self.acquired,
                "touched": self.touched,
                "boot_deadline": self.boot_deadline,
                "hard_deadline": self.hard_deadline,
                "drain_deadline": self.drain_deadline,
                "expected": self.expected,
            },
            "idle_timeout": self.idle_timeout,
            "owner_grace": self.owner_grace,
            "drain_timeout": self.drain_timeout,
            "longest_quiet": self.longest_quiet,
            "users": [user.to_json() for user in self.users],
            "accounts": list(self.accounts),
            "void_reason": self.void_reason,
            "ended": self.ended,
            "attempts": self.attempts,
            "reaper_pid": self.reaper_pid,
            "reaper_started": self.reaper_started,
        }

    @classmethod
    def from_json(cls, data: object) -> Lease:
        if not isinstance(data, dict):
            raise LeaseFormatError("the file does not hold a JSON object")
        if data.get("schema") != SCHEMA_VERSION:
            raise LeaseFormatError(f"the file does not have schema {SCHEMA_VERSION}")
        awake = data.get("awake")
        if not isinstance(awake, dict):
            raise LeaseFormatError("awake is missing or not valid")
        users = data.get("users")
        if not isinstance(users, list):
            raise LeaseFormatError("users is missing or not valid")
        accounts = _get(data, "accounts", _is_accounts)
        lease = cls(
            lease_id=_get(data, "lease_id", _is_text),
            holding=_get(data, "holding", _is_text),
            resource=_get(data, "resource", _is_resource),
            kind=_get(data, "kind", _is_kind),
            state=_get(data, "state", lambda value: isinstance(value, str) and value in STATES),
            owner=_get(data, "owner", is_owner),
            owner_pid=_get(data, "owner_pid", _optional(_is_pid)),
            owner_started=_get(data, "owner_started", _optional(_is_text)),
            issue=_get(data, "issue", _optional(_is_issue)),
            agent=_get(data, "agent", _optional(_is_program_name)),
            session=_get(data, "session", _optional(_is_session)),
            purpose=_get(data, "purpose", _optional(is_purpose)),
            boot_id=_get(data, "boot_id", _is_text),
            acquired_at=_get(data, "acquired_at", is_wall_time),
            touched_at=_get(data, "touched_at", is_wall_time),
            touched=_get(awake, "touched", _is_number),
            acquired=_get(awake, "acquired", _is_number),
            boot_deadline=_get(awake, "boot_deadline", _optional(_is_number)),
            hard_deadline=_get(awake, "hard_deadline", _is_number),
            drain_deadline=_get(awake, "drain_deadline", _optional(_is_number)),
            expected=_get(awake, "expected", _optional(_is_number)),
            idle_timeout=_get(data, "idle_timeout", _optional(_is_number)),
            owner_grace=_get(data, "owner_grace", _is_number),
            drain_timeout=_get(data, "drain_timeout", _is_number),
            longest_quiet=_get(data, "longest_quiet", _is_seconds),
            users=tuple(User.from_json(user) for user in users),
            accounts=tuple(accounts),
            void_reason=_get(data, "void_reason", _optional(_is_void_reason)),
            ended=_get(data, "ended", lambda value: isinstance(value, bool)),
            attempts=_get(data, "attempts", _is_count),
            reaper_pid=_get(data, "reaper_pid", _optional(_is_pid)),
            reaper_started=_get(data, "reaper_started", _optional(_is_text)),
        )
        if (lease.owner_pid is None) != (lease.owner_started is None):
            raise LeaseFormatError("owner_pid and owner_started must both be set or both be null")
        if (lease.reaper_pid is None) != (lease.reaper_started is None):
            raise LeaseFormatError(
                "reaper_pid and reaper_started must both be set or both be null"
            )
        if lease.state == BOOTING and lease.boot_deadline is None:
            raise LeaseFormatError("a booting lease needs a boot deadline")
        if lease.state == DRAINING and lease.drain_deadline is None:
            raise LeaseFormatError("a draining lease needs a drain deadline")
        if lease.state not in HELD and lease.void_reason is None:
            raise LeaseFormatError(f"a {lease.state} lease needs a void reason")
        return lease


def check_resource(resource: str) -> None:
    # A resource name becomes a file name, so it must not be able to leave the directory.
    if not _is_resource(resource):
        raise BanksmanError(f"not a valid resource name: {resource!r}")


def check_names(resource: str, kind: str, holder: Holder) -> None:
    check_resource(resource)
    if not _is_kind(kind):
        raise BanksmanError(f"not a valid kind name: {kind!r}")
    if not is_owner(holder.owner):
        raise BanksmanError(f"not a valid owner: {holder.owner!r}")
    if holder.issue is not None and not _is_issue(holder.issue):
        raise BanksmanError(
            "an issue id has 1 to 64 letters, digits, and the characters '#', '.', '_', '/',"
            " and '-'"
        )
    if holder.agent is not None and not _is_program_name(holder.agent):
        raise BanksmanError(f"not a valid agent program name: {holder.agent!r}")
    if holder.session is not None and not _is_session(holder.session):
        raise BanksmanError(f"not a valid session id: {holder.session!r}")
    if holder.purpose is not None and not is_purpose(holder.purpose):
        raise BanksmanError(
            f"a purpose has 1 to {MAX_PURPOSE_LENGTH} characters and no control characters"
        )


def void_reason(
    lease: Lease, *, boot_id: str, now: float, running: Mapping[int, str]
) -> str | None:
    """Return why the lease is void, or None while it is valid.

    `now` is awake time, and `running` maps each running process to its start time. The
    first rule that matches gives the reason.
    """
    if lease.boot_id != boot_id:
        return REBOOTED
    if lease.state == BOOTING and lease.boot_deadline is not None and now >= lease.boot_deadline:
        return BOOT_TIMEOUT
    if now >= lease.hard_deadline:
        return HARD_CAP
    quiet = now - lease.touched
    # A restarted run can keep touching its lease from carried-over commands, so owner death
    # voids the lease only after the grace period without a touch.
    if lease.owner_pid is not None and not owner_runs(lease, running) and quiet >= lease.owner_grace:
        return OWNER_GONE
    if lease.idle_timeout is not None and quiet >= lease.idle_timeout:
        return IDLE
    return None


def owner_runs(lease: Lease, running: Mapping[int, str]) -> bool:
    # The start time guards against a pid that the system has given to a new process.
    return lease.owner_pid is not None and running.get(lease.owner_pid) == lease.owner_started


@dataclass(frozen=True)
class FreeBy:
    """When a held lease can end, in awake time.

    banksman cannot know when a holder gives a resource back, so only `latest` is a promise.
    """

    # The holder's own estimate, from --expect.
    expected: float | None
    # The hard cap, which the reaper enforces.
    latest: float
    # When the lease becomes void if nothing touches it again.
    abandoned: float


def free_by(lease: Lease, running: Mapping[int, str]) -> FreeBy | None:
    """Return when a held lease can end, or None for a lease that is not held."""
    if lease.state not in HELD:
        return None
    ends = [lease.hard_deadline]
    if lease.state == BOOTING and lease.boot_deadline is not None:
        ends.append(lease.boot_deadline)
    if lease.idle_timeout is not None:
        ends.append(lease.touched + lease.idle_timeout)
    if lease.owner_pid is not None and not owner_runs(lease, running):
        ends.append(lease.touched + lease.owner_grace)
    return FreeBy(lease.expected, lease.hard_deadline, min(ends))


def _get(data: Mapping[str, object], key: str, check: Callable[[object], bool]) -> Any:
    value = data.get(key)
    if not check(value):
        raise LeaseFormatError(f"{key} is missing or not valid")
    return value


def _optional(check: Callable[[object], bool]) -> Callable[[object], bool]:
    return lambda value: value is None or check(value)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _is_number(value: object) -> bool:
    # json.loads accepts NaN and Infinity, and either would stop a lease from expiring.
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_seconds(value: object) -> bool:
    return _is_number(value) and value >= 0  # type: ignore[operator]


def is_wall_time(value: object) -> bool:
    return _is_number(value) and 0 <= value <= _MAX_WALL_TIME  # type: ignore[operator]


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_pid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_resource(value: object) -> bool:
    return isinstance(value, str) and RESOURCE_NAME.fullmatch(value) is not None


def _is_accounts(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_LEASE_ACCOUNTS
        and all(_is_resource(account) for account in value)
        and len(set(value)) == len(value)
    )


def _is_kind(value: object) -> bool:
    return isinstance(value, str) and KIND_NAME.fullmatch(value) is not None


def _is_void_reason(value: object) -> bool:
    return isinstance(value, str) and value in VOID_REASONS


def _is_issue(value: object) -> bool:
    return isinstance(value, str) and ISSUE.fullmatch(value) is not None


def _is_program_name(value: object) -> bool:
    return isinstance(value, str) and PROGRAM_NAME.fullmatch(value) is not None


def _is_session(value: object) -> bool:
    return isinstance(value, str) and SESSION.fullmatch(value) is not None


def is_purpose(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PURPOSE_LENGTH
        and _CONTROL.search(value) is None
    )


def is_owner(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_OWNER_LENGTH
        and _CONTROL.search(value) is None
    )

