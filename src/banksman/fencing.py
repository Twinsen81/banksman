"""Fencing: the scripts that use a leased resource, and how a void lease waits for them to end.

By default banksman sends no signal. When the operator turns stopping on for a kind, the
reaper signals only processes that it can prove the lease's owner started.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass

from banksman.errors import BanksmanError
from banksman.lease import Lease, User
from banksman.system import Process, System

TERM_WAIT_SECONDS = 10.0
KILL_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.2

Table = Mapping[int, Process]


class Refused(BanksmanError):
    """A script asked to register a process or a group that the reaper must never signal."""


def register(table: Table, lease: Lease, pid: int, pgid: int | None) -> User:
    """Return the user record for a script that enters a lease, or refuse it."""
    process = table.get(pid)
    if process is None:
        raise BanksmanError(f"process {pid} is not running")
    owner = owner_process(table, lease)
    # The owner is the agent, or the process that holds the lease for a person. A script must
    # not register it, or a process above it, such as the app that runs the agent.
    if owner is not None and (pid == owner or pid in ancestors(table, owner)):
        raise Refused(f"process {pid} is the owner of the lease or a process above it")
    if pgid is None:
        return User(pid=pid, started=process.started)
    members = [member.pid for member in table.values() if member.pgid == pgid]
    if not members:
        raise Refused(f"there is no process group {pgid}")
    leader = table.get(pgid)
    if leader is None:
        raise Refused(f"the leader of process group {pgid} has ended")
    # An agent can share one process group with the app that runs it, so a group is accepted
    # only if the script created it for its work.
    for member in sorted({pgid, *members}):
        if member != pid and pid not in ancestors(table, member):
            raise Refused(
                f"process group {pgid} has process {member} in it, which process {pid} did not"
                " start. Register a process group that the script creates for its work."
            )
    return User(pid=pid, started=process.started, pgid=pgid, leader_started=leader.started)


def runs(user: User, table: Table) -> bool:
    return bool(processes_of(user, table))


def processes_of(user: User, table: Table) -> set[int]:
    """Return the processes of a user that still run: its own, and those in its group."""
    pids = set()
    if _runs(table, user.pid, user.started):
        pids.add(user.pid)
    if user.pgid is not None:
        leader = table.get(user.pgid)
        # The system gives the id of a process group that still exists to no new process. So a
        # leader with another start time means that the registered group has ended, and a
        # process with this group id belongs to a new group.
        if leader is None or leader.started == user.leader_started:
            pids.update(process.pid for process in table.values() if process.pgid == user.pgid)
            if leader is not None:
                pids.add(leader.pid)
    return pids


def owner_process(table: Table, lease: Lease) -> int | None:
    if lease.owner_pid is None or not _runs(table, lease.owner_pid, lease.owner_started):
        return None
    return lease.owner_pid


def ancestors(table: Table, pid: int) -> list[int]:
    chain: list[int] = []
    process = table.get(pid)
    while process is not None and process.ppid > 0 and process.ppid not in chain:
        chain.append(process.ppid)
        process = table.get(process.ppid)
    return chain


@dataclass(frozen=True)
class Target:
    """A process, or a process group, that the reaper proved the owner process started."""

    pid: int  # the process, or the leader of the group
    started: str
    group: bool = False
    # The processes in the group when it was proven, with their start times.
    members: tuple[tuple[int, str], ...] = ()


def stop(lease: Lease, system: System) -> None:
    """Stop the scripts of a void lease: SIGTERM, then SIGKILL to the ones that still run.

    The targets are proven once, before the first signal: a process that SIGTERM ends gives its
    children to process 1, so they no longer descend from the owner process. Right before each
    signal, a new process list checks each target again by pid and start time.
    """
    table = system.process_table()
    owner = owner_process(table, lease)
    found = {user: signal_targets(table, lease, user) for user in lease.users}
    stoppable = [user for user, targets in found.items() if targets and runs(user, table)]
    if owner is None or not stoppable:
        return
    targets = {target for user in stoppable for target in found[user]}
    protected = _protected(table, owner)
    for signum, wait in ((signal.SIGTERM, TERM_WAIT_SECONDS), (signal.SIGKILL, KILL_WAIT_SECONDS)):
        if signum == signal.SIGKILL:
            table = system.process_table()
            protected |= _protected(table, owner)
        for target in sorted(targets, key=lambda target: (target.pid, target.group)):
            _send(system, table, target, signum, protected)
        give_up = system.clock() + wait
        while system.clock() < give_up:
            system.sleep(_POLL_SECONDS)
            table = system.process_table()
            if not any(runs(user, table) for user in stoppable):
                return


def signal_targets(table: Table, lease: Lease, user: User) -> list[Target]:
    """Return the process and the process group of a user that the reaper may signal.

    A target must run with the start time that the script registered, and the lease's owner
    process must have started it. Without a running owner process, nothing can be proven, and
    nothing is signalled. The owner, the processes above it, the reaper, and the processes above
    the reaper are never signalled, also not as members of a group.
    """
    owner = owner_process(table, lease)
    if owner is None:
        return []
    protected = _protected(table, owner)
    targets = []
    if (
        _runs(table, user.pid, user.started)
        and user.pid not in protected
        and owner in ancestors(table, user.pid)
    ):
        targets.append(Target(user.pid, user.started))
    if (
        user.pgid is not None
        and user.leader_started is not None
        and _runs(table, user.pgid, user.leader_started)
        and owner in ancestors(table, user.pgid)
    ):
        members = [process for process in table.values() if process.pgid == user.pgid]
        if not any(process.pid in protected for process in members):
            started = tuple((process.pid, process.started) for process in members)
            targets.append(Target(user.pgid, user.leader_started, group=True, members=started))
    return targets


def _send(system: System, table: Table, target: Target, signum: int, protected: set[int]) -> None:
    if not target.group:
        if _runs(table, target.pid, target.started) and target.pid not in protected:
            system.signal(target.pid, signum)
        return
    if any(process.pgid == target.pid and process.pid in protected for process in table.values()):
        return
    if _runs(table, target.pid, target.started):
        # While its leader runs, the group id still names the group that was proven.
        system.signal_group(target.pid, signum)
        return
    # The leader has ended, so the group id could name a new group. Only the processes that were
    # in the group when it was proven get the signal.
    for pid, started in target.members:
        if _runs(table, pid, started) and pid not in protected:
            system.signal(pid, signum)


def _protected(table: Table, owner: int) -> set[int]:
    reaper = os.getpid()
    return {owner, reaper, *ancestors(table, owner), *ancestors(table, reaper)}


def _runs(table: Table, pid: int, started: str | None) -> bool:
    process = table.get(pid)
    return process is not None and process.started == started
