"""Lead the process group of a command that `banksman run` runs.

banksman runs this file as a program, as the leader of a new process group, with one end of a
socket to banksman: `leader.py FD PROGRAM [ARGUMENT ...]`. The program starts the command in its
group only after banksman has registered the group with the lease, and tells banksman how the
command ended. Then it waits until banksman is done, so that the id of the group cannot name
another group while banksman can still signal it.

When the socket closes before banksman is done, because banksman has ended, also when it was
killed, this program ends the group: SIGTERM, and SIGKILL when the command still runs after 10
seconds. So the command never runs without the check loop of its banksman.

It runs in isolated mode, so it uses the standard library only.
"""

import contextlib
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import threading

# What banksman sends: the command may start, and banksman is done with the group.
GO = b"g"
DONE = b"d"
# The exit status of a command that cannot start, as a shell gives it.
NOT_FOUND = 127
CANNOT_RUN = 126
TERM_WAIT_SECONDS = 10.0


def _ignore(signum, frame):
    pass


def _receive(channel):
    # Linux reports the end of banksman as an error when banksman left a report unread.
    try:
        return channel.recv(1)
    except OSError:
        return b""


def _send(channel, message):
    with contextlib.suppress(OSError):
        channel.sendall(json.dumps(message).encode() + b"\n")


def _report(channel, process):
    _send(channel, {"returncode": process.wait()})


def _end_group(process):
    os.killpg(0, signal.SIGTERM)
    # A stopped process acts on SIGTERM only when it continues.
    os.killpg(0, signal.SIGCONT)
    if process is not None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=TERM_WAIT_SECONDS)
    os.killpg(0, signal.SIGKILL)


def main():
    channel = socket.socket(fileno=int(sys.argv[1]))
    # The terminal and banksman send these signals to the whole group. The command decides what
    # they do, and this program ends only with banksman. A handler, not SIG_IGN: a handled
    # signal has its default action again in the command. A signal that the caller ignores, as
    # nohup does, stays ignored, also in the command.
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
        if signal.getsignal(signum) != signal.SIG_IGN:
            signal.signal(signum, _ignore)
    if _receive(channel) != GO:
        # banksman has not registered the group, so the command must not start.
        os._exit(0)
    process = None
    try:
        try:
            process = subprocess.Popen(sys.argv[2:])
        except OSError as exc:
            status = NOT_FOUND if exc.errno == errno.ENOENT else CANNOT_RUN
            _send(channel, {"returncode": status, "error": exc.strerror or str(exc)})
        else:
            threading.Thread(target=_report, args=(channel, process), daemon=True).start()
        if _receive(channel) == DONE:
            os._exit(0)
    finally:
        # banksman has ended, or this program failed: the command must not run on alone.
        _end_group(process)


if __name__ == "__main__":
    main()

