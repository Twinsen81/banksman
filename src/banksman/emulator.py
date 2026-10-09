"""The starts of the android-emulator preset, and the emulators that banksman knows.

This is the only module that runs the emulator command. `acquire --start` starts an AVD that does
not run on a console port that banksman chooses before the boot, under the lock of the lease
store, so the serial is known from the start, and two starts never get the same one. The lease
is booting until the Android system has completed its boot.

banksman records each emulator that it starts, and each emulator that runs when a lease of its
AVD ends, by its pid and start time. Every other emulator of an allowed AVD is unmanaged: a
person or a program outside banksman started it, and a request does not get it unless its kind
says so. The reaper ends only an emulator that banksman started.
"""

from __future__ import annotations

import contextlib
import errno
import math
import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from banksman import android, hooks, records
from banksman.config import Android, Kind
from banksman.errors import BanksmanError
from banksman.lease import RESOURCE_NAME
from banksman.sanitize import clean
from banksman.store import STATE_DIR_ENV, lasting_dir
from banksman.system import Machine, Process, System

EMULATOR_DIR_ENV = "BANKSMAN_EMULATOR_DIR"
FILE_SCHEMA = 1
# adb finds an emulator by itself only when its console port is one of the even ports from 5554
# to 5584; the adb port is the next one.
FIRST_PORT = 5554
LAST_PORT = 5584
_POLL_SECONDS = 1.0
_STOP_SECONDS = 20.0
_KILL_SECONDS = 10.0
_STOP_POLL_SECONDS = 0.2
_MAX_STARTED = 64
_MAX_LOG_TAIL = 64 * 1024
_MAX_LINE = 300
# The programs that take the name of an AVD as @name: the emulator command, and the program
# that it becomes, such as qemu-system-aarch64-headless.
_PROGRAMS = ("emulator", "qemu-system")

Document = dict[str, Any]


class EmulatorError(BanksmanError):
    """An emulator could not be started or ended, or the record of the emulators is not valid."""


@dataclass(frozen=True)
class Known:
    """An emulator that banksman knows: one that it started, or one that ran when a lease of its
    AVD ended."""

    resource: str
    pid: int
    started: str
    # The boot of the machine. A pid and a start time name one process only on one boot.
    boot: str
    # Whether banksman started it. The reaper ends only those.
    created: bool
    # When banksman recorded it, in wall-clock time, for people only.
    at: float
    port: int | None = None


@dataclass(frozen=True)
class Started:
    serial: str
    pid: int


# Records a serial in the lease: the first of these that no held lease records. Returns it.
Claim = Callable[[Sequence[str]], str]


def default_emulator_dir() -> Path:
    override = os.environ.get(EMULATOR_DIR_ENV)
    if override:
        return Path(override)
    # Next to a state directory for tests, as the log and the quarantines.
    state_dir = os.environ.get(STATE_DIR_ENV)
    if state_dir:
        return Path(state_dir).with_name(f"{Path(state_dir).name}-emulator")
    # Outside /tmp, as the log: an emulator can run for days, and a cleaner of temporary files
    # must not make it unmanaged.
    return lasting_dir(EMULATOR_DIR_ENV) / "emulator"


def program(settings: Android) -> Path:
    return android.sdk(settings) / "emulator" / "emulator"


def avd_of(arguments: Sequence[str]) -> str | None:
    """Return the AVD that a command line starts the emulator of, or None."""
    emulator = bool(arguments) and os.path.basename(arguments[0]).startswith(_PROGRAMS)
    for index, argument in enumerate(arguments[1:], 1):
        if argument == "-avd" and index + 1 < len(arguments):
            return arguments[index + 1]
        # Another program can take a file as @path, so only an emulator counts.
        if emulator and argument.startswith("@") and len(argument) > 1:
            return argument[1:]
    return None


def by_avd(table: Mapping[int, Process]) -> dict[str, list[Process]]:
    """Return the processes of the emulators that run, by the name of their AVD."""
    found: dict[str, list[Process]] = {}
    for process in sorted(table.values(), key=lambda each: each.pid):
        name = avd_of(process.arguments)
        if name is not None:
            found.setdefault(name, []).append(process)
    return found


def port_free(port: int) -> bool:
    """Whether a new emulator can listen on this port, as it does on 127.0.0.1 and ::1."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((host, port))
        except OSError as exc:
            # A machine without IPv6 has no ::1, and the emulator then uses 127.0.0.1 only.
            missing = (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT)
            if family == socket.AF_INET6 and exc.errno in missing:
                continue
            return False
    return True


def discover(
    kind: Kind, settings: Android, *, accounts: bool = True, system: System | None = None
) -> tuple[Document, set[str]]:
    """Find the AVDs, as the preset does, and which of those that run are unmanaged.

    An AVD also runs when a process has its emulator command line: adb does not list an
    emulator in the first seconds of its start, or on a console port outside its range.
    """
    system = Machine() if system is None else system
    document = android.emulators(settings, accounts=accounts)
    notes = document.setdefault("notes", [])
    processes: dict[str, list[Process]] | None
    try:
        processes = by_avd(system.commands())
    except BanksmanError as exc:
        notes.append(f"cannot read the command lines of the processes: {exc}")
        processes = None
    for instance in document["instances"]:
        running = (processes or {}).get(instance["name"], [])
        if running:
            instance["facts"]["running"] = True
        if len(running) > 1:
            # Several emulators of one AVD, with -read-only. adb gave the serial of one of them,
            # and banksman cannot tell which, so a lease keeps its own serial.
            instance["facts"].pop("serial", None)
            instance.pop("accounts", None)
            name = instance["name"]
            notes.append(f"{name} runs as {len(running)} emulators, so it has no serial")
    try:
        known = load(kind, system)
    except BanksmanError as exc:
        # Without the record, no emulator counts as one that banksman knows, so none is given
        # to a request that it could take from somebody.
        notes.append(str(exc))
        known = {}
    unmanaged = {
        instance["name"]
        for instance in document["instances"]
        if instance["facts"].get("running") is True
        and not _only_known(instance["name"], processes, known)
    }
    return document, unmanaged


def _only_known(
    resource: str, processes: Mapping[str, list[Process]] | None, known: Mapping[str, Known]
) -> bool:
    """Whether the only emulator of an AVD that runs is the one that banksman knows.

    The record names one process for each AVD. A second emulator of the AVD, for example one
    with -read-only that a person started, can be the one at the serial that adb gives.
    """
    entry = known.get(resource)
    if entry is None or processes is None:
        return False
    return [process.pid for process in processes.get(resource, [])] == [entry.pid]


# The record of the emulators that banksman knows.


def load(kind: Kind, system: System) -> dict[str, Known]:
    """Return the emulators of a kind that banksman knows and that still run, by AVD."""
    data = records.read(_path(kind), EmulatorError)
    if data is None:
        return {}
    if (
        not isinstance(data, dict)
        or data.get("schema") != FILE_SCHEMA
        or not isinstance(data.get("emulators"), list)
    ):
        raise EmulatorError(f"{_path(kind)} is not valid")
    entries = [entry for entry in map(_known, data["emulators"]) if entry is not None]
    return _alive(entries, system)


@contextmanager
def changing(kind: Kind, system: System) -> Iterator[dict[str, Known]]:
    """Change the record of the emulators of a kind under its lock. The records of emulators
    that have ended go."""
    lock = default_emulator_dir() / f".{kind.name}-emulators.lock"
    with records.locked(lock, EmulatorError):
        known = load(kind, system)
        yield known
        alive = _alive(list(known.values()), system)
        records.write(
            _path(kind),
            {"schema": FILE_SCHEMA, "emulators": [_known_json(each) for each in alive.values()]},
            EmulatorError,
        )


def managed(kind: Kind, system: System | None = None) -> set[str]:
    """Return the AVDs whose emulator banksman knows and that runs now."""
    return set(load(kind, Machine() if system is None else system))


def adopt(kind: Kind, resource: str, system: System | None = None) -> None:
    """Record the emulator of an AVD that runs now, when a lease of it ends.

    The holder started it under the lease, so it is not unmanaged when it is free. banksman did
    not start it, so the reaper never ends it.
    """
    system = Machine() if system is None else system
    running = by_avd(system.commands()).get(resource, [])
    # With several emulators of the AVD, banksman cannot tell which one the holder used.
    if len(running) != 1:
        return
    (process,) = running
    boot = system.boot_id()
    with changing(kind, system) as known:
        if resource not in known:
            known[resource] = Known(
                resource, process.pid, process.started, boot, False, time.time()
            )


def take_back(kind: Kind, resource: str, system: System | None = None) -> str | None:
    """End the emulator of a void lease when banksman started it; record any other emulator of
    the AVD that runs, as at a release.

    Return None when that is done, or why the emulator did not end.
    """
    system = Machine() if system is None else system
    try:
        entry = load(kind, system).get(resource)
        if entry is None or not entry.created:
            adopt(kind, resource, system)
            return None
    except BanksmanError as exc:
        return str(exc)
    # Outside the lock of the record: the end can take seconds. The lease drains meanwhile, so no
    # request gets the AVD.
    if not stop(system, entry.pid, entry.started):
        return f"the emulator of {resource} (pid {entry.pid}) did not end"
    try:
        with changing(kind, system) as known:
            if known.get(resource) == entry:
                del known[resource]
    except BanksmanError as exc:
        return str(exc)
    return None


def stop(
    system: System,
    pid: int,
    started: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """End one emulator process: SIGTERM, and SIGKILL when it still runs. Return whether it has
    ended.

    Only the process: an emulator can start the netsimd daemon in its process group, and other
    emulators use that daemon.
    """
    for signum, seconds in ((signal.SIGTERM, _STOP_SECONDS), (signal.SIGKILL, _KILL_SECONDS)):
        if not _runs(system, pid, started):
            return True
        system.signal(pid, signum)
        give_up = clock() + seconds
        while clock() < give_up:
            if not _runs(system, pid, started):
                return True
            sleep(_STOP_POLL_SECONDS)
    return not _runs(system, pid, started)


# The start.


def start(
    kind: Kind,
    settings: Android,
    resource: str,
    *,
    deadline: float,
    claim: Claim,
    system: System | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    free: Callable[[int], bool] | None = None,
    run: android.Run | None = None,
) -> Started:
    """Start the emulator of an AVD on a free console port, and wait until its boot completes.

    `deadline` is when the boot must have completed, by `clock`. `claim` records the serial in
    the lease before the emulator starts. When the start fails or does not complete in time, or
    this command ends in the middle, banksman ends the emulator that it started.
    """
    system = Machine() if system is None else system
    free = port_free if free is None else free
    running = by_avd(system.commands()).get(resource)
    if running:
        # Discovery did not see it, for example because it started a moment ago.
        raise EmulatorError(
            f"an emulator of {resource} runs already (pid {running[0].pid}), but banksman did not"
            " start it, so it does not start another"
        )
    ports = [port for port in range(FIRST_PORT, LAST_PORT + 1, 2) if free(port) and free(port + 1)]
    if not ports:
        raise EmulatorError(
            f"no console port from {FIRST_PORT} to {LAST_PORT} is free, so adb could not find"
            " another emulator"
        )
    serial = claim([f"emulator-{port}" for port in ports])
    port = int(serial.removeprefix("emulator-"))
    command = [str(program(settings)), "-avd", resource, "-port", str(port), *kind.start_args]
    log = _open_log(resource)
    try:
        # A session of its own: the emulator outlives this command, and a signal to the process
        # group of the caller, such as Ctrl-C, does not reach it.
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd="/",
            env=_environment(settings),
            start_new_session=True,
        )
    except FileNotFoundError:
        raise EmulatorError(f"{command[0]} does not exist; set sdk in [android]") from None
    except OSError as exc:
        raise EmulatorError(f"cannot run {command[0]}: {exc.strerror}") from exc
    finally:
        os.close(log)
    # The emulator command replaces itself with the emulator, so the pid is the emulator's.
    started = system.running([process.pid]).get(process.pid)
    try:
        if started is not None:
            with changing(kind, system) as known:
                known[resource] = Known(
                    resource, process.pid, started, system.boot_id(), True, time.time(), port
                )
        _wait_for_boot(settings, resource, serial, process, deadline, clock, sleep, run)
    except BaseException:
        if started is not None:
            stop(system, process.pid, started, clock=clock, sleep=sleep)
            with contextlib.suppress(BanksmanError), changing(kind, system) as known:
                known.pop(resource, None)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_KILL_SECONDS)
        raise
    return Started(serial, process.pid)


def _wait_for_boot(
    settings: Android,
    resource: str,
    serial: str,
    process: subprocess.Popen[bytes],
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    run: android.Run | None,
) -> None:
    while True:
        status = process.poll()
        if status is not None:
            raise EmulatorError(
                f"the emulator of {resource} ended with exit status {status} before its boot"
                f" completed{_last_line(resource)}"
            )
        if android.booted(settings, serial, run):
            name = android.avd_name(settings, serial, run)
            if name != resource:
                # A program outside banksman took the port before the emulator could.
                raise EmulatorError(
                    f"{serial} is not the emulator of {resource} but of {name or 'another AVD'}"
                )
            return
        left = deadline - clock()
        if left <= 0:
            raise EmulatorError(
                f"the emulator of {resource} did not complete its boot by the boot deadline"
            )
        sleep(min(_POLL_SECONDS, left))


def _environment(settings: Android) -> dict[str, str]:
    # The variables of banksman's commands do not reach the emulator, and the SDK and the AVDs
    # are those of banksman's discovery, never of the caller's environment.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in hooks.VARIABLES and key != hooks.ANDROID_SERIAL
    }
    sdk = str(android.sdk(settings))
    env.update(
        ANDROID_HOME=sdk, ANDROID_SDK_ROOT=sdk, ANDROID_AVD_HOME=str(android.avd_home(settings))
    )
    return env


def _open_log(resource: str) -> int:
    # What the emulator prints, for the operator, and for the reason of a start that fails. Each
    # start begins it again.
    path = default_emulator_dir() / f"{resource}.log"
    records.directory(path.parent, EmulatorError)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise EmulatorError(f"cannot open {path}: {exc.strerror}") from exc
    try:
        records.check_owner(path, os.fstat(fd), EmulatorError)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _last_line(resource: str) -> str:
    path = default_emulator_dir() / f"{resource}.log"
    try:
        with open(path, "rb") as file:
            file.seek(max(0, os.fstat(file.fileno()).st_size - _MAX_LOG_TAIL))
            lines = file.read().decode(errors="replace").splitlines()
    except OSError:
        return ""
    line = next((line.strip() for line in reversed(lines) if line.strip()), "")
    if not line:
        return ""
    line = clean(" ".join(line.split()))
    if len(line) > _MAX_LINE:
        line = line[: _MAX_LINE - 3] + "..."
    return f": {line}"


def _runs(system: System, pid: int, started: str) -> bool:
    return system.running([pid]).get(pid) == started


def _alive(entries: Sequence[Known], system: System) -> dict[str, Known]:
    boot = system.boot_id()
    pids = sorted({entry.pid for entry in entries if entry.boot == boot})
    running = system.running(pids) if pids else {}
    return {
        entry.resource: entry
        for entry in entries
        if entry.boot == boot and running.get(entry.pid) == entry.started
    }


def _path(kind: Kind) -> Path:
    return default_emulator_dir() / f"{kind.name}-emulators.json"


def _known(entry: object) -> Known | None:
    if not isinstance(entry, dict):
        return None
    resource, pid, started = entry.get("resource"), entry.get("pid"), entry.get("started")
    boot, created = entry.get("boot"), entry.get("created")
    at, port = entry.get("at"), entry.get("port")
    if (
        not isinstance(resource, str)
        or RESOURCE_NAME.fullmatch(resource) is None
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 1
        or not isinstance(started, str)
        or not 0 < len(started) <= _MAX_STARTED
        or not isinstance(boot, str)
        or not boot
        or not isinstance(created, bool)
        or not isinstance(at, (int, float))
        or isinstance(at, bool)
        or not math.isfinite(at)
        or not (port is None or (isinstance(port, int) and not isinstance(port, bool)))
    ):
        return None
    return Known(resource, pid, started, boot, created, at, port)


def _known_json(entry: Known) -> dict[str, object]:
    return {
        "resource": entry.resource,
        "pid": entry.pid,
        "started": entry.started,
        "boot": entry.boot,
        "created": entry.created,
        "at": entry.at,
        "port": entry.port,
    }
