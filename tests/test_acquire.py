"""The commands that take and give back leases: acquire, ready, touch, and release."""

import json
import os
import subprocess
import sys

import pytest
from helpers import age_lease, hook, table_rows, write_config

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
        "KEPT": "false",
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
        "parts": [
            {
                "part": None,
                "lease_id": leases()["build-0"].lease_id,
                "resource": "build-0",
                "kind": "build",
                "state": "ready",
                "serial": None,
                "handle": None,
                "paid": False,
                "accounts": [],
                "kept": False,
            }
        ],
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
    assert (status, again["LEASE"], again["KEPT"]) == (0, first["LEASE"], "true")


def test_a_kept_lease_records_the_new_owner_process_and_the_new_expected_time(
    capsys, config_path
):
    configure(config_path)
    _, first, _ = acquire(capsys, "--where", "kind=build")
    assert main(
        ["acquire", "--where", "kind=build", "--lease", first["LEASE"], "--json"]
        + ["--owner-pid", str(os.getpid()), "--expect", "5m"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["parts"][0]["kept"] is True
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
    # A caller that waits, such as a build, says once why it does not go on.
    assert err.splitlines() == [
        "banksman: every matching resource is in use: build-0; waiting up to 1s. banksman status"
        " shows who holds them",
        "banksman: every matching resource is still in use: build-0",
    ]


def test_a_request_that_waits_stops_when_its_owner_process_ends(capsys, config_path, monkeypatch):
    configure(config_path)
    Store(cli.default_state_dir(), Machine()).acquire("build-0", "build", Holder("/w/b"))
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:

        def end_owner_while_waiting(self, seconds):
            owner.kill()
            owner.wait()

        monkeypatch.setattr(Machine, "sleep", end_owner_while_waiting)
        status, _, err = acquire(
            capsys, "--where", "kind=build", "--wait", "15m", "--owner-pid", str(owner.pid)
        )
    finally:
        owner.kill()
        owner.wait()
    assert status == 1
    assert err.splitlines()[-1] == (
        f"banksman: the owner process {owner.pid} has ended, so the request stops waiting"
    )


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
    assert leases()["qa_phone"].serial == "emulator-5556"


def test_the_lease_records_the_serial_of_its_instance(capsys, config_path):
    configure(config_path)
    assert acquire(capsys, "--where", "form=phone")[1]["SERIAL"] == "emulator-5554"
    first = leases()["qa_phone"]
    assert first.serial == "emulator-5554"
    values = acquire(capsys, "--where", "form=tablet")[1]
    assert "SERIAL" not in values
    tablet = leases()["qa_tablet"]
    assert tablet.serial is None
    # The holder starts the tablet, and keeps its lease: the lease gets the serial now.
    started = [
        {**each, "facts": {**each["facts"], "running": True, "serial": "emulator-5556"}}
        if each["name"] == "qa_tablet"
        else each
        for each in BENCH
    ]
    configure(config_path, instances=started)
    values = acquire(capsys, "--where", "form=tablet", "--lease", values["LEASE"])[1]
    assert (values["KEPT"], values["SERIAL"]) == ("true", "emulator-5556")
    assert leases()["qa_tablet"].serial == "emulator-5556"
    assert leases()["qa_tablet"].serial_seen > first.serial_seen


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
    assert leases()["qa_phone"].serial is None


def test_no_serial_is_kept_from_before_a_reset_when_discovery_fails_after_it(
    capsys, config_path, tmp_path
):
    # The hook can restart the instance on another port. A discovery that fails then cannot tell
    # which serial it has, and the serial from before the reset can be another instance's.
    found = tmp_path / "found.json"
    before = {"name": "qa_phone", "facts": {"running": True, "serial": "emulator-5554"}}
    found.write_text(json.dumps({"schema": 1, "instances": [before]}))
    broken = f"open({str(found)!r}, 'w').write('not JSON')"
    write_config(
        config_path,
        "[holder]\nagents = []\n[kinds.emulator]\n"
        f"discover = {json.dumps(hook(READ, str(found)))}\n"
        f"on_acquire = {json.dumps(hook(broken))}\n",
    )
    save_inventory(Inventory(kinds={"emulator": Decisions(allowed=("qa_phone",))}))
    status, values, _ = acquire(capsys, "--where", "serial=emulator-5554")
    assert (status, values["RESOURCE"]) == (0, "qa_phone")
    assert "SERIAL" not in values
    assert leases()["qa_phone"].serial is None


def test_a_reset_next_to_a_kept_lease_records_the_serials(capsys, config_path, tmp_path):
    record = tmp_path / "reset"
    configure(config_path, extra=f"on_acquire = {json.dumps(hook(RECORD, str(record)))}\n")
    status, kept, _ = acquire(capsys, "--where", "kind=build")
    assert status == 0
    # One part keeps the build slot, and the other gets a new emulator, which is reset.
    status, values, err = acquire(
        capsys,
        "--lease",
        kept["LEASE"],
        "--as",
        "slot",
        "--where",
        "kind=build",
        "--as",
        "phone",
        "--where",
        "form=phone",
    )
    assert status == 0, err
    assert (values["SLOT_KEPT"], values["PHONE_RESOURCE"]) == ("true", "qa_phone")
    assert values["PHONE_SERIAL"] == "emulator-5554"
    assert record.read_text() == "qa_phone\n"


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


# Joint requests: several resources at once, and accounts with their device.

PAIRED = [
    {
        "name": "R5CR0001",
        "facts": {"form": "phone", "running": True, "serial": "R5CR0001"},
        "accounts": ["qa@example.test", "me@example.com"],
    },
    {
        "name": "R5CR0002",
        "facts": {"form": "phone", "running": True, "serial": "R5CR0002"},
        "accounts": ["qa@example.test"],
    },
    # Its accounts are not known, so it is not offered for work that needs an account.
    {"name": "R5CR0003", "facts": {"form": "tablet", "running": True, "serial": "R5CR0003"}},
]


def configure_paired(config_path):
    found = json.dumps({"schema": 1, "instances": PAIRED})
    write_config(
        config_path,
        "[holder]\nagents = []\n"
        f"[kinds.device]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, found))}\n",
    )
    save_inventory(
        Inventory(
            kinds={"device": Decisions(allowed=("R5CR0001", "R5CR0002", "R5CR0003"))},
            accounts=Decisions(allowed=("qa@example.test",), refused=("me@example.com",)),
        )
    )


def test_an_account_is_leased_with_its_device(capsys, config_path):
    configure_paired(config_path)
    status, values, _ = acquire(capsys, "--where", "form=phone", "--accounts", "1")
    assert status == 0
    assert values == {
        "RESOURCE": "R5CR0001",
        "KIND": "device",
        "LEASE": leases()["R5CR0001"].lease_id,
        "STATE": "ready",
        "KEPT": "false",
        "SERIAL": "R5CR0001",
        "ACCOUNTS": "qa@example.test",
    }
    assert leases()["R5CR0001"].accounts == ("qa@example.test",)
    # The other phone is signed in to the same account, so a second run waits for it.
    status, _, err = acquire(capsys, "--where", "form=phone", "--accounts", "1")
    assert status == cli.EXIT_BUSY
    assert err == (
        "banksman: every matching resource, or the accounts on it, is in use: R5CR0001,"
        " R5CR0002\n"
    )
    # A run that does not ask for an account can still get that phone.
    _, other, _ = acquire(capsys, "--where", "form=phone")
    assert other["RESOURCE"] == "R5CR0002"
    assert main(["release", "--lease", values["LEASE"]]) == 0
    capsys.readouterr()
    status, again, _ = acquire(capsys, "--where", "kind=device", "--accounts", "1")
    assert (status, again["RESOURCE"], again["ACCOUNTS"]) == (0, "R5CR0001", "qa@example.test")


def test_status_shows_the_accounts_of_a_lease(capsys, config_path):
    configure_paired(config_path)
    acquire(capsys, "--where", "form=phone", "--accounts", "1")
    assert main(["status"]) == 0
    rows = {row["RESOURCE"]: row for row in table_rows(capsys.readouterr().out)}
    assert rows["R5CR0001"]["ACCOUNTS"] == "qa@example.test"
    assert main(["status", "--json"]) == 0
    shown = {each["resource"]: each for each in json.loads(capsys.readouterr().out)["resources"]}
    assert shown["R5CR0001"]["lease"]["accounts"] == ["qa@example.test"]


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        (
            ["--where", "form=phone", "--accounts", "2"],
            "matches form=phone with 2 allowed accounts signed in",
        ),
        (["--where", "form=tablet", "--accounts", "1"], "matches form=tablet with 1 allowed"),
    ],
)
def test_a_request_for_accounts_that_no_device_has_fails_at_once(
    capsys, config_path, arguments, problem
):
    configure_paired(config_path)
    status, _, err = acquire(capsys, *arguments, "--wait", "1h")
    assert status == 1
    assert problem in err


def test_a_joint_acquire_prints_each_part_under_its_name(capsys, config_path):
    configure(config_path)
    status, values, _ = acquire(
        capsys,
        *["--as", "phone", "--where", "form=phone"],
        *["--as", "tablet", "--where", "form=tablet"],
        *["--as", "build", "--where", "kind=build"],
    )
    assert status == 0
    held = leases()
    assert values == {
        "PHONE_RESOURCE": "qa_phone",
        "PHONE_KIND": "emulator",
        "PHONE_LEASE": held["qa_phone"].lease_id,
        "PHONE_STATE": "ready",
        "PHONE_KEPT": "false",
        "PHONE_SERIAL": "emulator-5554",
        "TABLET_RESOURCE": "qa_tablet",
        "TABLET_KIND": "emulator",
        "TABLET_LEASE": held["qa_tablet"].lease_id,
        "TABLET_STATE": "ready",
        "TABLET_KEPT": "false",
        "BUILD_RESOURCE": "build-0",
        "BUILD_KIND": "build",
        "BUILD_LEASE": held["build-0"].lease_id,
        "BUILD_STATE": "ready",
        "BUILD_KEPT": "false",
    }
    # Each lease has its own token for its scripts, and the three form one holding.
    assert len({lease.lease_id for lease in held.values()}) == 3
    assert len({lease.holding for lease in held.values()}) == 1


def test_a_joint_acquire_prints_json_with_the_names_of_the_parts(capsys, config_path):
    configure(config_path)
    arguments = ["--as", "phone", "--where", "form=phone", "--as", "build", "--json"]
    assert main(["acquire", *arguments]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [(part["part"], part["resource"], part["lease_id"]) for part in shown["parts"]] == [
        ("phone", "qa_phone", leases()["qa_phone"].lease_id),
        ("build", "build-0", leases()["build-0"].lease_id),
    ]


def test_parts_that_cannot_all_be_met_together_fail_at_once(capsys, config_path, monkeypatch):
    configure(config_path)
    monkeypatch.setattr(Machine, "sleep", lambda self, seconds: pytest.fail("it waited"))
    arguments = ["--as", "a", "--where", "form=tablet", "--as", "b", "--where", "form=tablet"]
    status, _, err = acquire(capsys, *arguments, "--wait", "1h")
    assert status == 1
    assert "cannot meet all parts of the request at the same time" in err
    status, _, err = acquire(capsys, "--as", "a", "--as", "b", "--where", "form=car")
    assert status == 1
    assert "part b: no permitted resource that is present now matches form=car" in err
    assert leases() == {}


def test_a_joint_acquire_takes_nothing_while_a_part_is_in_use(capsys, config_path):
    configure(config_path)
    acquire(capsys, "--where", "form=tablet")
    arguments = ["--as", "phone", "--where", "form=phone", "--as", "tablet"]
    status, _, err = acquire(capsys, *arguments, "--where", "form=tablet")
    assert status == cli.EXIT_BUSY
    assert err == (
        "banksman: the parts of the request cannot all be granted, because resources or accounts"
        " that they need are in use: phone: qa_phone; tablet: qa_tablet\n"
    )
    assert list(leases()) == ["qa_tablet"]


def test_a_failing_reset_gives_back_every_new_lease_of_the_request(capsys, config_path):
    configure(config_path, extra=f"on_acquire = {json.dumps(hook(FAIL))}\n")
    arguments = ["--as", "build", "--where", "kind=build", "--as", "phone"]
    status, values, err = acquire(capsys, *arguments, "--where", "form=phone")
    assert (status, values) == (1, {})
    assert err == (
        "banksman: cannot reset qa_phone: the on_acquire hook failed with exit status 5;"
        " the new leases are given back\n"
    )
    assert leases() == {}


def test_touch_and_release_act_on_every_resource_of_a_lease(capsys, config_path, state_dir):
    configure(config_path)
    arguments = ["--as", "build", "--where", "kind=build", "--as", "tablet"]
    _, values, _ = acquire(capsys, *arguments, "--where", "form=tablet")
    # The id of any lease of the holding names the holding.
    lease_id = values["TABLET_LEASE"]
    for resource in ("build-0", "qa_tablet"):
        age_lease(state_dir, resource, 15 * 60)
    assert main(["touch", "--lease", lease_id]) == 0
    for resource in ("build-0", "qa_tablet"):
        age_lease(state_dir, resource, 15 * 60)
    assert main(["reap"]) == 0
    assert sorted(leases()) == ["build-0", "qa_tablet"]
    capsys.readouterr()
    assert main(["release", "--lease", lease_id]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Released build-0: it is free.",
        "Released qa_tablet: it is free.",
    ]
    assert main(["touch", "--lease", lease_id]) == 3


# The hook makes the lease of another part of the request idle, as a short idle timeout would
# during a long reset.
AGE = (
    "import json, sys; data = json.load(open(sys.argv[1])); data['awake']['touched'] -= 1800;"
    " open(sys.argv[1], 'w').write(json.dumps(data))"
)


def test_a_lease_lost_while_the_request_is_reset_gives_back_every_new_lease(
    capsys, config_path, state_dir
):
    reset = hook(AGE, str(state_dir / "build-0.json"))
    configure(config_path, extra=f"on_acquire = {json.dumps(reset)}\n")
    arguments = ["--as", "build", "--where", "kind=build", "--as", "phone"]
    status, values, err = acquire(capsys, *arguments, "--where", "form=phone")
    assert (status, values) == (1, {})
    assert err == (
        "banksman: the lease on build-0 is void: nothing touched the lease for the idle timeout"
        " while the instances of the request were started or reset; the new leases are given"
        " back\n"
    )
    assert leases() == {}


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        (["--where", "form=phone", "--as", "tablet"], "after an --as"),
        (["--as", "phone", "--as", "phone"], "the part phone is named twice"),
        (["--as", "Phone"], "a part name starts with a lowercase letter"),
        (["--accounts", "0"], "a number from 1 to 10"),
        (["--accounts", "1", "--accounts", "2"], "--accounts is given twice for one part"),
        ([arg for index in range(9) for arg in ("--as", f"p{index}")], "at most 8 parts"),
    ],
)
def test_parts_that_are_not_valid_are_a_usage_error(capsys, arguments, problem):
    with pytest.raises(SystemExit) as exc:
        main(["acquire", *arguments])
    assert exc.value.code == 2
    assert problem in capsys.readouterr().err


def test_test_accounts_of_a_server_can_be_a_kind_that_is_leased_with_a_device(
    capsys, config_path
):
    # The accounts of a test server are not signed in on a device, so a project lists them as the
    # instances of a kind, and leases one with its device.
    configure(
        config_path,
        extra="[kinds.test-account]\n"
        'instances = ["qa+1@example.test", "qa+2@example.test"]\n',
    )
    request = ["--as", "phone", "--where", "kind=emulator", "--where", "api>=33"]
    request += ["--as", "account", "--where", "kind=test-account"]
    status, first, _ = acquire(capsys, *request)
    assert status == 0
    assert (first["PHONE_RESOURCE"], first["ACCOUNT_RESOURCE"]) == ("qa_phone", "qa+1@example.test")
    status, second, _ = acquire(capsys, *request)
    assert status == 0
    assert (second["PHONE_RESOURCE"], second["ACCOUNT_RESOURCE"]) == (
        "qa_tablet",
        "qa+2@example.test",
    )
    status, _, _ = acquire(capsys, *request)
    assert status == cli.EXIT_BUSY
