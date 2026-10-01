import os
import signal
from dataclasses import replace

import pytest
from helpers import FakeSystem

from banksman import fencing
from banksman.errors import BanksmanError
from banksman.fencing import Refused
from banksman.lease import READY, Lease, User
from banksman.system import Process

# A process tree like the ones that agent apps have (parent, process group):
#   100 the app      (100)
#   101 the agent    (100)  the agent shares its group with the app
#   200 a command    (200)  the agent runs each command in its own group
#   201 a script     (200)
#   300 its work     (300)  the group that the script created for its work
#   301 a work child (300)
TREE = {
    100: (1, 100),
    101: (100, 100),
    200: (101, 200),
    201: (200, 200),
    300: (201, 300),
    301: (300, 300),
}

LEASE = Lease(
    lease_id="lease-1",
    resource="phone-1",
    kind="device",
    state=READY,
    owner="/work/tree-a",
    owner_pid=101,
    owner_started="start-101",
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
SCRIPT = User(pid=201, started="start-201", pgid=300, leader_started="start-300")


def table(**changes):
    tree = {**TREE, **{int(pid.lstrip("p")): value for pid, value in changes.items()}}
    processes = {
        pid: Process(pid, ppid, pgid, f"start-{pid}")
        for pid, value in tree.items()
        if value is not None
        for ppid, pgid in [value]
    }
    # The reaper is this process, outside the tree unless a test puts it in.
    processes.setdefault(os.getpid(), Process(os.getpid(), 1, os.getpid(), "start-self"))
    return processes


def test_a_script_registers_its_own_process():
    assert fencing.register(table(), LEASE, 201, None) == User(pid=201, started="start-201")


def test_a_script_registers_the_group_that_it_created_for_its_work():
    assert fencing.register(table(), LEASE, 201, 300) == SCRIPT


def test_a_script_that_leads_its_own_group_can_register_it():
    tree = table(p201=(200, 201), p300=(201, 201), p301=(300, 201))
    user = fencing.register(tree, LEASE, 201, 201)
    assert (user.pgid, user.leader_started) == (201, "start-201")


def test_the_group_of_the_agents_command_is_refused():
    # Its leader is the shell that the agent started, a process above the script.
    with pytest.raises(Refused, match="has process 200 in it, which process 201 did not start"):
        fencing.register(table(), LEASE, 201, 200)


def test_a_group_with_a_fake_agent_in_it_is_refused():
    # The script runs in the group of the agent and the app, as in some agent apps.
    tree = table(p201=(101, 100))
    with pytest.raises(Refused, match="process group 100 has process 100 in it"):
        fencing.register(tree, LEASE, 201, 100)


def test_a_group_with_a_process_that_the_script_did_not_start_is_refused():
    tree = table(p302=(200, 300))
    with pytest.raises(Refused, match="has process 302 in it"):
        fencing.register(tree, LEASE, 201, 300)


def test_a_group_whose_leader_has_ended_is_refused():
    tree = table(p300=None, p301=(1, 300))
    with pytest.raises(Refused, match="leader of process group 300 has ended"):
        fencing.register(tree, LEASE, 201, 300)


def test_a_group_that_does_not_exist_is_refused():
    with pytest.raises(Refused, match="no process group 999"):
        fencing.register(table(), LEASE, 201, 999)


def test_a_process_that_does_not_run_cannot_register():
    with pytest.raises(BanksmanError, match="not running"):
        fencing.register(table(), LEASE, 999, None)


@pytest.mark.parametrize("pid", [101, 100])
def test_the_owner_and_the_processes_above_it_cannot_register(pid):
    with pytest.raises(Refused, match="owner of the lease or a process above it"):
        fencing.register(table(), LEASE, pid, None)


def test_a_script_runs_while_its_process_or_its_group_runs():
    assert fencing.runs(SCRIPT, table())
    assert fencing.runs(SCRIPT, table(p201=None))
    # The leader has ended, but the group still has a process.
    assert fencing.runs(SCRIPT, table(p201=None, p300=None, p301=(1, 300)))
    assert not fencing.runs(SCRIPT, table(p201=None, p300=None, p301=None))


def test_a_reused_pid_is_not_the_script():
    tree = table(p300=None, p301=None)
    tree[201] = Process(201, 1, 201, "start-of-another-process")
    assert not fencing.runs(SCRIPT, tree)


def test_a_group_whose_leader_pid_is_reused_has_ended():
    # The system reuses the id of a group only after the group has ended, so the processes
    # with this group id now belong to a new group.
    tree = table(p201=None, p301=(1, 300))
    tree[300] = Process(300, 1, 300, "start-of-another-process")
    assert fencing.processes_of(SCRIPT, tree) == set()


def targets(tree, lease=LEASE, user=SCRIPT):
    return [(target.pid, target.group) for target in fencing.signal_targets(tree, lease, user)]


def test_the_reaper_signals_the_script_and_its_group():
    assert targets(table()) == [(201, False), (300, True)]


def test_without_a_running_owner_process_nothing_is_signalled():
    assert targets(table(p101=None)) == []
    unknown = replace(LEASE, owner_pid=None, owner_started=None)
    assert targets(table(), unknown) == []
    restarted = table()
    restarted[101] = Process(101, 100, 100, "start-of-another-process")
    assert targets(restarted) == []


def test_a_script_that_the_owner_did_not_start_is_not_signalled():
    # For example a script that registered the agent itself, or a process of another agent.
    other = User(pid=501, started="start-501", pgid=501, leader_started="start-501")
    tree = table(p500=(1, 500), p501=(500, 501))
    assert targets(tree, user=other) == []
    agent = User(pid=101, started="start-101")
    assert targets(table(), user=agent) == []


def test_the_reaper_and_the_processes_above_it_are_never_signalled():
    # The script runs `banksman check`, which reaps.
    tree = table()
    tree[os.getpid()] = Process(os.getpid(), 201, 200, "start-self")
    assert targets(tree) == [(300, True)]
    # The work runs banksman: its group holds the reaper.
    tree[os.getpid()] = Process(os.getpid(), 301, 300, "start-self")
    assert targets(tree) == []


def test_a_group_that_holds_the_owner_is_never_signalled():
    tree = table(p101=(100, 300))
    assert targets(tree) == [(201, False)]


def test_a_group_whose_leader_has_ended_is_not_signalled():
    # Its id could belong to a new group.
    tree = table(p300=None, p301=(1, 300))
    assert targets(tree) == [(201, False)]


def fake(system):
    for pid, (ppid, pgid) in TREE.items():
        system.spawn(pid, parent=ppid, group=pgid)
    return system


def test_stop_ends_the_scripts_with_sigterm():
    system = fake(FakeSystem())
    fencing.stop(replace(LEASE, users=(SCRIPT,)), system)
    assert system.signals == [("pid", 201, signal.SIGTERM), ("group", 300, signal.SIGTERM)]
    assert not fencing.runs(SCRIPT, system.process_table())


def test_stop_sends_sigkill_to_a_script_that_does_not_end():
    system = fake(FakeSystem())
    system.ignores = {201: {signal.SIGTERM}, 300: {signal.SIGTERM}, 301: {signal.SIGTERM}}
    started = system.now
    fencing.stop(replace(LEASE, users=(SCRIPT,)), system)
    assert system.signals == [
        ("pid", 201, signal.SIGTERM),
        ("group", 300, signal.SIGTERM),
        ("pid", 201, signal.SIGKILL),
        ("group", 300, signal.SIGKILL),
    ]
    assert system.now - started >= fencing.TERM_WAIT_SECONDS
    assert not fencing.runs(SCRIPT, system.process_table())


def test_stop_sends_sigkill_to_a_work_group_whose_script_ended():
    # SIGTERM ends the script, so its work belongs to process 1 now, not to the agent.
    system = fake(FakeSystem())
    system.ignores = {300: {signal.SIGTERM}, 301: {signal.SIGTERM}}
    fencing.stop(replace(LEASE, users=(SCRIPT,)), system)
    assert system.signals[-1] == ("group", 300, signal.SIGKILL)
    assert not fencing.runs(SCRIPT, system.process_table())


def test_stop_sends_sigkill_to_the_processes_of_a_group_whose_leader_ended():
    # The group id could name a new group, so only the processes that were in it get SIGKILL.
    system = fake(FakeSystem())
    system.ignores = {301: {signal.SIGTERM}}
    system.spawn(302, parent=1, group=302)
    fencing.stop(replace(LEASE, users=(SCRIPT,)), system)
    assert system.signals[-1] == ("pid", 301, signal.SIGKILL)
    assert not fencing.runs(SCRIPT, system.process_table())
    assert 302 in system.process_table()


def test_stop_gives_up_on_a_process_that_does_not_end():
    system = fake(FakeSystem())
    system.ignores = {201: {signal.SIGTERM, signal.SIGKILL}}
    fencing.stop(replace(LEASE, users=(User(201, "start-201"),)), system)
    assert [entry[2] for entry in system.signals] == [signal.SIGTERM, signal.SIGKILL]
    assert 201 in system.process_table()


def test_stop_without_an_owner_process_sends_nothing_and_does_not_wait():
    system = fake(FakeSystem())
    system.end(101)
    started = system.now
    fencing.stop(replace(LEASE, users=(SCRIPT,)), system)
    assert (system.signals, system.now) == ([], started)
