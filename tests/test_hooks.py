import json
import os
import signal
import subprocess
import sys
import time

import pytest
from helpers import SRC, hook

from banksman import hooks
from banksman.config import Kind
from banksman.lease import READY, Lease
from banksman.system import Machine

# These tests run hooks that are new Python interpreters. Every process that a test signals is
# one that the test started, directly or through banksman.

LEASE = Lease(
    lease_id="lease-1",
    resource="emu-1",
    kind="emulator",
    state=READY,
    owner="/work/tree-a",
    owner_pid=None,
    owner_started=None,
    boot_id="boot-1",
    acquired_at=1_790_000_000.0,
    touched_at=1_790_000_000.0,
    touched=1000.0,
    boot_deadline=None,
    hard_deadline=1000.0 + 3 * 60 * 60,
    idle_timeout=20 * 60,
    owner_grace=5 * 60,
    drain_timeout=5 * 60,
)


def emulator(*command):
    return {"emulator": Kind("emulator", on_void=tuple(command) or None)}


def test_a_kind_without_on_void_has_nothing_to_end():
    assert hooks.take_back(emulator(), LEASE) is None


def test_a_kind_that_the_configuration_no_longer_declares_has_nothing_to_end():
    assert hooks.take_back({}, LEASE) is None


def test_an_on_void_hook_that_succeeds_frees_the_resource():
    assert hooks.take_back(emulator(*hook("pass")), LEASE) is None


def test_an_on_void_hook_that_fails_says_why():
    failure = hooks.take_back(emulator(*hook("raise SystemExit(3)")), LEASE)
    assert failure == "the on_void hook failed with exit status 3"


def test_an_on_acquire_hook_resets_the_instance_and_says_why_it_failed():
    assert hooks.reset({"emulator": Kind("emulator")}, LEASE) is None
    assert hooks.reset({}, LEASE) is None
    resetting = {"emulator": Kind("emulator", on_acquire=tuple(hook("pass")))}
    assert hooks.reset(resetting, LEASE) is None
    failing = {"emulator": Kind("emulator", on_acquire=tuple(hook("raise SystemExit(2)")))}
    assert hooks.reset(failing, LEASE) == "the on_acquire hook failed with exit status 2"


def test_a_hook_that_a_signal_ended_has_failed():
    code = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    assert hooks.run(hook(code), LEASE, timeout=30) == "was ended by signal 9"


def test_a_hook_that_cannot_start_has_failed(tmp_path):
    missing = str(tmp_path / "no-such-hook")
    assert hooks.run([missing], LEASE, timeout=30) == (
        f"could not start {missing}: No such file or directory"
    )


def test_a_hook_gets_the_resource_and_the_kind_a_fixed_directory_and_no_input(tmp_path):
    out = tmp_path / "out"
    code = (
        "import os, sys\n"
        "values = [os.environ['BANKSMAN_RESOURCE'], os.environ['BANKSMAN_KIND'], os.getcwd()]\n"
        "open(sys.argv[1], 'w').write(' '.join(values) + '|' + sys.stdin.read())\n"
    )
    assert hooks.run(hook(code, str(out)), LEASE, timeout=30) is None
    assert out.read_text() == "emu-1 emulator /|"


def test_the_output_of_a_hook_is_not_shown(capfd):
    code = "import sys; print('hook output'); print('hook output', file=sys.stderr)"
    assert hooks.run(hook(code), LEASE, timeout=30) is None
    out, err = capfd.readouterr()
    assert "hook output" not in out + err


def shell_hook(script, pid_file):
    """Return a hook that runs a shell script, which writes a pid to the file in one step."""
    return ["/bin/sh", "-c", script, "sh", str(pid_file)]


def test_a_hook_that_does_not_end_is_killed_with_the_programs_it_started(tmp_path):
    pid_file = tmp_path / "program"
    command = shell_hook('sleep 60 & echo $! > "$1.new" && mv "$1.new" "$1"; wait', pid_file)
    started = time.monotonic()
    assert hooks.run(command, LEASE, timeout=2) == "did not end within 2 s"
    assert time.monotonic() - started < 30
    _wait_until_ended(int(pid_file.read_text()))


RUNS_A_HOOK = """
import json, sys
from banksman import hooks
from banksman.lease import Lease
hooks.run(sys.argv[2:], Lease.from_json(json.loads(sys.argv[1])), timeout=60)
"""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGKILL])
def test_a_hook_ends_when_its_command_is_stopped(tmp_path, signum):
    # A caller can stop banksman while its hook runs, for example at the end of a timeout. The
    # next reaper would otherwise start a second copy of the hook.
    pid_file = tmp_path / "hook"
    command = shell_hook('echo $$ > "$1.new" && mv "$1.new" "$1" && exec sleep 60', pid_file)
    banksman = subprocess.Popen(
        [sys.executable, "-c", RUNS_A_HOOK, json.dumps(LEASE.to_json()), *command],
        env={**os.environ, "PYTHONPATH": SRC},
    )
    try:
        give_up = time.monotonic() + 30
        while not pid_file.exists():
            assert time.monotonic() < give_up, "the hook did not start"
            time.sleep(0.01)
        banksman.send_signal(signum)
        banksman.wait(timeout=30)
        _wait_until_ended(int(pid_file.read_text()))
    finally:
        banksman.kill()
        banksman.wait()


def test_a_command_that_is_interrupted_kills_its_hook(monkeypatch):
    processes = []
    real_wait = subprocess.Popen.wait

    def interrupted_wait(process, timeout=None):
        if not processes:
            processes.append(process)
            raise KeyboardInterrupt
        return real_wait(process, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_wait)
    with pytest.raises(KeyboardInterrupt):
        hooks.run(hook("import time; time.sleep(60)"), LEASE, timeout=60)
    (process,) = processes
    assert process.returncode == -signal.SIGKILL


def _wait_until_ended(pid):
    give_up = time.monotonic() + 10
    while time.monotonic() < give_up:
        if pid not in Machine().running([pid]):
            return
        time.sleep(0.01)
    raise AssertionError(f"process {pid} still runs")
