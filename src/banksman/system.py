"""The machine: the clock, the boot session, and the running processes.

This is the only module that runs `ps` or `sysctl`, or reads `/proc`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from banksman.errors import BanksmanError

_TIMEOUT_SECONDS = 5.0
_MAX_MACOS_PID = 99_999
_OUTSIDE_SANDBOX = (
    "If banksman runs inside an agent's sandbox, let the agent run it outside the sandbox."
)


class MachineError(BanksmanError):
    """The machine cannot give a fact that the lease rules need."""


class System(Protocol):
    def boot_id(self) -> str:
        """Return an id that changes at every boot of the machine."""

    def clock(self) -> float:
        """Return awake time: seconds that do not advance while the machine sleeps."""

    def wall_clock(self) -> float:
        """Return seconds since the epoch, for display only."""

    def running(self, pids: Iterable[int]) -> dict[int, str]:
        """Map each of the pids that runs now, zombies excluded, to its start time."""


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
        read = _running_from_proc if sys.platform.startswith("linux") else _running_from_ps
        running = read(sorted(wanted | {own}))
        # A process list that cannot see other processes, as inside an agent's sandbox, would
        # make every owner look ended. banksman itself always runs, so it must be in the list.
        if own not in running:
            raise MachineError(
                f"the process list does not show banksman itself. {_OUTSIDE_SANDBOX}"
            )
        return {pid: started for pid, started in running.items() if pid in wanted}


def _read_boot_id() -> str:
    if sys.platform.startswith("linux"):
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
                f"cannot read the boot session id: {exc}. {_OUTSIDE_SANDBOX}"
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
        try:
            line = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            continue
        # The program name in field 2 is in parentheses and can contain spaces and
        # parentheses itself, so the other fields are read after its last ")".
        fields = line[line.rindex(")") + 2 :].split()
        state, start_ticks = fields[0], fields[19]
        # The start time in clock ticks after boot does not change when the wall clock is
        # set, unlike the start date that ps prints on Linux.
        if state not in ("Z", "X"):
            running[pid] = start_ticks
    return running


def _running_from_ps(pids: list[int]) -> dict[int, str]:
    # macOS gives out pids up to 99999, and ps refuses a whole query that has a larger one.
    possible = [pid for pid in pids if pid <= _MAX_MACOS_PID]
    # A fixed locale and time zone make every caller get the same start time for one
    # process, whatever its own environment is.
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,stat=,lstart=", "-p", ",".join(str(pid) for pid in possible)],
            capture_output=True,
            text=True,
            env=env,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # On macOS, ps is a setuid program, and a sandboxed process cannot start one.
        raise MachineError(f"cannot run ps: {exc}. {_OUTSIDE_SANDBOX}") from exc
    # The query always includes banksman itself, so ps must find at least one process.
    if result.returncode != 0:
        raise MachineError(f"ps failed with exit status {result.returncode}. {_OUTSIDE_SANDBOX}")
    running = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        if not fields[1].startswith("Z"):
            running[int(fields[0])] = " ".join(fields[2:])
    return running

