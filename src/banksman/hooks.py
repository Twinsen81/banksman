"""Hooks: the commands that a kind declares in the configuration.

This is the only module that runs them, through the supervisor program.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence

from banksman import supervisor
from banksman.config import PRESETS, Kind
from banksman.lease import Lease

HOOK_TIMEOUT_SECONDS = 60.0
# The most that banksman reads of what a discover hook prints.
MAX_OUTPUT = 1024 * 1024
_KILL_WAIT_SECONDS = 5.0
_MAX_REASON = 4096
# The variables that banksman sets for the programs that it runs: hooks, and the commands of
# `banksman run`. A value from banksman's own environment, for example when such a program runs
# banksman, must not reach the next program.
VARIABLES = (
    "BANKSMAN_RESOURCE",
    "BANKSMAN_KIND",
    "BANKSMAN_LEASE",
    "BANKSMAN_SERIAL",
    "BANKSMAN_HANDLE",
    "BANKSMAN_ACCOUNTS",
)
# adb, and the connected tests of the Android Gradle plugin, act on the device that this variable
# names when a command does not name one.
ANDROID_SERIAL = "ANDROID_SERIAL"
# The value of ANDROID_SERIAL while the serial of an Android instance is not known. No device has
# it, so a command that does not name a device fails with "device not found" instead of acting
# on a device that adb chooses, which can be the device of another holder.
NO_SERIAL = "banksman-serial-unknown"


def take_back(
    kinds: Mapping[str, Kind], lease: Lease, *, timeout: float = HOOK_TIMEOUT_SECONDS
) -> str | None:
    """End the instance of a void lease with the `on_void` hook of its kind, if it has one.

    Return None when the resource is free, or why that cannot be confirmed.
    """
    kind = kinds.get(lease.kind)
    # A kind that the configuration no longer declares has no hook to run either.
    if kind is None or kind.on_void is None:
        return None
    failure = run(kind.on_void, lease, timeout=timeout, android=is_android(kind))
    return None if failure is None else f"the on_void hook {failure}"


def reset(
    kinds: Mapping[str, Kind], lease: Lease, *, timeout: float = HOOK_TIMEOUT_SECONDS
) -> str | None:
    """Reset the instance of a new lease with the `on_acquire` hook of its kind, if it has one.

    Return None when the instance is ready for the holder, or how the hook failed.
    """
    kind = kinds.get(lease.kind)
    if kind is None or kind.on_acquire is None:
        return None
    failure = run(kind.on_acquire, lease, timeout=timeout, android=is_android(kind))
    return None if failure is None else f"the on_acquire hook {failure}"


def discover(kind: Kind, *, timeout: float = HOOK_TIMEOUT_SECONDS) -> tuple[str | None, bytes]:
    """Run the `discover` hook of a kind. Return how it failed, or None, and what it printed."""
    if kind.discover is None:
        raise ValueError(f"kind {kind.name} has no discover hook")
    failure, output = _run(kind.discover, {"BANKSMAN_KIND": kind.name}, timeout, capture=True)
    return (None if failure is None else f"the discover hook {failure}"), output


def run(
    command: Sequence[str], lease: Lease, *, timeout: float, android: bool = False
) -> str | None:
    """Run a hook for the resource of a lease. Return None when it succeeded, or how it failed.

    With `android`, the hook also gets ANDROID_SERIAL.
    """
    return _run(command, variables(lease, android=android), timeout)[0]


def variables(lease: Lease, *, android: bool) -> dict[str, str]:
    """Return the variables that name the resource of a lease for a hook or a command.

    The serial is the one in the lease. With `android`, ANDROID_SERIAL names it too, or names no
    device while the serial is not known. The handle is the id of the reservation of a remote
    device, so that a script can extend it or disconnect it.
    """
    values = {"BANKSMAN_RESOURCE": lease.resource, "BANKSMAN_KIND": lease.kind}
    if lease.serial is not None:
        values["BANKSMAN_SERIAL"] = lease.serial
    if lease.handle is not None:
        values["BANKSMAN_HANDLE"] = lease.handle
    if android:
        values[ANDROID_SERIAL] = NO_SERIAL if lease.serial is None else lease.serial
    return values


def is_android(kind: Kind | None) -> bool:
    return kind is not None and kind.preset in PRESETS


def _run(
    command: Sequence[str], variables: Mapping[str, str], timeout: float, *, capture: bool = False
) -> tuple[str | None, bytes]:
    # A hook runs in the environment of whichever command reaps, for example a command that
    # banksman run started with the ANDROID_SERIAL of another lease.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in VARIABLES and key != ANDROID_SERIAL
    }
    env.update(variables)
    with contextlib.ExitStack() as stack:
        # The output goes to a file, not a pipe: a program that the hook leaves running would
        # otherwise keep a pipe open, and the command that reads it waiting. Output that banksman
        # does not read is discarded, not inherited, for the same reason.
        output = stack.enter_context(tempfile.TemporaryFile()) if capture else None
        # No shell and no input. A fixed working directory makes a hook behave the same for
        # every caller.
        process = subprocess.Popen(
            [sys.executable, "-I", supervisor.__file__, *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL if output is None else output,
            stderr=subprocess.PIPE,
            cwd="/",
            env=env,
            start_new_session=True,
        )
        with process.stdin, process.stderr:  # type: ignore[union-attr]
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill(process)
                return f"did not end within {timeout:g} s", b""
            except BaseException:
                _kill(process)
                raise
            if status == supervisor.COULD_NOT_START:
                written = process.stderr.read(_MAX_REASON)  # type: ignore[union-attr]
                reason = written.decode(errors="replace")
                if reason:
                    return f"could not start {command[0]}: {reason}", b""
        if status < 0:
            return f"was ended by signal {-status}", b""
        if status > 0:
            return f"failed with exit status {status}", b""
        if output is None:
            return None, b""
        output.seek(0)
        printed = output.read(MAX_OUTPUT + 1)
    if len(printed) > MAX_OUTPUT:
        return f"printed more than {MAX_OUTPUT} bytes", b""
    return None, printed


def _kill(process: subprocess.Popen[bytes]) -> None:
    # The supervisor leads a process group that banksman created for the hook, so the signal
    # reaches the supervisor, the hook, and the programs that the hook started, and nothing
    # else.
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    # A program that banksman may not signal, such as a setuid one, can keep running; the
    # command must not wait for it forever.
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_KILL_WAIT_SECONDS)
