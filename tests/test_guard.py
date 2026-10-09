"""The adb guard: an adb wrapper that refuses an agent's device command on the device of another
holding.

The tests use a fake process tree and a fake adb. The tests that run the guard or the wrapper in
a process of their own use a configuration without agents, so the process tree of the person who
runs the tests does not change what the guard decides.
"""

import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from importlib import resources

import pytest
from helpers import SRC, FakeSystem, drain, edit_lease, write_config

from banksman import android, cli, guard
from banksman.android import Transport
from banksman.config import load_config
from banksman.guard import ALL, DEVICE, HOST, Call, parse
from banksman.lease import Holder, SerialSeen
from banksman.store import Choice, Need, Store
from banksman.system import MachineError

ME = os.getpid()
# The agent of the caller, a shell under it, and the agent of another holder.
AGENT, SHELL, OTHER_AGENT = 500, 501, 600
OTHER = Holder(
    "/work/tree-b", issue="#123", agent="codex", purpose="verify the tablet layout"
)


@pytest.fixture(autouse=True)
def caller_environment(monkeypatch):
    # The environment of the person who runs the tests must not change what the guard decides.
    for name in ("BANKSMAN_LEASE", "ANDROID_SERIAL", guard.MARKER):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_real_sdk(monkeypatch, tmp_path):
    # A test without [android] in its configuration finds no adb, so it can never run the adb
    # of the machine.
    monkeypatch.setattr(android, "_home", lambda: tmp_path / "home")


class NoProcessList(FakeSystem):
    """A machine whose process list fails the test: the fast path must not read it."""

    def process_table(self):
        raise AssertionError("the guard read the process list")


def no_adb():
    raise AssertionError("the guard ran adb devices")


def listing(*transports):
    return lambda: list(transports)


@pytest.fixture
def system():
    machine = FakeSystem()
    machine.spawn(AGENT, program="/opt/bin/claude")
    machine.spawn(SHELL, parent=AGENT)
    machine.spawn(OTHER_AGENT, program="/opt/bin/codex")
    # The test process runs the guard, as a command of the agent.
    machine.parents[ME] = SHELL
    return machine


@pytest.fixture
def store(state_dir, system):
    return Store(state_dir, system)


def mine(owner_pid=AGENT):
    return Holder("/work/tree-a", owner_pid=owner_pid)


def other(owner_pid=OTHER_AGENT):
    return Holder(OTHER.owner, owner_pid, OTHER.issue, OTHER.agent, purpose=OTHER.purpose)


def lease(store, system, resource, holder, serial=None, kind="emulator"):
    granted = store.acquire(resource, kind, holder)
    if serial is not None:
        store.observe([SerialSeen(resource, kind, serial, system.now)])
    return granted


def check(system, *arguments, env=None, adb=no_adb):
    return guard.check(list(arguments), load_config(), env or {}, system, adb)


# Reading the command line.


@pytest.mark.parametrize(
    ("arguments", "call"),
    [
        ([], Call(HOST)),
        (["devices", "-l"], Call(HOST, "devices")),
        (["-s", "emulator-5554", "devices"], Call(HOST, "devices")),
        (["version"], Call(HOST, "version")),
        (["--version"], Call(HOST, "--version")),
        (["connect", "192.0.2.7"], Call(HOST, "connect")),
        (["forward", "--list"], Call(HOST, "forward")),
        (["start-server"], Call(HOST, "start-server")),
        (["-P", "5038", "nodaemon", "server"], Call(HOST, "nodaemon")),
        (["shell", "ls"], Call(DEVICE, "shell")),
        (["-s", "emulator-5554", "shell", "ls"], Call(DEVICE, "shell", "emulator-5554")),
        (["-s123ABC", "install", "app.apk"], Call(DEVICE, "install", "123ABC")),
        (["-t", "7", "shell"], Call(DEVICE, "shell", transport_id="7")),
        (["-t7", "shell"], Call(DEVICE, "shell", transport_id="7")),
        (["-t", "0", "shell"], Call(DEVICE, "shell")),
        (["-d", "reboot"], Call(DEVICE, "reboot", transport="usb")),
        (["-e", "emu", "kill"], Call(DEVICE, "emu", transport="local", console=True)),
        (["-e", "-s", "X", "shell"], Call(DEVICE, "shell", "X", transport="local")),
        (
            ["-H", "host", "-P5038", "-s", "X", "shell"],
            Call(DEVICE, "shell", "X", server=("-H", "host", "-P5038")),
        ),
        (["-L", "tcp:5038", "shell"], Call(DEVICE, "shell", server=("-L", "tcp:5038"))),
        (["-a", "--exit-on-write-error", "logcat"], Call(DEVICE, "logcat")),
        (["forward", "tcp:8000", "tcp:8000"], Call(DEVICE, "forward")),
        (["wait-for-device", "shell", "ls"], Call(DEVICE, "wait-for-device")),
        (["reconnect"], Call(DEVICE, "reconnect")),
        (["reconnect", "device"], Call(DEVICE, "reconnect")),
        (["unknown-command"], Call(DEVICE, "unknown-command")),
        (["kill-server"], Call(ALL, "kill-server")),
        (["-P", "5038", "kill-server"], Call(ALL, "kill-server", server=("-P", "5038"))),
        (["kill-server", "now"], Call(ALL, "kill-server")),
        (["reconnect", "offline"], Call(ALL, "reconnect offline")),
        (["-s", "X", "forward", "--remove-all"], Call(ALL, "forward --remove-all")),
        (["-s", "X", "emu", "kill"], Call(DEVICE, "emu", "X", console=True)),
        (["wait-for-device", "emu", "kill"], Call(DEVICE, "wait-for-device", console=True)),
        (["disconnect"], Call(ALL, "disconnect")),
        (["-s", "X", "disconnect", "192.0.2.7:5555"], Call(DEVICE, "disconnect", "192.0.2.7:5555")),
        # adb refuses these itself, and acts on nothing.
        (["-s"], Call(HOST)),
        (["-sync", "shell"], Call(HOST)),
        (["-t", "abc", "shell"], Call(HOST)),
        (["-H"], Call(HOST)),
    ],
)
def test_the_command_line_is_read_as_adb_reads_it(arguments, call):
    assert parse(arguments) == call


# Commands that act on no device.


@pytest.mark.parametrize(
    "arguments", [["devices"], ["-s", "emulator-5554", "devices"], ["forward", "--list"], []]
)
def test_a_command_without_a_device_passes_without_reading_anything(store, system, arguments):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert check(NoProcessList(), *arguments) is None


# The device of another holding.


def test_a_device_command_on_the_device_of_another_holding_is_refused(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    refusal = check(system, "-s", "emulator-5554", "shell", "input", "tap", "1", "2")
    assert refusal == (
        "adb shell: refused: another holder leases emulator-5554 (e2e_phone), and the lease is"
        " ready: codex · #123 · verify the tablet layout. Use a device of your own lease;"
        " banksman status shows who holds what"
    )


def test_a_device_command_on_the_device_of_the_caller_passes(store, system):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert check(system, "-s", "emulator-5554", "shell", "ls") is None


def test_the_lease_id_of_banksman_run_passes_without_the_process_list(store, system):
    granted = lease(store, system, "e2e_phone", mine(owner_pid=None), serial="emulator-5554")
    env = {"BANKSMAN_LEASE": granted.lease_id}
    assert check(NoProcessList(), "-s", "emulator-5554", "shell", "ls", env=env) is None


def test_every_lease_of_the_holding_of_the_lease_id_passes(store, system):
    needs = [Need((Choice(name, "emulator"),)) for name in ("e2e_phone", "e2e_tablet")]
    phone, _ = store.grant(needs, mine(owner_pid=None))
    store.observe([SerialSeen("e2e_tablet", "emulator", "emulator-5556", system.now)])
    env = {"BANKSMAN_LEASE": phone.lease.lease_id}
    assert check(NoProcessList(), "-s", "emulator-5556", "shell", "ls", env=env) is None


def test_a_lease_id_of_another_holding_does_not_pass(store, system):
    mine_now = lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    env = {"BANKSMAN_LEASE": mine_now.lease_id}
    assert "another holder" in check(system, "-s", "emulator-5556", "shell", env=env)


def test_an_owner_process_with_another_start_time_is_not_the_owner(store, system):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    # The agent ended, and the system gave its pid to a new agent process above the caller.
    system.processes[AGENT] = "start-new"
    assert "another holder" in check(system, "-s", "emulator-5554", "shell")


def test_a_person_passes_also_on_the_device_of_another_holding(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    system.parents[ME] = 1
    assert check(system, "-s", "emulator-5554", "shell") is None


def test_a_device_without_a_lease_passes_without_the_process_list(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert check(NoProcessList(), "-s", "emulator-5556", "shell") is None


def test_without_leases_nothing_is_read(system):
    assert check(NoProcessList(), "shell", "ls") is None


def test_an_own_lease_that_is_lost_is_refused(store, system, state_dir):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    edit_lease(state_dir, "e2e_phone", drain)
    assert check(system, "-s", "emulator-5554", "shell") == (
        "adb shell: refused: your lease of emulator-5554 (e2e_phone) is lost: it is draining."
        " Stop using the device, and acquire one again"
    )


def test_a_physical_device_is_found_by_its_name_in_every_state(store, system, state_dir):
    lease(store, system, "R5CR1234ABC", other(), kind="device")
    edit_lease(state_dir, "R5CR1234ABC", drain)
    assert "another holder leases R5CR1234ABC, and the lease is draining" in check(
        system, "-s", "R5CR1234ABC", "install", "app.apk"
    )


def test_the_old_serial_of_a_quarantined_lease_gives_way_to_a_held_lease(
    store, system, state_dir
):
    lease(store, system, "e2e_old", other(), serial="emulator-5554")

    def quarantine(data):
        drain(data)
        data["state"] = "quarantined"

    edit_lease(state_dir, "e2e_old", quarantine)
    # A new emulator got the port of the old one, and the caller leases it.
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert check(system, "-s", "emulator-5554", "shell") is None


def test_the_serial_of_a_quarantined_lease_counts_while_no_held_lease_has_it(
    store, system, state_dir
):
    lease(store, system, "e2e_old", other(), serial="emulator-5554")

    def quarantine(data):
        drain(data)
        data["state"] = "quarantined"

    edit_lease(state_dir, "e2e_old", quarantine)
    assert "the lease is quarantined" in check(system, "-s", "emulator-5554", "shell")


def test_the_hook_of_a_reaper_passes_also_inside_the_command_of_another_agent(
    store, system, state_dir
):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    # A command of the caller's agent reaps the void lease, and runs its on_void hook.
    system.spawn(700, parent=SHELL)
    system.spawn(701, parent=700)
    system.parents[ME] = 701

    def taken_back(data):
        drain(data)
        data.update(reaper_pid=700, reaper_started="start-700")

    edit_lease(state_dir, "e2e_phone", taken_back)
    assert check(system, "-s", "emulator-5554", "emu", "kill") is None


def test_the_reaper_must_be_above_the_caller(store, system, state_dir):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    system.spawn(700)

    def taken_back(data):
        drain(data)
        data.update(reaper_pid=700, reaper_started="start-700")

    edit_lease(state_dir, "e2e_phone", taken_back)
    assert "another holder" in check(system, "-s", "emulator-5554", "emu", "kill")


def test_a_quarantine_counts_also_after_a_restart_emptied_the_state_directory(
    state_dir, quarantine_dir, system
):
    store = Store(state_dir, system, take_back=lambda lease: False)
    lease(store, system, "R5CR1234ABC", other(), kind="device")
    system.advance(20 * 60)
    store.reap()
    shutil.rmtree(state_dir)
    assert "the lease is quarantined" in check(system, "-s", "R5CR1234ABC", "shell")


def test_a_lease_file_that_cannot_be_read_keeps_its_device_out_of_use(state_dir, system):
    state_dir.mkdir(mode=0o700)
    (state_dir / "R5CR1234ABC.json").write_text("{not json")
    assert check(system, "-s", "R5CR1234ABC", "shell") == (
        "adb shell: refused: the lease file of R5CR1234ABC cannot be read, so the device is out"
        " of use: the file is not valid JSON"
    )


def test_leases_of_kinds_without_devices_need_no_adb(store, system, config_path):
    write_config(config_path, "[kinds.build]\ncount = 1\n")
    store.acquire("build-0", "build", other())
    assert check(NoProcessList(), "shell", "ls") is None


# Which device a command acts on.


def test_android_serial_names_the_device_when_the_command_does_not(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    env = {"ANDROID_SERIAL": "emulator-5554"}
    assert "another holder" in check(system, "shell", env=env)
    assert check(system, "-s", "emulator-5556", "shell", env=env) is None
    # -d and -e choose a device themselves, so adb does not use ANDROID_SERIAL then.
    usb = Transport("R5CR1234ABC", "device", devpath="usb:1-1")
    assert check(system, "-d", "shell", env=env, adb=listing(usb)) is None


def test_the_only_device_is_the_target_of_a_command_without_one(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    phone = Transport("emulator-5554", "device")
    assert "another holder" in check(system, "shell", adb=listing(phone))
    # With several devices, adb refuses the command itself.
    second = Transport("emulator-5556", "device")
    assert check(system, "shell", adb=listing(phone, second)) is None


def test_a_device_that_adb_may_not_open_is_not_counted(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    phone = Transport("emulator-5554", "device")
    blocked = Transport("0A1B2C3D", "no", devpath="usb:1-2")
    assert "another holder" in check(system, "shell", adb=listing(phone, blocked))


@pytest.mark.parametrize(
    ("option", "found"),
    [
        (["-d"], "R5CR1234ABC"),
        (["-e"], "emulator-5554"),
        (["-t", "3"], "R5CR1234ABC"),
        (["-t", "4"], "emulator-5554"),
    ],
)
def test_d_e_and_t_choose_the_device_as_adb_does(store, system, option, found):
    lease(store, system, "R5CR1234ABC", other(), kind="device", serial="R5CR1234ABC")
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    devices = listing(
        Transport("R5CR1234ABC", "device", devpath="usb:1-1", transport_id="3"),
        Transport("emulator-5554", "device", transport_id="4"),
    )
    assert f"another holder leases {found}" in check(system, *option, "shell", adb=devices)


@pytest.mark.parametrize(
    "given",
    ["model:Pixel_8", "product:husky", "device:husky", "usb:1-1", "tcp:192.0.2.7", "192.0.2.7"],
)
def test_s_finds_a_device_by_the_other_names_that_adb_accepts(store, system, given):
    lease(store, system, "R5CR1234ABC", other(), kind="device", serial="R5CR1234ABC")
    lease(store, system, "192.0.2.7:5555", other(), kind="device", serial="192.0.2.7:5555")
    devices = listing(
        Transport(
            "R5CR1234ABC", "device", "usb:1-1", product="husky", model="Pixel_8", device="husky"
        ),
        Transport("192.0.2.7:5555", "device"),
    )
    assert "another holder" in check(system, "-s", given, "shell", adb=devices)


def test_a_serial_that_names_a_lease_needs_no_adb(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert "another holder" in check(system, "-s", "emulator-5554", "shell")


def test_disconnect_acts_on_the_device_that_it_names(store, system):
    lease(store, system, "192.0.2.7:5555", other(), kind="device", serial="192.0.2.7:5555")
    refusal = check(system, "-s", "emulator-5556", "disconnect", "192.0.2.7:5555")
    assert "adb disconnect: refused: another holder leases 192.0.2.7:5555" in refusal


# adb emu finds its emulator by rules of its own.

PHONE = Transport("R5CR1234ABC", "device", devpath="usb:1-1", transport_id="3")
EMULATOR = Transport("emulator-5554", "device", transport_id="4")


@pytest.mark.parametrize("option", [[], ["-d"], ["-e"], ["-t", "3"]])
def test_emu_without_a_serial_acts_on_the_only_emulator(store, system, option):
    # The physical device does not make the command ambiguous for adb emu, and adb emu ignores
    # -d, -e, and -t.
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    refusal = check(system, *option, "emu", "kill", adb=listing(PHONE, EMULATOR))
    assert "another holder leases emulator-5554 (e2e_phone)" in refusal


def test_emu_with_several_emulators_and_no_serial_passes(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    second = Transport("emulator-5556", "device")
    assert check(system, "emu", "kill", adb=listing(EMULATOR, second)) is None


def test_emu_takes_the_console_port_from_the_serial(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert "another holder" in check(system, "-s", "emulator-05554", "emu", "kill")
    assert "another holder" in check(
        system, "emu", "kill", env={"ANDROID_SERIAL": "emulator-5554"}
    )
    # A serial that is not an emulator has no console, so adb emu fails by itself.
    assert check(system, "-s", "R5CR1234ABC", "emu", "kill") is None


def test_emu_with_d_or_e_ignores_android_serial(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    env = {"ANDROID_SERIAL": "emulator-5556"}
    assert "another holder" in check(system, "-e", "emu", "kill", env=env, adb=listing(EMULATOR))


def test_emu_after_a_wait_follows_the_rules_of_emu(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    refusal = check(system, "wait-for-device", "emu", "kill", adb=listing(PHONE, EMULATOR))
    assert "another holder" in refusal


# Strict mode.


@pytest.fixture
def strict(config_path):
    write_config(config_path, "[guard]\nstrict = true\n")


def test_strict_refuses_a_device_that_no_lease_of_the_caller_has(store, system, strict):
    assert check(system, "-s", "emulator-5556", "shell") == (
        "adb shell: refused: emulator-5556 is not a device of your leases, and the adb guard is"
        " strict. Get a device with banksman acquire"
    )


def test_strict_passes_the_device_of_the_caller(store, system, strict):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert check(system, "-s", "emulator-5554", "shell") is None


def test_strict_passes_a_person(store, system, strict):
    system.parents[ME] = 1
    assert check(system, "-s", "emulator-5556", "shell") is None


def test_strict_passes_a_command_that_adb_refuses_itself(store, system, strict):
    devices = listing(Transport("emulator-5554", "device"), Transport("emulator-5556", "device"))
    assert check(system, "shell", adb=devices) is None


# Commands that act on every device.


@pytest.mark.parametrize(
    "arguments",
    [["kill-server"], ["reconnect", "offline"], ["disconnect"], ["forward", "--remove-all"]],
)
def test_a_command_for_every_device_is_refused_while_another_holding_has_a_device(
    store, system, arguments
):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert check(system, *arguments) == (
        f"adb {' '.join(arguments)}: refused: it acts on every device, also on devices that other"
        " holders lease and that run now: emulator-5554 (codex · #123 · verify the tablet"
        " layout)"
    )


def test_removing_every_port_forward_is_refused_also_with_the_own_device(store, system):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    assert "refused" in check(system, "-s", "emulator-5554", "forward", "--remove-all")


def test_a_command_for_every_device_passes_with_only_own_devices(store, system):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert check(system, "kill-server") is None


def test_a_command_for_every_device_passes_while_no_leased_instance_runs(store, system):
    lease(store, system, "e2e_phone", other())
    assert check(NoProcessList(), "kill-server") is None


def test_a_person_may_stop_the_adb_server(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    system.parents[ME] = 1
    assert check(system, "kill-server") is None


def test_reconnect_without_offline_acts_on_one_device(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert check(system, "-s", "emulator-5556", "reconnect") is None


# The command: banksman guard adb.


@pytest.fixture
def ran(monkeypatch, system):
    """Record the adb that the guard runs, instead of running it. Like the real exec, the
    recording never returns: it ends the command with status 0."""
    calls = []

    def exec_adb(adb, arguments, env):
        calls.append((adb, list(arguments), env.get(guard.MARKER)))
        raise SystemExit(0)

    monkeypatch.setattr(android, "exec_adb", exec_adb)
    monkeypatch.setattr(guard, "Machine", lambda: system)
    return calls


def guarded(*arguments):
    """Run banksman guard adb, and return its exit status."""
    try:
        return guard.main(["adb", "--", *arguments])
    except SystemExit as exc:
        return exc.code


def test_the_guard_runs_the_adb_of_the_sdk_with_the_same_arguments(ran, config_path, tmp_path):
    write_config(config_path, f'[android]\nsdk = "{tmp_path / "sdk"}"\n')
    assert guarded("-s", "emulator-5554", "shell", "echo", "a b") == 0
    adb = str(tmp_path / "sdk" / "platform-tools" / "adb")
    assert ran == [(adb, ["-s", "emulator-5554", "shell", "echo", "a b"], "1")]


def test_a_refused_call_exits_with_3_and_runs_no_adb(ran, store, system, capsys):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert guarded("-s", "emulator-5554", "shell") == guard.EXIT_REFUSED
    assert ran == []
    assert capsys.readouterr().err.startswith("banksman: adb shell: refused: another holder")


def test_a_refusal_has_no_terminal_control_sequences(ran, system, config_path, capsys):
    # The serial in the message comes from the command line of the caller.
    write_config(config_path, "[guard]\nstrict = true\n")
    assert guarded("-s", "\x1b]0;title\x07emulator-5556", "shell") == guard.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "emulator-5556 is not a device of your leases" in err
    assert "\x1b" not in err and "\x07" not in err


@pytest.mark.parametrize(
    "broken",
    [
        "configuration",
        "lease schema",
        "process list",
    ],
)
def test_a_guard_that_fails_lets_the_call_run_and_says_so(
    broken, ran, store, system, config_path, state_dir, capsys, monkeypatch
):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    if broken == "configuration":
        write_config(config_path, "[guard]\nstrict = 'yes'\n")
    elif broken == "lease schema":
        edit_lease(state_dir, "e2e_phone", lambda data: data.update(schema=99))
    else:

        def blocked():
            raise MachineError("cannot run ps: not permitted")

        monkeypatch.setattr(system, "process_table", blocked)
    assert guarded("-s", "emulator-5554", "shell") == 0
    assert len(ran) == 1
    err = capsys.readouterr().err
    assert err.startswith("banksman: adb guard: this adb call is not checked: ")


def test_without_a_configuration_the_guard_runs_the_adb_of_the_wrapper(ran, config_path, capsys):
    write_config(config_path, "[guard]\nstrict = 'yes'\n")
    status = guarded_with_fallback("/opt/sdk/platform-tools/adb", "devices")
    assert status == 0
    assert ran == [("/opt/sdk/platform-tools/adb", ["devices"], "1")]
    assert "this adb call is not checked" in capsys.readouterr().err


def test_with_a_configuration_the_guard_runs_the_adb_of_the_configuration(
    ran, config_path, tmp_path
):
    write_config(config_path, f'[android]\nsdk = "{tmp_path / "sdk"}"\n')
    assert guarded_with_fallback("/opt/sdk/platform-tools/adb", "devices") == 0
    assert ran[0][0] == str(tmp_path / "sdk" / "platform-tools" / "adb")


def guarded_with_fallback(fallback, *arguments):
    try:
        return guard.main(["adb", guard.FALLBACK_OPTION, fallback, "--", *arguments])
    except SystemExit as exc:
        return exc.code


def test_a_guard_that_runs_itself_again_stops(ran, monkeypatch, capsys):
    monkeypatch.setenv(guard.MARKER, "1")
    assert guarded("devices") == 1
    assert ran == []
    assert "runs this guard again" in capsys.readouterr().err


def test_an_adb_that_does_not_exist_gives_127(config_path, tmp_path, capsys):
    write_config(config_path, f'[android]\nsdk = "{tmp_path / "missing"}"\n')
    assert guard.main(["adb", "--", "devices"]) == 127
    assert "Set sdk in [android]" in capsys.readouterr().err


def test_an_adb_that_cannot_run_gives_126(config_path, tmp_path):
    adb = tmp_path / "sdk" / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text("not a program")
    adb.chmod(0o644)
    write_config(config_path, f'[android]\nsdk = "{tmp_path / "sdk"}"\n')
    assert guard.main(["adb", "--", "devices"]) == 126


def test_the_guard_knows_only_adb(capsys):
    assert guard.main(["fastboot", "--", "devices"]) == 2
    assert capsys.readouterr().err == guard.USAGE + "\n"


def test_the_command_line_starts_the_guard_too(ran, config_path):
    write_config(config_path, "not toml")
    with pytest.raises(SystemExit):
        cli.main(["guard", "adb", guard.FALLBACK_OPTION, "/opt/adb", "--", "devices"])
    assert ran == [("/opt/adb", ["devices"], "1")]


# The guard in a process of its own, with the real exec.

FAKE_ADB = """#!/bin/sh
printf '%s\\n' "$@" > "$(dirname "$0")/arguments"
echo "${BANKSMAN_GUARD:-unset}" > "$(dirname "$0")/marker"
exit 7
"""


@pytest.fixture
def fake_adb(tmp_path, config_path):
    adb = tmp_path / "sdk" / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text(FAKE_ADB)
    adb.chmod(0o755)
    write_config(config_path, f'[android]\nsdk = "{tmp_path / "sdk"}"\n[holder]\nagents = []\n')
    return adb


def run_banksman(*arguments, env=None, **options):
    return subprocess.run(
        [sys.executable, "-m", "banksman", *arguments],
        env={**os.environ, "PYTHONPATH": SRC, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        **options,
    )


def test_the_guard_runs_adb_in_its_own_place(fake_adb):
    result = run_banksman("guard", "adb", "--", "-s", "emulator-5554", "shell", "echo 'a b'")
    assert result.returncode == 7
    arguments = (fake_adb.parent / "arguments").read_text().splitlines()
    assert arguments == ["-s", "emulator-5554", "shell", "echo 'a b'"]
    assert (fake_adb.parent / "marker").read_text().strip() == "1"


def test_the_guard_does_not_import_the_rest_of_the_command_line():
    code = "import json, sys; import banksman.guard; print(json.dumps(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    loaded = set(json.loads(result.stdout))
    slow = {
        "banksman.cli",
        "banksman.console",
        "banksman.discovery",
        "banksman.request",
        "banksman.hooks",
        "subprocess",
        "tempfile",
    }
    assert loaded & slow == set()


def test_the_launcher_starts_the_guard_without_the_command_line(monkeypatch):
    from banksman import launcher

    calls = []
    monkeypatch.setattr(guard, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["banksman", "guard", "adb", "--", "devices"])
    assert launcher.main() == 0
    assert calls == [["adb", "--", "devices"]]


def test_the_fast_path_adds_little_time_to_an_adb_call(tmp_path):
    # Without leases, the guard reads no process list and runs no adb devices. The bound is
    # wide, so that a slow machine does not fail the test; the measurement is printed.
    fast = tmp_path / "fast-sdk" / "platform-tools" / "adb"
    fast.parent.mkdir(parents=True)
    fast.write_text("#!/bin/sh\nexit 0\n")
    fast.chmod(0o755)
    config = tmp_path / "fast.toml"
    write_config(config, f'[android]\nsdk = "{fast.parent.parent}"\n')
    env = {**os.environ, "PYTHONPATH": SRC, "BANKSMAN_CONFIG": str(config)}

    def median(command):
        times = []
        for _ in range(5):
            start = time.perf_counter()
            subprocess.run(command, env=env, check=True, timeout=60)
            times.append(time.perf_counter() - start)
        return statistics.median(times)

    alone = median([str(fast), "-s", "emulator-5554", "shell", "true"])
    guard_command = [sys.executable, "-m", "banksman", "guard", "adb", "--"]
    with_guard = median([*guard_command, "-s", "emulator-5554", "shell", "true"])
    print(f"\nthe adb guard adds {1000 * (with_guard - alone):.0f} ms to an adb call")
    assert with_guard - alone < 0.5


# The wrapper: banksman admin adb-shim.


@pytest.fixture
def shim(fake_adb, tmp_path, capsys):
    assert cli.main(["admin", "adb-shim"]) == 0
    path = tmp_path / "shim" / "adb"
    path.parent.mkdir()
    path.write_text(capsys.readouterr().out)
    path.chmod(0o755)
    return path


def test_admin_adb_shim_prints_the_wrapper_with_the_adb_of_the_sdk(shim, fake_adb):
    template = resources.files("banksman").joinpath(cli.ADB_SHIM_SCRIPT).read_text()
    assert shim.read_text() == template.replace(cli.ADB_SHIM_PATH, shlex.quote(str(fake_adb)))


def test_admin_adb_shim_warns_when_the_sdk_has_no_adb(config_path, tmp_path, capsys):
    write_config(config_path, f'[android]\nsdk = "{tmp_path / "missing"}"\n')
    assert cli.main(["admin", "adb-shim"]) == 0
    assert "does not exist. Set sdk in [android]" in capsys.readouterr().err


def test_the_wrapper_passes_every_argument_to_the_guard(shim, fake_adb, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "banksman"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {shlex.quote(str(tmp_path / 'got'))}\n")
    fake.chmod(0o755)
    result = subprocess.run(
        [str(shim), "-s", "emulator-5554", "shell", "echo 'a  b'", ""],
        env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert (tmp_path / "got").read_text().split("\n")[:-1] == [
        "guard",
        "adb",
        "--fallback-adb",
        str(fake_adb),
        "--",
        "-s",
        "emulator-5554",
        "shell",
        "echo 'a  b'",
        "",
    ]


def test_without_banksman_the_wrapper_runs_adb_and_says_that_the_call_is_not_checked(
    shim, fake_adb
):
    result = subprocess.run(
        [str(shim), "devices"],
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 7
    assert (fake_adb.parent / "arguments").read_text() == "devices\n"
    assert "banksman is not on the PATH, so this adb call is not checked" in result.stderr


def test_the_wrapper_uses_no_banksman_from_a_relative_directory(shim, fake_adb, tmp_path):
    relative = tmp_path / "project" / "bin"
    relative.mkdir(parents=True)
    (relative / "banksman").write_text("#!/bin/sh\nexit 99\n")
    (relative / "banksman").chmod(0o755)
    result = subprocess.run(
        [str(shim), "devices"],
        env={**os.environ, "PATH": "bin:/usr/bin:/bin"},
        cwd=relative.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 7
