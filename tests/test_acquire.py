"""The commands that take and give back leases: acquire, ready, touch, and release."""

import json
import os

import pytest
from helpers import age_lease, hook, write_config

from banksman import cli
from banksman.cli import main
from banksman.inventory import Decisions, Inventory, save_inventory
from banksman.lease import Holder
from banksman.store import Store
from banksman.system import Machine

PRINT_ARGUMENT = "import sys; print(sys.argv[1])"
# The hooks write the resource that they got to a file, so that a test sees that they ran.
RECORD = "import os, sys; open(sys.argv[1], 'a').write(os.environ['BANKSMAN_RESOURCE'] + '\\n')"
FAIL = "raise SystemExit(5)"
READ = "import sys; print(open(sys.argv[1]).read())"
BENCH = [
    {"name": "qa_tablet", "facts": {"form": "tablet", "api": 33, "running": False}},
    {
        "name": "qa_phone",
        "facts": {"form": "phone", "api": 35, "running": True, "serial": "emulator-5554"},
    },
    {"name": "qa_odd", "facts": {"form": "watch", "running": True, "serial": "emulator 5556"}},
    {"name": "Personal_AVD", "facts": {"form": "phone", "api": 35, "running": True}},
]


def configure(config_path, instances=BENCH, extra=""):
    found = json.dumps({"schema": 1, "instances": instances})
    write_config(
        config_path,
        # No agent process: the tests own their leases through --owner-pid, or not at all.
        "[holder]\nagents = []\n"
        f"[kinds.emulator]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\n"
        f"{extra}"
        "[kinds.build]\ncount = 1\n",
    )
    save_inventory(
        Inventory(
            kinds={
                "emulator": Decisions(
                    allowed=("qa_tablet", "qa_phone", "qa_odd", "qa_gone"),
                    refused=("Personal_AVD",),
                )
            },
            tags={"lab": ("qa_tablet",)},
        )
    )


def acquire(capsys, *arguments):
    status = main(["acquire", *arguments])
    captured = capsys.readouterr()
    values = dict(line.split("=", 1) for line in captured.out.splitlines())
    return status, values, captured.err


def leases():
    snapshot = Store(cli.default_state_dir(), Machine()).snapshot()
    return {lease.resource: lease for lease in snapshot.leases}


def test_acquire_prints_the_lease_as_key_value_lines(capsys, config_path):
    configure(config_path)
    status, values, _ = acquire(
        capsys, "--where", "form=phone", "--for", "verify the layout", "--expect", "20m"
    )
    assert status == 0
    lease = leases()["qa_phone"]
    assert values == {
        "RESOURCE": "qa_phone",
        "KIND": "emulator",
        "LEASE": lease.lease_id,
        "STATE": "ready",
        "SERIAL": "emulator-5554",
    }
    assert (lease.purpose, lease.owner_pid) == ("verify the layout", None)
    assert lease.expected == lease.touched + 20 * 60


def test_acquire_prints_json(capsys, config_path):
    configure(config_path)
    assert main(["acquire", "--where", "kind=build", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown == {
        "schema": shown["schema"],
        "resource": "build-0",
        "kind": "build",
        "lease_id": leases()["build-0"].lease_id,
        "state": "ready",
        "serial": None,
        "kept": False,
    }


def test_a_serial_that_a_shell_could_misread_is_not_printed(capsys, config_path):
    configure(config_path)
    status, values, _ = acquire(capsys, "--where", "form=watch")
    assert (status, values["RESOURCE"]) == (0, "qa_odd")
    assert "SERIAL" not in values


def test_an_instance_that_does_not_run_is_granted_ready_for_its_holder_to_start(
    capsys, config_path
):
    # banksman starts nothing, and a fact from discovery can be wrong, so the holder checks
    # whether the instance runs, and starts it.
    configure(config_path)
    status, values, _ = acquire(capsys, "--where", "form=tablet")
    assert (status, values["RESOURCE"], values["STATE"]) == (0, "qa_tablet", "ready")
    assert "SERIAL" not in values
    assert leases()["qa_tablet"].state == "ready"


def test_a_running_instance_is_chosen_before_one_that_must_be_started(capsys, config_path):
    configure(config_path)
    _, first, _ = acquire(capsys, "--where", "kind=emulator", "--where", "api>=33")
    _, second, _ = acquire(capsys, "--where", "kind=emulator", "--where", "api>=33")
    assert (first["RESOURCE"], second["RESOURCE"]) == ("qa_phone", "qa_tablet")


def test_tags_from_the_inventory_can_be_requested(capsys, config_path):
    configure(config_path)
    _, values, _ = acquire(capsys, "--where", "tag=lab")
    assert values["RESOURCE"] == "qa_tablet"


def test_a_held_resource_is_not_granted_again_without_its_lease_id(capsys, config_path):
    configure(config_path)
    _, first, _ = acquire(capsys, "--where", "kind=build")
    status, _, err = acquire(capsys, "--where", "kind=build")
    assert status == cli.EXIT_BUSY == 4
    assert err == "banksman: every matching resource is in use: build-0\n"
    # The caller that passes its lease id keeps its lease.
    status, again, _ = acquire(capsys, "--where", "kind=build", "--lease", first["LEASE"])
    assert (status, again["LEASE"]) == (0, first["LEASE"])


def test_a_kept_lease_records_the_new_owner_process_and_the_new_expected_time(
    capsys, config_path
):
    configure(config_path)
    _, first, _ = acquire(capsys, "--where", "kind=build")
    assert main(
        ["acquire", "--where", "kind=build", "--lease", first["LEASE"], "--json"]
        + ["--owner-pid", str(os.getpid()), "--expect", "5m"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["kept"] is True
    lease = leases()["build-0"]
    assert lease.owner_pid == os.getpid()
    # One clock reading gives both, so that they differ by exactly the expected time.
    assert lease.expected == lease.touched + 5 * 60


def test_a_waiting_acquire_gets_a_resource_when_it_is_released(capsys, config_path, monkeypatch):
    configure(config_path)
    held = Store(cli.default_state_dir(), Machine()).acquire("build-0", "build", Holder("/w/b"))

    def release_while_waiting(self, seconds):
        Store(cli.default_state_dir(), Machine()).release("build-0", held.lease_id)

    monkeypatch.setattr(Machine, "sleep", release_while_waiting)
    status, values, _ = acquire(capsys, "--where", "kind=build", "--wait", "15m")
    assert (status, values["RESOURCE"]) == (0, "build-0")
    assert values["LEASE"] != held.lease_id


def test_a_waiting_acquire_reaps_with_the_current_configuration(
    capsys, config_path, monkeypatch, state_dir, tmp_path
):
    record = tmp_path / "taken-back"
    hook_line = f"on_void = {json.dumps(hook(RECORD, str(record)))}\n"
    write_config(config_path, f"[holder]\nagents = []\n[kinds.build]\ncount = 1\n{hook_line}")
    Store(cli.default_state_dir(), Machine()).acquire("build-0", "build", Holder("/w/b"))

    def change_while_waiting(self, seconds):
        # The operator removes the hook, and the lease of the other holder becomes void.
        write_config(config_path, "[holder]\nagents = []\n[kinds.build]\ncount = 1\n")
        age_lease(state_dir, "build-0", 21 * 60)

    monkeypatch.setattr(Machine, "sleep", change_while_waiting)
    status, values, _ = acquire(capsys, "--where", "kind=build", "--wait", "15m")
    assert (status, values["RESOURCE"]) == (0, "build-0")
    assert not record.exists()


def test_a_wait_has_a_limit(capsys, config_path, monkeypatch):
    configure(config_path)
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.05)
    Store(cli.default_state_dir(), Machine()).acquire("build-0", "build", Holder("/w/b"))
    status, _, err = acquire(capsys, "--where", "kind=build", "--wait", "1s")
    assert status == 4
    assert err == "banksman: every matching resource is still in use: build-0\n"


def test_a_request_that_nothing_permitted_matches_fails_at_once(
    capsys, config_path, monkeypatch
):
    configure(config_path)
    monkeypatch.setattr(Machine, "sleep", lambda self, seconds: pytest.fail("it waited"))
    status, _, err = acquire(capsys, "--where", "avd=Personal_AVD", "--wait", "1h")
    assert status == 1
    assert "no permitted resource that is present now matches avd=Personal_AVD" in err
    assert "banksman: note: qa_gone (kind emulator) is allowed but not present now" in err
    assert leases() == {}


@pytest.mark.parametrize(
    ("clause", "problem"),
    [
        ("colour=red", "unknown attribute colour"),
        ("kind=device", "the configuration declares no kind device"),
    ],
)
def test_a_request_with_an_unknown_attribute_or_kind_is_an_error(
    capsys, config_path, clause, problem
):
    configure(config_path)
    assert main(["acquire", "--where", clause, "--wait", "1h"]) == 1
    assert problem in capsys.readouterr().err


def test_a_machine_without_permitted_resources_says_so(capsys):
    assert main(["acquire"]) == 1
    assert capsys.readouterr().err.startswith(
        "banksman: no permitted resource is present now. The operator decides"
    )


def test_a_clause_that_is_not_valid_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["acquire", "--where", "api"])
    assert exc.value.code == 2
    assert "Quote it in a shell" in capsys.readouterr().err


def test_on_acquire_resets_a_new_instance_once(capsys, config_path, tmp_path):
    record = tmp_path / "reset"
    configure(config_path, extra=f"on_acquire = {json.dumps(hook(RECORD, str(record)))}\n")
    _, first, _ = acquire(capsys, "--where", "form=phone")
    assert first["STATE"] == "ready"
    _, cold, _ = acquire(capsys, "--where", "form=tablet")
    assert cold["STATE"] == "ready"
    _, kept, _ = acquire(capsys, "--where", "form=phone", "--lease", first["LEASE"])
    assert kept["LEASE"] == first["LEASE"]
    assert record.read_text() == "qa_phone\nqa_tablet\n"


def test_the_serial_is_read_again_after_a_reset(capsys, config_path, tmp_path):
    # The hook restarts the emulator, and it comes back with another serial.
    found = tmp_path / "found.json"
    before = {"name": "qa_phone", "facts": {"running": True, "serial": "emulator-5554"}}
    after = {"name": "qa_phone", "facts": {"running": True, "serial": "emulator-5556"}}
    found.write_text(json.dumps({"schema": 1, "instances": [before]}))
    moved = json.dumps({"schema": 1, "instances": [after]})
    restart = f"open({str(found)!r}, 'w').write({moved!r})"
    write_config(
        config_path,
        "[holder]\nagents = []\n[kinds.emulator]\n"
        f"discover = {json.dumps(hook(READ, str(found)))}\n"
        f"on_acquire = {json.dumps(hook(restart))}\n",
    )
    save_inventory(Inventory(kinds={"emulator": Decisions(allowed=("qa_phone",))}))
    status, values, _ = acquire(capsys, "--where", "serial=emulator-5554")
    assert (status, values["RESOURCE"], values["SERIAL"]) == (0, "qa_phone", "emulator-5556")


def test_no_serial_is_printed_for_an_instance_that_is_gone_after_its_reset(
    capsys, config_path, tmp_path
):
    found = tmp_path / "found.json"
    before = {"name": "qa_phone", "facts": {"running": True, "serial": "emulator-5554"}}
    found.write_text(json.dumps({"schema": 1, "instances": [before]}))
    stop = f"open({str(found)!r}, 'w').write({json.dumps({'schema': 1, 'instances': []})!r})"
    write_config(
        config_path,
        "[holder]\nagents = []\n[kinds.emulator]\n"
        f"discover = {json.dumps(hook(READ, str(found)))}\n"
        f"on_acquire = {json.dumps(hook(stop))}\n",
    )
    save_inventory(Inventory(kinds={"emulator": Decisions(allowed=("qa_phone",))}))
    status, values, _ = acquire(capsys, "--where", "serial=emulator-5554")
    assert (status, values["RESOURCE"]) == (0, "qa_phone")
    assert "SERIAL" not in values


def test_a_failing_on_acquire_hook_gives_the_lease_back(capsys, config_path):
    configure(config_path, extra=f"on_acquire = {json.dumps(hook(FAIL))}\n")
    status, values, err = acquire(capsys, "--where", "form=phone")
    assert (status, values) == (1, {})
    assert err == (
        "banksman: cannot reset qa_phone: the on_acquire hook failed with exit status 5;"
        " the lease is given back\n"
    )
    assert leases() == {}


def test_touch_keeps_the_lease_and_can_change_the_expected_time(capsys, config_path, state_dir):
    configure(config_path)
    _, values, _ = acquire(capsys, "--where", "kind=build")
    age_lease(state_dir, "build-0", 15 * 60)
    arguments = ["--resource", "build-0", "--lease", values["LEASE"]]
    assert main(["touch", *arguments, "--expect", "5m", "--owner-pid", str(os.getpid())]) == 0
    age_lease(state_dir, "build-0", 15 * 60)
    # Not idle for 20 minutes since the touch, so the reaper leaves the lease alone.
    assert main(["reap"]) == 0
    lease = leases()["build-0"]
    assert lease.owner_pid == os.getpid()
    assert main(["touch", "--resource", "build-0", "--lease", "an-earlier-lease"]) == 3


def test_release_frees_the_resource_and_says_so(capsys, config_path):
    configure(config_path)
    _, values, _ = acquire(capsys, "--where", "kind=build")
    arguments = ["release", "--resource", "build-0", "--lease", values["LEASE"]]
    assert main(arguments) == 0
    assert capsys.readouterr().out == "Released build-0: it is free.\n"
    assert main(arguments) == 3
    assert capsys.readouterr().err == "banksman: the lease is lost: build-0 has no lease\n"


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        (["--resource", "build-0"], "needs --lease"),
        (["--all", "--lease", "lease-1"], "takes no --lease"),
        (["--resource", "build-0", "--lease", "x", "--owner-pid", "1"], "needs --all"),
    ],
)
def test_release_needs_a_lease_id_or_all(capsys, arguments, problem):
    assert main(["release", *arguments]) == 1
    assert problem in capsys.readouterr().err


def test_release_all_gives_back_only_the_leases_of_one_agent_process(capsys, config_path):
    configure(config_path)
    me = ["--owner-pid", str(os.getpid())]
    acquire(capsys, "--where", "kind=build", *me)
    acquire(capsys, "--where", "form=phone", *me)
    acquire(capsys, "--where", "form=tablet")
    assert main(["release", "--all", *me]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Released build-0: it is free.",
        "Released qa_phone: it is free.",
    ]
    assert list(leases()) == ["qa_tablet"]
    assert main(["release", "--all", *me]) == 0
    assert capsys.readouterr().out == f"Process {os.getpid()} holds no lease.\n"


def test_release_all_without_an_agent_process_is_an_error(capsys, config_path):
    configure(config_path)
    assert main(["release", "--all"]) == 1
    assert "needs the agent process" in capsys.readouterr().err
