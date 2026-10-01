"""Hooks: the commands that a kind declares in the configuration.

This is the only module that runs them.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence

from banksman.config import Kind
from banksman.lease import Lease

HOOK_TIMEOUT_SECONDS = 60.0
_KILL_WAIT_SECONDS = 5.0


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
    failure = run(kind.on_void, lease, timeout=timeout)
    return None if failure is None else f"the on_void hook {failure}"


def run(command: Sequence[str], lease: Lease, *, timeout: float) -> str | None:
    """Run a hook for the resource of a lease. Return None when it succeeded, or how it failed."""
    env = {**os.environ, "BANKSMAN_RESOURCE": lease.resource, "BANKSMAN_KIND": lease.kind}
    try:
        # No shell and no input. The output is discarded rather than inherited: a program that
        # the hook leaves running would otherwise keep a caller that reads banksman's output
        # waiting. A fixed working directory makes a hook behave the same for every caller.
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return f"could not start {command[0]}: {exc.strerror or exc}"
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(process)
        return f"did not end within {timeout:g} s"
    except BaseException:
        _kill(process)
        raise
    if status < 0:
        return f"was ended by signal {-status}"
    if status > 0:
        return f"failed with exit status {status}"
    return None


def _kill(process: subprocess.Popen[bytes]) -> None:
    # The hook runs in a process group that banksman created for it, so the signal reaches the
    # hook and the programs that it started, and nothing else.
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    # A program that banksman may not signal, such as a setuid one, can keep running; the
    # command must not wait for it forever.
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_KILL_WAIT_SECONDS)
