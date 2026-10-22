"""Who reads the notices of the reaper: a person sees every fact and the command for the
operator. An agent, a build, or a scheduled job sees only a short line about a lease of its own,
and the log keeps every fact."""

import json
import os
import subprocess
import sys

import pytest
from helpers import FakeSystem, drain, edit_lease, hook, write_config

from banksman import cli
from banksman.cli import main
from banksman.history import QUARANTINE, History
from banksman.lease import Holder, User
from banksman.store import Store
from banksman.system import Machine

OWNER = "/work/tree-a"
OTHER = "/work/tree-b"
OWN_LINE = (
    "banksman: your lease on phone-1 is quarantined, because its scripts still ran at the drain"
    " deadline. Only the operator can release it.\n"
)


@pytest.fixture
def agent(monkeypatch):
    """Let an agent process be above each command: a process that the test started."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(cli, "find_agent", lambda table, agents, pid, env: (process.pid, "claude"))
    yield process.pid
    process.kill()
    process.wait()


@pytest.fixture
def no_agent(monkeypatch):
    # The agent that runs the tests must not count as the agent of the commands.
    monkeypatch.setattr(cli, "find_agent", lambda table, agents, pid, env: (None, None))


def quarantine(state_dir, owner_pid=None, owner=OTHER):
    """Leave a lease on phone-1 whose script still runs after its drain deadline, so that the
    next command quarantines it. The script is the test process: stopping is off, so nothing
    signals it."""
    machine = Machine()
    Store(state_dir, machine).acquire("phone-1", "device", Holder(owner, owner_pid=owner_pid))
    me = User(os.getpid(), machine.running([os.getpid()])[os.getpid()])

    def change(data):
        drain(data, deadline=0)
        data["users"] = [me.to_json()]

    edit_lease(state_dir, "phone-1", change)


def quarantined_events(log_path):
    events, _ = History(log_path).read()
    return [event for event in events if event.event == QUARANTINE]


def test_a_person_sees_every_fact_and_the_command_for_the_operator(
    capsys, state_dir, log_path, person
):
    quarantine(state_dir)
    assert main(["status"]) == 0
    assert capsys.readouterr().err == (
        "banksman: quarantined phone-1: its scripts still run after the drain timeout:"
        f" pid {os.getpid()}. Stopping is off for kind device, so banksman sent them no signal."
        " When they have ended, run: banksman admin release --force --resource phone-1\n"
    )


def test_an_agent_sees_nothing_about_the_quarantine_of_another_holder(
    capsys, state_dir, log_path, agent
):
    quarantine(state_dir)
    assert main(["status"]) == 0
    assert capsys.readouterr().err == ""
    # The log keeps every fact, and status shows the quarantine.
    (event,) = quarantined_events(log_path)
    assert (event.resource, [each.pid for each in event.running]) == ("phone-1", [os.getpid()])
    assert event.stopping is False
    assert main(["status", "--json"]) == 0
    (shown,) = json.loads(capsys.readouterr().out)["resources"]
    assert shown["state"] == "quarantined"


def test_an_agent_sees_one_line_without_a_command_about_its_own_lease(capsys, state_dir, agent):
    quarantine(state_dir, owner_pid=agent, owner=OWNER)
    assert main(["status"]) == 0
    err = capsys.readouterr().err
    assert err == OWN_LINE
    assert "banksman admin" not in err


def test_an_agent_at_a_terminal_is_not_a_person(capsys, state_dir, agent, monkeypatch):
    monkeypatch.setattr(cli, "_stderr_is_terminal", lambda: True)
    quarantine(state_dir)
    assert main(["status"]) == 0
    assert capsys.readouterr().err == ""


def test_a_build_without_a_terminal_and_without_an_agent_is_not_a_person(
    capsys, state_dir, no_agent
):
    # A Gradle daemon can be older than the agent that uses it now, so banksman finds no agent.
    quarantine(state_dir)
    assert main(["status"]) == 0
    assert capsys.readouterr().err == ""


def test_a_lease_of_the_session_whose_agent_process_ended_is_its_own(
    capsys, state_dir, agent, monkeypatch
):
    # The agent restarted: its earlier process took the lease, and the session runs on in a new
    # process.
    earlier = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    holder = Holder(OWNER, owner_pid=earlier.pid, agent="claude", session="session-1")
    Store(state_dir, Machine()).acquire("phone-1", "device", holder)
    earlier.kill()
    earlier.wait()
    edit_lease(state_dir, "phone-1", lambda data: drain(data, deadline=0))
    edit_lease(state_dir, "phone-1", lambda data: data.update(users=[_me().to_json()]))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    assert main(["status"]) == 0
    assert capsys.readouterr().err == OWN_LINE


def _me() -> User:
    return User(os.getpid(), Machine().running([os.getpid()])[os.getpid()])


def test_a_lease_whose_owner_process_is_above_the_caller_is_its_own(capsys, state_dir, no_agent):
    # For example the build slot of a Gradle daemon, or the lease of banksman run --where.
    quarantine(state_dir, owner_pid=os.getpid(), owner=OWNER)
    assert main(["status"]) == 0
    assert capsys.readouterr().err == OWN_LINE


def test_a_failed_take_back_is_told_only_to_a_person_and_the_log_keeps_why(
    capsys, state_dir, config_path, log_path, agent
):
    write_config(
        config_path, f"[kinds.emulator]\non_void = {json.dumps(hook('raise SystemExit(3)'))}\n"
    )
    machine = Machine()
    Store(state_dir, machine).acquire("emu-1", "emulator", Holder(OTHER))
    Store(state_dir, machine).acquire("emu-2", "emulator", Holder(OWNER, owner_pid=agent))
    for resource in ("emu-1", "emu-2"):
        edit_lease(state_dir, resource, lambda data: data["awake"].update(hard_deadline=0))
    assert main(["status"]) == 0
    assert capsys.readouterr().err == (
        "banksman: your lease on emu-2 is quarantined, because banksman could not take back its"
        " instance. Only the operator can release it.\n"
    )
    problems = {event.resource: event.problem for event in quarantined_events(log_path)}
    assert problems == {
        resource: "its instance could not be taken back: the on_void hook failed with exit"
        " status 3"
        for resource in ("emu-1", "emu-2")
    }


def test_the_process_list_is_read_only_when_there_is_something_to_tell(
    capsys, state_dir, monkeypatch
):
    def unexpected(self):
        pytest.fail("the process list was read")

    monkeypatch.setattr(cli._Audience, "_find", unexpected)
    Store(state_dir, Machine()).acquire("phone-1", "device", Holder(OTHER))
    edit_lease(state_dir, "phone-1", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert capsys.readouterr().out == "phone-1: released, because the lease reached its hard cap\n"


def test_the_leases_of_a_caller_are_those_of_its_branch_and_of_its_agent_process():
    system = FakeSystem()
    system.spawn(300, program="/usr/local/bin/claude")
    system.spawn(400, parent=300, program="/bin/zsh")
    system.spawn(500, parent=400, program="/opt/python/bin/python3")
    system.spawn(600, program="/bin/zsh")
    table = system.process_table()
    found = cli._caller_of(table, ("claude",), 500, {})
    assert found.agent is True
    assert found.processes == {(300, "start-300"), (400, "start-400"), (500, "start-500")}
    # Without a known agent, the branch of the caller is still its own.
    alone = cli._caller_of(table, (), 500, {})
    assert alone.agent is False
    assert alone.processes == found.processes


@pytest.mark.parametrize(
    "command",
    [["status"], ["status", "--json"], ["acquire"], ["acquire", "--where", "kind=device"]],
)
def test_the_commands_for_agents_name_no_command_of_the_operator(capsys, command):
    main(command)
    captured = capsys.readouterr()
    assert "banksman admin" not in captured.out + captured.err
