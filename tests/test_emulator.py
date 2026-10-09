"""The start of emulators, the emulators that banksman knows, and unmanaged instances, against a
fake emulator command and a fake adb."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from helpers import SRC, FakeSdk, FakeSystem, edit_lease, table_rows, write_config

from banksman import emulator
from banksman.cli import main
from banksman.config import Kind
from banksman.errors import BanksmanError
from banksman.inventory import Decisions, Inventory, save_inventory
from banksman.lease import Holder
from banksman.store import NotHeld, Store, default_state_dir
from banksman.system import Machine

AVDS = ("qa_phone", "qa_tablet", "qa_watch")


@pytest.fixture
def sdk(tmp_path):
    fake = FakeSdk(tmp_path, avds=AVDS)
    yield fake
    fake.stop_emulators()


@pytest.fixture(autouse=True)
def ports(monkeypatch):
    # No test depends on the ports of the machine: every console port is free, except the ones
    # that a test takes.
    taken = set()
    monkeypatch.setattr(emulator, "port_free", lambda port: port not in taken)
    return taken


def configure(config_path, sdk, keys=""):
    write_config(
        config_path,
        f"[holder]\nagents = []\n{sdk.config()}[kinds.emulator]\npreset = \"android-emulator\"\n"
        f"{keys}",
    )
    save_inventory(Inventory(kinds={"emulator": Decisions(allowed=AVDS)}))
    # adb runs, and lists no emulator yet.
    sdk.running({})


def kind(**changes):
    return Kind("emulator", preset="android-emulator", **changes)


def acquire(capsys, *arguments):
    status = main(["acquire", *arguments])
    captured = capsys.readouterr()
    values = dict(line.split("=", 1) for line in captured.out.splitlines() if "=" in line)
    return status, values, captured.err


def leases():
    snapshot = Store(default_state_dir(), Machine()).snapshot()
    return {lease.resource: lease for lease in snapshot.leases}


def known():
    return emulator.load(kind(), Machine())


def runs(pid):
    return pid in Machine().running([pid])


def wait_for(condition, seconds=20.0):
    give_up = time.monotonic() + seconds
    while time.monotonic() < give_up:
        if condition():
            return
        time.sleep(0.05)
    pytest.fail("the condition did not come true in time")


def listed(sdk, serial):
    answers = json.loads((sdk.sdk / "platform-tools" / "answers.json").read_text())
    return f"-s {serial} shell getprop sys.boot_completed" in answers


def states(capsys):
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)["resources"]
    return {each["resource"]: each["state"] for each in shown}


# The command lines of emulators.


@pytest.mark.parametrize(
    ("arguments", "avd"),
    [
        (("/sdk/emulator/emulator", "-avd", "qa_phone", "-port", "5556"), "qa_phone"),
        (("/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64-headless", "-avd", "qa"), "qa"),
        (("/sdk/emulator/emulator", "@qa_phone", "-no-window"), "qa_phone"),
        (("/usr/bin/javac", "@qa_phone"), None),
        (("/sdk/emulator/emulator", "-list-avds"), None),
        (("/sdk/emulator/emulator", "-avd"), None),
        ((), None),
    ],
)
def test_the_avd_of_a_command_line(arguments, avd):
    assert emulator.avd_of(arguments) == avd


# The record of the emulators that banksman knows.


def test_the_record_keeps_only_emulators_that_still_run_on_this_boot(system):
    system.spawn(300)
    system.spawn(301)
    with emulator.changing(kind(), system) as record:
        for name, pid in (("qa_phone", 300), ("qa_tablet", 301), ("qa_watch", 302)):
            record[name] = emulator.Known(name, pid, f"start-{pid}", "boot-1", True, 1.0)
    system.end(301)
    assert sorted(emulator.load(kind(), system)) == ["qa_phone"]
    system.boot = "boot-2"
    assert emulator.load(kind(), system) == {}


def test_a_stop_ends_the_emulator_process_only(system):
    system.spawn(300, group=290)
    system.spawn(301, group=290)
    assert emulator.stop(system, 300, "start-300", clock=system.clock, sleep=system.sleep)
    assert system.signals == [("pid", 300, signal.SIGTERM)]
    # Another process of its group, such as the netsimd daemon of every emulator, still runs.
    assert 301 in system.processes


def test_an_emulator_that_ignores_sigterm_gets_sigkill(system):
    system.spawn(300)
    system.ignores[300] = {signal.SIGTERM}
    assert emulator.stop(system, 300, "start-300", clock=system.clock, sleep=system.sleep)
    assert system.signals == [("pid", 300, signal.SIGTERM), ("pid", 300, signal.SIGKILL)]


def test_a_stop_never_signals_a_pid_that_another_process_has_now(system):
    system.spawn(300)
    assert emulator.stop(system, 300, "start-of-an-earlier-process")
    assert system.signals == []


# The serial before the boot.


def test_two_starts_never_claim_the_same_serial(state_dir, system):
    store = Store(state_dir, system)
    holder = Holder("/work/a")
    first = store.reserve("qa_phone", "emulator", holder)
    second = store.reserve("qa_tablet", "emulator", holder)
    serials = ["emulator-5554", "emulator-5556"]
    assert store.claim_serial("qa_phone", first.lease_id, serials).serial == "emulator-5554"
    assert store.claim_serial("qa_tablet", second.lease_id, serials).serial == "emulator-5556"
    with pytest.raises(BanksmanError, match="recorded by another lease"):
        store.claim_serial("qa_tablet", second.lease_id, serials[:1])


def test_a_lease_that_is_void_claims_no_serial(state_dir, system):
    store = Store(state_dir, system)
    lease = store.reserve("qa_phone", "emulator", Holder("/work/a"))
    system.advance(lease.boot_deadline - system.clock())
    with pytest.raises(NotHeld):
        store.claim_serial("qa_phone", lease.lease_id, ["emulator-5554"])


# The start.


def test_start_boots_the_avd_on_a_console_port_that_banksman_chose(
    capsys, config_path, sdk, ports, monkeypatch
):
    configure(config_path, sdk, 'start_args = ["-no-window", "-no-snapshot-save"]\n')
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5590")
    ports.add(5554)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert (values["RESOURCE"], values["STATE"], values["SERIAL"]) == (
        "qa_phone",
        "ready",
        "emulator-5556",
    )
    assert sdk.emulator_calls() == ["-avd qa_phone -port 5556 -no-window -no-snapshot-save"]
    # The emulator finds the SDK and the AVDs of banksman, and no variable of the caller.
    assert sdk.emulator_environments() == [
        {
            "ANDROID_HOME": str(sdk.sdk),
            "ANDROID_SDK_ROOT": str(sdk.sdk),
            "ANDROID_AVD_HOME": str(sdk.avd_home),
            "ANDROID_SERIAL": None,
            "BANKSMAN_LEASE": None,
        }
    ]
    lease = leases()["qa_phone"]
    assert (lease.state, lease.serial, lease.boot_deadline, lease.reaper_pid) == (
        "ready",
        "emulator-5556",
        None,
        None,
    )
    record = known()["qa_phone"]
    assert (record.created, record.port, runs(record.pid)) == (True, 5556, True)
    # The emulator runs in a session of its own, so it outlives this command.
    assert os.getsid(record.pid) == record.pid


def test_without_start_banksman_starts_nothing(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone")
    assert (status, values["STATE"], "SERIAL" in values) == (0, "ready", False), err
    assert sdk.emulator_calls() == []


def test_two_starts_at_the_same_time_get_different_serials(config_path, sdk):
    configure(config_path, sdk)
    sdk.emulator_behaves(boot=1)
    # Separate processes, so that the starts really overlap. They see the real ports of the
    # machine, so the test does not depend on which ports they get.
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "banksman", "acquire", "--where", f"avd={avd}", "--start"],
            env={**os.environ, "PYTHONPATH": SRC},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for avd in ("qa_phone", "qa_tablet")
    ]
    serials = []
    for process in processes:
        out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
        serials.append(dict(line.split("=", 1) for line in out.splitlines())["SERIAL"])
    assert len(set(serials)) == 2
    assert {call.split()[3] for call in sdk.emulator_calls()} == {
        serial.removeprefix("emulator-") for serial in serials
    }


def test_the_starts_of_one_request_get_different_serials(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, values, err = acquire(
        capsys,
        "--as", "phone", "--where", "avd=qa_phone",
        "--as", "tablet", "--where", "avd=qa_tablet",
        "--start",
    )  # fmt: skip
    assert status == 0, err
    assert (values["PHONE_SERIAL"], values["TABLET_SERIAL"]) == ("emulator-5554", "emulator-5556")


def test_a_boot_that_does_not_complete_by_the_boot_deadline_ends_the_emulator(
    capsys, config_path, sdk
):
    configure(config_path, sdk, 'boot_timeout = "2s"\n')
    sdk.emulator_behaves(boot=None)
    started = time.monotonic()
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert time.monotonic() - started < 30
    assert (status, values) == (1, {})
    assert (
        "cannot start qa_phone: the emulator of qa_phone did not complete its boot by the boot"
        " deadline; the lease is given back"
    ) in err
    assert leases() == {}
    assert known() == {}
    (pid,) = sdk.emulator_pids()
    wait_for(lambda: not runs(pid))


def test_an_emulator_that_ends_at_once_says_why(capsys, config_path, sdk):
    configure(config_path, sdk)
    sdk.emulator_behaves(exit=1, say="ERROR | Running multiple emulators with the same AVD")
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 1
    assert (
        "the emulator of qa_phone ended with exit status 1 before its boot completed: ERROR |"
        " Running multiple emulators with the same AVD; the lease is given back"
    ) in err
    assert leases() == {}
    assert known() == {}


def test_another_emulator_at_the_port_fails_the_start(capsys, config_path, sdk):
    configure(config_path, sdk)
    sdk.emulator_behaves(name="Personal_AVD")
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 1
    assert "emulator-5554 is not the emulator of qa_phone but of Personal_AVD" in err
    assert leases() == {}


def test_without_a_free_console_port_nothing_starts(capsys, config_path, sdk, ports):
    configure(config_path, sdk)
    ports.update(range(5554, 5586))
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 1
    assert "no console port from 5554 to 5584 is free" in err
    assert sdk.emulator_calls() == []


def test_a_start_that_is_killed_leaves_the_emulator_to_the_reaper(config_path, sdk, state_dir):
    configure(config_path, sdk)
    sdk.emulator_behaves(boot=None)
    process = subprocess.Popen(
        [sys.executable, "-m", "banksman", "acquire", "--where", "avd=qa_phone", "--start"],
        env={**os.environ, "PYTHONPATH": SRC},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: "qa_phone" in known())
        # The lease is booting, and it records the serial before the boot completes, so the adb
        # guard protects the emulator from the start.
        lease = leases()["qa_phone"]
        assert lease.state == "booting"
        assert lease.serial == f"emulator-{known()['qa_phone'].port}"
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    pid = known()["qa_phone"].pid
    assert runs(pid)
    edit_lease(state_dir, "qa_phone", lambda data: data["awake"].update(boot_deadline=0))
    assert main(["reap"]) == 0
    assert not runs(pid)
    assert leases() == {}
    assert known() == {}


# Instances that banksman did not start.


def test_an_emulator_that_banksman_did_not_start_is_unmanaged_and_in_use(
    capsys, config_path, sdk
):
    configure(config_path, sdk)
    sdk.running({"emulator-5554": "qa_phone"})
    assert main(["status"]) == 0
    rows = {row["RESOURCE"]: row for row in table_rows(capsys.readouterr().out)}
    assert (rows["qa_phone"]["STATE"], rows["qa_phone"]["SERIAL"]) == ("unmanaged", "emulator-5554")
    assert rows["qa_tablet"]["STATE"] == "free"
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 4
    assert "qa_phone runs, but banksman did not start it" in err
    assert "every matching resource is in use: qa_phone" in err
    assert sdk.emulator_calls() == []


def test_a_request_that_needs_any_emulator_gets_one_that_nobody_uses(capsys, config_path, sdk):
    configure(config_path, sdk)
    sdk.running({"emulator-5554": "qa_phone"})
    status, values, err = acquire(capsys, "--where", "kind=emulator")
    assert (status, values["RESOURCE"]) == (0, "qa_tablet"), err


def test_an_emulator_that_adb_does_not_list_yet_runs(capsys, config_path, sdk):
    configure(config_path, sdk)
    sdk.start_emulator("qa_phone", 5600, listed=False, boot=None)
    wait_for(lambda: "qa_phone" in emulator.by_avd(Machine().commands()))
    assert states(capsys)["qa_phone"] == "unmanaged"
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 4, err
    # Only the emulator of the person ran: banksman did not start a second one.
    assert len(sdk.emulator_calls()) == 1


def test_the_start_refuses_an_avd_that_runs_already(sdk):
    sdk.start_emulator("qa_phone", 5600, listed=False, boot=None)
    wait_for(lambda: "qa_phone" in emulator.by_avd(Machine().commands()))
    with pytest.raises(BanksmanError, match="an emulator of qa_phone runs already"):
        emulator.start(
            kind(), _android(sdk), "qa_phone", deadline=time.monotonic() + 60, claim=_no_claim
        )


def test_with_grant_an_unmanaged_emulator_comes_after_the_ones_that_banksman_knows(
    capsys, config_path, sdk
):
    configure(config_path, sdk, 'unmanaged = "grant"\n')
    sdk.running({"emulator-5554": "qa_phone", "emulator-5556": "qa_tablet"})
    machine = Machine()
    me = os.getpid()
    with emulator.changing(kind(), machine) as record:
        # This process stands for the emulator of qa_tablet. banksman did not start it, so no
        # take-back ever signals it.
        record["qa_tablet"] = emulator.Known(
            "qa_tablet", me, machine.running([me])[me], machine.boot_id(), False, time.time()
        )
    granted = []
    for _ in AVDS:
        status, values, err = acquire(capsys, "--where", "kind=emulator")
        assert status == 0, err
        granted.append(values["RESOURCE"])
    assert granted == ["qa_tablet", "qa_phone", "qa_watch"]


def test_a_release_records_the_emulator_that_the_holder_started(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 0, err
    # The holder starts the emulator itself, as without --start.
    holder = sdk.start_emulator("qa_phone", 5556)
    wait_for(lambda: listed(sdk, "emulator-5556"))
    assert main(["release", "--lease", values["LEASE"]]) == 0
    record = known()["qa_phone"]
    assert (record.pid, record.created) == (holder.pid, False)
    assert states(capsys)["qa_phone"] == "free"
    status, again, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert (status, again["SERIAL"]) == (0, "emulator-5556"), err
    assert len(sdk.emulator_calls()) == 1


def test_a_release_keeps_the_emulator_that_banksman_started(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    capsys.readouterr()
    record = known()["qa_phone"]
    assert (record.created, runs(record.pid)) == (True, True)
    status, again, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert (status, again["SERIAL"], again["STATE"]) == (0, values["SERIAL"], "ready"), err
    assert len(sdk.emulator_calls()) == 1


def test_run_with_where_and_start_leaves_the_emulator_running(capsys, config_path, sdk, tmp_path):
    configure(config_path, sdk)
    shown = tmp_path / "shown"
    code = (
        "import os, sys;"
        " open(sys.argv[1], 'w').write(sys.argv[2] + ' ' + os.environ['ANDROID_SERIAL'])"
    )
    status = main(
        [
            "run", "--where", "avd=qa_phone", "--start", "--",
            sys.executable, "-c", code, str(shown), "{serial}",
        ]
    )  # fmt: skip
    assert status == 0, capsys.readouterr().err
    assert shown.read_text() == "emulator-5554 emulator-5554"
    assert leases() == {}
    assert runs(known()["qa_phone"].pid)


# The take-back.


def test_a_void_lease_ends_the_emulator_that_banksman_started(
    capsys, config_path, sdk, state_dir
):
    configure(config_path, sdk)
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    pid = known()["qa_phone"].pid
    edit_lease(state_dir, "qa_phone", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert not runs(pid)
    assert leases() == {}
    assert known() == {}


def test_a_void_lease_ends_an_emulator_that_banksman_started_for_an_earlier_lease(
    capsys, config_path, sdk, state_dir
):
    configure(config_path, sdk)
    status, first, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", first["LEASE"]]) == 0
    status, _, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 0, err
    pid = known()["qa_phone"].pid
    edit_lease(state_dir, "qa_phone", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert not runs(pid)


def test_a_void_lease_leaves_the_emulator_that_the_holder_started(
    capsys, config_path, sdk, state_dir
):
    configure(config_path, sdk)
    status, _, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 0, err
    holder = sdk.start_emulator("qa_phone", 5556)
    wait_for(lambda: listed(sdk, "emulator-5556"))
    edit_lease(state_dir, "qa_phone", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert runs(holder.pid)
    assert leases() == {}
    # It ran when its lease ended, so it is free for the next request, not unmanaged.
    assert known()["qa_phone"].created is False
    assert states(capsys)["qa_phone"] == "free"


def test_an_emulator_that_does_not_end_quarantines_the_avd(
    capsys, config_path, sdk, state_dir, monkeypatch
):
    configure(config_path, sdk)
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    monkeypatch.setattr(emulator, "stop", lambda system, pid, started: False)
    edit_lease(state_dir, "qa_phone", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    assert "cannot take back qa_phone: the emulator of qa_phone (pid" in capsys.readouterr().err
    assert leases()["qa_phone"].state == "quarantined"


def test_a_hook_of_the_kind_runs_after_the_end(capsys, config_path, sdk, state_dir, tmp_path):
    out = tmp_path / "hook"
    code = "import os, sys; open(sys.argv[1], 'w').write(os.environ.get('BANKSMAN_SERIAL', '-'))"
    hook = json.dumps([sys.executable, "-c", code, str(out)])
    configure(config_path, sdk, f"on_void = {hook}\n")
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    edit_lease(state_dir, "qa_phone", lambda data: data["awake"].update(hard_deadline=0))
    assert main(["reap"]) == 0
    # The emulator has ended, so the hook gets no serial.
    assert out.read_text() == "-"


def _android(sdk):
    from banksman.config import Android

    return Android(sdk=sdk.sdk, avd_home=sdk.avd_home)


def _no_claim(serials):
    raise AssertionError("the start claimed a serial")


def test_a_fake_system_gives_the_command_lines(system: FakeSystem):
    system.spawn(300)
    system.arguments[300] = ("/sdk/emulator/emulator", "-avd", "qa_phone")
    assert emulator.by_avd(system.commands())["qa_phone"][0].pid == 300
