"""The start of emulators, the emulators that banksman knows, and unmanaged instances, against a
fake emulator command and a fake adb."""

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest
import shapes
from helpers import (
    SRC,
    FakeSdk,
    FakeSystem,
    edit_lease,
    reap_in_child,
    table_rows,
    work_in,
    write_config,
)

from banksman import android, cli, emulator
from banksman.cli import main
from banksman.config import Android, Kind
from banksman.errors import BanksmanError
from banksman.identity import StartedBy
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


def configure(config_path, sdk, keys="", agents=()):
    write_config(
        config_path,
        f"[holder]\nagents = {json.dumps(list(agents))}\n{sdk.config()}[kinds.emulator]\n"
        f'preset = "android-emulator"\n{keys}',
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
        # A shell gets its script as one argument, so the emulator command in it is not its own.
        (("/bin/zsh", "-c", "banksman acquire --start && emulator -avd qa_phone"), None),
        (("/sdk/emulator/emulator", "-list-avds"), None),
        (("/sdk/emulator/emulator", "-avd"), None),
        ((), None),
    ],
)
def test_the_avd_of_a_command_line(arguments, avd):
    assert emulator.avd_of(arguments) == avd


def test_a_shell_whose_script_starts_an_emulator_is_not_an_emulator():
    # As the shell tool of an agent runs a command: the whole script is one argument.
    shell = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30; emulator -avd qa_shell_avd -no-window"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        table = Machine().commands()
        assert table[shell.pid].arguments[-1].startswith("sleep 30; emulator -avd")
        assert "qa_shell_avd" not in emulator.by_avd(table)
    finally:
        shell.kill()
        shell.wait()


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


def test_a_record_of_schema_1_is_read_without_free_times(system):
    system.spawn(300)
    entry = {
        "resource": "qa_phone",
        "pid": 300,
        "started": "start-300",
        "boot": "boot-1",
        "created": True,
        "at": 1.0,
        "port": 5554,
    }
    path = emulator.default_emulator_dir() / "emulator-emulators.json"
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text(json.dumps({"schema": 1, "emulators": [entry]}))
    path.chmod(0o600)
    record = emulator.load(kind(), system)["qa_phone"]
    assert (record.free_since, record.serial) == (None, "emulator-5554")


@pytest.mark.parametrize("schema", [[], {}, True, 3])
def test_a_record_with_a_schema_that_banksman_does_not_read_is_not_valid(system, schema):
    path = emulator.default_emulator_dir() / "emulator-emulators.json"
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text(json.dumps({"schema": schema, "emulators": []}))
    path.chmod(0o600)
    with pytest.raises(emulator.EmulatorError, match="is not valid"):
        emulator.load(kind(), system)


def started_by_banksman(system, *names):
    for pid, name in enumerate(names, 300):
        system.spawn(pid)
        with emulator.changing(kind(), system) as record:
            record[name] = emulator.Known(name, pid, f"start-{pid}", "boot-1", True, 1.0, 5554)


def free_since(system):
    return {name: entry.free_since for name, entry in emulator.load(kind(), system).items()}


def test_the_free_time_of_an_avd_counts_from_when_its_last_lease_ended(system):
    free = kind(free_timeout=30 * 60)
    started_by_banksman(system, "qa_phone", "qa_tablet")
    start = system.clock()
    assert emulator.unused(free, {"qa_tablet"}, start, system) == []
    assert free_since(system) == {"qa_phone": start, "qa_tablet": None}
    system.advance(30 * 60 - 1)
    assert emulator.unused(free, {"qa_tablet"}, system.clock(), system) == []
    system.advance(1)
    assert [each.resource for each in emulator.unused(free, set(), system.clock(), system)] == [
        "qa_phone"
    ]
    # A lease of an AVD sets its free time back.
    assert emulator.unused(free, {"qa_phone"}, system.clock(), system) == []
    assert free_since(system) == {"qa_phone": None, "qa_tablet": start + 30 * 60}


def test_without_a_free_timeout_the_record_does_not_change(system):
    started_by_banksman(system, "qa_phone")
    assert emulator.unused(kind(), set(), system.clock(), system) == []
    assert free_since(system) == {"qa_phone": None}


def test_the_free_timeout_never_ends_an_emulator_that_banksman_did_not_start(system):
    system.spawn(300)
    system.arguments[300] = ("/sdk/emulator/emulator", "-avd", "qa_phone")
    emulator.adopt(kind(), "qa_phone", system)
    assert free_since(system) == {"qa_phone": system.clock()}
    system.advance(24 * 60 * 60)
    assert emulator.unused(kind(free_timeout=60), set(), system.clock(), system) == []


def test_a_release_records_when_the_lease_of_the_avd_ended(system):
    started_by_banksman(system, "qa_phone")
    system.arguments[300] = ("/sdk/emulator/emulator", "-avd", "qa_phone")
    system.advance(60)
    emulator.adopt(kind(), "qa_phone", system)
    record = emulator.load(kind(), system)["qa_phone"]
    assert (record.created, record.free_since) == (True, system.clock())


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
    assert (values["RESOURCE"], values["STATE"], values["SERIAL"], values["STARTED"]) == (
        "qa_phone",
        "ready",
        "emulator-5556",
        "true",
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
    assert values["STARTED"] == "false"
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


# The tests name their agent process with --owner-pid.
ME = ["--owner-pid", str(os.getpid())]


def acquire_in_child(*arguments):
    return subprocess.Popen(
        [sys.executable, "-m", "banksman", "acquire", *arguments],
        env={**os.environ, "PYTHONPATH": SRC},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_a_start_that_is_stopped_is_taken_back_at_the_next_command(config_path, sdk, signum):
    configure(config_path, sdk)
    sdk.emulator_behaves(boot=None)
    process = acquire_in_child("--where", "avd=qa_phone", "--start")
    try:
        wait_for(lambda: "qa_phone" in known())
        # The lease is booting, and it records the serial before the boot completes, so the adb
        # guard protects the emulator from the start.
        lease = leases()["qa_phone"]
        assert lease.state == "booting"
        assert lease.serial == f"emulator-{known()['qa_phone'].port}"
    finally:
        # Only the acquire: the emulator runs in a session of its own.
        os.killpg(process.pid, signum)
        process.wait()
    pid = known()["qa_phone"].pid
    assert runs(pid)
    # Nothing can mark the lease ready any more, so it does not wait for its boot deadline.
    assert main(["reap"]) == 0
    assert not runs(pid)
    assert leases() == {}
    assert known() == {}


def test_after_a_stop_of_a_start_the_same_label_starts_again_at_once(capsys, config_path, sdk):
    configure(config_path, sdk)
    sdk.emulator_behaves(boot=None)
    label = ["--holding", "ui-tests", "--where", "avd=qa_phone", "--start", *ME]
    process = acquire_in_child(*label)
    try:
        wait_for(lambda: "qa_phone" in known())
        stopped = known()["qa_phone"].pid
    finally:
        # The tool of an agent stops a command by signalling every process below it, also the
        # emulator in its own session.
        os.killpg(process.pid, signal.SIGTERM)
        os.kill(stopped, signal.SIGTERM)
        process.wait()
    wait_for(lambda: not runs(stopped))
    sdk.emulator_behaves(boot=0)
    status, values, err = acquire(capsys, *label)
    assert status == 0, err
    assert (values["RESOURCE"], values["KEPT"]) == ("qa_phone", "false")
    assert leases()["qa_phone"].state == "ready"
    assert known()["qa_phone"].pid != stopped


# A start that continues after its acquire: acquire --no-wait, and banksman wait.

NO_WAIT = ["--holding", "ui", "--where", "avd=qa_phone", "--start", "--no-wait", *ME]


def no_wait_in_child(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "banksman", "acquire", *arguments],
        env={**os.environ, "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
        timeout=60,
    )


def values_of(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


@pytest.fixture
def starters():
    """The processes that continue the starts of a test: each ends when the test does."""
    pids = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def starter(starters, err):
    """Return the process that continues the start, which the note of acquire names."""
    found = re.search(r"banksman process (\d+) starts or resets", err)
    assert found is not None, err
    pid = int(found[1])
    starters.append(pid)
    return pid


def descendants(pid):
    table = Machine().process_table()
    found, more = set(), {pid}
    while more:
        found |= more
        more = {each for each, process in table.items() if process.ppid in more} - found
    return found - {pid}


def test_acquire_with_no_wait_returns_while_its_start_goes_on_in_a_process_of_its_own(
    capsys, config_path, sdk, starters
):
    configure(config_path, sdk)
    sdk.emulator_behaves(boot=2)
    done = no_wait_in_child(*NO_WAIT, "--json")
    assert done.returncode == 0, done.stderr
    (part,) = json.loads(done.stdout)["parts"]
    assert (part["resource"], part["state"], part["kept"], part["started"]) == (
        "qa_phone",
        "booting",
        False,
        True,
    )
    assert "banksman wait --holding ui --wait 5m waits until it is ready" in done.stderr
    pid = starter(starters, done.stderr)
    assert leases()["qa_phone"].reaper_pid == pid
    # The acquire has ended, and the lease names the process that continues the start. That
    # process left the process tree and the session of the test.
    assert runs(pid)
    assert Machine().process_table()[pid].ppid != os.getpid()
    assert os.getsid(pid) != os.getsid(0)
    assert main(["wait", "--holding", "ui", "--wait", "60s", *ME]) == 0
    (row,) = table_rows(capsys.readouterr().out)
    record = known()["qa_phone"]
    assert (row["STATE"], row["SERIAL"]) == ("ready", f"emulator-{record.port}")
    lease = leases()["qa_phone"]
    assert (lease.state, lease.reaper_pid, lease.label) == ("ready", None, "ui")
    assert (record.created, runs(record.pid)) == (True, True)
    wait_for(lambda: not runs(pid))
    # The same request keeps the holding now.
    status, values, err = acquire(capsys, *NO_WAIT)
    assert status == 0, err
    assert (values["KEPT"], values["STATE"], values["STARTED"]) == ("true", "ready", "false")


def test_a_start_that_continues_outlives_a_stop_of_the_command_that_ran_acquire(
    config_path, sdk, starters, tmp_path
):
    configure(config_path, sdk)
    sdk.emulator_behaves(boot=3)
    out = tmp_path / "grant"
    acquire_line = " ".join(
        [sys.executable, "-m", "banksman", "acquire", *NO_WAIT, ">", str(out), "2>&1"]
    )
    # The command of an agent's tool, which goes on after acquire, until the tool stops it.
    command = subprocess.Popen(
        ["/bin/sh", "-c", f"{acquire_line}; sleep 60"],
        env={**os.environ, "PYTHONPATH": SRC},
        start_new_session=True,
    )
    try:
        wait_for(lambda: "STATE=booting" in (out.read_text() if out.exists() else ""))
        pid = starter(starters, out.read_text())
        assert pid not in descendants(command.pid)
    finally:
        # The tool stops every process below the command, and the process group of the command.
        for each in descendants(command.pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(each, signal.SIGKILL)
        os.killpg(command.pid, signal.SIGKILL)
        command.wait()
    assert values_of(out.read_text())["STARTED"] == "true"
    assert runs(pid)
    status = subprocess.run(
        [sys.executable, "-m", "banksman", "wait", "--holding", "ui", "--wait", "60s", *ME],
        env={**os.environ, "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert status.returncode == 0, status.stderr
    assert leases()["qa_phone"].state == "ready"


def test_a_start_that_continues_and_dies_frees_the_instance(capsys, config_path, sdk, starters):
    configure(config_path, sdk)
    sdk.emulator_behaves(boot=None)
    done = no_wait_in_child(*NO_WAIT)
    assert done.returncode == 0, done.stderr
    pid = starter(starters, done.stderr)
    wait_for(lambda: "qa_phone" in known())
    emulator_pid = known()["qa_phone"].pid
    os.kill(pid, signal.SIGKILL)
    wait_for(lambda: not runs(pid))
    # wait reaps first, so it frees the instance, and then finds no holding.
    assert main(["wait", "--holding", "ui", *ME]) == cli.EXIT_LOST
    assert "has no held holding with the label ui" in capsys.readouterr().err
    assert not runs(emulator_pid)
    assert leases() == {}
    assert known() == {}


def test_a_start_that_continues_writes_its_error_into_a_line_of_its_own_in_the_trace(
    config_path, sdk, starters, trace_path
):
    # Its standard error goes nowhere, so the trace is the only place where its error is kept.
    configure(config_path, sdk, keys="[log]\ntrace = true\n")
    sdk.emulator_behaves(exit=1, say="ERROR | the AVD is broken")
    done = no_wait_in_child(*NO_WAIT)
    assert done.returncode == 0, done.stderr
    pid = starter(starters, done.stderr)
    wait_for(lambda: not runs(pid))
    wait_for(lambda: len(trace_path.read_text().splitlines()) == 2)
    lines = [json.loads(line) for line in trace_path.read_text().splitlines()]
    for line in lines:
        shapes.check_trace_line(line)
    (acquire_line,) = [line for line in lines if line["continues"] is None]
    (start_line,) = [line for line in lines if line["continues"] is not None]
    assert (acquire_line["command"][0], acquire_line["status"]) == ("acquire", 0)
    assert start_line["continues"] == acquire_line["pid"]
    assert (start_line["pid"], start_line["status"]) == (pid, 1)
    assert "ERROR | the AVD is broken" in start_line["error"]
    # The steps of the acquire stay in its own line.
    assert [step["step"] for step in start_line["steps"]].count("search") == 0


def test_a_start_that_continues_and_fails_gives_its_lease_back(capsys, config_path, sdk, starters):
    configure(config_path, sdk)
    sdk.emulator_behaves(exit=1, say="ERROR | the AVD is broken")
    done = no_wait_in_child(*NO_WAIT)
    assert done.returncode == 0, done.stderr
    pid = starter(starters, done.stderr)
    wait_for(lambda: not runs(pid))
    assert main(["wait", "--holding", "ui", "--wait", "30s", *ME]) == cli.EXIT_LOST
    assert "acquire again without --no-wait to see why a start fails" in capsys.readouterr().err
    assert leases() == {}
    status, _, err = acquire(capsys, "--holding", "ui", "--where", "avd=qa_phone", "--start", *ME)
    assert status == 1
    assert "ERROR | the AVD is broken" in err


def test_a_release_stops_a_start_that_continues(capsys, config_path, sdk, starters):
    configure(config_path, sdk)
    sdk.emulator_behaves(boot=None)
    done = no_wait_in_child(*NO_WAIT)
    assert done.returncode == 0, done.stderr
    pid = starter(starters, done.stderr)
    wait_for(lambda: "qa_phone" in known())
    emulator_pid = known()["qa_phone"].pid
    assert main(["release", "--holding", "ui", *ME]) == 0
    assert f"it is free when banksman process {pid} has ended" in capsys.readouterr().out
    # The start sees that its lease is no longer booting, and ends the emulator that it started.
    wait_for(lambda: not runs(pid))
    assert not runs(emulator_pid)
    assert main(["reap"]) == 0
    assert leases() == {}


@pytest.mark.parametrize(
    "request_",
    [
        # The request keeps the phone, and adds the tablet.
        ["--as", "phone", "--where", "avd=qa_phone", "--as", "tablet", "--where", "avd=qa_tablet"],
        # The request names only the tablet, which joins the holding of the phone all the same.
        ["--where", "avd=qa_tablet"],
    ],
)
def test_no_wait_refuses_a_request_that_joins_a_holding_with_other_leases(
    capsys, config_path, sdk, request_
):
    configure(config_path, sdk)
    status, _, err = acquire(capsys, "--holding", "ui", "--where", "avd=qa_phone", *ME)
    assert status == 0, err
    status, values, err = acquire(
        capsys, "--holding", "ui", *request_, "--start", "--no-wait", *ME
    )
    assert (status, values) == (1, {})
    assert "this request joins a holding that has other leases: qa_phone" in err
    assert "the new lease is given back" in err
    assert set(leases()) == {"qa_phone"}
    assert sdk.emulator_calls() == []


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
    sdk.running({"emulator-5554": "qa_phone"})
    # A holder started this one under a lease that has ended, so banksman knows it.
    sdk.start_emulator("qa_tablet", 5556)
    wait_for(lambda: listed(sdk, "emulator-5556"))
    emulator.adopt(kind(), "qa_tablet")
    granted = []
    for _ in AVDS:
        status, values, err = acquire(capsys, "--where", "kind=emulator")
        assert status == 0, err
        granted.append(values["RESOURCE"])
    assert granted == ["qa_tablet", "qa_phone", "qa_watch"]


def test_a_second_emulator_of_an_avd_makes_it_unmanaged_and_without_a_serial(
    capsys, config_path, sdk
):
    configure(config_path, sdk, 'start_args = ["-read-only"]\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    # A person starts a second, read-only emulator of the same AVD, and adb lists both.
    sdk.start_emulator("qa_phone", 5600)
    wait_for(lambda: listed(sdk, "emulator-5600"))
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    (phone,) = [
        each
        for each in json.loads(capsys.readouterr().out)["resources"]
        if each["resource"] == "qa_phone"
    ]
    assert (phone["state"], phone["serial"]) == ("unmanaged", None)
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 4, err
    assert len(sdk.emulator_calls()) == 2


def test_a_held_lease_keeps_its_serial_when_a_second_emulator_of_its_avd_starts(
    capsys, config_path, sdk
):
    configure(config_path, sdk, 'start_args = ["-read-only"]\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    sdk.start_emulator("qa_phone", 5600)
    wait_for(lambda: listed(sdk, "emulator-5600"))
    assert main(["status"]) == 0
    assert leases()["qa_phone"].serial == values["SERIAL"] == "emulator-5554"


def test_a_release_records_nothing_when_the_avd_runs_as_several_emulators(
    capsys, config_path, sdk
):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 0, err
    sdk.start_emulator("qa_phone", 5556)
    sdk.start_emulator("qa_phone", 5558)
    wait_for(lambda: listed(sdk, "emulator-5556") and listed(sdk, "emulator-5558"))
    assert main(["release", "--lease", values["LEASE"]]) == 0
    assert known() == {}


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
    assert (status, again["SERIAL"], again["STARTED"]) == (0, "emulator-5556", "false"), err
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
    # The emulator ran before this request, so another holder can still need it.
    assert (values["STARTED"], again["STARTED"]) == ("true", "false")
    assert len(sdk.emulator_calls()) == 1


def test_a_kept_lease_did_not_start_its_emulator_in_this_request(capsys, config_path, sdk):
    configure(config_path, sdk)
    label = ["--holding", "ui-tests", "--where", "avd=qa_phone", "--start", *ME]
    assert main(["acquire", *label, "--json"]) == 0
    (part,) = json.loads(capsys.readouterr().out)["parts"]
    assert (part["kept"], part["started"]) == (False, True)
    status, again, err = acquire(capsys, *label)
    assert (status, again["KEPT"], again["STARTED"]) == (0, "true", "false"), err


def test_each_part_says_whether_this_request_started_its_emulator(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, tablet, err = acquire(capsys, "--where", "avd=qa_tablet", "--start")
    assert status == 0, err
    assert main(["release", "--lease", tablet["LEASE"]]) == 0
    status, values, err = acquire(
        capsys,
        *["--as", "phone", "--where", "avd=qa_phone"],
        *["--as", "tablet", "--where", "avd=qa_tablet"],
        "--start",
    )
    assert status == 0, err
    assert (values["PHONE_STARTED"], values["TABLET_STARTED"]) == ("true", "false")
    assert len(sdk.emulator_calls()) == 2
@pytest.mark.parametrize("on_void", [False, True])
def test_release_with_end_ends_the_emulator_that_banksman_started(
    capsys, config_path, sdk, on_void
):
    keys = f"on_void = {json.dumps([sys.executable, '-c', 'pass'])}\n" if on_void else ""
    configure(config_path, sdk, keys)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    pid = known()["qa_phone"].pid
    assert main(["release", "--lease", values["LEASE"], "--end"]) == 0
    assert capsys.readouterr().out == (
        "Released qa_phone: banksman ended its instance, and it is free.\n"
    )
    assert not runs(pid)
    assert (leases(), known()) == ({}, {})
    assert main(["log", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)["events"]
    assert [(each["event"], each["end"], each["reason"]) for each in events] == [
        ("acquire", None, None),
        ("release", True, None),
        ("reap", None, "released_end"),
    ]
    # The disk of the AVD keeps what the worktree set up, unless a hook can have changed it.
    status, again, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert (status, again["PREVIOUS"]) == (0, "other" if on_void else "self"), err
    assert len(sdk.emulator_calls()) == 2


def test_release_with_end_leaves_the_emulator_that_banksman_did_not_start(
    capsys, config_path, sdk
):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 0, err
    holder = sdk.start_emulator("qa_phone", 5556)
    wait_for(lambda: listed(sdk, "emulator-5556"))
    assert main(["release", "--lease", values["LEASE"], "--end"]) == 0
    assert capsys.readouterr().out == (
        "Released qa_phone: it is free. banksman did not start its emulator, so it leaves it"
        " running.\n"
    )
    assert runs(holder.pid)
    # It ran when its lease ended, so it is free for the next request, not unmanaged.
    assert known()["qa_phone"].created is False
    assert states(capsys)["qa_phone"] == "free"


def test_release_with_end_of_an_emulator_that_does_not_run_says_so(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"], "--end"]) == 0
    assert capsys.readouterr().out == (
        "Released qa_phone: it is free. Its emulator does not run, so there is nothing to end.\n"
    )


def test_release_with_end_ends_the_emulator_after_the_scripts(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    pid = known()["qa_phone"].pid
    script = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lease = ["--resource", "qa_phone", "--lease", values["LEASE"]]
        assert main(["enter", *lease, "--pid", str(script.pid)]) == 0
        assert main(["release", "--lease", values["LEASE"], "--end"]) == 0
        assert capsys.readouterr().out == (
            f"Released qa_phone: it is free when its scripts have ended: pid {script.pid}, and"
            " banksman has ended its instance.\n"
        )
        # The script can still use the emulator.
        assert main(["reap"]) == 0
        assert runs(pid)
        assert leases()["qa_phone"].state == "draining"
    finally:
        script.kill()
        script.wait()
    capsys.readouterr()
    assert main(["reap"]) == 0
    assert capsys.readouterr().out == (
        "qa_phone: released, because the holder released the lease, and asked banksman to end"
        " its instance\n"
    )
    assert not runs(pid)
    assert (leases(), known()) == ({}, {})


def test_release_with_end_during_a_reset_leaves_an_emulator_that_banksman_did_not_start(
    capsys, config_path, sdk, state_dir, tmp_path
):
    hooked = tmp_path / "on_void"
    reset = json.dumps([sys.executable, "-c", "import time; time.sleep(3)"])
    void = json.dumps([sys.executable, "-c", f"open({str(hooked)!r}, 'w').close()"])
    configure(config_path, sdk, f'unmanaged = "grant"\non_acquire = {reset}\non_void = {void}\n')
    person = sdk.start_emulator("qa_phone", 5556)
    wait_for(lambda: listed(sdk, "emulator-5556"))
    process = acquire_in_child("--where", "avd=qa_phone")
    wait_for(lambda: (state_dir / "qa_phone.json").exists())
    lease = leases()["qa_phone"]
    assert lease.state == "booting"
    assert main(["release", "--lease", lease.lease_id, "--end"]) == 0
    assert capsys.readouterr().out == (
        f"Released qa_phone: it is free when banksman process {process.pid} has ended its reset,"
        " and banksman has ended its instance, if it started it.\n"
    )
    assert process.wait(timeout=60) == 1
    assert main(["reap"]) == 0
    # The records say that a person started the emulator, so it runs on, and no hook ran.
    assert runs(person.pid)
    assert not hooked.exists()
    assert leases() == {}
    assert known()["qa_phone"].created is False


def test_release_with_end_quarantines_an_emulator_that_does_not_end(
    capsys, config_path, sdk, monkeypatch, person
):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    monkeypatch.setattr(emulator, "stop", lambda system, pid, started: False)
    assert main(["release", "--lease", values["LEASE"], "--end"]) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "Released qa_phone: its instance did not end, so it is quarantined until the operator"
        " releases it.\n"
    )
    assert "cannot take back qa_phone: the emulator of qa_phone (pid" in captured.err
    assert leases()["qa_phone"].state == "quarantined"


def test_release_with_end_never_ends_the_emulator_of_another_holder(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, first, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", first["LEASE"]]) == 0
    status, second, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", first["LEASE"], "--end"]) == 3
    assert main(["release", "--lease", first["LEASE"], "--resource", "qa_phone", "--end"]) == 3
    assert runs(known()["qa_phone"].pid)
    assert leases()["qa_phone"].lease_id == second["LEASE"]


def test_release_of_a_holding_with_end_ends_each_emulator_that_banksman_started(
    capsys, config_path, sdk
):
    configure(config_path, sdk)
    parts = ["--as", "phone", "--where", "avd=qa_phone"]
    parts += ["--as", "tablet", "--where", "avd=qa_tablet"]
    status, values, err = acquire(capsys, "--holding", "ui", *ME, "--start", *parts)
    assert status == 0, err
    pids = [entry.pid for entry in known().values()]
    assert len(pids) == 2
    assert main(["release", "--holding", "ui", *ME, "--end"]) == 0
    assert sorted(capsys.readouterr().out.splitlines()) == [
        "Released qa_phone: banksman ended its instance, and it is free.",
        "Released qa_tablet: banksman ended its instance, and it is free.",
    ]
    assert not any(runs(pid) for pid in pids)
    assert (leases(), known()) == ({}, {})


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
    capsys, config_path, sdk, state_dir, monkeypatch, person
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


# The end of free emulators.


def free_for(state_dir, avd, seconds):
    """Move the time since when the AVD has no lease back by `seconds`."""
    path = emulator.default_emulator_dir() / "emulator-emulators.json"
    data = json.loads(path.read_text())
    for entry in data["emulators"]:
        if entry["resource"] == avd:
            entry["free_since"] -= seconds
    path.write_text(json.dumps(data))


def test_a_free_emulator_that_banksman_started_ends_after_the_free_timeout(
    capsys, config_path, sdk, state_dir
):
    configure(config_path, sdk, 'free_timeout = "30m"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    pid = known()["qa_phone"].pid
    assert main(["release", "--lease", values["LEASE"]]) == 0
    free_for(state_dir, "qa_phone", 30 * 60 - 60)
    assert main(["reap"]) == 0
    assert runs(pid)
    free_for(state_dir, "qa_phone", 60)
    capsys.readouterr()
    assert main(["reap"]) == 0
    assert capsys.readouterr().out == (
        "qa_phone: released, because the instance had no lease for the free timeout of its kind\n"
    )
    assert not runs(pid)
    assert (leases(), known()) == ({}, {})
    assert main(["log", "--json"]) == 0
    last = json.loads(capsys.readouterr().out)["events"][-1]
    assert (last["event"], last["resource"], last["reason"], last["owner"]) == (
        "reap",
        "qa_phone",
        "free_timeout",
        "banksman",
    )
    # The next request starts the AVD again.
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert len(sdk.emulator_calls()) == 2


def test_a_free_emulator_that_the_holder_started_never_ends(capsys, config_path, sdk, state_dir):
    configure(config_path, sdk, 'free_timeout = "30m"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 0, err
    holder = sdk.start_emulator("qa_phone", 5556)
    wait_for(lambda: listed(sdk, "emulator-5556"))
    assert main(["release", "--lease", values["LEASE"]]) == 0
    free_for(state_dir, "qa_phone", 24 * 60 * 60)
    assert main(["reap"]) == 0
    assert runs(holder.pid)
    assert states(capsys)["qa_phone"] == "free"


def test_an_emulator_with_a_lease_does_not_end_after_the_free_timeout(
    capsys, config_path, sdk, state_dir
):
    configure(config_path, sdk, 'free_timeout = "30m"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    status, again, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    free_for(state_dir, "qa_phone", 24 * 60 * 60)
    assert main(["reap"]) == 0
    # The reaper saw the new lease, so the free time of the AVD starts again at its end.
    assert known()["qa_phone"].free_since is None
    assert main(["release", "--lease", again["LEASE"]]) == 0
    free_for(state_dir, "qa_phone", 30 * 60 - 60)
    assert main(["reap"]) == 0
    assert runs(known()["qa_phone"].pid)


def test_a_request_waits_while_the_reaper_ends_a_free_emulator(
    capsys, config_path, sdk, state_dir, monkeypatch
):
    configure(config_path, sdk, 'free_timeout = "30m"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    free_for(state_dir, "qa_phone", 30 * 60)
    stop = emulator.stop
    during = []

    def stopping(system, pid, started, **options):
        lease = leases()["qa_phone"]
        during.append((lease.state, lease.owner, lease.serial, lease.void_reason))
        # Another process asks for the AVD meanwhile, and does not get it.
        result = subprocess.run(
            [sys.executable, "-m", "banksman", "acquire", "--where", "avd=qa_phone"],
            env={**os.environ, "PYTHONPATH": SRC},
            capture_output=True,
            text=True,
            timeout=60,
        )
        during.append((result.returncode, result.stderr))
        return stop(system, pid, started, **options)

    monkeypatch.setattr(emulator, "stop", stopping)
    assert main(["reap"]) == 0
    (lease, (code, err)) = during
    assert lease == ("draining", "banksman", values["SERIAL"], "free_timeout")
    assert code == 4 and "in use" in err, err
    assert leases() == {}


def test_an_emulator_that_the_reaper_ends_after_the_discovery_of_a_request_is_started_again(
    capsys, config_path, sdk, state_dir, monkeypatch
):
    configure(config_path, sdk, 'free_timeout = "30m"\n')
    status, first, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", first["LEASE"]]) == 0
    search = cli.search
    looks = []

    def ended_meanwhile(*arguments, **options):
        found = search(*arguments, **options)
        if not looks:
            # The reaper of another command ends the emulator after its free timeout, after this
            # discovery saw it run, and deletes its own lease before the grant.
            free_for(state_dir, "qa_phone", 30 * 60)
            assert [each["void_reason"] for each in reap_in_child(state_dir)] == ["free_timeout"]
        looks.append(found)
        return found

    monkeypatch.setattr(cli, "search", ended_meanwhile)
    status, again, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert (status, again["STATE"], len(looks)) == (0, "ready", 2), err
    # The second look found the AVD stopped, so the request started it again.
    assert len(sdk.emulator_calls()) == 2
    assert runs(known()["qa_phone"].pid)


def test_a_failed_start_of_another_request_does_not_give_its_ended_emulator(
    capsys, config_path, sdk, monkeypatch
):
    configure(config_path, sdk, 'boot_timeout = "3s"\n')
    sdk.emulator_behaves(boot=None)
    search = cli.search
    looks = []

    def failed_meanwhile(*arguments, **options):
        if looks:
            return search(*arguments, **options)
        # Another request starts the AVD, and this discovery sees its emulator boot.
        other = acquire_in_child("--where", "avd=qa_phone", "--start")
        wait_for(lambda: "qa_phone" in known())
        found = search(*arguments, **options)
        looks.append(found)
        # Its boot does not complete, so it ends the emulator and gives its lease back before
        # the grant of this request.
        assert other.wait(timeout=60) == 1
        sdk.emulator_behaves(boot=0)
        return found

    monkeypatch.setattr(cli, "search", failed_meanwhile)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert (status, values["STATE"]) == (0, "ready"), err
    # The request started the AVD again: it did not get the emulator that had ended as running.
    first, second = sdk.emulator_pids()
    assert not runs(first)
    assert known()["qa_phone"].pid == second
    assert runs(second)


def test_a_free_timeout_that_is_off_keeps_free_emulators(capsys, config_path, sdk, state_dir):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    free_for(state_dir, "qa_phone", 24 * 60 * 60)
    assert main(["reap"]) == 0
    assert runs(known()["qa_phone"].pid)


# The end of emulators without input.

UNUSED_LINE = (
    "qa_phone: released, because the instance had no lease, and no input for the unused timeout"
    " of its kind\n"
)


def reaps(capsys):
    """Run banksman reap, and return what it printed."""
    capsys.readouterr()
    assert main(["reap"]) == 0
    return capsys.readouterr().out


def last_log(capsys):
    assert main(["log", "--json"]) == 0
    return json.loads(capsys.readouterr().out)["events"][-1]


def test_an_emulator_that_banksman_started_ends_after_the_unused_timeout(
    capsys, config_path, sdk
):
    configure(config_path, sdk, 'unused_timeout = "3h"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    pid = known()["qa_phone"].pid
    assert main(["release", "--lease", values["LEASE"]]) == 0
    sdk.last_input(values["SERIAL"], 3 * 60 * 60 - 60)
    assert reaps(capsys) == "Nothing to reap.\n"
    assert runs(pid)
    sdk.last_input(values["SERIAL"], 3 * 60 * 60)
    assert reaps(capsys) == UNUSED_LINE
    assert not runs(pid)
    assert (leases(), known()) == ({}, {})
    last = last_log(capsys)
    assert (last["event"], last["resource"], last["reason"], last["owner"]) == (
        "reap",
        "qa_phone",
        "unused",
        "banksman",
    )
    # The next request starts the AVD again.
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert len(sdk.emulator_calls()) == 2


def test_only_banksman_reap_ends_an_emulator_without_input(capsys, config_path, sdk):
    configure(config_path, sdk, 'unused_timeout = "3h"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    sdk.last_input(values["SERIAL"], 24 * 60 * 60)
    # Every command reaps first, but only banksman reap asks the emulators for their input.
    assert states(capsys)["qa_phone"] == "free"
    status, again, err = acquire(capsys, "--where", "avd=qa_tablet")
    assert status == 0, err
    assert main(["release", "--lease", again["LEASE"]]) == 0
    assert runs(known()["qa_phone"].pid)
    assert reaps(capsys) == UNUSED_LINE


def test_an_emulator_with_a_lease_or_an_unknown_input_age_does_not_end(capsys, config_path, sdk):
    configure(config_path, sdk, 'unused_timeout = "3h"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    pid = known()["qa_phone"].pid
    sdk.last_input(values["SERIAL"], 24 * 60 * 60)
    assert reaps(capsys) == "Nothing to reap.\n"
    assert main(["release", "--lease", values["LEASE"]]) == 0
    # An emulator that cannot say when its last input was is never idle.
    sdk.last_input(values["SERIAL"], None)
    assert reaps(capsys) == "Nothing to reap.\n"
    assert runs(pid)


def test_an_unused_timeout_that_is_off_keeps_emulators_without_input(capsys, config_path, sdk):
    configure(config_path, sdk)
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    sdk.last_input(values["SERIAL"], 24 * 60 * 60)
    assert reaps(capsys) == "Nothing to reap.\n"
    assert runs(known()["qa_phone"].pid)


def test_the_reap_asks_adb_nothing_while_no_emulator_of_an_allowed_avd_runs(
    capsys, config_path, sdk
):
    configure(config_path, sdk, 'unused_timeout = "3h"\n')
    # adb would start its server, also for a job on a machine where nobody uses adb.
    assert reaps(capsys) == "Nothing to reap.\n"
    assert sdk.calls() == []


def test_an_emulator_that_a_person_started_never_ends(capsys, config_path, sdk):
    configure(config_path, sdk, 'unused_timeout = "3h"\n', agents=["claude"])
    person = sdk.start_emulator("qa_phone", 5556)
    wait_for(lambda: listed(sdk, "emulator-5556"))
    sdk.last_input("emulator-5556", 24 * 60 * 60)
    assert reaps(capsys) == "Nothing to reap.\n"
    assert runs(person.pid)
    assert states(capsys)["qa_phone"] == "unmanaged"


def test_an_emulator_without_input_whose_agent_has_ended_ends(capsys, config_path, sdk, agent):
    configure(config_path, sdk, 'unused_timeout = "3h"\n', agents=["claude"])
    pid = start_for(sdk, agent)
    sdk.last_input("emulator-5556", 4 * 60 * 60)
    # The agent that started it still runs, and can still use it.
    assert reaps(capsys) == "Nothing to reap.\n"
    assert runs(pid)
    agent.kill()
    agent.wait()
    assert reaps(capsys) == UNUSED_LINE
    assert not runs(pid)
    assert (leases(), known()) == ({}, {})
    assert last_log(capsys)["reason"] == "unused"
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert (status, values["STARTED"]) == (0, "true"), err


def test_an_emulator_that_ran_under_a_lease_of_an_agent_that_has_ended_ends(
    capsys, config_path, sdk, agent
):
    configure(config_path, sdk, 'unused_timeout = "3h"\n', agents=["claude"])
    pid = start_for(sdk, agent)
    status, values, err = as_agent(agent, "acquire", "--where", "avd=qa_phone")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    assert (known()["qa_phone"].pid, known()["qa_phone"].created) == (pid, False)
    sdk.last_input("emulator-5556", 4 * 60 * 60)
    assert reaps(capsys) == "Nothing to reap.\n"
    agent.kill()
    agent.wait()
    assert reaps(capsys) == UNUSED_LINE
    assert not runs(pid)


def test_an_emulator_of_an_avd_that_is_not_allowed_never_ends(capsys, config_path, sdk, agent):
    configure(config_path, sdk, 'unused_timeout = "3h"\n', agents=["claude"])
    save_inventory(Inventory(kinds={"emulator": Decisions(allowed=("qa_tablet",))}))
    pid = start_for(sdk, agent)
    sdk.last_input("emulator-5556", 24 * 60 * 60)
    agent.kill()
    agent.wait()
    assert reaps(capsys) == "Nothing to reap.\n"
    assert runs(pid)
    assert sdk.calls() == []


def test_an_emulator_that_gets_a_lease_after_the_look_does_not_end(
    capsys, config_path, sdk, monkeypatch
):
    configure(config_path, sdk, 'unused_timeout = "3h"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    sdk.last_input(values["SERIAL"], 24 * 60 * 60)
    look = cli._idle_emulators

    def leased_meanwhile(config):
        found = look(config)
        assert [each.resource for _, each in found] == ["qa_phone"]
        # Another process gets the AVD after the look, and before the reaper takes the lock.
        result = subprocess.run(
            [sys.executable, "-m", "banksman", "acquire", "--where", "avd=qa_phone"],
            env={**os.environ, "PYTHONPATH": SRC},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return found

    monkeypatch.setattr(cli, "_idle_emulators", leased_meanwhile)
    assert reaps(capsys) == "Nothing to reap.\n"
    assert runs(known()["qa_phone"].pid)
    assert leases()["qa_phone"].state == "ready"


def test_the_reaper_ends_only_the_emulator_that_it_looked_at(system):
    system.spawn(600)
    found = emulator.Idle("qa_phone", 600, "start-600", "emulator-5556")
    assert emulator.ending(kind(), found, system) is True
    record = emulator.load(kind(), system)["qa_phone"]
    assert (record.pid, record.created, record.serial) == (600, False, "emulator-5556")
    # Another emulator of the AVD runs now, or the emulator has ended.
    other = emulator.Idle("qa_phone", 700, "start-700", "emulator-5558")
    assert emulator.ending(kind(), other, system) is False
    system.end(600)
    assert emulator.ending(kind(), found, system) is False


def test_a_request_can_ask_for_the_input_age(capsys, config_path, sdk):
    configure(config_path, sdk)
    # Without a running emulator, the attribute is known all the same: nothing matches now.
    status, _, err = acquire(capsys, "--where", "input_age>=3600")
    assert status == 1
    assert "no permitted resource that is present now matches input_age>=3600" in err
    status, values, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    sdk.last_input(values["SERIAL"], 2 * 60 * 60)
    status, again, err = acquire(capsys, "--where", "input_age>=3600")
    assert (status, again["RESOURCE"]) == (0, "qa_phone"), err
    assert main(["release", "--lease", again["LEASE"]]) == 0
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    shown = {each["resource"]: each for each in json.loads(capsys.readouterr().out)["resources"]}
    assert shown["qa_phone"]["facts"]["input_age"] == 2 * 60 * 60
    assert "input_age" not in shown["qa_tablet"]["facts"]


# The AVD that the worktree used last.


@pytest.mark.parametrize("on_void", [False, True])
def test_after_the_free_timeout_a_worktree_starts_its_own_avd_before_one_that_runs(
    capsys, config_path, sdk, state_dir, tmp_path, monkeypatch, on_void
):
    keys = 'free_timeout = "30m"\n'
    if on_void:
        keys += f"on_void = {json.dumps([sys.executable, '-c', 'pass'])}\n"
    configure(config_path, sdk, keys)
    work_in(monkeypatch, tmp_path / "tree-a")
    status, mine, err = acquire(capsys, "--where", "avd=qa_tablet", "--start")
    assert status == 0, err
    assert main(["release", "--lease", mine["LEASE"]]) == 0
    work_in(monkeypatch, tmp_path / "tree-b")
    status, other, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert main(["release", "--lease", other["LEASE"]]) == 0
    free_for(state_dir, "qa_tablet", 30 * 60)
    assert main(["reap"]) == 0
    assert list(known()) == ["qa_phone"]
    capsys.readouterr()
    # The reaper ended the emulator of qa_tablet, but its disk keeps what tree-a set up. A hook
    # can have changed it.
    work_in(monkeypatch, tmp_path / "tree-a")
    status, again, err = acquire(capsys, "--where", "kind=emulator", "--start")
    assert status == 0, err
    assert (again["RESOURCE"], again["PREVIOUS"]) == ("qa_tablet", "other" if on_void else "self")
    assert len(sdk.emulator_calls()) == 3


def test_an_unmanaged_instance_never_counts_as_the_worktrees_own(capsys, config_path, sdk):
    configure(config_path, sdk, 'unmanaged = "grant"\n')
    status, values, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 0, err
    assert main(["release", "--lease", values["LEASE"]]) == 0
    # A person starts qa_phone, and a holder started qa_tablet under a lease that has ended.
    sdk.running({"emulator-5554": "qa_phone"})
    sdk.start_emulator("qa_tablet", 5556)
    wait_for(lambda: listed(sdk, "emulator-5556"))
    emulator.adopt(kind(), "qa_tablet")
    _, first, _ = acquire(capsys, "--where", "kind=emulator")
    _, second, _ = acquire(capsys, "--where", "kind=emulator")
    assert (first["RESOURCE"], second["RESOURCE"], second["PREVIOUS"]) == (
        "qa_tablet",
        "qa_phone",
        "other",
    )


# Emulators that an agent started outside banksman.

# Runs banksman as a command that has left the process tree of its agent, so that only the pid
# variable names the agent: the init process adopts it when the process between them has ended.
AS_AGENT = """
import os, subprocess, sys, time
# Before the fork: the parent can end before the child runs.
parent = os.getpid()
if os.fork():
    os._exit(0)
deadline = time.monotonic() + 10
while os.getppid() == parent:
    if time.monotonic() > deadline:
        sys.exit("the process between them did not end")
    time.sleep(0.01)
status = subprocess.run([sys.executable, "-m", "banksman", *sys.argv[1:]]).returncode
print(f"EXIT={status}", flush=True)
"""


@pytest.fixture
def agent(tmp_path):
    """A process whose program is named like the agent claude, started before the emulators."""
    if not os.path.exists("/bin/sleep"):
        pytest.skip("needs /bin/sleep")
    program = tmp_path / "bin" / "claude"
    program.parent.mkdir()
    program.symlink_to("/bin/sleep")
    process = subprocess.Popen([str(program), "120"])
    yield process
    process.kill()
    process.wait()


def as_agent(agent, *arguments):
    """Run banksman for the agent process, and return its exit status, the KEY=value lines that
    it printed, and its standard error."""
    result = subprocess.run(
        [sys.executable, "-c", AS_AGENT, *arguments],
        env={**os.environ, "PYTHONPATH": SRC, "CLAUDE_PID": str(agent.pid)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    *lines, last = result.stdout.splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    return int(last.removeprefix("EXIT=")), values, result.stderr


def start_for(sdk, agent, avd="qa_phone", port=5556):
    pid = sdk.start_emulator_outside(avd, port, {**os.environ, "CLAUDE_PID": str(agent.pid)})
    wait_for(lambda: listed(sdk, f"emulator-{port}"))
    return pid


def test_the_agent_that_started_an_unmanaged_emulator_gets_it(capsys, config_path, sdk, agent):
    configure(config_path, sdk, agents=["claude"])
    pid = start_for(sdk, agent)
    # Any other caller waits for it, as for the emulator of a person.
    status, _, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert status == 4
    assert "qa_phone runs, but banksman did not start it" in err
    status, values, err = as_agent(agent, "acquire", "--where", "avd=qa_phone", "--start")
    assert status == 0, err
    assert (values["SERIAL"], values["STATE"], values["STARTED"]) == (
        "emulator-5556",
        "ready",
        "false",
    )
    # A person or another program could have used it before.
    assert values["PREVIOUS"] == "other"
    assert leases()["qa_phone"].owner_pid == agent.pid
    assert len(sdk.emulator_calls()) == 1
    # At the end of the lease, banksman records it as an emulator that ran under a lease, so
    # every request can get it, and the reaper never ends it.
    assert main(["release", "--lease", values["LEASE"]]) == 0
    record = known()["qa_phone"]
    assert (record.pid, record.created) == (pid, False)
    assert states(capsys)["qa_phone"] == "free"
    status, again, err = acquire(capsys, "--where", "avd=qa_phone", "--start")
    assert (status, again["SERIAL"], again["STARTED"]) == (0, "emulator-5556", "false"), err
    assert len(sdk.emulator_calls()) == 1


def test_a_run_of_the_agent_gets_the_emulator_that_the_agent_started(config_path, sdk, agent):
    # The owner process of the lease is the run, and the run's agent started the emulator.
    configure(config_path, sdk, agents=["claude"])
    pid = start_for(sdk, agent)
    command = [sys.executable, "-c", "import os; print('GOT=' + os.environ['ANDROID_SERIAL'])"]
    status, values, err = as_agent(agent, "run", "--where", "avd=qa_phone", "--", *command)
    assert (status, values["GOT"]) == (0, "emulator-5556"), err
    assert known()["qa_phone"].pid == pid


def test_status_shows_the_agent_that_started_an_unmanaged_emulator(
    capsys, config_path, sdk, agent
):
    configure(config_path, sdk, agents=["claude"])
    pid = start_for(sdk, agent)
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    shown = {each["resource"]: each for each in json.loads(capsys.readouterr().out)["resources"]}
    assert shown["qa_phone"]["state"] == "unmanaged"
    assert shown["qa_phone"]["started_by"] == {"agent": "claude", "pid": agent.pid, "running": True}
    assert shown["qa_tablet"]["started_by"] is None
    assert main(["status"]) == 0
    rows = {row["RESOURCE"]: row for row in table_rows(capsys.readouterr().out)}
    assert rows["qa_phone"]["HOLDER"] == f"started by claude (pid {agent.pid})"
    agent.kill()
    agent.wait()
    assert main(["status"]) == 0
    rows = {row["RESOURCE"]: row for row in table_rows(capsys.readouterr().out)}
    assert (rows["qa_phone"]["STATE"], rows["qa_phone"]["HOLDER"]) == (
        "unmanaged",
        f"started by claude (pid {agent.pid}), which has ended",
    )
    # Nobody gets it now, and banksman does not end an emulator that it did not start.
    status, _, err = acquire(capsys, "--where", "avd=qa_phone")
    assert status == 4, err
    assert main(["reap"]) == 0
    assert runs(pid)


def test_an_agent_without_the_pid_variable_leaves_its_emulator_unmanaged(
    capsys, config_path, sdk, agent
):
    configure(config_path, sdk, agents=["claude"])
    sdk.start_emulator_outside("qa_phone", 5556, dict(os.environ))
    wait_for(lambda: listed(sdk, "emulator-5556"))
    assert states(capsys)["qa_phone"] == "unmanaged"
    status, _, err = as_agent(agent, "acquire", "--where", "avd=qa_phone")
    assert status == 4, err


def test_discovery_reads_the_environment_only_of_an_emulator_that_banksman_does_not_know(
    monkeypatch, system
):
    avds = ("qa_phone", "qa_tablet", "qa_watch")

    def emulators(settings, accounts, input_age):
        return {"schema": 1, "instances": [{"name": name, "facts": {}} for name in avds]}

    monkeypatch.setattr(android, "emulators", emulators)
    system.spawn(300, program="/usr/local/bin/claude")
    emulators = {600: "qa_phone", 700: "qa_tablet", 800: "qa_watch", 801: "qa_watch"}
    for pid, avd in emulators.items():
        system.spawn(pid)
        system.arguments[pid] = ("/sdk/emulator/emulator", "-avd", avd)
        system.environments[pid] = {"CLAUDE_PID": "300"}
    system.processes.update({300: "1000", **dict.fromkeys(emulators, "2000")})
    with emulator.changing(kind(), system) as record:
        record["qa_tablet"] = emulator.Known("qa_tablet", 700, "2000", system.boot, False, 0.0)
    _, unmanaged = emulator.discover(kind(), Android(), agents=("claude",), system=system)
    # With two emulators of qa_watch, banksman cannot tell which one adb gives.
    assert unmanaged == {"qa_phone": StartedBy("claude", 300), "qa_watch": None}
    assert system.environments_read == [600]
    system.environments_read.clear()
    _, unmanaged = emulator.discover(kind(), Android(), system=system)
    assert (unmanaged, system.environments_read) == ({"qa_phone": None, "qa_watch": None}, [])


def _android(sdk):
    return Android(sdk=sdk.sdk, avd_home=sdk.avd_home)


def _no_claim(serials):
    raise AssertionError("the start claimed a serial")


def test_a_fake_system_gives_the_command_lines(system: FakeSystem):
    system.spawn(300)
    system.arguments[300] = ("/sdk/emulator/emulator", "-avd", "qa_phone")
    assert emulator.by_avd(system.commands())["qa_phone"][0].pid == 300
