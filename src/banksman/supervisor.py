"""Run a hook, and end it together with the banksman process that started it.

banksman runs this file as a program, in the process group of a new session, with a pipe on
its standard input: `supervisor.py PROGRAM [ARGUMENT ...]`. When banksman ends before the
hook, also when it is killed, the system closes that pipe, and this program kills the whole
group. Without it, a hook would outlive a reaper that its caller stopped, and the next reaper
would start a second copy of the hook.

The hook gets the standard output of this program, so that banksman decides whether to read
it. When the hook cannot start, this program writes the reason on its standard error. While the
operator traces the commands, banksman gives `--stderr FD` before the program, and the hook
writes its standard error to that descriptor, so that the reason stays apart from it.

It runs in isolated mode, so it uses the standard library only.
"""

import contextlib
import os
import signal
import subprocess
import sys
import threading

COULD_NOT_START = 127
# A program of a hook is an absolute path, so it is never this option.
KEEP_STDERR = "--stderr"


def _end_with_banksman() -> None:
    sys.stdin.buffer.read()
    os.killpg(0, signal.SIGKILL)


def main() -> None:
    command = sys.argv[1:]
    errors = subprocess.DEVNULL
    if command[:1] == [KEEP_STDERR]:
        errors, command = int(command[1]), command[2:]
    try:
        hook = subprocess.Popen(command, stdin=subprocess.DEVNULL, stderr=errors)
    except OSError as exc:
        sys.stderr.write(exc.strerror or str(exc))
        sys.stderr.flush()
        os._exit(COULD_NOT_START)
    threading.Thread(target=_end_with_banksman, daemon=True).start()
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
