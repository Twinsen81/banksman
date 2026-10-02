import os
import signal
import subprocess
import sys
import time

import pytest

from banksman import system
from banksman.system import Machine, MachineError, Process

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


def test_the_process_table_shows_parent_group_and_start_time():
    machine = Machine()
    table = machine.process_table()
    own = os.getpid()
    assert table[own] == Process(own, os.getppid(), os.getpgrp(), machine.running([own])[own])
    assert table[os.getppid()].pid == os.getppid()


def test_a_child_in_a_new_session_leads_its_own_group():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        process = Machine().process_table()[child.pid]
        assert (process.ppid, process.pgid) == (os.getpid(), child.pid)
    finally:
        child.kill()
        child.wait()


def test_a_zombie_is_not_in_the_process_table():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        child.kill()
        _wait_until_zombie(child.pid)
        assert child.pid not in Machine().process_table()
    finally:
        child.kill()
        child.wait()


def test_a_process_table_without_banksman_itself_is_an_error(monkeypatch):
    monkeypatch.setattr(system, "_table_from_ps", lambda: {})
    monkeypatch.setattr(system, "_table_from_proc", lambda: {})
    with pytest.raises(MachineError, match="outside the sandbox"):
        Machine().process_table()


@pytest.mark.parametrize("pid", [-1, 0, 1])
def test_banksman_never_signals_the_special_process_ids(pid):
    # Signal 0 only checks; it would change nothing even if the guard failed.
    for send in (Machine().signal, Machine().signal_group):
        with pytest.raises(ValueError, match="never signals"):
            send(pid, 0)


def test_a_signal_to_a_process_that_has_ended_is_not_an_error():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    Machine().signal(child.pid, 0)


def test_a_group_signal_reaches_every_process_in_the_group(tmp_path):
    # The test's own child leads a new group, and starts a second process in it.
    code = (
        "import subprocess, sys, time\n"
        "second = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(second.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        second = int(leader.stdout.readline())
        Machine().signal_group(leader.pid, signal.SIGKILL)
        assert leader.wait(timeout=30) == -signal.SIGKILL
        _wait_until_ended(second)
    finally:
        leader.kill()
        leader.wait()
        leader.stdout.close()


def _wait_until_ended(pid):
    give_up = time.monotonic() + 10
    while time.monotonic() < give_up:
        if pid not in Machine().running([pid]):
            return
        time.sleep(0.01)
    raise AssertionError(f"process {pid} still runs")


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
