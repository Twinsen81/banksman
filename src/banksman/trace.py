"""The trace: an opt-in record of what each banksman command saw and decided, for the operator.

The log keeps the history of the leases. The trace keeps the actions of banksman: one line for
each command and for each adb call through the guard, with the arguments, the caller, the exit
status, the duration, what the command printed on standard error, and the steps on the way, such
as what discovery found, why a request skipped a resource, the void checks of the reaper, the
runs of the hooks, and the decision of the adb guard. A module reports a step with `step`, and
the command line writes the line at the end, so no module needs a logger of its own.

The trace holds what the log keeps out on purpose: full adb command lines, which can carry
tokens, and the purposes and labels of other agents. So it is off unless the operator turns it
on, the file belongs to the user with mode 0600, and no command reads it. It is written without
the lock of the lease store, because the adb guard writes a line before every adb call, and a
trace that cannot be written never changes what a command does.

The adb guard imports this module, so it imports nothing slow.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from banksman import __version__
from banksman.errors import BanksmanError
from banksman.sanitize import clean

if TYPE_CHECKING:
    from banksman.lease import Holder

# The version of the lines of the trace. It is not the output schema: no command prints them.
TRACE_SCHEMA = 1
# The file starts again at this size, and the earlier file is kept next to it.
MAX_TRACE_BYTES = 10 * 1024 * 1024
# A command that waits, such as acquire --wait or watch, takes a step for each look. A line keeps
# the first steps and the last ones: the context, and the decision.
MAX_STEPS = 100
MAX_LINES = 100
MAX_TEXT = 4096
# What a line keeps of the end of the output of a hook.
OUTPUT_BYTES = 4096
OUTPUT_LINES = 20
# How long the start of a new file waits for the lock of the lease store. The adb guard writes a
# line for every adb call, and must not wait for a holder of the lock that is stopped or hung.
LOCK_WAIT = 0.5


class TraceError(BanksmanError):
    """The trace file cannot be used safely."""


class _Bounded:
    """The first and the last items of a sequence that can be long, and how many items between
    them are left out."""

    def __init__(self, limit: int) -> None:
        self._first: list[Any] = []
        self._kept = limit // 2
        self._last: deque[Any] = deque(maxlen=limit - self._kept)
        self.left_out = 0

    def add(self, item: Any) -> None:
        if len(self._first) < self._kept:
            self._first.append(item)
            return
        if len(self._last) == self._last.maxlen:
            self.left_out += 1
        self._last.append(item)

    def items(self) -> list[Any]:
        return [*self._first, *self._last]


class _Stderr:
    """Standard error, which also keeps each line that the command prints there."""

    def __init__(self, stream: TextIO, lines: _Bounded) -> None:
        self.stream = stream
        self._lines = lines
        self._partial = ""

    def write(self, text: str) -> int:
        written = self.stream.write(text)
        *complete, partial = (self._partial + text).split("\n")
        for line in complete:
            self._lines.add(_text(line))
        self._partial = partial[: MAX_TEXT + 1]
        return written

    def end(self) -> None:
        if self._partial:
            self._lines.add(_text(self._partial))
            self._partial = ""

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class Trace:
    def __init__(self, command: Sequence[str], path: Path) -> None:
        self.command = list(command)
        self.path = path
        self.at = time.time()
        self.started = time.monotonic()
        self.caller: dict[str, object] | None = None
        # The pid of the acquire whose start this process continues.
        self.continues: int | None = None
        self.steps = _Bounded(MAX_STEPS)
        self.stderr = _Bounded(MAX_LINES)
        self.stream: _Stderr | None = None

    def line(self, status: int | None, error: str | None) -> dict[str, object]:
        return {
            "schema": TRACE_SCHEMA,
            "version": __version__,
            "at": self.at,
            "pid": os.getpid(),
            "continues": self.continues,
            "command": self.command,
            "caller": self.caller,
            "status": status,
            "error": error,
            "seconds": round(time.monotonic() - self.started, 3),
            "stderr": self.stderr.items(),
            "stderr_left_out": self.stderr.left_out,
            "steps": self.steps.items(),
            "steps_left_out": self.steps.left_out,
        }


_current: Trace | None = None


def start(command: Sequence[str], path: Path | None = None) -> None:
    """Start the trace of this process: keep the steps and the lines of standard error until
    `finish` writes them."""
    global _current
    cancel()
    _current = Trace(command, default_path() if path is None else path)
    _keep_stderr(_current)


def restart(continues: int) -> None:
    """Start a new line in a process that a command forked to continue its work, such as the
    start of acquire --no-wait. The steps of the command stay in its own line."""
    global _current
    earlier = _current
    if earlier is None:
        return
    cancel()
    _current = Trace(earlier.command, earlier.path)
    _current.caller = earlier.caller
    _current.continues = continues
    _keep_stderr(_current)


def active() -> bool:
    return _current is not None


def step(name: str, **fields: object) -> None:
    """Add a step to the line of this command, when the trace is on."""
    if _current is not None:
        _current.steps.add({"step": name, **fields})


def caller(holder: Holder) -> None:
    """Record the caller of this command as the holder of a lease would be recorded."""
    if _current is not None:
        _current.caller = {
            "owner": holder.owner,
            "owner_pid": holder.owner_pid,
            "agent": holder.agent,
            "issue": holder.issue,
            "session": holder.session,
        }


def has_caller() -> bool:
    return _current is not None and _current.caller is not None


def finish(status: int | None, error: str | None = None) -> None:
    """Write the line of this command, once. A trace that cannot be written does not fail the
    command: the command says so on standard error."""
    trace = _current
    if trace is None:
        return
    cancel()
    try:
        append(trace.path, trace.line(status, error))
    except (BanksmanError, OSError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        print(
            clean(f"banksman: warning: this command is not in the trace: {reason}"),
            file=sys.stderr,
        )


def cancel() -> None:
    """Stop the trace of this process without writing it."""
    global _current
    trace, _current = _current, None
    if trace is None or trace.stream is None:
        return
    trace.stream.end()
    if sys.stderr is trace.stream:
        sys.stderr = trace.stream.stream


def error_text(exc: BaseException) -> str:
    if isinstance(exc, BanksmanError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def append(path: Path, line: dict[str, object], *, max_bytes: int = MAX_TRACE_BYTES) -> None:
    """Add a line at the end of the trace with one write, without the lock of the lease store.

    Every text is cleaned of control characters, as everywhere in banksman. Only the start of a
    new file takes the lock, so that one process at a time moves the file.
    """
    data = (json.dumps(_cleaned(line), separators=(",", ":")) + "\n").encode()
    _ensure_directory(path.parent)
    fd = _open(path)
    try:
        size = os.fstat(fd).st_size
        if size > 0 and size + len(data) > max_bytes:
            os.close(fd)
            fd = -1
            _start_again(path, len(data), max_bytes)
            fd = _open(path)
        os.write(fd, data)
    finally:
        if fd >= 0:
            os.close(fd)


def earlier_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.1")


def default_path() -> Path:
    # Not at the top: the store imports this module.
    from banksman.store import default_trace_path

    return default_trace_path()


def output_tail(data: bytes) -> list[str]:
    """Return the last lines of what a program printed, each as a text of the trace."""
    text = data[-OUTPUT_BYTES:].decode(errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    return [_text(line) for line in lines[-OUTPUT_LINES:]]


def _keep_stderr(trace: Trace) -> None:
    trace.stream = _Stderr(sys.stderr, trace.stderr)
    sys.stderr = trace.stream  # type: ignore[assignment]


def _start_again(path: Path, needed: int, max_bytes: int) -> None:
    from banksman.store import default_state_dir, log_lock

    try:
        with log_lock(default_state_dir(), LOCK_WAIT):
            try:
                size = os.lstat(path).st_size
            except FileNotFoundError:
                return
            # Another process can have started the file again while this one waited.
            if size > 0 and size + needed > max_bytes:
                os.replace(path, earlier_path(path))
    except (BanksmanError, OSError):
        # The line goes into the full file, and a later command starts it again.
        pass


def _open(path: Path) -> int:
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
    except OSError as exc:
        raise TraceError(f"cannot open {path}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TraceError(f"{path} is not a regular file")
        _check_owner(path, info)
        # The trace holds full adb command lines and the purposes of other agents, so only the
        # user may read it, also when a person or a program changed its mode.
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _ensure_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise TraceError(f"cannot create {directory}: {exc.strerror}") from exc
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise TraceError(f"{directory} is not a directory")
    _check_owner(directory, info)


def _check_owner(path: Path, info: os.stat_result) -> None:
    if info.st_uid != os.geteuid():
        raise TraceError(f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise TraceError(f"group or others can write to {path}")


def _text(text: str) -> str:
    text = clean(text)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 3] + "..."


def _cleaned(value: object) -> object:
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        return {_text(str(key)): _cleaned(each) for key, each in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cleaned(each) for each in value]
    return value
