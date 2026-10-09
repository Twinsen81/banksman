"""Named holdings: a label that the caller gives a holding, and `held`, which lists them."""

import json
import os
import re
import subprocess
import sys

import pytest
from helpers import SRC, age_lease, edit_lease, hook, table_rows, write_config

from banksman import __version__, cli
from banksman.cli import main
from banksman.config import parse_duration
from banksman.errors import BanksmanError
from banksman.inventory import Decisions, Inventory, save_inventory
from banksman.lease import Holder
from banksman.store import Choice, Need, Store
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
    assert main(["held", *ME]) == 0
    rows = table_rows(capsys.readouterr().out)
    assert [(row["LABEL"], row["RESOURCE"], row["SERIAL"]) for row in rows] == [
        ("tests", "build-0", "-"),
        ("tests", "qa_tablet", "-"),
        ("-", "qa_phone", "emulator-5554"),
    ]
    assert rows[2]["LEASE"] == unlabelled["LEASE"]
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
    assert main(["release", "--all", *ME]) == 0
    capsys.readouterr()
    assert main(["held", *ME]) == 0
    assert capsys.readouterr().out == f"Process {os.getpid()} holds no lease.\n"


def test_held_without_an_agent_process_is_an_error(capsys, config_path):
    configure(config_path)
    assert main(["held"]) == 1
    assert "held needs the agent process" in capsys.readouterr().err


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


def test_run_with_a_label_of_several_resources_needs_the_resource(capsys, config_path):
    configure(config_path)
    _, values, _ = acquire(
        capsys, "--holding", "tests", "--as", "a", "--where", "form=phone", "--as", "b", *ME
    )
    done = run_in_child("--holding", "tests", *ME, "--", sys.executable, "-c", SHOW)
    assert done.returncode == 1
    assert done.stderr == (
        "banksman: the holding tests has several resources: build-0, qa_phone. Give one with"
        " --resource\n"
    )
    done = run_in_child(
        "--holding", "tests", "--resource", "qa_phone", *ME, "--", sys.executable, "-c", SHOW
    )
    assert (done.returncode, done.stdout) == (0, f"{values['A_LEASE']}\n")


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


def test_the_guide_waits_less_than_the_time_limit_of_an_agent(capsys):
    # Claude Code stops a command of its Bash tool after 10 minutes at most.
    assert main(["agent-guide"]) == 0
    waits = re.findall(r"--wait ([0-9]+[smh])", capsys.readouterr().out)
    assert waits
    assert all(parse_duration(wait) < 10 * 60 for wait in waits)


def test_agents_may_run_the_guide(capsys):
    # The permission rules of agents refuse banksman admin, and the guide is for agents.
    with pytest.raises(SystemExit):
        main(["admin", "agent-guide"])
    capsys.readouterr()



# A holding whose lease is still booting: an acquire starts or resets its instance, or ended
# before it was done, for example because the time limit of the agent's tool stopped it.


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


def interrupted(state_dir, resource):
    """Change a lease into a booting lease whose acquire ended before its reset was done."""
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()

    def change(data):
        data.update(state="booting", reaper_pid=ended.pid, reaper_started="gone")
        data["awake"]["boot_deadline"] = data["awake"]["touched"] + 5 * 60

    edit_lease(state_dir, resource, change)


def test_an_interrupted_reset_is_neither_kept_nor_run_and_release_gives_it_back(
    capsys, config_path, state_dir
):
    configure(config_path)
    _, first, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    interrupted(state_dir, "qa_phone")
    status, values, err = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    assert status == cli.EXIT_BUSY
    assert values == {}
    assert "qa_phone of the kept holding is still booting" in err
    done = run_in_child("--holding", "tests", *ME, "--", "true")
    assert done.returncode == cli.EXIT_BUSY
    assert "qa_phone of the holding tests is still booting" in done.stderr
    # The acquire that reset it has ended, so a release frees the resource at once.
    assert main(["release", "--holding", "tests", *ME]) == 0
    assert capsys.readouterr().out == "Released qa_phone: it is free.\n"
    status, again, _ = acquire(capsys, "--holding", "tests", "--where", "form=phone", *ME)
    assert (status, again["RESOURCE"], again["KEPT"]) == (0, "qa_phone", "false")
    assert again["LEASE"] != first["LEASE"]
