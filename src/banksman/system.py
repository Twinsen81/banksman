"""The machine: the clock, the boot session, and the running processes.

This is the only module that runs `ps` or `sysctl`, reads `/proc`, or sends signals.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from banksman.errors import BanksmanError

_TIMEOUT_SECONDS = 5.0
_MAX_MACOS_PID = 99_999
OUTSIDE_SANDBOX = (
    "If banksman runs inside an agent's sandbox, let the agent run it outside the sandbox."
)


class MachineError(BanksmanError):
    """The machine cannot give a fact that the lease rules need."""


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    pgid: int
    started: str


class System(Protocol):
    def boot_id(self) -> str:
        """Return an id that changes at every boot of the machine."""

    def clock(self) -> float:
        """Return awake time: seconds that do not advance while the machine sleeps."""

    def wall_clock(self) -> float:
        """Return seconds since the epoch, for display only."""

    def running(self, pids: Iterable[int]) -> dict[int, str]:
        """Map each of the pids that runs now, zombies excluded, to its start time."""

    def process_table(self) -> dict[int, Process]:
        """Map every process that runs now, zombies excluded, to its parent, group, and start."""

    def signal(self, pid: int, signum: int) -> None:
        """Send a signal to one process."""

    def signal_group(self, pgid: int, signum: int) -> None:
        """Send a signal to every process in a process group."""

    def sleep(self, seconds: float) -> None:
        """Wait, for example for processes to end."""


class Machine:
    """The real machine."""

    def __init__(self) -> None:
        self._boot_id: str | None = None

    def boot_id(self) -> str:
        if self._boot_id is None:
            self._boot_id = _read_boot_id()
        return self._boot_id

    def clock(self) -> float:
        # mach_absolute_time on macOS and CLOCK_MONOTONIC on Linux: both stop while the
        # machine sleeps, and all processes on one boot read the same clock.
        return time.monotonic()

    def wall_clock(self) -> float:
        return time.time()

    def running(self, pids: Iterable[int]) -> dict[int, str]:
        wanted = {pid for pid in pids if pid > 0}
        if not wanted:
            return {}
        own = os.getpid()
        read = _running_from_proc if _LINUX else _running_from_ps
        running = read(sorted(wanted | {own}))
        _check_visible(running)
        return {pid: started for pid, started in running.items() if pid in wanted}

    def process_table(self) -> dict[int, Process]:
        table = _table_from_proc() if _LINUX else _table_from_ps()
        _check_visible(table)
        return table

    def signal(self, pid: int, signum: int) -> None:
        _check_target(pid)
        # A process that has ended, or that banksman may not signal, is not an error here: the
        # reaper confirms the end of a process with the process list, never with this call.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signum)

    def signal_group(self, pgid: int, signum: int) -> None:
        _check_target(pgid)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signum)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_LINUX = sys.platform.startswith("linux")


def _check_visible(processes: dict[int, object]) -> None:
    # A process list that cannot see other processes, as inside an agent's sandbox, would make
    # every owner look ended. banksman itself always runs, so it must be in the list.
    if os.getpid() not in processes:
        raise MachineError(f"the process list does not show banksman itself. {OUTSIDE_SANDBOX}")


def _check_target(pid: int) -> None:
    # 0 names the group of banksman itself, -1 every process that it may signal, and 1 the
    # init process.
    if pid <= 1:
        raise ValueError(f"banksman never signals process {pid}")


def _read_boot_id() -> str:
    if _LINUX:
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError as exc:
            raise MachineError(f"cannot read the boot id: {exc}") from exc
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MachineError(
                f"cannot read the boot session id: {exc}. {OUTSIDE_SANDBOX}"
            ) from exc
        value = result.stdout.strip()
    else:
        raise MachineError(f"banksman does not support this platform: {sys.platform}")
    if not value:
        raise MachineError("the boot id is empty")
    return value


def _running_from_proc(pids: list[int]) -> dict[int, str]:
    running = {}
    for pid in pids:
        process = _read_stat(pid)
        if process is not None:
            running[pid] = process.started
    return running


def _table_from_proc() -> dict[int, Process]:
    table = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            process = _read_stat(int(name))
            if process is not None:
                table[process.pid] = process
    return table


def _read_stat(pid: int) -> Process | None:
    try:
        line = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The program name in field 2 is in parentheses and can contain spaces and parentheses
    # itself, so the other fields are read after its last ")".
    fields = line[line.rindex(")") + 2 :].split()
    state, ppid, pgid, start_ticks = fields[0], fields[1], fields[2], fields[19]
    if state in ("Z", "X"):
        return None
    # The start time in clock ticks after boot does not change when the wall clock is set,
    # unlike the start date that ps prints on Linux.
    return Process(pid=pid, ppid=int(ppid), pgid=int(pgid), started=start_ticks)


def _running_from_ps(pids: list[int]) -> dict[int, str]:
    # macOS gives out pids up to 99999, and ps refuses a whole query that has a larger one.
    possible = [pid for pid in pids if pid <= _MAX_MACOS_PID]
    output = _ps(["-o", "pid=,stat=,lstart=", "-p", ",".join(str(pid) for pid in possible)])
    running = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        if not fields[1].startswith("Z"):
            running[int(fields[0])] = " ".join(fields[2:])
    return running


def _table_from_ps() -> dict[int, Process]:
    table = {}
    for line in _ps(["-A", "-o", "pid=,ppid=,pgid=,stat=,lstart="]).splitlines():
        fields = line.split()
        if len(fields) < 5 or not all(field.isdigit() for field in fields[:3]):
            continue
        if not fields[3].startswith("Z"):
            pid, ppid, pgid = (int(field) for field in fields[:3])
            # Joined the same way as by _running_from_ps, so both give one start time.
            table[pid] = Process(pid=pid, ppid=ppid, pgid=pgid, started=" ".join(fields[4:]))
    return table


def _ps(arguments: list[str]) -> str:
    # A fixed locale and time zone make every caller get the same start time for one
    # process, whatever its own environment is.
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    try:
        result = subprocess.run(
            ["ps", *arguments],
            capture_output=True,
            text=True,
            env=env,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # On macOS, ps is a setuid program, and a sandboxed process cannot start one.
        raise MachineError(f"cannot run ps: {exc}. {OUTSIDE_SANDBOX}") from exc
    # Every query includes banksman itself, so ps must find at least one process.
    if result.returncode != 0:
        raise MachineError(f"ps failed with exit status {result.returncode}. {OUTSIDE_SANDBOX}")
    return result.stdout
