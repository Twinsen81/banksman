"""The small JSON files that a preset keeps outside the lease store, each under a lock of its own.

The remote preset keeps its catalogue and its reservations in them, and the emulator preset the
emulators that banksman knows. A record decides what the reaper ends, so a file that another user
can change, or that is not a regular file, is refused.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from banksman.errors import BanksmanError

MAX_FILE = 1024 * 1024
LOCK_WAIT_SECONDS = 10.0

Error = type[BanksmanError]


def directory(path: Path, error: Error) -> Path:
    """Create the directory of the records, and check that only this user can change it."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise error(f"cannot create {path}: {exc.strerror}") from exc
    check_owner(path, os.lstat(path), error, directory=True)
    return path


def check_owner(path: Path, info: os.stat_result, error: Error, *, directory: bool = False) -> None:
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok:
        raise error(f"{path} is not a {'directory' if directory else 'regular file'}")
    if info.st_uid != os.geteuid():
        raise error(f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise error(f"group or others can write to {path}")


def read(path: Path, error: Error) -> object | None:
    """Return the JSON value in a file, or None when the file does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise error(f"cannot open {path}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as file:
        check_owner(path, os.fstat(file.fileno()), error)
        data = file.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise error(f"{path} is larger than {MAX_FILE} bytes")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise error(f"{path} is not valid JSON") from None


def write(path: Path, document: Mapping[str, object], error: Error) -> None:
    directory(path.parent, error)
    data = (json.dumps(document, indent=2) + "\n").encode()
    try:
        fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    except OSError as exc:
        raise error(f"cannot write {path}: {exc.strerror}") from exc
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        # rename is atomic: a reader sees the old file or the new one.
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


@contextmanager
def locked(path: Path, error: Error) -> Iterator[None]:
    """Hold the lock file `path` while the block runs."""
    directory(path.parent, error)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise error(f"cannot open {path}: {exc.strerror}") from exc
    try:
        check_owner(path, os.fstat(fd), error)
        give_up = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # The lock is held for milliseconds, but not while its holder is stopped.
                if time.monotonic() >= give_up:
                    raise error(
                        f"another banksman process has held {path} for more than"
                        f" {LOCK_WAIT_SECONDS:g} s; it can be stopped or hung"
                    ) from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)
