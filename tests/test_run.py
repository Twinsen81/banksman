"""banksman run: a command in a process group of its own, fenced by its lease."""

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress

import pytest
from helpers import SRC, edit_lease, reap_in_child, write_config

from banksman import run as runs
from banksman.cli import main
from banksman.history import History
from banksman.lease import Holder, Timeouts
from banksman.store import Store
from banksman.system import Machine

OWNER = "/work/tree-a"
# A drain timeout of 2 seconds makes a run check its lease every half second.
FAST = Timeouts(drain_timeout=2)
# The command prints its process group, and the users that the lease file lists while it runs.
SHOW_USERS = """
import json, os, sys
lease = json.load(open(os.path.join(os.environ["BANKSMAN_STATE_DIR"], "phone-1.json")))
print(json.dumps({"pgid": os.getpgrp(), "users": lease["users"],
                  "env": [os.environ.get(name) for name in sys.argv[1:]]}))
"""
# The command says that it runs, and needs 2 seconds to end after SIGTERM.
SLOW_TO_STOP = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: (time.sleep(2), sys.exit(9)))
print(os.getpid(), flush=True)
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch):
    # The runs in this process must never take the terminal of the person who runs the tests.
    monkeypatch.setattr(runs, "_foreground_terminal", lambda: None)


def lease_on(state_dir, timeouts=FAST, owner_pid=None):
    return Store(state_dir, Machine()).acquire(
        "phone-1", "device", Holder(OWNER, owner_pid=owner_pid), timeouts=timeouts
    )


def banksman(*arguments, **options):
    """Start banksman in a process of its own, in a session without a terminal."""
    return subprocess.Popen(
        [sys.executable, "-m", "banksman", *arguments],
        env={**os.environ, "PYTHONPATH": SRC},
        start_new_session=True,
        text=True,
        **options,
    )


def finish(process, timeout=60):
    out, err = process.communicate(timeout=timeout)
    return process.returncode, out, err


def lease_file(state_dir):
    return json.loads((state_dir / "phone-1.json").read_text())


def runs_now(pid, started):
    return Machine().running([pid]).get(pid) == started


def test_the_group_is_registered_before_the_command_starts_and_left_after_it(state_dir):
    lease = lease_on(state_dir)
    names = ("BANKSMAN_RESOURCE", "BANKSMAN_KIND", "BANKSMAN_LEASE", "BANKSMAN_SERIAL")
    process = banksman(
        "run", "--lease", lease.lease_id, "--resource", "phone-1", "--",
        sys.executable, "-c", SHOW_USERS, *names,
        stdout=subprocess.PIPE,
    )
    status, out, _ = finish(process)
    assert status == 0
    shown = json.loads(out)
    # The group that banksman created for the command, led by another process than banksman.
    assert shown["pgid"] != process.pid
    assert [(user["pid"], user["pgid"]) for user in shown["users"]] == [
        (shown["pgid"], shown["pgid"])
    ]
    assert shown["env"] == ["phone-1", "device", lease.lease_id, None]
    after = lease_file(state_dir)
    assert (after["lease_id"], after["state"], after["users"]) == (lease.lease_id, "ready", [])


def test_run_passes_on_the_exit_status_of_a_command_that_fails(state_dir):
    lease = lease_on(state_dir)
    arguments = ["run", "--lease", lease.lease_id, "--resource", "phone-1", "--"]
    assert finish(banksman(*arguments, sys.executable, "-c", "raise SystemExit(7)"))[0] == 7
    # A command that a signal ends makes banksman end by the same signal.
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert finish(banksman(*arguments, sys.executable, "-c", killed))[0] == -signal.SIGTERM
    assert lease_file(state_dir)["users"] == []


def test_a_command_that_cannot_start_exits_127(state_dir):
    lease = lease_on(state_dir)
    process = banksman(
        "run", "--lease", lease.lease_id, "--resource", "phone-1", "--", "no-such-program-here",
        stderr=subprocess.PIPE,
    )
    status, _, err = finish(process)
    assert status == 127
    assert err.startswith("banksman: cannot run no-such-program-here: ")


def test_a_lease_that_is_not_held_runs_no_command(state_dir, tmp_path):
    lease_on(state_dir)
    marker = tmp_path / "ran"
    process = banksman(
        "run", "--lease", "an-earlier-lease", "--resource", "phone-1", "--",
        sys.executable, "-c", f"open({str(marker)!r}, 'w')",
        stderr=subprocess.PIPE,
    )
    status, _, err = finish(process)
    assert status == 3
    assert err == "banksman: the lease is lost: phone-1 has another lease now\n"
    assert not marker.exists()


def test_a_lost_lease_stops_the_group_and_frees_the_resource_after_the_group_has_ended(
    state_dir,
):
    lease = lease_on(state_dir)
    process = banksman(
        "run", "--lease", lease.lease_id, "--resource", "phone-1", "--",
        sys.executable, "-c", SLOW_TO_STOP,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    command = None
    try:
        pid = int(process.stdout.readline())
        command = (pid, Machine().running([pid])[pid])
        with Store(state_dir, Machine())._lock():
            edit_lease(state_dir, "phone-1", lambda data: data["awake"].update(hard_deadline=0))
        # The command still runs, so the lease drains, and the resource is not handed on.
        (reaped,) = reap_in_child(state_dir)
        assert (reaped["void_reason"], reaped["outcome"]) == ("hard_cap", "draining")
        assert [user["pgid"] for user in reaped["running"]] == [os.getpgid(pid)]
        assert runs_now(*command)
        status, _, err = finish(process)
        assert status == 3
        # The lease is void, or draining when the reaper was first.
        assert err.startswith("banksman: the lease is lost: the lease on phone-1 is ")
        assert err.endswith("; the command was stopped\n")
        assert not runs_now(*command)
        (reaped,) = reap_in_child(state_dir)
        assert reaped["outcome"] == "released"
    finally:
        _clean_up(process, command)


@pytest.mark.parametrize("exit_status, run_status", [(1, 3), (0, 0)])
def test_a_command_that_fails_after_its_lease_is_lost_reports_the_lost_lease(
    state_dir, exit_status, run_status
):
    # The usual check interval is longer than the command runs, so only the check after the
    # command sees that the lease is lost.
    lease = lease_on(state_dir, Timeouts())
    revoke = (
        "import json, os, sys\n"
        "path = os.path.join(os.environ['BANKSMAN_STATE_DIR'], 'phone-1.json')\n"
        "data = json.load(open(path)); data['awake']['hard_deadline'] = 0\n"
        "json.dump(data, open(path, 'w'))\n"
        f"sys.exit({exit_status})\n"
    )
    process = banksman(
        "run", "--lease", lease.lease_id, "--resource", "phone-1", "--",
        sys.executable, "-c", revoke,
        stderr=subprocess.PIPE,
    )
    status, _, err = finish(process)
    assert status == run_status
    assert ("the lease is lost" in err) == (run_status == 3)


def test_with_stopping_on_the_reaper_stops_the_group_and_the_run_says_that_the_lease_is_lost(
    state_dir, config_path
):
    write_config(config_path, "[kinds.device]\nstop = true\n")
    # This test process is the owner, so the reaper may signal the group that banksman created.
    lease = lease_on(state_dir, Timeouts(), owner_pid=os.getpid())
    process = banksman(
        "run", "--lease", lease.lease_id, "--resource", "phone-1", "--",
        sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(60)",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    command = None
    try:
        pid = int(process.stdout.readline())
        command = (pid, Machine().running([pid])[pid])
        with Store(state_dir, Machine())._lock():
            edit_lease(state_dir, "phone-1", lambda data: data["awake"].update(hard_deadline=0))
        (reaped,) = reap_in_child(state_dir)
        assert (reaped["void_reason"], reaped["outcome"]) == ("hard_cap", "released")
        status, _, err = finish(process)
        assert status == 3
        assert err.startswith("banksman: the lease is lost: ")
        assert not runs_now(*command)
    finally:
        _clean_up(process, command)


def test_a_command_that_ignores_sigterm_gets_sigkill(state_dir, monkeypatch, capfd):
    monkeypatch.setattr(runs, "TERM_WAIT_SECONDS", 0.5)
    store = Store(state_dir, Machine())
    # The lease reaches its hard cap during the run.
    lease = lease_on(state_dir, Timeouts(drain_timeout=2, hard_cap=1))
    deaf = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    started = time.monotonic()
    outcome = runs.run(
        store, "phone-1", lease.lease_id, [sys.executable, "-c", deaf], os.environ, print
    )
    assert outcome.lost is not None and outcome.running == ()
    assert time.monotonic() - started < 30
    assert lease_file(state_dir)["users"] == []


def test_run_waits_for_the_processes_that_the_command_left_in_its_group(state_dir, capfd):
    lease = lease_on(state_dir)
    store = Store(state_dir, Machine())
    leave_one = (
        "import subprocess, sys;"
        " subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1.5)'])"
    )
    warnings = []
    started = time.monotonic()
    outcome = runs.run(
        store, "phone-1", lease.lease_id, [sys.executable, "-c", leave_one], os.environ,
        warnings.append,
    )
    assert outcome.returncode == 0
    assert time.monotonic() - started >= 1.0
    (warning,) = warnings
    assert warning.startswith("the command has ended, but processes that it started still run")
    assert warning.endswith("because they can still use phone-1")
    assert lease_file(state_dir)["users"] == []


def test_the_command_ends_when_banksman_ends(state_dir, tmp_path):
    lease = lease_on(state_dir)
    process = banksman(
        "run", "--lease", lease.lease_id, "--resource", "phone-1", "--",
        sys.executable, "-c", SLOW_TO_STOP,
        stdout=subprocess.PIPE,
    )
    command = None
    try:
        pid = int(process.stdout.readline())
        command = (pid, Machine().running([pid])[pid])
        process.kill()
        process.wait()
        # SIGTERM from the leader, then 2 seconds until the command ends.
        give_up = time.monotonic() + 30
        while runs_now(*command) and time.monotonic() < give_up:
            time.sleep(0.1)
        assert not runs_now(*command)
    finally:
        _clean_up(process, command)


def test_run_where_leases_a_resource_for_the_command_and_gives_it_back(
    state_dir, config_path, log_path, tmp_path
):
    write_config(config_path, '[holder]\nagents = []\n[kinds.sdk]\ncount = 1\nowner_grace = "0s"\n')
    show = (
        "import json, os;"
        " lease = json.load(open(os.path.join(os.environ['BANKSMAN_STATE_DIR'], 'sdk-0.json')));"
        " print(json.dumps({'env': [os.environ['BANKSMAN_RESOURCE'], os.environ['BANKSMAN_KIND'],"
        " os.environ['BANKSMAN_LEASE']], 'lease': [lease['lease_id'], lease['owner_pid'],"
        " lease['purpose']]}));"
        " raise SystemExit(5)"
    )
    process = banksman(
        "run", "--where", "kind=sdk", "--for", "create an AVD", "--",
        sys.executable, "-c", show,
        stdout=subprocess.PIPE,
    )
    status, out, _ = finish(process)
    assert status == 5
    shown = json.loads(out)
    lease_id = shown["lease"][0]
    assert shown["env"] == ["sdk-0", "sdk", lease_id]
    # The lease belongs to the run: its owner process is banksman itself.
    assert shown["lease"][1:] == [process.pid, "create an AVD"]
    assert not (state_dir / "sdk-0.json").exists()
    events, _ = History(log_path).read()
    assert [(event.event, event.resource) for event in events] == [
        ("acquire", "sdk-0"),
        ("release", "sdk-0"),
    ]


def test_a_mutex_runs_one_command_at_a_time(state_dir, config_path, tmp_path):
    write_config(config_path, "[holder]\nagents = []\n[kinds.sdk]\ncount = 1\n")
    go = tmp_path / "go"
    marker = tmp_path / "second"
    wait_for_go = (
        "import os, sys, time\n"
        "print('holding', flush=True)\n"
        f"while not os.path.exists({str(go)!r}): time.sleep(0.05)\n"
    )
    first = banksman(
        "run", "--where", "kind=sdk", "--", sys.executable, "-c", wait_for_go,
        stdout=subprocess.PIPE,
    )
    try:
        assert first.stdout.readline() == "holding\n"
        second = banksman(
            "run", "--where", "kind=sdk", "--",
            sys.executable, "-c", f"open({str(marker)!r}, 'w')",
            stderr=subprocess.PIPE,
        )
        status, _, err = finish(second)
        assert status == 4
        assert err == "banksman: every matching resource is in use: sdk-0\n"
        assert not marker.exists()
    finally:
        go.touch()
        assert finish(first)[0] == 0
    assert not (state_dir / "sdk-0.json").exists()


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--", "true"], "run needs --lease and --resource, or --where, but not both"),
        (
            ["--lease", "x", "--where", "kind=sdk", "--", "true"],
            "run needs --lease and --resource, or --where, but not both",
        ),
        (["--lease", "x", "--", "true"], "run --lease needs --resource, the leased resource"),
        (
            ["--lease", "x", "--resource", "r", "--wait", "1m", "--", "true"],
            "run --wait needs --where",
        ),
        (
            ["--where", "kind=sdk", "--resource", "r", "--", "true"],
            "run --resource needs --lease; with --where, banksman chooses it",
        ),
        (
            ["--lease", "x", "--resource", "r"],
            "give the command after --, for example: banksman run --lease <id> --resource <name>"
            " -- ./run-tests",
        ),
    ],
)
def test_run_refuses_options_that_do_not_go_together(capsys, arguments, message):
    assert main(["run", *arguments]) == 1
    assert capsys.readouterr().err == f"banksman: {message}\n"


def test_the_check_interval_follows_the_timeouts_of_the_lease(state_dir):
    store = Store(state_dir, Machine())
    usual = store.acquire("phone-1", "device", Holder(OWNER))
    short = store.acquire("phone-2", "device", Holder(OWNER), timeouts=FAST)
    assert runs.check_interval(usual) == runs.CHECK_SECONDS
    assert runs.check_interval(short) == 0.5


# The command reads a line from the terminal, and then waits for Ctrl-C. The terminal echoes a
# command line that a test types, so the word "ready" is not in the command.
READ_A_LINE = (
    "import time; print('re' 'ady', flush=True); print('got', input(), flush=True);"
    " time.sleep(60)"
)


def at_terminal(lease, code, *, input_from_terminal=True):
    """Start banksman run as the leader of a new session with a terminal, as a login shell
    would start it. Return its pid and the other side of the terminal."""
    env = {**os.environ, "PYTHONPATH": SRC}
    arguments = ["run", "--lease", lease.lease_id, "--resource", "phone-1", "--"]
    pid, terminal = pty.fork()
    if pid == 0:
        try:
            if not input_from_terminal:
                os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
            os.execve(
                sys.executable,
                [sys.executable, "-m", "banksman", *arguments, sys.executable, "-c", code],
                env,
            )
        finally:
            os._exit(127)
    return pid, terminal


def test_at_a_terminal_the_command_reads_input_and_gets_ctrl_c(state_dir):
    pid, terminal = at_terminal(lease_on(state_dir), READ_A_LINE)
    try:
        assert "ready" in _read_until(terminal, "ready")
        os.write(terminal, b"hello\n")
        assert "got hello" in _read_until(terminal, "got hello")
        os.write(terminal, b"\x03")
        _read_until(terminal, "never printed", timeout=5)
        _, status = os.waitpid(pid, 0)
        # The command ended by Ctrl-C, and banksman ends by the same signal.
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGINT
        assert lease_file(state_dir)["users"] == []
    finally:
        _end_terminal_test(pid, terminal)


def test_a_command_that_exits_with_130_after_ctrl_c_ends_banksman_by_sigint(state_dir):
    # As a JVM does: it catches Ctrl-C, and exits with 130.
    code = (
        "import sys, time\n"
        "print('re' 'ady', flush=True)\n"
        "try:\n"
        "    time.sleep(60)\n"
        "except KeyboardInterrupt:\n"
        "    sys.exit(130)\n"
    )
    pid, terminal = at_terminal(lease_on(state_dir), code)
    try:
        assert "ready" in _read_until(terminal, "ready")
        os.write(terminal, b"\x03")
        _read_until(terminal, "never printed", timeout=5)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGINT
    finally:
        _end_terminal_test(pid, terminal)


def test_a_command_whose_input_is_not_the_terminal_does_not_get_it(state_dir):
    code = (
        "import os; tty = os.open('/dev/tty', os.O_RDONLY);"
        " print('foreground', os.tcgetpgrp(tty) == os.getpgrp(), flush=True)"
    )
    pid, terminal = at_terminal(lease_on(state_dir), code, input_from_terminal=False)
    try:
        assert "foreground False" in _read_until(terminal, "foreground ")
        _read_until(terminal, "never printed", timeout=5)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    finally:
        _end_terminal_test(pid, terminal)


def test_a_signal_that_the_caller_ignores_stays_ignored(state_dir):
    lease = lease_on(state_dir)
    show = (
        "import signal;"
        " print(signal.getsignal(signal.SIGHUP) == signal.SIG_IGN,"
        " signal.getsignal(signal.SIGTERM) == signal.SIG_IGN)"
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "banksman", "run", "--lease", lease.lease_id,
         "--resource", "phone-1", "--", sys.executable, "-c", show],
        env={**os.environ, "PYTHONPATH": SRC},
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
        # As nohup does.
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
    )
    status, out, _ = finish(process)
    assert (status, out) == (0, "True False\n")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash for job control")
def test_ctrl_z_stops_the_job_and_fg_continues_the_command(state_dir):
    lease = lease_on(state_dir)
    env = {**os.environ, "PYTHONPATH": SRC, "PS1": "$ "}
    pid, terminal = pty.fork()
    if pid == 0:
        try:
            os.execve(shutil.which("bash"), ["bash", "--norc", "--noprofile", "-i"], env)
        finally:
            os._exit(127)
    try:
        _read_until(terminal, "$ ")
        command = (
            f"{sys.executable} -m banksman run --lease {lease.lease_id} --resource phone-1 --"
            f" {sys.executable} -c \"{READ_A_LINE}\"\n"
        )
        os.write(terminal, command.encode())
        assert "ready" in _read_until(terminal, "ready")
        os.write(terminal, b"\x1a")
        assert "Stopped" in _read_until(terminal, "Stopped")
        _read_until(terminal, "$ ")
        os.write(terminal, b"fg\n")
        # The command has the terminal again, so it gets the line.
        time.sleep(1)
        os.write(terminal, b"hello\n")
        assert "got hello" in _read_until(terminal, "got hello")
        os.write(terminal, b"\x03")
        _read_until(terminal, "$ ")
        os.write(terminal, b"echo status=$?\n")
        assert "status=130" in _read_until(terminal, "status=130")
        assert lease_file(state_dir)["users"] == []
    finally:
        _end_terminal_test(pid, terminal)


def _end_terminal_test(pid, terminal):
    with suppress(ChildProcessError):
        if os.waitpid(pid, os.WNOHANG) == (0, 0):
            os.kill(pid, signal.SIGKILL)
    # The terminal closes first: on macOS, a process that ends waits until what it wrote to its
    # terminal is read.
    os.close(terminal)
    with suppress(ChildProcessError):
        os.waitpid(pid, 0)


def _read_until(terminal, text, timeout=30.0):
    seen = b""
    give_up = time.monotonic() + timeout
    while text.encode() not in seen and time.monotonic() < give_up:
        if select.select([terminal], [], [], 0.1)[0]:
            try:
                data = os.read(terminal, 4096)
            except OSError:
                break
            if not data:
                break
            seen += data
    return seen.decode(errors="replace")


def _clean_up(process, command):
    # Only processes that this test started: banksman leads its own session, and the command
    # is signalled only while it is the same process.
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    if command is not None and runs_now(*command):
        with suppress(ProcessLookupError):
            os.kill(command[0], signal.SIGKILL)

