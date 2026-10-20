"""The reap job: a job of the system that runs `banksman reap` every 5 minutes, so that the
timeouts of the kinds end emulators also while no other banksman command runs.

`banksman admin reap-agent` prints it: a launchd job on macOS, and a crontab line on other
systems. banksman does not install it, because `~/Library/LaunchAgents` and the crontab belong
to the operator. `banksman admin doctor` compares the saved job with what banksman prints. This
is the only module that runs `launchctl` and `crontab`, and only to read.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from xml.parsers.expat import ExpatError

from banksman.errors import BanksmanError

# Whether this system runs the jobs of a user with launchd; otherwise with cron.
LAUNCHD = sys.platform == "darwin"
LABEL = "banksman.reap"
INTERVAL_SECONDS = 5 * 60
# The place of the saved launchd job, relative to the home directory.
LAUNCH_AGENT = Path("Library/LaunchAgents") / f"{LABEL}.plist"
_LAUNCHCTL = "/bin/launchctl"
_CRON_TIMES = "*/5 * * * *"
_CRON_END = " >/dev/null"
# A line of a crontab that runs banksman reap, as this banksman prints it or not.
_CRON_REAP = re.compile(r"^[^#].*\bbanksman\b.*\breap\b")
_TIMEOUT_SECONDS = 10.0
_MAX_OUTPUT = 1024 * 1024
_HEAD = """\
<!--
  banksman reap job: runs banksman reap when it is loaded and then every 5 minutes, so that
  the free timeout and the unused timeout of a kind end emulators also while no other banksman
  command runs.

  Save it, and load it:

      mkdir -p ~/Library/LaunchAgents
      banksman admin reap-agent > ~/Library/LaunchAgents/banksman.reap.plist
      launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/banksman.reap.plist

  After an upgrade of banksman, unload it with launchctl bootout gui/$(id -u)/banksman.reap,
  save it again, and load it again.
-->
"""
_CRON_HEAD = (
    "# banksman reap job: runs banksman reap every 5 minutes. Add it with crontab -e, and replace"
    " it after an upgrade of banksman.\n"
)

# Runs a program with a timeout, and returns its exit status and what it printed, or raises
# JobError.
Run = Callable[[Sequence[str]], tuple[int, str]]


class JobError(BanksmanError):
    """launchctl or crontab did not give an answer."""


def program(argv0: str) -> tuple[str, ...]:
    """Return the command that runs this banksman, from `sys.argv[0]`.

    The path stays as the shell found it, with its symbolic links, so that the job still runs
    the same command after an upgrade that changes where a link points. With `python -m
    banksman`, it is this Python with `-m banksman`.
    """
    path = os.path.abspath(argv0)
    if os.path.basename(path) == "banksman" and os.path.isfile(path) and os.access(path, os.X_OK):
        return (path,)
    return (sys.executable, "-m", "banksman")


def printed(command: Sequence[str]) -> str:
    """Return the job for this system: a launchd job on macOS, a crontab line elsewhere."""
    if LAUNCHD:
        return launchd(command)
    return _CRON_HEAD + cron_line(command) + "\n"


def launchd(command: Sequence[str]) -> str:
    job = {
        "Label": LABEL,
        "ProgramArguments": [*command, "reap"],
        "StartInterval": INTERVAL_SECONDS,
        # Also at once when it is loaded, so that the operator sees it work without waiting for
        # the first interval.
        "RunAtLoad": True,
        # adb starts its server when it does not run, and launchd would end the server with the
        # job. Other programs use that server.
        "AbandonProcessGroup": True,
        "ProcessType": "Background",
    }
    text = plistlib.dumps(job, sort_keys=False).decode()
    declaration, doctype, rest = text.split("\n", 2)
    return f"{declaration}\n{doctype}\n{_HEAD}{rest}"


def cron_line(command: Sequence[str]) -> str:
    return f"{_CRON_TIMES} {shlex.join([*command, 'reap'])}{_CRON_END}"


def saved_launchd(text: str) -> tuple[str, ...] | None:
    """Return the command of banksman that a saved launchd job runs, or None when the rest of
    the job is not what this banksman prints."""
    try:
        job = plistlib.loads(text.encode())
    except (ExpatError, ValueError, TypeError):
        return None
    arguments = job.get("ProgramArguments") if isinstance(job, dict) else None
    if (
        not isinstance(arguments, list)
        or len(arguments) < 2
        or not all(isinstance(argument, str) for argument in arguments)
    ):
        return None
    command = tuple(arguments[:-1])
    return command if launchd(command) == text else None


def saved_cron(table: str) -> tuple[tuple[str, ...] | None, bool]:
    """Return the command of banksman that a line of a crontab runs as this banksman prints the
    line, or None; and whether a line runs banksman reap at all."""
    found = False
    for line in table.splitlines():
        if _CRON_REAP.match(line) is None:
            continue
        found = True
        if not (line.startswith(f"{_CRON_TIMES} ") and line.endswith(_CRON_END)):
            continue
        try:
            words = shlex.split(line[len(_CRON_TIMES) + 1 : -len(_CRON_END)])
        except ValueError:
            continue
        if len(words) >= 2 and cron_line(words[:-1]) == line:
            return tuple(words[:-1]), True
    return None, found


def loaded(run: Run | None = None) -> bool:
    """Whether launchd has loaded the job in the session of this user."""
    run = run_program if run is None else run
    status, _ = run([_LAUNCHCTL, "print", f"gui/{os.getuid()}/{LABEL}"])
    return status == 0


def crontab(run: Run | None = None) -> str:
    """Return the crontab of this user, or an empty text when the user has none."""
    run = run_program if run is None else run
    status, output = run(["crontab", "-l"])
    # crontab -l fails when the user has no crontab.
    return output if status == 0 else ""


def run_program(command: Sequence[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        where = "does not exist" if os.path.isabs(command[0]) else "is not on the PATH"
        raise JobError(f"{command[0]} {where}") from None
    except OSError as exc:
        raise JobError(f"cannot run {command[0]}: {exc.strerror}") from exc
    except subprocess.TimeoutExpired:
        raise JobError(f"{command[0]} did not end within {_TIMEOUT_SECONDS:g} s") from None
    return result.returncode, result.stdout[:_MAX_OUTPUT].decode(errors="replace")
