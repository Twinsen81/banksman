"""Run a hook, and end it together with the banksman process that started it.

banksman runs this file as a program, in the process group of a new session, with a pipe on
its standard input: `supervisor.py PROGRAM [ARGUMENT ...]`. When banksman ends before the
hook, also when it is killed, the system closes that pipe, and this program kills the whole
group. Without it, a hook would outlive a reaper that its caller stopped, and the next reaper
would start a second copy of the hook.

The hook gets the standard output of this program, so that banksman decides whether to read
it. When the hook cannot start, this program writes the reason on its standard error.

It runs in isolated mode, so it uses the standard library only.
"""

import contextlib
import os
import signal
import subprocess
import sys
import threading

COULD_NOT_START = 127


def _end_with_banksman() -> None:
    sys.stdin.buffer.read()
    os.killpg(0, signal.SIGKILL)


def main() -> None:
    try:
        hook = subprocess.Popen(
            sys.argv[1:],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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
