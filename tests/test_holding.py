"""Named holdings: a label that the caller gives a holding, and `held`, which lists them."""

import json
import os
import re
import shlex
import subprocess
import sys
import time

import pytest
from helpers import SRC, age_lease, edit_lease, hook, table_rows, write_config

from banksman import __version__, cli, console, hooks, identity
from banksman.cli import main
from banksman.config import parse_duration
from banksman.errors import BanksmanError
from banksman.history import GUARD, NOT_LEASED, OTHER_HOLDER, REFUSED, WARNED, Event, History
from banksman.inventory import Decisions, Inventory, save_inventory
from banksman.lease import Holder, Session, Timeouts
from banksman.store import Choice, Need, NotHeld, Store
from banksman.system import Machine

OWNER = "/work/tree-a"
PRINT_ARGUMENT = "import sys; print(sys.argv[1])"
BENCH = [
    {"name": "qa_phone", "facts": {"form": "phone", "running": True, "serial": "emulator-5554"}},
    {"name": "qa_phone2", "facts": {"form": "phone", "running": True, "serial": "emulator-5556"}},
    {"name": "qa_tablet", "facts": {"form": "tablet", "running": False}},
]


# The lease store.


@pytest.fixture
def agents(system):
    # Two agent processes, such as two sessions of an agent.
    system.spawn(101)
    system.spawn(102)
    return 101, 102


def grant(store, holder, label, *resources, **options):
    """Grant one resource for each name in `resources`, and return the leases by resource."""
    needs = [Need((Choice(name, "emulator"),)) for name in resources]
    granted = store.grant(needs, holder, label=label, **options)
    assert granted is not None
    return {each.lease.resource: each for each in granted}


def test_a_label_names_a_holding_that_its_agent_process_keeps(store, agents):
    holder = Holder(OWNER, owner_pid=101)
    first = grant(store, holder, "login-tests", "emu-1")["emu-1"]
    assert (first.kept, first.lease.label) == (False, "login-tests")
    again = grant(store, holder, "login-tests", "emu-1")["emu-1"]
    assert (again.kept, again.lease.lease_id) == (True, first.lease.lease_id)
    assert store.labelled(101, "login-tests") == {"emu-1": again.lease}


def test_two_labels_in_one_agent_process_are_two_holdings(store, agents):
    holder = Holder(OWNER, owner_pid=101)
    choices = (Choice("emu-1", "emulator"), Choice("emu-2", "emulator"))
    (login,) = store.grant([Need(choices)], holder, label="login-tests")
    (search,) = store.grant([Need(choices)], holder, label="search-tests")
    assert (login.lease.resource, search.lease.resource) == ("emu-1", "emu-2")
    assert not search.kept
    assert login.lease.holding != search.lease.holding
    assert list(store.labelled(101, "search-tests")) == ["emu-2"]


def test_the_same_label_in_two_agent_processes_is_two_holdings(store, agents):
    choices = (Choice("emu-1", "emulator"), Choice("emu-2", "emulator"))
    (first,) = store.grant([Need(choices)], Holder(OWNER, owner_pid=101), label="tests")
    (second,) = store.grant([Need(choices)], Holder(OWNER, owner_pid=102), label="tests")
    assert (first.lease.resource, second.lease.resource) == ("emu-1", "emu-2")
    assert not second.kept
    assert first.lease.holding != second.lease.holding
    # Neither agent process holds a resource of the other one under the label.
    assert list(store.labelled(101, "tests")) == ["emu-1"]
    assert list(store.labelled(102, "tests")) == ["emu-2"]


def test_new_leases_join_a_labelled_holding_and_get_its_label(store, agents):
    holder = Holder(OWNER, owner_pid=101)
    first = grant(store, holder, "tests", "emu-1")["emu-1"].lease
    both = grant(store, holder, "tests", "emu-1", "emu-2")
    assert (both["emu-1"].kept, both["emu-2"].kept) == (True, False)
    joined = both["emu-2"].lease
    assert (joined.holding, joined.label) == (first.holding, "tests")
    assert joined.lease_id != first.lease_id
    # A request that keeps the holding by a lease id gives its new leases the label too.
    (kept,) = store.grant(
        [Need((Choice("emu-3", "emulator"),))], holder, keep=first.lease_id
    )
    assert (kept.lease.holding, kept.lease.label) == (first.holding, "tests")


def test_a_label_of_a_holding_that_ended_starts_a_new_holding(store, agents):
    holder = Holder(OWNER, owner_pid=101)
    first = grant(store, holder, "tests", "emu-1")["emu-1"].lease
    store.release("emu-1", first.lease_id)
    again = grant(store, holder, "tests", "emu-1")["emu-1"]
    assert (again.kept, again.lease.label) == (False, "tests")
    assert again.lease.holding != first.holding


def test_a_label_needs_an_owner_process(store):
    with pytest.raises(BanksmanError, match="a label needs an owner process"):
        grant(store, Holder(OWNER), "tests", "emu-1")
    with pytest.raises(BanksmanError, match="not a valid label"):
        grant(store, Holder(OWNER, owner_pid=os.getpid()), "two words", "emu-1")


def test_a_paid_lease_is_kept_by_its_label_without_a_new_acknowledgement(store, agents):
    holder = Holder(OWNER, owner_pid=101)
    need = [Need((Choice("remote-1", "remote", paid=True),))]
    (first,) = store.grant(need, holder, label="tests", paid=True)
    (kept,) = store.grant(need, holder, label="tests")
    assert (kept.kept, kept.lease.lease_id) == (True, first.lease.lease_id)
    # Another label is a new request, and needs the acknowledgement.
    store.release("remote-1", first.lease.lease_id)
    assert store.grant(need, holder, label="other") is None


def test_two_holdings_with_one_label_are_refused_instead_of_chosen(store, agents):
    # The agent restarted as process 102, started a holding with the label, and then moved the
    # earlier holding with the same label to itself with a touch.
    earlier = grant(store, Holder(OWNER, owner_pid=101), "tests", "emu-1")["emu-1"].lease
    grant(store, Holder(OWNER, owner_pid=102), "tests", "emu-2")
    store.touch_holding(earlier.lease_id, 102)
    with pytest.raises(BanksmanError, match="process 102 has 2 holdings with the label tests"):
        store.labelled(102, "tests")


def test_held_by_lists_the_held_leases_of_one_owner_process(store, system, agents):
    grant(store, Holder(OWNER, owner_pid=101), "tests", "emu-1", "emu-2")
    grant(store, Holder(OWNER, owner_pid=101), None, "emu-3")
    grant(store, Holder(OWNER, owner_pid=102), "tests", "emu-4")
    assert sorted(lease.resource for lease in store.held_by(101)) == ["emu-1", "emu-2", "emu-3"]
    # A void lease is not held, also before the reaper takes it back.
    system.advance(4 * 60 * 60)
    assert store.held_by(101) == []


# A session that continues in a new agent process.

SESSION = Session("claude", "session-1")


def in_session(owner_pid, session=SESSION):
    return Holder(OWNER, owner_pid=owner_pid, agent=session.agent, session=session.session_id)


def test_a_new_agent_process_of_the_session_continues_its_holding(store, system, agents):
    # The app restarted the agent: process 101 ended, and the session runs on in process 102.
    first = grant(store, in_session(101), "tests", "emu-1", "emu-2")
    system.end(101)
    assert sorted(store.labelled(102, "tests", SESSION)) == ["emu-1", "emu-2"]
    assert sorted(lease.resource for lease in store.held_by(102, SESSION)) == ["emu-1", "emu-2"]
    again = grant(store, in_session(102), "tests", "emu-1", session=SESSION)["emu-1"]
    assert (again.kept, again.lease.lease_id) == (True, first["emu-1"].lease.lease_id)
    # The grant recorded the new agent process in every lease of the holding, so they live with
    # it now, also without the session.
    held = store.labelled(102, "tests")
    assert {lease.owner_pid for lease in held.values()} == {102}
    system.advance(Timeouts().owner_grace + 1)
    assert sorted(store.labelled(102, "tests")) == ["emu-1", "emu-2"]


def test_a_request_that_keeps_a_holding_records_its_owner_process_in_every_lease(
    store, system, agents
):
    # An agent without a session: after a restart, it keeps its holding by a lease id.
    first = grant(store, Holder(OWNER, owner_pid=101), None, "emu-1", "emu-2")
    system.end(101)
    lease_id = first["emu-1"].lease.lease_id
    need = [Need((Choice("emu-1", "emulator"),))]
    (kept,) = store.grant(need, Holder(OWNER, owner_pid=102), keep=lease_id)
    assert kept.kept
    assert {lease.owner_pid for lease in store.holding(lease_id).values()} == {102}


@pytest.mark.parametrize(
    "session",
    [
        Session("claude", "session-2"),
        # Another agent with the same id.
        Session("codex", "session-1"),
        None,
    ],
)
def test_only_the_same_session_of_the_same_agent_continues_a_holding(
    store, system, agents, session
):
    grant(store, in_session(101), "tests", "emu-1")
    system.end(101)
    assert store.labelled(102, "tests", session) == {}
    assert store.held_by(102, session) == []
    started = grant(store, Holder(OWNER, owner_pid=102), "tests", "emu-2", session=session)
    assert started["emu-2"].kept is False


def test_a_lease_of_the_session_whose_owner_process_runs_stays_with_that_process(
    store, system, agents
):
    # Such as the lease of banksman run --where, or of a build that names its own process: the
    # lease records the session of the agent, and it ends with its own owner process.
    grant(store, in_session(101), "tests", "emu-1")
    assert store.labelled(102, "tests", SESSION) == {}
    assert store.held_by(102, SESSION) == []
    assert store.release_all(102, session=SESSION) == []
    assert list(store.labelled(101, "tests")) == ["emu-1"]


def test_release_all_gives_back_the_leases_that_the_session_continues(store, system, agents):
    grant(store, in_session(101), "tests", "emu-1")
    grant(store, in_session(101), None, "emu-2")
    system.end(101)
    assert store.release_all(102) == []
    released = store.release_all(102, session=SESSION)
    assert sorted(lease.resource for lease, _ in released) == ["emu-1", "emu-2"]
    assert store.held_by(102, SESSION) == []


def test_a_session_does_not_continue_a_lease_after_the_owner_grace(store, system, agents):
    grant(store, in_session(101), "tests", "emu-1")
    system.end(101)
    system.advance(Timeouts().owner_grace)
    assert store.labelled(102, "tests", SESSION) == {}
    store.reap()
    assert grant(store, in_session(102), "tests", "emu-1", session=SESSION)["emu-1"].kept is False


# The commands.


def configure(config_path):
    found = json.dumps({"schema": 1, "instances": BENCH})
    write_config(
        config_path,
        # No agent process: the tests name their agent process with --owner-pid.
        "[holder]\nagents = []\n"
        f"[kinds.emulator]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\n"
        "[kinds.build]\ncount = 1\n",
    )
    save_inventory(
        Inventory(
            kinds={"emulator": Decisions(allowed=("qa_phone", "qa_phone2", "qa_tablet"))}
        )
    )


@pytest.fixture
def other_agent():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield process.pid
    process.kill()
    process.wait()


ME = ["--owner-pid", str(os.getpid())]


def acquire(capsys, *arguments):
    status = main(["acquire", *arguments])
    captured = capsys.readouterr()
    values = dict(line.split("=", 1) for line in captured.out.splitlines())
    return status, values, captured.err


def leases():
    snapshot = Store(cli.default_state_dir(), Machine()).snapshot()
    return {lease.resource: lease for lease in snapshot.leases}


def test_acquire_keeps_the_holding_of_a_label(capsys, config_path):
    configure(config_path)
    status, first, _ = acquire(capsys, "--holding", "login-tests", "--where", "form=phone", *ME)
    assert (status, first["RESOURCE"], first["KEPT"]) == (0, "qa_phone", "false")
    assert leases()["qa_phone"].label == "login-tests"
    # The agent lost the lease id, and runs the same command again.
    status, again, _ = acquire(capsys, "--holding", "login-tests", "--where", "form=phone", *ME)
    assert (status, again["LEASE"], again["KEPT"]) == (0, first["LEASE"], "true")


def test_two_labels_of_one_agent_process_get_two_devices(capsys, config_path):
    configure(config_path)
    _, login, _ = acquire(capsys, "--holding", "login-tests", "--where", "form=phone", *ME)
    _, search, _ = acquire(capsys, "--holding", "search-tests", "--where", "form=phone", *ME)
    assert (login["RESOURCE"], search["RESOURCE"]) == ("qa_phone", "qa_phone2")
    assert search["KEPT"] == "false"
    status, _, err = acquire(capsys, "--holding", "third", "--where", "form=phone", *ME)
    assert status == cli.EXIT_BUSY
    assert "every matching resource is in use" in err


def test_one_label_of_two_agent_processes_gets_two_devices(capsys, config_path, other_agent):
    configure(config_path)
    _, mine, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    other = ["--owner-pid", str(other_agent)]
    _, theirs, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *other)
    assert (mine["RESOURCE"], theirs["RESOURCE"]) == ("qa_phone", "qa_phone2")
    assert theirs["KEPT"] == "false"


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        (["--holding", "tests", "--lease", "x"], "acquire takes --lease or --holding, not both"),
        (["--holding", "tests"], "--holding needs the agent process, and none was found"),
    ],
)
def test_acquire_refuses_a_label_that_cannot_work(capsys, config_path, arguments, problem):
    configure(config_path)
    status, _, err = acquire(capsys, *arguments, "--where", "form=phone")
    assert status == 1
    assert problem in err
    assert leases() == {}


@pytest.mark.parametrize("label", ["two words", "-tests", "x" * 65, "tests\x1b[2J"])
def test_a_label_that_is_not_valid_is_a_usage_error(capsys, label):
    for command in (["acquire"], ["touch"], ["release"], ["run"]):
        with pytest.raises(SystemExit) as exc:
            main([*command, f"--holding={label}"])
        assert exc.value.code == 2
        assert "a label starts with a letter or a digit" in capsys.readouterr().err


def test_touch_and_release_take_a_label(capsys, config_path, state_dir):
    configure(config_path)
    phones = ["--as", "a", "--where", "form=phone", "--as", "b", "--where", "form=phone"]
    acquire(capsys, "--holding", "tests", *phones, *ME)
    for resource in ("qa_phone", "qa_phone2"):
        age_lease(state_dir, resource, 15 * 60)
    assert main(["touch", "--holding", "tests", *ME]) == 0
    for resource in ("qa_phone", "qa_phone2"):
        age_lease(state_dir, resource, 15 * 60)
    assert main(["reap"]) == 0
    assert sorted(leases()) == ["qa_phone", "qa_phone2"]
    capsys.readouterr()
    assert main(["release", "--holding", "tests", "--resource", "qa_phone2", *ME]) == 0
    assert capsys.readouterr().out == "Released qa_phone2: it is free.\n"
    assert main(["release", "--holding", "tests", *ME]) == 0
    assert capsys.readouterr().out == "Released qa_phone: it is free.\n"
    # The holding has ended, so a script that still names it has lost its lease.
    assert main(["touch", "--holding", "tests", *ME]) == 3
    assert capsys.readouterr().err == (
        f"banksman: the lease is lost: process {os.getpid()} has no held holding with the label"
        " tests\n"
    )


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        (["touch"], "touch needs --lease, the id of the lease, or --holding, its label"),
        (["touch", "--lease", "x", "--holding", "tests"], "touch needs --lease"),
        (["release", "--holding", "tests", "--lease", "x"], "takes --lease or --holding, not"),
        (["release", "--all", "--holding", "tests"], "release --all takes no --lease, no --hold"),
        (["release", "--holding", "tests"], "--holding needs the agent process"),
    ],
)
def test_touch_and_release_refuse_options_that_do_not_go_together(
    capsys, config_path, command, problem
):
    configure(config_path)
    assert main(command) == 1
    assert problem in capsys.readouterr().err


def test_held_lists_the_holdings_of_the_agent_process(capsys, config_path, other_agent):
    configure(config_path)
    acquire(capsys, "--holding", "tests", "--as", "a", "--where", "form=tablet", "--as", "b", *ME)
    _, unlabelled, _ = acquire(capsys, "--where", "form=phone", *ME)
    acquire(capsys, "--holding", "tests", "--where", "form=phone", "--owner-pid", str(other_agent))
    held = leases()
    shown, calls = held_text(capsys, *ME)
    rows = table_rows(shown)
    assert [(row["LABEL"], row["RESOURCE"], row["SERIAL"]) for row in rows] == [
        ("tests", "build-0", "-"),
        ("tests", "qa_tablet", "-"),
        ("-", "qa_phone", "emulator-5554"),
    ]
    assert rows[2]["LEASE"] == unlabelled["LEASE"]
    assert calls == NO_CALLS
    assert main(["held", "--json", *ME]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["owner_pid"] == os.getpid()
    assert shown["leases"][0] == {
        "label": "tests",
        "lease_id": held["build-0"].lease_id,
        "resource": "build-0",
        "kind": "build",
        "state": "ready",
        "serial": None,
        "handle": None,
    }
    assert shown["guard"] == []
    assert main(["release", "--all", *ME]) == 0
    capsys.readouterr()
    assert held_text(capsys, *ME) == (f"Process {os.getpid()} holds no lease.", NO_CALLS)


def sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


@pytest.mark.parametrize(
    "command",
    [
        ["acquire", "--holding", "tests", "--where", "form=phone"],
        ["run", "--holding", "tests", "--", sys.executable, "-c", "pass"],
        ["wait", "--holding", "tests"],
        ["touch", "--holding", "tests"],
    ],
)
def test_a_new_agent_process_of_the_session_takes_its_holding_over(
    capsys, config_path, monkeypatch, command
):
    configure(config_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    processes = []

    def agent_process():
        process = sleeper()
        processes.append(process)

        def found(table, agents, pid, env):
            return process.pid, "claude"

        monkeypatch.setattr(cli, "find_agent", found)
        monkeypatch.setattr(identity, "find_agent", found)
        return process

    try:
        earlier = agent_process()
        _, first, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone")
        assert (first["RESOURCE"], first["KEPT"]) == ("qa_phone", "false")
        # The app ends the agent process, and runs the session on in a new one.
        earlier.kill()
        earlier.wait()
        now = agent_process()
        shown, _ = held_text(capsys)
        assert [(row["LABEL"], row["RESOURCE"]) for row in table_rows(shown)] == [
            ("tests", "qa_phone")
        ]
        # banksman does not know the session of a process that the caller names.
        assert held_text(capsys, "--owner-pid", str(now.pid))[0] == (
            f"Process {now.pid} holds no lease."
        )
        assert main(command) == 0
        if command[0] == "acquire":
            assert "KEPT=true" in capsys.readouterr().out.splitlines()
        # The lease lives with the new agent process now.
        held = leases()["qa_phone"]
        assert (held.lease_id, held.owner_pid) == (first["LEASE"], now.pid)
        capsys.readouterr()
        assert main(["release", "--holding", "tests"]) == 0
        assert capsys.readouterr().out == "Released qa_phone: it is free.\n"
    finally:
        for process in processes:
            process.kill()
            process.wait()


def test_held_without_an_agent_process_is_an_error(capsys, config_path):
    configure(config_path)
    assert main(["held"]) == 1
    assert "held needs the agent process" in capsys.readouterr().err


# The adb calls that the adb guard refused or warned about.

NO_CALLS = f"No refused or warned adb calls of process {os.getpid()} in the last 15 minutes.\n"


def held_text(capsys, *arguments):
    """Return what held printed: the leases, and the adb calls that the adb guard refused."""
    assert main(["held", *arguments]) == 0
    shown, calls = capsys.readouterr().out.split("\n\n")
    return shown, calls


def guard_call(log_path, ago, resource, *, owner_pid=None, **fields):
    """Add a guard event of this process as the agent process to the log, `ago` seconds ago."""
    event = Event(
        time.time() - ago,
        GUARD,
        resource,
        owner=OWNER,
        owner_pid=os.getpid() if owner_pid is None else owner_pid,
        agent="claude",
        **{"action": REFUSED, "reason": OTHER_HOLDER, "command": "shell", **fields},
    )
    History(log_path).append(event)


def test_held_shows_the_adb_calls_of_its_agent_process_that_the_guard_refused(
    capsys, config_path, log_path, other_agent
):
    configure(config_path)
    them = ["--owner-pid", str(other_agent)]
    _, theirs, _ = acquire(capsys, "--where", "form=phone", "--for", "verify the layout", *them)
    device = {"serial": "emulator-5554", "kind": "emulator", "state": "ready"}
    guard_call(log_path, 120, "qa_phone", lease_id=theirs["LEASE"], **device)
    guard_call(log_path, 60, "qa_phone", lease_id=theirs["LEASE"], command="install", **device)
    guard_call(
        log_path, 30, "emulator-5556", serial="emulator-5556", action=WARNED, reason=NOT_LEASED
    )
    guard_call(log_path, 20, "qa_tablet", lease_id="0123456789abcdef", state="ready")
    # A call before the window, and a call of another agent process.
    guard_call(log_path, 16 * 60, "qa_phone2", serial="emulator-5556", reason=NOT_LEASED)
    guard_call(log_path, 10, "qa_phone2", owner_pid=other_agent, reason=NOT_LEASED)
    shown, calls = held_text(capsys, *ME)
    assert shown == f"Process {os.getpid()} holds no lease."
    note, table = calls.split("\n", 1)
    assert note.startswith("The adb guard refused or warned about these adb calls of process")
    assert "do not run it again" in note
    rows = table_rows(table)
    shown = [(row["COUNT"], row["RESOURCE"], row["DETAILS"], row["OTHER HOLDER"]) for row in rows]
    assert shown == [
        (
            "2",
            "qa_phone",
            "refused adb install, shell on emulator-5554: another holder leases the device, and"
            " the lease is ready",
            # The holder as status shows it, with the purpose, which is untrusted text.
            console.holder_label(leases()["qa_phone"]),
        ),
        (
            "1",
            "emulator-5556",
            "warned adb shell on emulator-5556: no lease of the caller has the device; a strict"
            " adb guard refuses the call",
            "-",
        ),
        (
            "1",
            "qa_tablet",
            "refused adb shell: another holder leases the device, and the lease is ready",
            "its lease has ended",
        ),
    ]
    assert "verify the layout" in rows[0]["OTHER HOLDER"]
    assert re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}", rows[0]["LAST"])
    assert main(["held", "--json", *ME]) == 0
    printed = capsys.readouterr().out
    assert "verify the layout" not in printed
    groups = json.loads(printed)["guard"]
    shown = [(each["count"], each["action"], each["resource"], each["commands"]) for each in groups]
    assert shown == [
        (2, REFUSED, "qa_phone", ["install", "shell"]),
        (1, WARNED, "emulator-5556", ["shell"]),
        (1, REFUSED, "qa_tablet", ["shell"]),
    ]


@pytest.mark.parametrize(
    ("agent", "arguments", "shown"),
    [
        # The session of the caller's agent process.
        ("claude", [], ["emulator-5556", "emulator-5558"]),
        # An agent without a session.
        ("codex", [], ["emulator-5558"]),
        # A process that the caller names: banksman does not know its session.
        ("claude", ME, ["emulator-5554", "emulator-5556", "emulator-5558"]),
    ],
)
def test_held_leaves_out_the_calls_of_an_earlier_agent_process_with_the_same_pid(
    capsys, config_path, log_path, monkeypatch, agent, arguments, shown
):
    configure(config_path)
    monkeypatch.setattr(cli, "find_agent", lambda table, agents, pid, env: (os.getpid(), agent))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-2")
    # The system gave the pid of an earlier agent process, of session-1, to this one.
    guard_call(log_path, 300, "emulator-5554", session="session-1", reason=NOT_LEASED)
    guard_call(log_path, 200, "emulator-5556", session="session-2", reason=NOT_LEASED)
    guard_call(log_path, 100, "emulator-5558", reason=NOT_LEASED)
    _, calls = held_text(capsys, *arguments)
    assert [row["RESOURCE"] for row in table_rows(calls.split("\n", 1)[1])] == shown


def test_held_lists_the_leases_also_when_the_log_cannot_be_read(capsys, config_path, log_path):
    configure(config_path)
    acquire(capsys, "--where", "form=phone", *ME)
    guard_call(log_path, 10, "qa_phone2", reason=NOT_LEASED)
    log_path.chmod(0o666)
    shown, calls = held_text(capsys, *ME)
    assert [row["RESOURCE"] for row in table_rows(shown)] == ["qa_phone"]
    assert calls.startswith("note: the log cannot be read, so the guard events are not shown: ")
    assert main(["held", "--json", *ME]) == 0
    assert json.loads(capsys.readouterr().out)["guard"] is None


def test_status_of_an_agent_points_to_held_for_its_refused_calls(
    capsys, config_path, log_path, monkeypatch
):
    configure(config_path)
    agent = (os.getpid(), "codex")
    monkeypatch.setattr(cli, "find_agent", lambda table, agents, pid, env: agent)
    assert main(["status"]) == 0
    assert "adb guard" not in capsys.readouterr().out
    guard_call(log_path, 10, "qa_phone2", reason=NOT_LEASED)
    assert main(["status"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "note: the adb guard refused or warned about adb calls of your agent process in the last"
        " 15 minutes: banksman held shows them"
    )
    assert main(["status", "--json"]) == 0
    assert "adb guard" not in capsys.readouterr().out
    # A person: no agent process is above the caller.
    agent = (None, None)
    assert main(["status"]) == 0
    assert "adb guard" not in capsys.readouterr().out


def test_status_shows_the_label_only_with_verbose(capsys, config_path):
    configure(config_path)
    acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    assert main(["status", "--json"]) == 0
    shown = {each["resource"]: each for each in json.loads(capsys.readouterr().out)["resources"]}
    assert "label" not in shown["qa_phone"]["lease"]
    assert main(["status", "--json", "--verbose"]) == 0
    shown = {each["resource"]: each for each in json.loads(capsys.readouterr().out)["resources"]}
    assert shown["qa_phone"]["lease"]["label"] == "tests"


def run_in_child(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "banksman", "run", *arguments],
        env={**os.environ, "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
        timeout=60,
    )


SHOW = "import os, sys; print(os.environ['BANKSMAN_LEASE'], *sys.argv[1:])"


def test_run_takes_a_label(capsys, config_path):
    configure(config_path)
    _, values, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    done = run_in_child(
        "--holding", "tests", *ME, "--", sys.executable, "-c", SHOW, "{serial}", "{resource}"
    )
    assert (done.returncode, done.stdout) == (0, f"{values['LEASE']} emulator-5554 qa_phone\n")
    done = run_in_child("--holding", "other", *ME, "--", "true")
    assert done.returncode == cli.EXIT_LOST
    assert "has no held holding with the label other" in done.stderr


def test_run_with_a_label_runs_under_every_lease_of_the_holding_or_the_chosen_ones(
    capsys, config_path
):
    configure(config_path)
    _, values, _ = acquire(
        capsys, "--holding", "tests", "--as", "a", "--where", "form=phone", "--as", "b", *ME
    )
    assert (values["A_RESOURCE"], values["B_RESOURCE"]) == ("qa_phone", "build-0")
    # BANKSMAN_LEASE names one lease of the holding, the first in the order of the resources.
    done = run_in_child(
        "--holding", "tests", *ME, "--", sys.executable, "-c", SHOW, "{resources}", "{serials}"
    )
    assert (done.returncode, done.stdout) == (
        0,
        f"{values['B_LEASE']} build-0,qa_phone emulator-5554\n",
    )
    done = run_in_child(
        "--holding", "tests", "--resource", "qa_phone", *ME, "--", sys.executable, "-c", SHOW
    )
    assert (done.returncode, done.stdout) == (0, f"{values['A_LEASE']}\n")
    done = run_in_child("--holding", "tests", "--resource", "qa_tablet", *ME, "--", "true")
    assert done.returncode == cli.EXIT_LOST
    assert "the holding tests has no held lease on qa_tablet" in done.stderr


# The guide for agents.


def test_agent_guide_prints_a_skill_for_the_installed_version(capsys):
    assert main(["agent-guide"]) == 0
    guide = capsys.readouterr().out
    assert guide.startswith("---\nname: banksman\ndescription: ")
    assert f"This guide is for banksman {__version__}." in guide
    assert "@VERSION@" not in guide
    # It is text for agents, so it says what the agents must not do.
    assert "Never run `banksman admin`" in guide
    assert "Do not use `banksman release --all`" in guide
    # It says when the agent can skip the setup of a device, and that a sibling subagent in the
    # same worktree counts as the worktree.
    assert "`PREVIOUS=self` says that your worktree used the device last" in guide
    assert "Every agent in your worktree counts as your worktree" in guide
    # It says when the agent ends what it started.
    assert "`STARTED=true` says that this request started the device" in guide
    # It says where the agent finds a refusal of the adb guard that a script hid.
    assert "run `banksman held` first" in guide


def test_the_guide_names_every_key_of_the_grant_and_every_variable_of_run(capsys):
    assert main(["agent-guide"]) == 0
    guide = capsys.readouterr().out
    keys = ("RESOURCE", "KIND", "LEASE", "STATE", "KEPT", "PREVIOUS", "STARTED", "SERIAL")
    for key in (*keys, "HANDLE", "ACCOUNTS"):
        assert f"`{key}`" in guide, key
    # With --as, each key has the prefix of its part.
    assert "`PHONE_SERIAL`" in guide
    for placeholder in (
        cli.SERIAL_PLACEHOLDER,
        cli.RESOURCE_PLACEHOLDER,
        cli.SERIALS_PLACEHOLDER,
        cli.RESOURCES_PLACEHOLDER,
    ):
        assert f"`{placeholder}`" in guide, placeholder
    for variable in (*hooks.VARIABLES, hooks.ANDROID_SERIAL):
        assert f"`{variable}`" in guide, variable
    assert f"`{hooks.NO_SERIAL}`" in guide


def test_the_guide_waits_less_than_the_time_limit_of_an_agent(capsys):
    # The Bash tool of Claude Code waits 10 minutes at most for a command, and then moves it to
    # the background, so a longer wait does not give its result in the same call.
    assert main(["agent-guide"]) == 0
    waits = re.findall(r"--wait ([0-9]+[smh])", capsys.readouterr().out)
    assert waits
    assert all(parse_duration(wait) < 10 * 60 for wait in waits)


def guide_commands(guide):
    """Return the banksman commands that the guide shows: the lines of its code blocks, and
    its inline code."""
    blocks = re.findall(r"^```sh\n(.*?)^```", guide, flags=re.MULTILINE | re.DOTALL)
    lines = [line for block in blocks for line in block.splitlines()]
    inline = re.findall(r"`(banksman [^`]+)`", guide)
    return [command for command in (*lines, *inline) if command.startswith("banksman ")]


def test_every_command_in_the_guide_is_one_that_banksman_takes(capsys):
    assert main(["agent-guide"]) == 0
    commands = guide_commands(capsys.readouterr().out)
    assert "banksman held" in commands
    parser = cli._build_parser()
    for command in commands:
        words = shlex.split(command)[1:]
        if words == ["admin"]:
            # The guide names it only to forbid it.
            continue
        try:
            parser.parse_args(words)
        except SystemExit:
            pytest.fail(f"the guide shows a command that banksman does not take: {command}")


def test_agents_may_run_the_guide(capsys):
    # The permission rules of agents refuse banksman admin, and the guide is for agents.
    with pytest.raises(SystemExit):
        main(["admin", "agent-guide"])
    capsys.readouterr()



# A holding whose lease is still booting: an acquire starts or resets its instance, for example
# one that runs in the background of the agent's tool.


def test_a_holding_with_a_booting_lease_is_not_kept(store, agents):
    holder = Holder(OWNER, owner_pid=101)
    reset = [Need((Choice("emu-1", "emulator", reset=True),))]
    (booting,) = store.grant(reset, holder, label="tests")
    assert booting.lease.state == "booting"
    # Neither by the label nor by the lease id: the acquire that resets it gave it to nobody yet.
    assert store.grant(reset, holder, label="tests") is None
    assert store.grant(reset, holder, keep=booting.lease.lease_id) is None
    store.ready("emu-1", booting.lease.lease_id)
    (kept,) = store.grant(reset, holder, label="tests")
    assert (kept.kept, kept.lease.state) == (True, "ready")


def booting(state_dir, resource, pid, started):
    """Change a lease into a booting lease whose instance the acquire `pid` resets."""

    def change(data):
        data.update(state="booting", reaper_pid=pid, reaper_started=started)
        data["awake"]["boot_deadline"] = data["awake"]["touched"] + 5 * 60

    edit_lease(state_dir, resource, change)


def test_a_holding_whose_reset_still_runs_is_neither_kept_nor_run(capsys, config_path, state_dir):
    configure(config_path)
    acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    reset = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        booting(state_dir, "qa_phone", reset.pid, Machine().running([reset.pid])[reset.pid])
        status, values, err = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
        assert status == cli.EXIT_BUSY
        assert values == {}
        assert (
            "qa_phone of the kept holding is still booting: a banksman process still starts or"
            " resets its instance: an acquire, for example one that runs in the background, or"
            " the process that continues the start of acquire --no-wait. The request waits until"
            " that process is done"
        ) in err
        done = run_in_child("--holding", "tests", *ME, "--", "true")
        assert done.returncode == cli.EXIT_BUSY
        assert "qa_phone of the holding tests is still booting" in done.stderr
        assert "Wait until it is ready with banksman wait --holding tests" in done.stderr
        assert leases()["qa_phone"].state == "booting"
    finally:
        reset.kill()
        reset.wait()


def test_a_reset_whose_acquire_ended_is_taken_back_and_the_label_starts_again(
    capsys, config_path, state_dir
):
    configure(config_path)
    _, first, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    booting(state_dir, "qa_phone", ended.pid, "gone")
    # Nothing marks the lease ready any more, so the request does not wait for its boot
    # deadline: the reaper takes it back first, and the label starts a new holding.
    status, again, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    assert (status, again["RESOURCE"], again["KEPT"]) == (0, "qa_phone", "false")
    assert again["LEASE"] != first["LEASE"]


def test_a_booting_lease_goes_to_the_process_that_continues_its_start(store, agents, state_dir):
    holder = Holder(OWNER, owner_pid=101)
    reset = [Need((Choice("emu-1", "emulator", reset=True),))]
    (granted,) = store.grant(reset, holder, label="tests")
    lease = granted.lease
    # The acquire, process 103, granted it.
    store.system.spawn(103)
    booting(state_dir, "emu-1", 103, "start-103")
    leases = [("emu-1", lease.lease_id)]
    # Only from the process that the lease names.
    with pytest.raises(NotHeld, match="not booting for process 104"):
        store.hand_over(leases, (104, "start-104"))
    (taken,) = store.hand_over(leases, (103, "start-103"))
    assert (taken.reaper_pid, taken.reaper_started) == (os.getpid(), "start-self")
    assert taken.state == "booting"
    # The lease stays valid after the acquire has ended.
    store.system.end(103)
    assert store.labelled(101, "tests") == {"emu-1": taken}


def test_a_lease_whose_acquire_ended_before_the_handover_is_not_handed_over(
    store, agents, state_dir
):
    (granted,) = store.grant(
        [Need((Choice("emu-1", "emulator", reset=True),))], Holder(OWNER, owner_pid=101)
    )
    booting(state_dir, "emu-1", 103, "start-103")
    with pytest.raises(NotHeld, match="void"):
        store.hand_over([("emu-1", granted.lease.lease_id)], (103, "start-103"))


def test_a_start_asks_whether_its_lease_is_still_booting(store, agents):
    holder = Holder(OWNER, owner_pid=101)
    (granted,) = store.grant([Need((Choice("emu-1", "emulator", reset=True),))], holder)
    lease_id = granted.lease.lease_id
    assert store.still_booting("emu-1", lease_id)
    assert not store.still_booting("emu-1", "another-lease")
    store.ready("emu-1", lease_id)
    assert not store.still_booting("emu-1", lease_id)
    (granted,) = store.grant([Need((Choice("emu-2", "emulator", reset=True),))], holder)
    store.release("emu-2", granted.lease.lease_id)
    assert not store.still_booting("emu-2", granted.lease.lease_id)


# banksman wait: until every lease of a holding is ready.


def wait_in_child(*arguments):
    return subprocess.Popen(
        [sys.executable, "-m", "banksman", "wait", *arguments],
        env={**os.environ, "PYTHONPATH": SRC},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.fixture
def resetting():
    """A process that the tests name as the banksman process that resets an instance."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield process
    process.kill()
    process.wait()


def test_wait_exits_with_4_while_a_lease_boots_and_with_0_when_it_is_ready(
    capsys, config_path, state_dir, resetting
):
    configure(config_path)
    _, values, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    booting(state_dir, "qa_phone", resetting.pid, Machine().running([resetting.pid])[resetting.pid])
    assert main(["wait", "--holding", "tests", *ME]) == cli.EXIT_BUSY
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "qa_phone of the holding tests is still booting" in captured.err
    Store(cli.default_state_dir(), Machine()).ready("qa_phone", values["LEASE"])
    age_lease(state_dir, "qa_phone", 60)
    before = leases()["qa_phone"].touched
    assert main(["wait", "--holding", "tests", *ME]) == 0
    (row,) = table_rows(capsys.readouterr().out)
    assert (row["LABEL"], row["LEASE"], row["STATE"], row["SERIAL"]) == (
        "tests",
        values["LEASE"],
        "ready",
        "emulator-5554",
    )
    # The holder is back, so wait touches the holding.
    assert leases()["qa_phone"].touched > before
    assert main(["wait", "--lease", values["LEASE"], "--json"]) == 0
    (shown,) = json.loads(capsys.readouterr().out)["leases"]
    assert (shown["resource"], shown["state"]) == ("qa_phone", "ready")


def test_wait_with_a_wait_returns_when_the_holding_is_ready(
    capsys, config_path, state_dir, resetting
):
    configure(config_path)
    _, values, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    booting(state_dir, "qa_phone", resetting.pid, Machine().running([resetting.pid])[resetting.pid])
    waiting = wait_in_child("--holding", "tests", "--wait", "60s", *ME)
    try:
        time.sleep(1.5)
        assert waiting.poll() is None
        Store(cli.default_state_dir(), Machine()).ready("qa_phone", values["LEASE"])
        out, err = waiting.communicate(timeout=30)
    finally:
        waiting.kill()
        waiting.wait()
    assert waiting.returncode == 0, err
    assert table_rows(out)[0]["STATE"] == "ready"


def test_wait_exits_with_3_when_the_process_that_starts_the_holding_has_ended(
    capsys, config_path, state_dir
):
    configure(config_path)
    acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    booting(state_dir, "qa_phone", ended.pid, "gone")
    assert main(["wait", "--holding", "tests", "--wait", "60s", *ME]) == cli.EXIT_LOST
    err = capsys.readouterr().err
    assert f"process {os.getpid()} has no held holding with the label tests" in err
    assert "acquire again without --no-wait to see why a start fails" in err
    # wait reaped first: the lease is void, and its resource is free.
    assert leases() == {}


def test_wait_exits_with_3_when_a_lease_of_the_holding_is_lost_while_it_waits(
    capsys, config_path, state_dir, resetting
):
    configure(config_path)
    _, values, _ = acquire(
        capsys,
        "--holding", "tests", "--as", "a", "--where", "form=phone",
        "--as", "b", "--where", "form=phone", *ME,
    )  # fmt: skip
    started = Machine().running([resetting.pid])[resetting.pid]
    for resource in ("qa_phone", "qa_phone2"):
        booting(state_dir, resource, resetting.pid, started)
    waiting = wait_in_child("--holding", "tests", "--wait", "60s", *ME)
    try:
        time.sleep(1.5)
        # The other lease of the holding still boots, but the holding is not what was asked for.
        Store(cli.default_state_dir(), Machine()).release("qa_phone", values["A_LEASE"])
        out, err = waiting.communicate(timeout=30)
    finally:
        waiting.kill()
        waiting.wait()
    assert (waiting.returncode, out) == (cli.EXIT_LOST, "")
    assert "the lease on qa_phone of the holding tests was given back or became void" in err


@pytest.mark.parametrize(
    "arguments,problem",
    [
        ([], "wait needs --lease, the id of a lease, or --holding, its label"),
        (["--lease", "x", "--holding", "tests"], "wait needs --lease"),
        (["--lease", "x", *ME], "wait --owner-pid needs --holding"),
    ],
)
def test_wait_needs_one_holding(capsys, config_path, arguments, problem):
    configure(config_path)
    assert main(["wait", *arguments]) == 1
    assert problem in capsys.readouterr().err


def test_no_wait_needs_start(capsys, config_path):
    configure(config_path)
    status, values, err = acquire(capsys, "--where", "form=phone", "--no-wait", *ME)
    assert (status, values) == (1, {})
    assert "acquire --no-wait needs --start" in err


def test_with_no_wait_a_grant_that_starts_nothing_is_ready_at_once(capsys, config_path):
    configure(config_path)
    status, values, err = acquire(
        capsys, "--holding", "tests", "--where", "form=phone", "--start", "--no-wait", *ME
    )
    assert status == 0, err
    assert (values["STATE"], values["STARTED"]) == ("ready", "false")
    assert "banksman wait" not in err
