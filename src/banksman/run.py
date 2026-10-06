"""Run a command that uses a leased resource, inside the fence of the lease.

The command runs in a process group of its own, which the program in leader.py leads. banksman
registers the group with the lease before the command starts. While the command runs, banksman
checks the lease, and the check also touches it. When the lease is lost, banksman stops the
group, and it leaves the lease only after the group has ended, so that the resource is never
handed on while a process of the command can still use it.

This is the only module that runs such a command.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from banksman import leader
from banksman.errors import BanksmanError
from banksman.fencing import KILL_WAIT_SECONDS, TERM_WAIT_SECONDS
from banksman.lease import Lease
from banksman.store import NotHeld, Store

# The most time between two checks of the lease. A lease with a short drain timeout or idle
# timeout is checked more often, so that the run sees that the lease is lost before it is
# quarantined, and touches the lease before it is idle.
CHECK_SECONDS = 10.0
MIN_CHECK_SECONDS = 0.5
# The signals that banksman passes on to the group. A terminal sends them to its foreground
# process group, and an agent or a script sends them to banksman.
FORWARDED = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)
_JOB_STOPS = (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU)
_POLL_SECONDS = 0.1
# Each look for the processes of the group runs ps.
_STOP_POLL_SECONDS = 0.2
_GROUP_POLL_SECONDS = 0.5
# Longer than the leader waits for the command when banksman closes the socket early.
_LEADER_WAIT_SECONDS = leader.TERM_WAIT_SECONDS + 5

Warn = Callable[[str], None]


@dataclass(frozen=True)
class Outcome:
    # The exit status of the command, or the negative number of the signal that ended it. None
    # when the lease was lost before the command ended.
    returncode: int | None
    # Why the command could not start.
    error: str | None = None
    # Why the lease was lost while processes of the command ran.
    lost: str | None = None
    # The processes of the group that still ran when banksman stopped waiting for them.
    running: tuple[int, ...] = ()
    # The command had the terminal when it ended, so a signal that the terminal sent, such as
    # Ctrl-C, reached its group and not banksman's.
    terminal: bool = False


def check_interval(lease: Lease) -> float:
    limits = [CHECK_SECONDS, lease.drain_timeout / 4]
    if lease.idle_timeout is not None:
        limits.append(lease.idle_timeout / 4)
    return max(MIN_CHECK_SECONDS, min(limits))


def run(
    store: Store,
    resource: str,
    lease_id: str,
    command: Sequence[str],
    env: Mapping[str, str],
    warn: Warn,
) -> Outcome:
    """Run a command under a lease, and return how it ended."""
    return _Run(store, resource, lease_id, warn).run(command, env)


class _Run:
    def __init__(self, store: Store, resource: str, lease_id: str, warn: Warn) -> None:
        self.store = store
        self.system = store.system
        self.resource = resource
        self.lease_id = lease_id
        self.warn = warn
        self.leader: subprocess.Popen[bytes] | None = None
        self.group = 0
        # The leader has not been reaped, so the id of the group cannot name another group.
        self.anchored = False
        self.tty: int | None = None
        self.warned = False

    def run(self, command: Sequence[str], env: Mapping[str, str]) -> Outcome:
        ours, theirs = socket.socketpair()
        with ours:
            try:
                self.leader = subprocess.Popen(
                    [sys.executable, "-I", leader.__file__, str(theirs.fileno()), *command],
                    pass_fds=(theirs.fileno(),),
                    env=dict(env),
                    process_group=0,
                )
            finally:
                theirs.close()
            self.group = self.leader.pid
            self.anchored = True
            previous: dict[int, object] = {}
            try:
                lease = self.store.enter(self.resource, self.lease_id, self.group, self.group)
                # A signal that the caller ignores, as nohup does, stays ignored.
                for signum in FORWARDED:
                    if signal.getsignal(signum) != signal.SIG_IGN:
                        previous[signum] = signal.signal(signum, self._forward)
                self.tty = _foreground_terminal()
                self._give_terminal()
                ours.sendall(leader.GO)
                return self._supervise(ours, check_interval(lease))
            finally:
                self._take_terminal()
                if self.tty is not None:
                    os.close(self.tty)
                for signum, handler in previous.items():
                    signal.signal(signum, handler)  # type: ignore[arg-type]
                # Without a word from banksman, the leader ends its group.
                ours.close()
                if self.anchored:
                    self._reap()

    def _supervise(self, channel: socket.socket, interval: float) -> Outcome:
        due = self.system.clock() + interval
        received = b""
        while True:
            wait = min(_POLL_SECONDS, max(0.0, due - self.system.clock()))
            if select.select([channel], [], [], wait)[0]:
                data = channel.recv(4096)
                received += data
                if not data or received.endswith(b"\n"):
                    break
            if not self._leader_runs():
                break
            if self.system.clock() >= due:
                lost = self._check()
                if lost is not None:
                    return self._stop(channel, lost)
                due = self.system.clock() + interval
        report = _report(received)
        terminal = self._take_terminal()
        shown = False
        while left := self._members():
            if not shown:
                self.warn(
                    f"the command has ended, but processes that it started still run in its"
                    f" process group: {_pids(left)}. banksman waits for them, because they can"
                    f" still use {self.resource}"
                )
                shown = True
            self.system.sleep(min(_GROUP_POLL_SECONDS, max(0.0, due - self.system.clock())))
            if self.system.clock() >= due:
                lost = self._check()
                if lost is not None:
                    return self._stop(channel, lost)
                due = self.system.clock() + interval
        # A command can fail because its lease is lost: the reaper can stop its group or end its
        # instance. Then the status of the command does not tell the caller what happened.
        lost = self._check() if report is None or report[0] != 0 else None
        self._finish(channel)
        if lost is not None:
            return Outcome(None, lost=lost, terminal=terminal)
        if report is None:
            # The leader ended without a report, so its own end is all that is known.
            return Outcome(self.leader.returncode, terminal=terminal)  # type: ignore[union-attr]
        returncode, error = report
        return Outcome(returncode, error=error, terminal=terminal)

    def _check(self) -> str | None:
        """Check and touch the lease. Return why it is lost, or None while it is held."""
        try:
            self.store.check(self.resource, self.lease_id)
        except NotHeld as exc:
            return str(exc)
        except (BanksmanError, OSError) as exc:
            # A check that cannot run now, for example while another command holds the lock for
            # too long, does not stop the command. The next check tries again.
            if not self.warned:
                self.warn(f"cannot check the lease on {self.resource} now: {exc}")
                self.warned = True
        return None

    def _stop(self, channel: socket.socket, lost: str) -> Outcome:
        """Stop the group of a lost lease: SIGTERM, then SIGKILL to what still runs."""
        terminal = self._take_terminal()
        if self.anchored:
            self._signal(signal.SIGTERM)
            self._signal(signal.SIGCONT)
            if not self._ended(TERM_WAIT_SECONDS):
                self._signal(signal.SIGKILL)
                self._ended(KILL_WAIT_SECONDS)
        else:
            # Without the leader, the id of the group could name another group by now, so
            # banksman sends no signal and only waits.
            self._ended(TERM_WAIT_SECONDS)
        left = tuple(self._members())
        if left:
            # banksman does not leave: the lease drains until these processes end, and the
            # reaper quarantines it at its drain deadline when they still run.
            return Outcome(None, lost=lost, running=left, terminal=terminal)
        self._finish(channel)
        return Outcome(None, lost=lost, terminal=terminal)

    def _finish(self, channel: socket.socket) -> None:
        with contextlib.suppress(OSError):
            channel.sendall(leader.DONE)
        self._reap()
        with contextlib.suppress(NotHeld):
            self.store.leave(self.resource, self.lease_id, self.group)

    def _leader_runs(self) -> bool:
        """Whether the leader still runs. A stop of the group is passed on to banksman's job."""
        if not self.anchored or self.leader is None:
            return False
        with _forwarding_held():
            pid, status = os.waitpid(self.group, os.WNOHANG | os.WUNTRACED)
            if pid != 0 and not os.WIFSTOPPED(status):
                self.anchored = False
                self.leader.returncode = os.waitstatus_to_exitcode(status)
                return False
        if pid != 0:
            self._stopped(os.WSTOPSIG(status))
        return True

    def _stopped(self, signum: int) -> None:
        # The shell waits for banksman, not for the group of the command, so on Ctrl-Z banksman
        # stops its own job too, and continues the group when the shell continues banksman.
        # Without a terminal there is no job control: the group stays stopped until something
        # continues it.
        if self.tty is None:
            return
        self._take_terminal()
        os.killpg(os.getpgrp(), signum if signum in _JOB_STOPS else signal.SIGTSTP)
        self._give_terminal()
        self._signal(signal.SIGCONT)

    def _forward(self, signum: int, frame: object) -> None:
        if not self.anchored:
            # Something has killed the leader, so banksman can no longer signal the group, and
            # it ends as the signal asks.
            raise SystemExit(128 + signum)
        self._signal(signum)
        # A stopped process acts on a signal only when it continues.
        self._signal(signal.SIGCONT)

    def _signal(self, signum: int) -> None:
        # While the leader is not reaped, the id of the group names the group of the command.
        if self.anchored:
            self.system.signal_group(self.group, signum)

    def _members(self) -> list[int]:
        """Return the processes in the group of the command, except the leader."""
        table = self.system.process_table()
        return sorted(
            pid
            for pid, process in table.items()
            if process.pgid == self.group and pid != self.group
        )

    def _ended(self, timeout: float) -> bool:
        give_up = self.system.clock() + timeout
        while True:
            if not self._members():
                return True
            if self.system.clock() >= give_up:
                return False
            self.system.sleep(_STOP_POLL_SECONDS)

    def _reap(self) -> None:
        give_up = self.system.clock() + _LEADER_WAIT_SECONDS
        while self.anchored and self.leader is not None:
            with _forwarding_held():
                if self.leader.poll() is not None:
                    self.anchored = False
                    return
            if self.system.clock() >= give_up:
                # The leader is not reaped yet, so the id still names the group of the command.
                self._signal(signal.SIGKILL)
            self.system.sleep(_POLL_SECONDS)

    def _give_terminal(self) -> None:
        """Make the group of the command the foreground of the terminal while banksman has it."""
        if self.tty is not None and self.anchored and _foreground(self.tty) == os.getpgrp():
            _set_foreground(self.tty, self.group)

    def _take_terminal(self) -> bool:
        """Take the terminal back from the group of the command. Return whether it had it."""
        if self.tty is None or _foreground(self.tty) != self.group:
            return False
        _set_foreground(self.tty, os.getpgrp())
        return True


def _foreground_terminal() -> int | None:
    """Return the terminal that is the standard input of banksman while banksman runs in its
    foreground, or None.

    The input tells whether the command is for a person at the terminal. An agent that runs its
    commands in its own process group gives them other input, so it keeps its terminal.
    """
    if not os.isatty(0) or _foreground(0) != os.getpgrp():
        return None
    return os.dup(0)


@contextlib.contextmanager
def _forwarding_held() -> Iterator[None]:
    # Between the reap of the leader and the end of `anchored`, a forwarded signal would reach a
    # group id that the system can give to another group.
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set(FORWARDED))
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


def _foreground(tty: int) -> int | None:
    # A terminal that has hung up has no foreground.
    try:
        return os.tcgetpgrp(tty)
    except OSError:
        return None


def _set_foreground(tty: int, group: int) -> None:
    # A process outside the foreground group gets SIGTTOU when it changes the foreground,
    # unless it blocks that signal.
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        with contextlib.suppress(OSError):
            os.tcsetpgrp(tty, group)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


def _report(received: bytes) -> tuple[int, str | None] | None:
    """Return the exit status and the start error that the leader reported, or None."""
    try:
        message = json.loads(received)
    except ValueError:
        return None
    if not isinstance(message, dict) or not isinstance(message.get("returncode"), int):
        return None
    error = message.get("error")
    return message["returncode"], error if isinstance(error, str) else None


def _pids(pids: Sequence[int], shown: int = 10) -> str:
    text = ", ".join(f"pid {pid}" for pid in pids[:shown])
    more = len(pids) - shown
    return f"{text}, and {more} more" if more > 0 else text

