"""Run a hook, and end it together with the banksman process that started it.

banksman runs this file as a program, in the process group of a new session, with a pipe on
its standard input: `supervisor.py PROGRAM [ARGUMENT ...]`. When banksman ends before the
hook, also when it is killed, the system closes that pipe, and this program kills the whole
group. Without it, a hook would outlive a reaper that its caller stopped, and the next reaper
would start a second copy of the hook.

The hook gets the standard output of this program, so that banksman decides whether to read
it. When the hook cannot start, this program writes the reason on its standard error.

While the operator traces the commands, banksman gives `--tail FD` before the program, for the
standard error of the hook, or `--tail-all FD`, for its standard output too. The hook then
writes them to a pipe, and this program keeps only the end of what it reads, in the file of
that descriptor. A program that the hook leaves running can write for hours, so its output must
never fill a file, and it must never get SIGPIPE either, which would end it: when the hook ends
and such a program still has the pipe, a small process reads the pipe and discards what it reads
until that program closes it.

It runs in isolated mode, so it uses the standard library only.
"""

import contextlib
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import termios
import threading

COULD_NOT_START = 127
# A program of a hook is an absolute path, so it is never one of these options.
TAIL = "--tail"
TAIL_ALL = "--tail-all"
# How much of the end of the output the file of the tail keeps.
TAIL_BYTES = 4096
# How often the supervisor looks whether a hook that says nothing has ended.
_POLL_SECONDS = 0.05
_DRAIN = "import os\nwhile os.read(0, 65536):\n    pass\n"


def _end_with_banksman() -> None:
    sys.stdin.buffer.read()
    os.killpg(0, signal.SIGKILL)


class _Tail:
    """The end of what the hook says, at the start of a file that never grows past TAIL_BYTES."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.kept = b""

    def add(self, chunk: bytes) -> None:
        self.kept = (self.kept + chunk)[-TAIL_BYTES:]
        # Written at once: banksman kills a hook that passes its timeout with this program.
        with contextlib.suppress(OSError):
            os.pwrite(self.fd, self.kept, 0)


def _keep_tail(hook: subprocess.Popen[bytes], reading: int, tail: _Tail) -> bool:
    """Read the pipe of the hook until the hook ends, and keep the end of it. Return whether a
    program that the hook started can still write to the pipe."""
    while hook.poll() is None:
        if not select.select([reading], [], [], _POLL_SECONDS)[0]:
            continue
        chunk = os.read(reading, 65536)
        if not chunk:
            # Nothing writes to the pipe any more.
            hook.wait()
            return False
        tail.add(chunk)
    # What the hook wrote before it ended is in the pipe now. A program that it left running can
    # write more after it, which is not the output of the hook.
    left = struct.unpack("i", fcntl.ioctl(reading, termios.FIONREAD, b"\0" * 4))[0]
    while left > 0:
        chunk = os.read(reading, min(left, 65536))
        if not chunk:
            return False
        tail.add(chunk)
        left -= len(chunk)
    os.set_blocking(reading, False)
    try:
        return os.read(reading, 1) != b""
    except BlockingIOError:
        return True
    finally:
        # The drain shares this open file, and must wait for data.
        os.set_blocking(reading, True)


def main() -> None:
    command = sys.argv[1:]
    tail = None
    both = False
    if command[:1] in ([TAIL], [TAIL_ALL]):
        both = command[0] == TAIL_ALL
        tail, command = _Tail(int(command[1])), command[2:]
    reading = writing = None
    if tail is not None:
        reading, writing = os.pipe()
    try:
        hook = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=writing if both else None,
            stderr=subprocess.DEVNULL if writing is None else writing,
        )
    except OSError as exc:
        sys.stderr.write(exc.strerror or str(exc))
        sys.stderr.flush()
        os._exit(COULD_NOT_START)
    threading.Thread(target=_end_with_banksman, daemon=True).start()
    if reading is not None and writing is not None and tail is not None:
        os.close(writing)
        try:
            left = _keep_tail(hook, reading, tail)
        except OSError:
            left = True
        if left:
            # A program that the hook started still has the pipe. A process in the group of the
            # hook reads it until that program ends, so that the group signals of banksman reach
            # it too.
            with contextlib.suppress(OSError):
                subprocess.Popen(
                    [sys.executable, "-I", "-c", _DRAIN],
                    stdin=reading,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
    status = hook.wait()
    if status < 0:
        # End with the same signal, so that banksman can name it.
        with contextlib.suppress(OSError, ValueError):
            signal.signal(-status, signal.SIG_DFL)
        os.kill(os.getpid(), -status)
        os._exit(128 - status)
    os._exit(status)


if __name__ == "__main__":
    main()
