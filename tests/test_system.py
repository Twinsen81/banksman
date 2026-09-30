import os
import subprocess
import sys

from banksman.system import Machine

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
        # Wait for the end without collecting the child, so that it stays a zombie.
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert Machine().running([child.pid]) == {}
    finally:
        child.kill()
        child.wait()


def test_no_pids_need_no_process_list():
    assert Machine().running([]) == {}

