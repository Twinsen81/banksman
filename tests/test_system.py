import os
import subprocess
import sys
import time

import pytest

from banksman import system
from banksman.system import Machine, MachineError

# These tests read the real machine: its boot id, its clock, and processes that the tests
# start themselves. They change nothing.


def test_the_boot_id_is_stable():
    boot_id = Machine().boot_id()
    assert boot_id
    assert Machine().boot_id() == boot_id


def test_the_clock_does_not_go_back():
    machine = Machine()
    first = machine.clock()
    assert machine.clock() >= first


def test_a_running_process_has_a_stable_start_time():
    running = Machine().running([os.getpid()])
    assert running[os.getpid()]
    assert Machine().running([os.getpid()]) == running


def test_an_ended_process_is_not_running():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert Machine().running([child.pid, os.getpid()]).keys() == {os.getpid()}


def test_a_zombie_is_not_running():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert child.pid in Machine().running([child.pid])
        child.kill()
        # The child stays a zombie until the test collects it with wait().
        _wait_until_zombie(child.pid)
        assert Machine().running([child.pid]) == {}
    finally:
        child.kill()
        child.wait()


def test_no_pids_need_no_process_list():
    assert Machine().running([]) == {}


def test_a_process_list_without_banksman_itself_is_an_error(monkeypatch):
    # For example inside an agent's sandbox: every owner would look ended.
    monkeypatch.setattr(system, "_running_from_ps", lambda pids: {})
    monkeypatch.setattr(system, "_running_from_proc", lambda pids: {})
    with pytest.raises(MachineError, match="outside the sandbox"):
        Machine().running([os.getpid() + 1])


def test_a_ps_that_cannot_start_is_an_error(monkeypatch):
    def blocked(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted", "ps")

    monkeypatch.setattr(system.subprocess, "run", blocked)
    with pytest.raises(MachineError, match="cannot run ps.*outside the sandbox"):
        system._running_from_ps([os.getpid()])


def test_a_failing_ps_is_an_error(monkeypatch):
    def failing(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="ps: not permitted")

    monkeypatch.setattr(system.subprocess, "run", failing)
    with pytest.raises(MachineError, match="exit status 1.*outside the sandbox"):
        system._running_from_ps([os.getpid()])


def test_the_answer_holds_only_the_asked_pids():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert Machine().running([child.pid]) == {}


def test_a_pid_that_cannot_exist_is_not_running():
    assert Machine().running([1_000_000]) == {}


def _wait_until_zombie(pid):
    give_up = time.monotonic() + 10
    while time.monotonic() < give_up:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        if state.startswith("Z"):
            return
        time.sleep(0.01)
    raise AssertionError(f"process {pid} did not become a zombie")
