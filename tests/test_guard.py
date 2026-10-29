"""The adb guard: an adb wrapper that refuses an agent's device command on the device of another
holding.

The tests use a fake process tree and a fake adb. The tests that run the guard or the wrapper in
a process of their own use a configuration without agents, so the process tree of the person who
runs the tests does not change what the guard decides.
"""

import contextlib
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
import shapes
from helpers import NOT_ENDED, SRC, FakeSystem, drain, edit_lease, write_config

from banksman import android, cli, guard, inventory
from banksman import store as store_module
from banksman.android import Transport
from banksman.config import load_config
from banksman.errors import BanksmanError
from banksman.guard import ALL, DEVICE, HOST, Call, parse
from banksman.history import (
    COMMAND,
    EVERY_DEVICE,
    GUARD,
    LOST,
    NOT_LEASED,
    OTHER_HOLDER,
    REFUSED,
    UNREADABLE,
    WARNED,
    Event,
    History,
)
from banksman.lease import Holder, SerialSeen, Timeouts
from banksman.printed import ADB_SHIM_PATH, ADB_SHIM_SCRIPT
from banksman.store import Choice, Need, Store, log_lock, peek
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
    for name in ("BANKSMAN_LEASE", "ANDROID_SERIAL", "BANKSMAN_GUARD"):
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


PHONE = Transport("R5CR1234ABC", "device", devpath="usb:1-1", transport_id="3")
EMULATOR = Transport("emulator-5554", "device", transport_id="4")


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


def verdict(system, *arguments, env=None, adb=no_adb):
    return guard.check(list(arguments), load_config(), env or {}, system, adb).verdict


def check(system, *arguments, env=None, adb=no_adb):
    """Return the text of a refusal, or None when the call runs without a warning."""
    found = verdict(system, *arguments, env=env, adb=adb)
    if found is None:
        return None
    assert found.action == REFUSED, found.text
    return found.text


# Reading the command line.


@pytest.mark.parametrize(
    ("arguments", "call"),
    [
        ([], Call(HOST)),
        (["devices"], Call(HOST, "devices", lists=True)),
        (["devices", "-l"], Call(HOST, "devices", lists=True)),
        (["-s", "emulator-5554", "devices"], Call(HOST, "devices", lists=True)),
        (["devices", "-x"], Call(HOST, "devices")),
        (["track-devices"], Call(HOST, "track-devices")),
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
        (["-s", "X", "-t", "4", "shell"], Call(DEVICE, "shell", "X", "4")),
        (["--one-device", "X", "start-server"], Call(HOST, "start-server")),
        (["--one-device", "X", "-s", "Y", "shell"], Call(DEVICE, "shell", "Y")),
        (["--reply-fd", "3", "-s", "Y", "shell"], Call(DEVICE, "shell", "Y")),
        (["--one-device"], Call(HOST)),
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
        (["wait-for-device", "shell", "ls"], Call(DEVICE, "wait-for-device shell")),
        (["wait-for-device"], Call(HOST, "wait-for-device")),
        (["wait-for-device", "devices"], Call(HOST, "wait-for-device devices", lists=True)),
        (["wait-for-device", "kill-server"], Call(ALL, "wait-for-device kill-server")),
        (
            ["wait-for-usb-device", "forward", "--remove-all"],
            Call(ALL, "wait-for-usb-device forward --remove-all"),
        ),
        (["reconnect"], Call(DEVICE, "reconnect")),
        (["reconnect", "device"], Call(DEVICE, "reconnect")),
        (["unknown-command"], Call(DEVICE, "unknown-command")),
        (["kill-server"], Call(ALL, "kill-server")),
        (["-P", "5038", "kill-server"], Call(ALL, "kill-server", server=("-P", "5038"))),
        (["kill-server", "now"], Call(ALL, "kill-server")),
        (["reconnect", "offline"], Call(ALL, "reconnect offline")),
        (["-s", "X", "forward", "--remove-all"], Call(ALL, "forward --remove-all")),
        (["-s", "X", "emu", "kill"], Call(DEVICE, "emu", "X", console=True)),
        (["wait-for-device", "emu", "kill"], Call(DEVICE, "wait-for-device emu", console=True)),
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
    "arguments", [["track-devices"], ["devices", "-x"], ["version"], ["forward", "--list"], []]
)
def test_a_command_without_a_device_passes_without_reading_anything(store, system, arguments):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    decision = guard.check(arguments, load_config(), {}, NoProcessList(), no_adb)
    assert decision == guard.RUNS


# The device of another holding.


def test_a_device_command_on_the_device_of_another_holding_is_refused(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    refusal = check(system, "-s", "emulator-5554", "shell", "input", "tap", "1", "2")
    assert refusal == (
        "adb shell: refused: another holder leases emulator-5554 (e2e_phone), and the lease is"
        " ready: codex · #123 · verify the tablet layout. Use a device of your own lease;"
        " banksman status shows who holds what. Run banksman agent-guide and follow it"
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


# The agent process that ran the session before a restart of the agent.
EARLIER_AGENT = 400
IN_SESSION = {"CLAUDE_CODE_SESSION_ID": "session-1"}


def earlier(agent="claude", session="session-1"):
    return Holder("/work/tree-a", owner_pid=EARLIER_AGENT, agent=agent, session=session)


@pytest.fixture
def restarted(store, system):
    """Lease e2e_phone to the earlier agent process of the caller's session, which has ended."""
    system.spawn(EARLIER_AGENT, program="/opt/bin/claude")
    lease(store, system, "e2e_phone", earlier(), serial="emulator-5554")
    system.end(EARLIER_AGENT)


def test_a_new_agent_process_of_the_session_uses_the_devices_of_the_earlier_one(
    store, system, restarted
):
    assert check(system, "-s", "emulator-5554", "shell", env=IN_SESSION) is None
    assert note(system, env=IN_SESSION).startswith("your holding has emulator-5554.")
    # Another session, and a caller that gives no session, are other holders.
    for env in ({"CLAUDE_CODE_SESSION_ID": "session-2"}, {}):
        assert "another holder" in check(system, "-s", "emulator-5554", "shell", env=env)
        assert note(system, env=env) == NO_DEVICE


def test_after_the_owner_grace_the_lease_of_the_earlier_agent_process_is_lost(
    store, system, restarted
):
    system.now += Timeouts().owner_grace
    assert check(system, "-s", "emulator-5554", "shell", env=IN_SESSION) == (
        "adb shell: refused: your lease of emulator-5554 (e2e_phone) is lost: the owner process"
        " ended, and nothing touched the lease for the grace period. Stop using the device, and"
        " acquire one again"
    )


def test_the_lease_of_the_session_from_an_earlier_boot_is_lost(store, system, restarted):
    # The lease file outlived a restart of the machine, no banksman command has reaped since,
    # and the session was resumed. Awake time starts again at every boot.
    system.boot = "boot-2"
    system.now = 100.0
    assert check(system, "-s", "emulator-5554", "shell", env=IN_SESSION) == (
        "adb shell: refused: your lease of emulator-5554 (e2e_phone) is lost: the machine"
        " restarted after the lease was written. Stop using the device, and acquire one again"
    )
    assert note(system, env=IN_SESSION) == NO_DEVICE


def test_only_a_lease_of_the_session_alone_reads_the_boot(store, system, restarted):
    # A lease whose owner process runs above the caller is of this boot, so no call waits for
    # the boot id.
    lease(store, system, "e2e_tablet", mine(), serial="emulator-5556")

    def no_boot_id():
        raise AssertionError("the guard read the boot id")

    system.boot_id = no_boot_id
    assert check(system, "-s", "emulator-5556", "shell", env=IN_SESSION) is None


@pytest.mark.parametrize("agent", ["codex", None])
def test_the_session_of_another_agent_is_another_holder(store, system, agent):
    system.spawn(EARLIER_AGENT)
    lease(store, system, "e2e_phone", earlier(agent=agent), serial="emulator-5554")
    system.end(EARLIER_AGENT)
    assert "another holder" in check(system, "-s", "emulator-5554", "shell", env=IN_SESSION)


def test_a_lease_of_the_session_whose_owner_process_runs_is_not_the_callers(store, system):
    # Such as the lease of banksman run --where: it ends with its own process.
    system.spawn(EARLIER_AGENT)
    lease(store, system, "e2e_phone", earlier(), serial="emulator-5554")
    assert "another holder" in check(system, "-s", "emulator-5554", "shell", env=IN_SESSION)


def test_a_person_passes_also_on_the_device_of_another_holding(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    system.parents[ME] = 1
    assert check(system, "-s", "emulator-5554", "shell") is None


def detach(system, agent_started="1000"):
    """Make the caller a command that has left the process tree of its agent, and return its
    environment, which names the agent."""
    # Clock ticks after boot, as the process list on Linux gives a start time.
    system.processes.update({AGENT: agent_started, ME: "2000"})
    system.parents[ME] = 1
    return {"CLAUDE_PID": str(AGENT)}


def test_a_detached_command_of_an_agent_is_refused_on_the_device_of_another_holding(
    store, system
):
    env = detach(system)
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert "another holder" in check(system, "-s", "emulator-5554", "shell", env=env)
    assert "refused" in check(system, "kill-server", env=env)
    # Without the variable, it passes as a person's.
    assert check(system, "-s", "emulator-5554", "shell") is None


def test_a_detached_command_of_the_holder_passes_on_its_own_device(store, system):
    env = detach(system)
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    assert check(system, "-s", "emulator-5554", "shell", env=env) is None
    assert "another holder" in check(system, "-s", "emulator-5556", "shell", env=env)


def test_a_detached_command_names_its_agent_in_the_refusal_log(
    machine, store, system, worktree, log_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_PID", detach(system)["CLAUDE_PID"])
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert guarded("-s", "emulator-5554", "shell") == guard.EXIT_REFUSED
    (event,) = logged(log_path)
    assert (event.owner_pid, event.agent, event.session) == (AGENT, "claude", "session-1")


def test_a_variable_that_names_a_process_started_after_the_caller_counts_for_nothing(
    store, system
):
    env = detach(system, agent_started="2001")
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert check(system, "-s", "emulator-5554", "shell", env=env) is None


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


def test_an_own_lease_that_is_void_but_not_reaped_yet_is_lost(store, system):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    # The guard does not reap, so the lease file still says that the lease is ready.
    system.advance(Timeouts().idle_timeout)
    found = verdict(system, "-s", "emulator-5554", "shell")
    assert (found.action, found.reason) == (REFUSED, LOST)
    assert found.text == (
        "adb shell: refused: your lease of emulator-5554 (e2e_phone) is lost: nothing touched the"
        " lease for the idle timeout. Stop using the device, and acquire one again"
    )


def test_a_void_lease_of_the_lease_id_is_lost_for_an_agent(store, system):
    granted = lease(store, system, "e2e_phone", mine(owner_pid=None), serial="emulator-5554")
    env = {"BANKSMAN_LEASE": granted.lease_id}
    system.advance(Timeouts().hard_cap)
    assert check(system, "-s", "emulator-5554", "shell", env=env) == (
        "adb shell: refused: your lease of emulator-5554 (e2e_phone) is lost: the lease reached"
        " its hard cap. Stop using the device, and acquire one again"
    )
    # A person's call passes, as on a lease that drains.
    system.parents[ME] = 1
    assert check(system, "-s", "emulator-5554", "shell", env=env) is None


def test_the_lease_id_reads_the_process_list_only_after_the_grace_of_the_owner(store, system):
    # The owner process is the run that acquired the lease, as for banksman run --where.
    system.spawn(700, parent=SHELL)
    granted = lease(store, system, "e2e_phone", mine(owner_pid=700), serial="emulator-5554")
    env = {"BANKSMAN_LEASE": granted.lease_id}
    system.end(700)
    system.advance(Timeouts().owner_grace - 1)
    assert check(NoProcessList(), "-s", "emulator-5554", "shell", env=env) is None
    system.advance(1)
    assert "is lost: the owner process ended" in check(
        system, "-s", "emulator-5554", "shell", env=env
    )


def test_a_physical_device_is_found_by_its_name_in_every_state(store, system, state_dir):
    lease(store, system, "R5CR1234ABC", other(), kind="device")
    edit_lease(state_dir, "R5CR1234ABC", drain)
    assert "another holder leases R5CR1234ABC, and the lease is draining" in check(
        system, "-s", "R5CR1234ABC", "install", "app.apk"
    )


# The serial that adb gives the device R5CR1234ABC over Wi-Fi after `adb pair`.
WIFI = "adb-R5CR1234ABC-aYi6h4._adb-tls-connect._tcp"


def test_a_physical_device_over_wifi_is_found_by_the_serial_number_in_its_serial(store, system):
    # The lease records the serial over USB, and the device is also connected over Wi-Fi.
    lease(store, system, "R5CR1234ABC", other(), serial="R5CR1234ABC", kind="device")
    assert check(system, "-s", WIFI, "install", "app.apk").startswith(
        f"adb install: refused: another holder leases {WIFI} (R5CR1234ABC), and the lease is ready"
    )
    devices = listing(Transport(WIFI, "device", transport_id="5"))
    assert "another holder" in check(system, "-e", "shell", adb=devices)
    assert check(system, "-s", "adb-R5CR9999XYZ-aYi6h4._adb-tls-connect._tcp", "shell") is None


def test_a_lease_of_the_name_over_wifi_from_an_earlier_banksman_keeps_its_device(store, system):
    # An earlier banksman named the device by its serial over Wi-Fi. Discovery now gives that
    # serial to the device under its serial number, and the old lease loses it.
    lease(store, system, WIFI, other(), kind="device")
    lease(store, system, "R5CR1234ABC", mine(), serial=WIFI, kind="device")
    assert f"another holder leases {WIFI}" in check(system, "-s", WIFI, "shell")


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
    store = Store(state_dir, system, take_back=lambda lease: NOT_ENDED)
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


def test_t_wins_over_s_as_in_adb(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    devices = listing(PHONE, EMULATOR)
    assert "another holder" in check(system, "-s", "R5CR1234ABC", "-t", "4", "shell", adb=devices)
    assert check(system, "-s", "emulator-5554", "-t", "3", "shell", adb=devices) is None


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
        " strict. Get a device through banksman: run banksman agent-guide and follow it"
    )


def test_strict_passes_the_device_of_the_caller(store, system, strict):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert check(system, "-s", "emulator-5554", "shell") is None


def test_strict_passes_the_device_of_the_caller_also_over_wifi(store, system, strict):
    lease(store, system, "R5CR1234ABC", mine(), serial="R5CR1234ABC", kind="device")
    assert check(system, "-s", WIFI, "shell") is None


def test_strict_passes_a_person(store, system, strict):
    system.parents[ME] = 1
    assert check(system, "-s", "emulator-5556", "shell") is None


def test_strict_passes_a_command_that_adb_refuses_itself(store, system, strict):
    devices = listing(Transport("emulator-5554", "device"), Transport("emulator-5556", "device"))
    assert check(system, "shell", adb=devices) is None


# Warn mode.


@pytest.fixture
def warn(config_path):
    write_config(config_path, '[guard]\nstrict = "warn"\n')


def test_warn_lets_a_device_that_no_lease_of_the_caller_has_run_with_a_warning(
    store, system, warn
):
    found = verdict(system, "-s", "emulator-5556", "shell")
    assert (found.action, found.reason, found.serial) == (WARNED, NOT_LEASED, "emulator-5556")
    assert found.text == (
        "warning: adb shell: emulator-5556 is not a device of your leases, and a strict adb guard"
        " would refuse this call. Get a device through banksman: run banksman agent-guide and"
        " follow it"
    )


def test_warn_still_refuses_the_device_of_another_holding(store, system, warn):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert "another holder" in check(system, "-s", "emulator-5554", "shell")


def test_warn_passes_the_device_of_the_caller_and_a_person(store, system, warn):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert verdict(system, "-s", "emulator-5554", "shell") is None
    system.parents[ME] = 1
    assert verdict(system, "-s", "emulator-5556", "shell") is None


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
def machine(monkeypatch, system):
    monkeypatch.setattr(guard, "Machine", lambda: system)


def guarded(*arguments, fallback=None):
    """Run banksman guard adb, and return its exit status."""
    options = [] if fallback is None else [guard.FALLBACK_OPTION, fallback]
    return guard.main(["adb", *options, "--", *arguments])


def test_a_call_that_may_run_prints_the_adb_of_the_configuration(
    machine, config_path, tmp_path, capsys
):
    write_config(config_path, f'[android]\nsdk = "{tmp_path / "sdk"}"\n')
    assert guarded("-s", "emulator-5554", "shell", fallback="/opt/sdk/platform-tools/adb") == 0
    assert capsys.readouterr().out == f"{tmp_path / 'sdk' / 'platform-tools' / 'adb'}\n"


def test_a_refused_call_exits_with_3_and_prints_no_adb(machine, store, system, capsys):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert guarded("-s", "emulator-5554", "shell") == guard.EXIT_REFUSED
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err.startswith("banksman: adb shell: refused: another holder")


def test_a_refusal_has_no_terminal_control_sequences(machine, config_path, capsys):
    # The serial in the message comes from the command line of the caller.
    write_config(config_path, "[guard]\nstrict = true\n")
    assert guarded("-s", "\x1b]0;title\x07emulator-5556", "shell") == guard.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "emulator-5556 is not a device of your leases" in err
    assert "\x1b" not in err and "\x07" not in err


@pytest.mark.parametrize("broken", ["configuration", "lease schema", "process list"])
def test_a_guard_that_fails_lets_the_call_run_and_says_so(
    broken, machine, store, system, config_path, state_dir, capsys, monkeypatch
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
    assert guarded("-s", "emulator-5554", "shell", fallback="/opt/adb") == 0
    printed = capsys.readouterr()
    assert printed.out == "/opt/adb\n"
    assert printed.err.startswith("banksman: adb guard: this adb call is not checked: ")


def test_a_warned_call_runs_and_says_so_in_one_line(machine, warn, capsys):
    assert guarded("-s", "emulator-5556", "shell", "ls", fallback="/opt/adb") == 0
    printed = capsys.readouterr()
    assert printed.out == "/opt/adb\n"
    # The agent learns from its first call that bypasses banksman how to get a device.
    assert printed.err.splitlines() == [
        "banksman: warning: adb shell: emulator-5556 is not a device of your leases, and a strict"
        " adb guard would refuse this call. Get a device through banksman: run banksman"
        " agent-guide and follow it"
    ]


@pytest.mark.parametrize(
    "configuration", [None, "[holder]\nagents = []\n", "[guard]\nstrict = 'yes'\n"]
)
def test_without_an_sdk_in_the_configuration_the_guard_gives_the_adb_of_the_wrapper(
    machine, config_path, capsys, configuration
):
    # No file, a file without [android], and a file that cannot be read.
    if configuration is not None:
        write_config(config_path, configuration)
    assert guarded("-s", "emulator-5554", "shell", fallback="/opt/sdk/platform-tools/adb") == 0
    assert capsys.readouterr().out == "/opt/sdk/platform-tools/adb\n"


def test_without_the_adb_of_the_wrapper_the_guard_gives_the_adb_of_the_default_sdk(
    machine, tmp_path, capsys
):
    assert guarded("devices") == 0
    assert capsys.readouterr().out.startswith(str(tmp_path / "home"))


def traced_lines(path):
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for line in lines:
        shapes.check_trace_line(line)
    return lines


def test_the_trace_has_every_decision_of_the_guard_with_the_command_line(
    machine, store, system, config_path, trace_path, capsys
):
    write_config(config_path, "[log]\ntrace = true\n")
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert guarded("devices", fallback="/opt/adb") == 0
    assert guarded("-s", "emulator-5554", "shell", "am", "start", "token=abc") == 3
    listed, refused = traced_lines(trace_path)
    assert listed["command"] == ["guard", "adb", "--fallback-adb", "/opt/adb", "--", "devices"]
    assert (listed["status"], listed["steps"][0]["action"]) == (0, "runs")
    assert listed["steps"][0]["adb"] == "/opt/adb"
    # The full command line, which the log never keeps.
    assert refused["command"][-4:] == ["shell", "am", "start", "token=abc"]
    assert refused["status"] == guard.EXIT_REFUSED
    (step,) = refused["steps"]
    held = store.snapshot().leases[0]
    assert step == {
        "step": "guard",
        "action": "refused",
        "reason": OTHER_HOLDER,
        "serial": "emulator-5554",
        "leases": [
            {
                "resource": "e2e_phone",
                "lease_id": held.lease_id,
                "state": "ready",
                "holding": held.holding,
            }
        ],
        "used": [],
        "touched": [],
        "adb": None,
        "error": None,
    }
    # The decision read the process list, so the caller has its agent.
    assert (refused["caller"]["agent"], refused["caller"]["owner_pid"]) == ("claude", AGENT)
    assert refused["stderr"][0].startswith("banksman: adb shell: refused: another holder")


def test_the_trace_has_a_call_that_runs_on_the_device_of_the_caller(
    machine, store, system, config_path, trace_path, capsys
):
    write_config(config_path, "[log]\ntrace = true\n")
    own = lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    system.advance(2 * guard.TOUCH_EVERY)
    assert guarded("-s", "emulator-5554", "shell", "ls", fallback="/opt/adb") == 0
    (line,) = traced_lines(trace_path)
    (step,) = line["steps"]
    assert (step["action"], step["serial"], step["adb"]) == ("runs", "emulator-5554", "/opt/adb")
    assert (step["used"], step["touched"]) == ([own.resource], [own.resource])


def test_a_guard_that_fails_records_the_failure_in_the_trace(
    machine, config_path, trace_path, capsys, monkeypatch
):
    write_config(config_path, "[log]\ntrace = true\n")

    def broken(*arguments, **options):
        raise BanksmanError("the lease files cannot be read")

    monkeypatch.setattr(guard, "check", broken)
    assert guarded("-s", "emulator-5554", "shell", fallback="/opt/adb") == 0
    (line,) = traced_lines(trace_path)
    (step,) = line["steps"]
    assert (step["action"], step["error"]) == ("unchecked", "the lease files cannot be read")
    # Without the process list, which the guard did not read, the caller has no agent.
    assert line["caller"]["agent"] is None


def test_a_trace_that_cannot_be_written_refuses_no_call(
    machine, config_path, trace_path, capsys
):
    write_config(config_path, "[log]\ntrace = true\n")
    trace_path.mkdir(parents=True)
    assert guarded("-s", "emulator-5554", "shell", fallback="/opt/adb") == 0
    printed = capsys.readouterr()
    assert printed.out == "/opt/adb\n"
    assert "this command is not in the trace" in printed.err


def test_the_guard_knows_only_adb(capsys):
    assert guard.main(["fastboot", "--", "devices"]) == 2
    assert capsys.readouterr().err == guard.USAGE + "\n"


def test_the_command_line_gives_the_arguments_of_adb_to_the_guard_as_they_are(
    monkeypatch, capsys
):
    arguments = ["guard", "adb", guard.FALLBACK_OPTION, "/opt/adb", "--", "devices", "-l"]
    calls = []
    original = guard.main
    monkeypatch.setattr(guard, "main", lambda argv: calls.append(argv) or original(argv))
    assert cli.main(arguments) == 0
    assert calls == [arguments[1:]]
    assert capsys.readouterr().out == "/opt/adb\n"


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


# The note on adb devices: which devices are the caller's own.


def note(system, *arguments, env=None):
    return guard.check(list(arguments or ["devices"]), load_config(), env or {}, system).note


NO_DEVICE = (
    "no device here is yours. Get one with banksman acquire; do not choose a serial from this"
    " list."
)


def labelled(store, system, label, serials):
    """Grant a holding with a label to the caller's agent, with a serial for each resource."""
    needs = [Need((Choice(resource, "emulator"),)) for resource in serials]
    store.grant(needs, mine(), label=label)
    store.observe(
        [SerialSeen(name, "emulator", serial, system.now) for name, serial in serials.items()]
    )


@pytest.mark.parametrize("arguments", [["devices"], ["devices", "-l"], ["-P", "5038", "devices"]])
def test_adb_devices_of_an_agent_names_the_device_of_its_holding(store, system, arguments):
    labelled(store, system, "login-tests", {"e2e_phone": "emulator-5554"})
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    assert note(system, *arguments) == (
        "your holding login-tests has emulator-5554. Use only that device; get another with"
        " banksman acquire."
    )


def test_adb_devices_of_an_agent_without_a_device_says_so(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert note(system) == NO_DEVICE


def test_the_note_names_no_other_holder_and_no_free_device(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    found = note(system)
    for text in ("e2e_", "emulator-", "codex", "#123", "tablet layout", "tree-b"):
        assert text not in found


def test_a_device_that_does_not_run_yet_is_named_by_its_resource(store, system):
    lease(store, system, "e2e_phone", mine())
    assert note(system) == (
        "your holding has e2e_phone. Use only that device; get another with banksman acquire."
    )


def test_the_note_names_every_device_of_the_holding(store, system):
    serials = {"e2e_phone": "emulator-5554", "e2e_tablet": "emulator-5556"}
    labelled(store, system, "login-tests", serials)
    assert note(system) == (
        "your holding login-tests has emulator-5554, emulator-5556. Use only these devices; get"
        " another with banksman acquire."
    )


def test_several_holdings_are_named_with_their_labels(store, system):
    # The subagents of one session share its agent process, so each sees the holdings of all.
    labelled(store, system, "search-tests", {"e2e_tablet": "emulator-5556"})
    lease(store, system, "R5CR1234ABC", mine(), kind="device")
    labelled(store, system, "login-tests", {"e2e_phone": "emulator-5554"})
    assert note(system) == (
        "your holdings have these devices: login-tests: emulator-5554; search-tests:"
        " emulator-5556; without a label: R5CR1234ABC. Use only the devices of your own holding;"
        " get another with banksman acquire."
    )


def test_a_lost_lease_and_a_lease_without_a_device_are_not_named(
    store, system, state_dir, config_path
):
    write_config(config_path, "[kinds.build]\ncount = 1\n")
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    edit_lease(state_dir, "e2e_phone", drain)
    store.acquire("build-0", "build", mine())
    assert note(system) == NO_DEVICE


@pytest.mark.parametrize(
    ("timeouts", "past"),
    [(Timeouts(), Timeouts().idle_timeout), (Timeouts(idle_timeout=None), Timeouts().hard_cap)],
)
def test_a_lease_that_is_void_but_not_reaped_yet_is_not_named(store, system, timeouts, past):
    store.acquire("e2e_phone", "emulator", mine(), timeouts=timeouts)
    store.observe([SerialSeen("e2e_phone", "emulator", "emulator-5554", system.now)])
    # The guard does not reap, so the lease file still says that the lease is ready.
    system.now += past
    assert note(system) == NO_DEVICE


def test_a_detached_command_of_an_agent_gets_the_note_of_its_agent(store, system):
    env = detach(system)
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert note(system, env=env).startswith("your holding has emulator-5554.")


def test_adb_devices_of_a_person_gets_no_note(store, system):
    lease(store, system, "e2e_phone", mine(owner_pid=None), serial="emulator-5554")
    system.parents[ME] = 1
    assert note(system) is None


def test_inside_banksman_run_the_note_reads_nothing(store, system):
    granted = lease(store, system, "e2e_phone", mine(owner_pid=None), serial="emulator-5554")
    assert note(NoProcessList(), env={"BANKSMAN_LEASE": granted.lease_id}) is None


def test_the_operator_can_turn_the_note_off(system, config_path):
    write_config(config_path, "[guard]\ndevices_note = false\n")
    assert note(NoProcessList()) is None


def test_adb_devices_prints_the_note_on_standard_error_before_adb_runs(
    machine, store, system, capsys
):
    labelled(store, system, "login-tests", {"e2e_phone": "emulator-5554"})
    assert guarded("devices", "-l", fallback="/opt/adb") == 0
    printed = capsys.readouterr()
    assert printed.out == "/opt/adb\n"
    assert printed.err == (
        "banksman: your holding login-tests has emulator-5554. Use only that device; get another"
        " with banksman acquire.\n"
    )


@pytest.mark.parametrize("broken", ["lease schema", "process list"])
def test_a_note_that_cannot_be_made_is_left_out_without_a_word(
    broken, machine, store, system, state_dir, capsys, monkeypatch
):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    if broken == "lease schema":
        edit_lease(state_dir, "e2e_phone", lambda data: data.update(schema=99))
    else:

        def blocked():
            raise MachineError("cannot run ps: not permitted")

        monkeypatch.setattr(system, "process_table", blocked)
    assert guarded("devices", fallback="/opt/adb") == 0
    assert capsys.readouterr() == ("/opt/adb\n", "")


def test_the_note_writes_nothing_to_the_log_and_takes_no_lock(
    machine, store, system, log_path, monkeypatch
):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")

    def no_lock(*args, **kwargs):
        raise AssertionError("the guard took the lock")

    monkeypatch.setattr(guard, "log_lock", no_lock)
    monkeypatch.setattr(guard, "Store", no_lock)
    assert guarded("devices") == 0
    assert not log_path.exists()


# The log of refused and warned calls.

CALLER = Holder("/work/tree-a", owner_pid=AGENT, agent="claude")


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    # The caller runs adb in a worktree of a session of its agent.
    path = tmp_path / "tree-a"
    (path / ".git").mkdir(parents=True)
    monkeypatch.chdir(path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    return os.path.realpath(path)


def logged(log_path):
    events, skipped = History(log_path).read()
    assert skipped == 0
    return events


def test_a_warned_call_is_in_the_log_with_the_caller(machine, system, warn, worktree, log_path):
    assert guarded("-s", "emulator-5556", "shell", "ls") == 0
    assert logged(log_path) == [
        Event(
            system.wall_clock(),
            GUARD,
            "emulator-5556",
            owner=worktree,
            owner_pid=AGENT,
            agent="claude",
            session="session-1",
            reason=NOT_LEASED,
            action=WARNED,
            command="shell",
            serial="emulator-5556",
        )
    ]


def test_a_call_without_a_lease_names_the_device_as_the_inventory_does(
    machine, system, warn, worktree, log_path, inventory_path
):
    # The serial over Wi-Fi changes when wireless debugging is reset, and the serial number in it
    # does not. A refused name is a name of the inventory too.
    inventory_path.write_text(
        '[kinds.device]\nallowed = ["R5CR1234ABC"]\nrefused = ["R5CR5678DEF"]\n'
    )
    unknown = "adb-R5CR9999XYZ-aYi6h4._adb-tls-connect._tcp"
    for serial in ("R5CR1234ABC", WIFI, "R5CR5678DEF", unknown, "emulator-5556"):
        assert guarded("-s", serial, "shell") == 0
    assert [(event.resource, event.kind, event.serial) for event in logged(log_path)] == [
        ("R5CR1234ABC", "device", "R5CR1234ABC"),
        ("R5CR1234ABC", "device", WIFI),
        ("R5CR5678DEF", "device", "R5CR5678DEF"),
        (unknown, None, unknown),
        ("emulator-5556", None, "emulator-5556"),
    ]


def test_an_inventory_that_cannot_be_read_leaves_the_serial_as_the_resource(
    machine, system, warn, worktree, log_path, inventory_path, capsys
):
    inventory_path.write_text("[kinds.device\n")
    assert guarded("-s", WIFI, "shell") == 0
    (event,) = logged(log_path)
    assert (event.resource, event.kind, event.serial) == (WIFI, None, WIFI)
    assert "not in the log" not in capsys.readouterr().err


@pytest.mark.parametrize("mode", [None, "true", '"warn"'])
def test_a_refused_call_is_in_the_log_in_every_mode(
    machine, store, system, config_path, worktree, log_path, mode
):
    if mode is not None:
        write_config(config_path, f"[guard]\nstrict = {mode}\n")
    theirs = lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert guarded("-s", "emulator-5554", "install", "app.apk") == guard.EXIT_REFUSED
    (event,) = logged(log_path)
    assert (event.action, event.reason, event.command, event.serial) == (
        REFUSED,
        OTHER_HOLDER,
        "install",
        "emulator-5554",
    )
    # The lease of the device, so that `held` can name its holder.
    assert (event.resource, event.kind, event.state, event.lease_id) == (
        "e2e_phone",
        "emulator",
        "ready",
        theirs.lease_id,
    )
    # The event names the caller, not the holder of the lease, whose purpose is untrusted text.
    assert (event.owner, event.agent, event.purpose) == (worktree, "claude", None)


def test_a_strict_refusal_is_in_the_log(machine, system, strict, worktree, log_path):
    assert guarded("-s", "emulator-5556", "shell") == guard.EXIT_REFUSED
    (event,) = logged(log_path)
    assert (event.action, event.reason, event.resource, event.kind) == (
        REFUSED,
        NOT_LEASED,
        "emulator-5556",
        None,
    )


def test_a_lost_lease_and_a_lease_file_that_cannot_be_read_have_their_reasons(
    store, system, state_dir
):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    edit_lease(state_dir, "e2e_phone", drain)
    (state_dir / "R5CR1234ABC.json").write_text("{not json")
    found = [verdict(system, "-s", serial, "shell") for serial in ("emulator-5554", "R5CR1234ABC")]
    shown = [
        (event.reason, event.resource, event.state, event.serial)
        for each in found
        for event in guard.events(each, CALLER, 1.0)
    ]
    assert shown == [
        (LOST, "e2e_phone", "draining", "emulator-5554"),
        (UNREADABLE, "R5CR1234ABC", None, "R5CR1234ABC"),
    ]


def test_a_command_for_every_device_has_an_event_for_each_device_of_another_holder(
    store, system
):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    lease(store, system, "e2e_mine", mine(), serial="emulator-5558")
    found = guard.events(verdict(system, "kill-server"), CALLER, 1.0)
    assert [(event.reason, event.resource, event.serial, event.command) for event in found] == [
        (EVERY_DEVICE, "e2e_phone", "emulator-5554", "kill-server"),
        (EVERY_DEVICE, "e2e_tablet", "emulator-5556", "kill-server"),
    ]


def test_the_log_never_has_the_arguments_of_adb(machine, store, system, worktree, log_path):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    arguments = ["shell", "am", "start", "--es", "token", "s3cr3t-value"]
    assert guarded("-s", "emulator-5554", *arguments) == guard.EXIT_REFUSED
    # A word in the place of the command that adb does not know can be a token too, also a
    # quoted one with a command and a space in it.
    for word in ("Bearer_s3cr3t", "s3cr3t-value", "shell s3cr3t-value"):
        assert guarded("-s", "emulator-5554", word) == guard.EXIT_REFUSED
    assert "s3cr3t" not in log_path.read_text()
    assert "token" not in log_path.read_text()
    assert [event.command for event in logged(log_path)] == ["shell", None, None, None]


@pytest.mark.parametrize(
    "arguments",
    [
        ["wait-for-usb-device", "install", "app.apk"],
        ["forward", "--remove-all"],
        ["reconnect", "offline"],
        ["install-multiple", "a.apk", "b.apk"],
    ],
)
def test_the_log_names_the_commands_that_adb_knows(arguments):
    assert COMMAND.fullmatch(parse(arguments).command)


def test_a_serial_that_is_not_a_valid_name_is_not_in_the_log(system, strict):
    found = verdict(system, "-s", "\x1b]0;title\x07emulator-5556", "shell")
    assert found.action == REFUSED
    assert guard.events(found, CALLER, 1.0) == []


def test_a_call_without_a_refusal_or_a_warning_takes_no_lock_and_reads_no_inventory(
    machine, store, system, warn, log_path, monkeypatch, capsys
):
    locks = []
    monkeypatch.setattr(guard, "log_lock", lambda *arguments: locks.append(arguments))

    def no_inventory(*arguments):
        raise AssertionError("the guard read the inventory")

    monkeypatch.setattr(inventory, "load_inventory", no_inventory)
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert guarded("-s", "emulator-5554", "shell") == 0
    system.parents[ME] = 1
    assert guarded("devices") == 0
    assert guarded("-s", "emulator-5556", "shell") == 0
    assert locks == []
    assert capsys.readouterr().err == ""
    assert not log_path.exists()


@pytest.mark.parametrize(("mode", "status"), [('"warn"', 0), ("true", guard.EXIT_REFUSED)])
def test_a_log_that_cannot_be_written_does_not_change_the_decision(
    machine, config_path, tmp_path, monkeypatch, capsys, mode, status
):
    write_config(config_path, f"[guard]\nstrict = {mode}\n")
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setenv("BANKSMAN_LOG", str(blocker / "log.jsonl"))
    assert guarded("-s", "emulator-5556", "shell", fallback="/opt/adb") == status
    printed = capsys.readouterr()
    assert printed.out == ("/opt/adb\n" if status == 0 else "")
    assert printed.err.splitlines()[-1].startswith(
        "banksman: adb guard: warning: this adb call is not in the log: "
    )


def test_the_guard_waits_only_shortly_for_the_lock(
    machine, warn, state_dir, log_path, monkeypatch, capsys
):
    monkeypatch.setattr(guard, "LOG_LOCK_WAIT", 0.05)
    with log_lock(state_dir, 1):
        start = time.monotonic()
        assert guarded("-s", "emulator-5556", "shell", fallback="/opt/adb") == 0
        assert time.monotonic() - start < 1
    assert "this adb call is not in the log: another banksman process" in capsys.readouterr().err
    assert not log_path.exists()


# The touch of a lease that the holder uses.


def touched(state_dir, *resources):
    return [json.loads((state_dir / f"{name}.json").read_text()) for name in resources]


def count_locks(monkeypatch):
    """Record each take of the lock of the lease store from now on."""
    taken = []
    original = store_module._take_lock
    monkeypatch.setattr(
        store_module, "_take_lock", lambda *arguments: taken.append(1) or original(*arguments)
    )
    return taken


def test_a_call_of_the_holder_touches_its_holding_a_minute_after_the_last_touch(
    machine, store, system, state_dir, capsys
):
    needs = [Need((Choice(name, "emulator"),)) for name in ("e2e_phone", "e2e_tablet")]
    store.grant(needs, mine())
    store.observe([SerialSeen("e2e_phone", "emulator", "emulator-5554", system.now)])
    granted = system.now
    system.advance(61)
    assert guarded("-s", "emulator-5554", "shell", "ls", fallback="/opt/adb") == 0
    assert capsys.readouterr() == ("/opt/adb\n", "")
    phone, tablet = touched(state_dir, "e2e_phone", "e2e_tablet")
    assert phone["awake"]["touched"] == tablet["awake"]["touched"] == system.now
    assert phone["touched_at"] == system.wall
    assert phone["longest_quiet"] == system.now - granted
    assert phone["owner_pid"] == AGENT


def test_a_call_within_the_minute_takes_no_lock(machine, store, system, state_dir, monkeypatch):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    system.advance(60)
    taken = count_locks(monkeypatch)
    assert guarded("-s", "emulator-5554", "shell") == 0
    assert taken == []
    system.advance(1)
    assert guarded("-s", "emulator-5554", "shell") == 0
    assert taken == [1]
    assert guarded("-s", "emulator-5554", "shell") == 0
    assert taken == [1]
    (phone,) = touched(state_dir, "e2e_phone")
    assert phone["awake"]["touched"] == system.now


def test_a_short_idle_timeout_is_touched_after_a_quarter_of_it(
    machine, store, system, state_dir
):
    store.acquire("e2e_phone", "emulator", mine(), timeouts=Timeouts(idle_timeout=60))
    store.observe([SerialSeen("e2e_phone", "emulator", "emulator-5554", system.now)])
    system.advance(16)
    assert guarded("-s", "emulator-5554", "shell") == 0
    (phone,) = touched(state_dir, "e2e_phone")
    assert phone["awake"]["touched"] == system.now


def test_a_short_idle_timeout_of_another_lease_of_the_holding_counts(
    machine, store, system, state_dir
):
    needs = [
        Need((Choice("e2e_phone", "emulator"),)),
        Need((Choice("port-8080", "port", Timeouts(idle_timeout=30)),)),
    ]
    store.grant(needs, mine())
    store.observe([SerialSeen("e2e_phone", "emulator", "emulator-5554", system.now)])
    # The calls go to the phone only, whose own idle timeout is 20 minutes.
    for _ in range(14):
        system.advance(5)
        assert guarded("-s", "emulator-5554", "shell") == 0
    (port,) = touched(state_dir, "port-8080")
    assert system.now - port["awake"]["touched"] <= 10
    assert store.holding(port["lease_id"]).keys() == {"e2e_phone", "port-8080"}


def test_the_lease_id_of_banksman_run_touches_and_keeps_the_owner(
    machine, store, system, state_dir, monkeypatch
):
    # A script with the lease id of a holding of another agent process, which is not above it.
    granted = lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    monkeypatch.setenv("BANKSMAN_LEASE", granted.lease_id)
    system.advance(61)
    assert guarded("-s", "emulator-5554", "shell") == 0
    (phone,) = touched(state_dir, "e2e_phone")
    assert phone["awake"]["touched"] == system.now
    assert phone["owner_pid"] == OTHER_AGENT


def test_a_detached_call_of_the_holder_touches_its_holding(
    machine, store, system, state_dir, monkeypatch
):
    monkeypatch.setenv("CLAUDE_PID", detach(system)["CLAUDE_PID"])
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    system.advance(61)
    assert guarded("-s", "emulator-5554", "shell") == 0
    (phone,) = touched(state_dir, "e2e_phone")
    assert phone["awake"]["touched"] == system.now
    assert phone["owner_pid"] == AGENT


def test_a_call_of_a_person_touches_nothing(machine, store, system, monkeypatch):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    system.parents[ME] = 1
    system.advance(61)
    taken = count_locks(monkeypatch)
    assert guarded("-s", "emulator-5554", "shell") == 0
    assert taken == []


@pytest.mark.parametrize("mode", [None, "true", '"warn"'])
def test_a_refused_or_warned_call_touches_nothing(
    machine, store, system, state_dir, config_path, monkeypatch, mode
):
    if mode is not None:
        write_config(config_path, f"[guard]\nstrict = {mode}\n")
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    before = touched(state_dir, "e2e_phone", "e2e_tablet")
    system.advance(61)
    assert guarded("-s", "emulator-5556", "shell") == guard.EXIT_REFUSED
    # A device without a lease: strict mode refuses the call, and warn mode lets it run.
    guarded("-s", "emulator-5558", "shell")
    assert guarded("kill-server") == guard.EXIT_REFUSED
    assert touched(state_dir, "e2e_phone", "e2e_tablet") == before


def test_a_void_lease_is_not_touched_again(machine, store, system, state_dir):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    system.advance(20 * 60)
    before = touched(state_dir, "e2e_phone")
    assert guarded("-s", "emulator-5554", "shell") == guard.EXIT_REFUSED
    assert touched(state_dir, "e2e_phone") == before


@pytest.mark.parametrize("failure", ["lock", "write"])
def test_a_touch_that_fails_leaves_the_call_as_it_is(
    machine, store, system, state_dir, monkeypatch, capsys, failure
):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    before = touched(state_dir, "e2e_phone")
    system.advance(61)
    if failure == "lock":
        monkeypatch.setattr(guard, "LOG_LOCK_WAIT", 0.05)
        held = log_lock(state_dir, 1)
    else:

        def unwritable(directory, name, data):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(store_module, "_replace_file", unwritable)
        held = contextlib.nullcontext()
    with held:
        start = time.monotonic()
        assert guarded("-s", "emulator-5554", "shell", fallback="/opt/adb") == 0
        assert time.monotonic() - start < 1
    assert capsys.readouterr() == ("/opt/adb\n", "")
    assert touched(state_dir, "e2e_phone") == before


def test_a_touch_leaves_a_lease_that_changed_since_the_guard_read_it(
    store, system, state_dir, quarantine_dir
):
    first = lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", mine(), serial="emulator-5556")
    seen = {each.resource: each for each in peek(state_dir, quarantine_dir).leases}
    system.advance(61)
    # Another call touched the phone, and the tablet has a new lease since.
    store.touch("e2e_phone", first.lease_id)
    store.release("e2e_tablet", seen["e2e_tablet"].lease_id)
    lease(store, system, "e2e_tablet", mine(), serial="emulator-5556")
    before = touched(state_dir, "e2e_phone", "e2e_tablet")
    system.advance(61)
    assert store.touch_seen(list(seen.values())) == []
    assert touched(state_dir, "e2e_phone", "e2e_tablet") == before


# The wrapper: banksman admin adb-shim.

# The fake adb records its arguments, the marker of the wrapper, and a variable of the caller.
FAKE_ADB = """#!/bin/sh
here=$(dirname "$0")
printf '%s\\n' "$@" > "$here/arguments"
echo "${BANKSMAN_GUARD:-unset} ${ANDROID_SERIAL:-unset}" > "$here/environment"
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


@pytest.fixture
def shim(fake_adb, tmp_path, capsys):
    assert cli.main(["admin", "adb-shim"]) == 0
    path = tmp_path / "shim" / "adb"
    path.parent.mkdir()
    path.write_text(capsys.readouterr().out)
    path.chmod(0o755)
    return path


def banksman_on_path(tmp_path, script):
    """Return a PATH whose banksman is this shell script, and the system directories."""
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    program = directory / "banksman"
    program.write_text(script)
    program.chmod(0o755)
    return f"{directory}:/usr/bin:/bin"


def real_banksman(tmp_path):
    source, python = shlex.quote(SRC), shlex.quote(sys.executable)
    return banksman_on_path(
        tmp_path, f'#!/bin/sh\nPYTHONPATH={source} exec {python} -m banksman "$@"\n'
    )


def run_wrapper(shim, *arguments, path, env=None):
    return subprocess.run(
        [str(shim), *arguments],
        env={**os.environ, "PATH": path, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_admin_adb_shim_prints_the_wrapper_with_the_adb_of_the_sdk(shim, fake_adb):
    template = resources.files("banksman").joinpath(ADB_SHIM_SCRIPT).read_text()
    assert shim.read_text() == template.replace(ADB_SHIM_PATH, shlex.quote(str(fake_adb)))


def test_admin_adb_shim_warns_when_the_sdk_has_no_adb(config_path, tmp_path, capsys):
    write_config(config_path, f'[android]\nsdk = "{tmp_path / "missing"}"\n')
    assert cli.main(["admin", "adb-shim"]) == 0
    assert "does not exist. Set sdk in [android]" in capsys.readouterr().err


def test_the_wrapper_runs_adb_with_the_arguments_and_the_environment_of_the_call(
    shim, fake_adb, tmp_path
):
    result = run_wrapper(
        shim,
        "-s",
        "emulator-5554",
        "shell",
        "echo 'a  b'",
        "",
        path=real_banksman(tmp_path),
        env={"ANDROID_SERIAL": "emulator-5554"},
    )
    assert (result.returncode, result.stderr) == (7, "")
    arguments = (fake_adb.parent / "arguments").read_text().split("\n")[:-1]
    assert arguments == ["-s", "emulator-5554", "shell", "echo 'a  b'", ""]
    assert (fake_adb.parent / "environment").read_text() == "1 emulator-5554\n"


def test_the_wrapper_passes_every_argument_to_the_guard(shim, fake_adb, tmp_path):
    got = shlex.quote(str(tmp_path / "got"))
    path = banksman_on_path(
        tmp_path, f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {got}\necho {shlex.quote(str(fake_adb))}\n"
    )
    result = run_wrapper(shim, "-s", "emulator-5554", "shell", "echo 'a  b'", "", path=path)
    assert result.returncode == 7
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


def test_the_note_of_the_guard_leaves_the_output_of_adb_as_it_is(shim, fake_adb, tmp_path):
    listed = "List of devices attached\nemulator-5554\tdevice\n\n"
    fake_adb.write_text(f"#!/bin/sh\nprintf {shlex.quote(listed)}\n")
    path = banksman_on_path(
        tmp_path, f"#!/bin/sh\necho 'banksman: a note' >&2\necho {shlex.quote(str(fake_adb))}\n"
    )
    result = run_wrapper(shim, "devices", path=path)
    assert (result.returncode, result.stdout, result.stderr) == (0, listed, "banksman: a note\n")


def test_a_refusal_ends_the_call_with_3_and_runs_no_adb(shim, fake_adb, tmp_path):
    path = banksman_on_path(tmp_path, "#!/bin/sh\necho 'banksman: refused' >&2\nexit 3\n")
    result = run_wrapper(shim, "-s", "emulator-5554", "shell", path=path)
    assert (result.returncode, result.stderr) == (3, "banksman: refused\n")
    assert not (fake_adb.parent / "arguments").exists()


def test_ctrl_c_during_the_check_ends_the_call(shim, fake_adb, tmp_path):
    path = banksman_on_path(tmp_path, "#!/bin/sh\nexit 130\n")
    assert run_wrapper(shim, "devices", path=path).returncode == 130
    assert not (fake_adb.parent / "arguments").exists()


@pytest.mark.parametrize(
    "script",
    [
        # Python cannot start, for example after an upgrade of Python.
        "#!/nonexistent/python3\n",
        # An error, such as an import error after a half-finished upgrade.
        "#!/bin/sh\necho 'Traceback' >&2\nexit 1\n",
        # An answer that is not the path of an adb.
        "#!/bin/sh\necho 'not a path'\n",
        "#!/bin/sh\nexit 0\n",
    ],
)
def test_a_banksman_that_fails_lets_the_wrapper_run_the_saved_adb(
    shim, fake_adb, tmp_path, script
):
    result = run_wrapper(shim, "devices", path=banksman_on_path(tmp_path, script))
    assert result.returncode == 7
    assert (fake_adb.parent / "arguments").read_text() == "devices\n"
    assert "banksman: adb guard: this adb call is not checked" in result.stderr


def test_the_wrapper_stops_when_the_adb_of_the_sdk_is_the_wrapper(shim, fake_adb, tmp_path):
    # A configuration whose sdk holds a copy of the wrapper as its adb.
    loop = tmp_path / "loop" / "platform-tools" / "adb"
    loop.parent.mkdir(parents=True)
    loop.write_text(shim.read_text())
    loop.chmod(0o755)
    path = banksman_on_path(tmp_path, f"#!/bin/sh\necho {shlex.quote(str(loop))}\n")
    result = run_wrapper(shim, "devices", path=path)
    assert result.returncode == 1
    assert "the adb of the Android SDK runs this wrapper again" in result.stderr
    assert not (fake_adb.parent / "arguments").exists()


def test_without_banksman_the_wrapper_runs_adb_and_says_that_the_call_is_not_checked(
    shim, fake_adb
):
    result = run_wrapper(shim, "devices", path="/usr/bin:/bin")
    assert result.returncode == 7
    assert (fake_adb.parent / "arguments").read_text() == "devices\n"
    assert "banksman is not on the PATH, so this adb call is not checked" in result.stderr


def test_the_wrapper_uses_no_banksman_from_a_relative_directory(shim, fake_adb, tmp_path):
    relative = tmp_path / "project" / "bin"
    relative.mkdir(parents=True)
    (relative / "banksman").write_text("#!/bin/sh\nexit 3\n")
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


def test_the_fast_path_adds_little_time_to_an_adb_call(shim, fake_adb, tmp_path):
    # Without leases, the guard reads no process list and runs no adb devices. The bound is
    # wide, so that a slow machine does not fail the test; the measurement is printed.
    env = {**os.environ, "PATH": real_banksman(tmp_path)}

    def median(command):
        times = []
        for _ in range(5):
            start = time.perf_counter()
            assert subprocess.run(command, env=env, timeout=60).returncode == 7
            times.append(time.perf_counter() - start)
        return statistics.median(times)

    arguments = ["-s", "emulator-5554", "shell", "true"]
    alone = median([str(fake_adb), *arguments])
    with_guard = median([str(shim), *arguments])
    print(f"\nthe adb guard adds {1000 * (with_guard - alone):.0f} ms to an adb call")
    assert with_guard - alone < 0.5


# The device tasks of a Gradle build: `banksman guard gradle`, which the part of the guard for
# Gradle runs before such a build runs its first task.

TASKS = [":app:installDebug", ":app:connectedDebugAndroidTest"]
SAID = "gradle :app:installDebug :app:connectedDebugAndroidTest"
NO_SERIAL = (
    ". The build names no device, so the Android Gradle plugin acts on every device that adb"
    " lists: run the build under banksman run, or set ANDROID_SERIAL to the serial of your device"
)


def gradle(system, *, env=None, adb=no_adb, tasks=TASKS, serials=()):
    return guard.check_gradle(
        list(tasks), load_config(), env or {}, system, adb, serials=list(serials)
    )


def gradle_check(system, **kwargs):
    """Return the text of a refusal, or None when the build runs without a warning."""
    found = gradle(system, **kwargs).verdict
    if found is None:
        return None
    assert found.action == REFUSED, found.text
    return found.text


def test_a_build_on_the_device_of_another_holding_in_android_serial_is_refused(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    assert gradle_check(system, env={"ANDROID_SERIAL": "emulator-5554"}) == (
        f"{SAID}: refused: another holder leases emulator-5554 (e2e_phone), and the lease is"
        " ready: codex · #123 · verify the tablet layout. Use a device of your own lease;"
        " banksman status shows who holds what. Run banksman agent-guide and follow it"
    )


def test_a_build_on_the_device_of_the_caller_passes_and_uses_its_holding(store, system):
    granted = lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    decision = gradle(system, env={"ANDROID_SERIAL": "emulator-5554"})
    assert decision.verdict is None
    assert [each.lease_id for each in decision.used] == [granted.lease_id]


def test_the_lease_id_of_banksman_run_passes_a_build_without_the_process_list(store, system):
    granted = lease(store, system, "e2e_phone", mine(owner_pid=None), serial="emulator-5554")
    env = {"BANKSMAN_LEASE": granted.lease_id, "ANDROID_SERIAL": "emulator-5554"}
    assert gradle_check(NoProcessList(), env=env) is None


def test_every_serial_of_the_list_in_android_serial_is_checked(store, system):
    # The Android Gradle plugin reads a list with commas.
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    env = {"ANDROID_SERIAL": "emulator-5554, emulator-5556"}
    assert "another holder leases emulator-5556" in gradle_check(system, env=env)


def test_without_android_serial_the_build_acts_on_every_device_that_adb_lists(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    refusal = gradle_check(system, adb=listing(PHONE, EMULATOR))
    assert refusal.startswith(f"{SAID}: refused: another holder leases emulator-5554 (e2e_phone)")
    assert refusal.endswith(f"Run banksman agent-guide and follow it{NO_SERIAL}")


def test_without_android_serial_a_build_passes_when_only_own_devices_are_listed(store, system):
    granted = lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    decision = gradle(system, adb=listing(EMULATOR))
    assert decision.verdict is None
    assert [each.lease_id for each in decision.used] == [granted.lease_id]


def test_a_device_that_is_not_online_is_not_a_device_of_the_build(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    offline = Transport("emulator-5554", "offline", transport_id="4")
    assert gradle_check(system, adb=listing(offline)) is None


def test_without_leases_a_build_without_android_serial_runs_without_adb(system):
    assert gradle(NoProcessList()) == guard.RUNS


def test_strict_refuses_a_free_device_of_a_build_and_says_what_to_do(store, system, strict):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    assert gradle_check(system, adb=listing(EMULATOR, PHONE)) == (
        f"{SAID}: refused: R5CR1234ABC is not a device of your leases, and the adb guard is"
        f" strict. Get a device through banksman: run banksman agent-guide and follow it{NO_SERIAL}"
    )


def test_strict_refuses_a_serial_that_no_lease_of_the_caller_has(store, system, strict):
    assert gradle_check(system, env={"ANDROID_SERIAL": "emulator-5556"}) == (
        f"{SAID}: refused: emulator-5556 is not a device of your leases, and the adb guard is"
        " strict. Get a device through banksman: run banksman agent-guide and follow it"
    )


def test_warn_lets_a_build_run_with_a_warning(store, system, warn):
    found = gradle(system, env={"ANDROID_SERIAL": "emulator-5556"}).verdict
    assert (found.action, found.reason, found.serial) == (WARNED, NOT_LEASED, "emulator-5556")
    assert found.text.startswith(f"warning: {SAID}: emulator-5556 is not a device of your leases")


def test_a_refusal_comes_before_a_warning_among_the_devices_of_a_build(store, system, warn):
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    tablet = Transport("emulator-5556", "device", transport_id="5")
    found = gradle(system, adb=listing(PHONE, EMULATOR, tablet)).verdict
    assert (found.action, found.serial) == (REFUSED, "emulator-5556")


def test_a_build_of_a_person_passes_also_on_the_device_of_another_holding(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    system.parents[ME] = 1
    assert gradle_check(system, env={"ANDROID_SERIAL": "emulator-5554"}) is None


def test_a_daemon_older_than_the_agent_acts_for_the_agent_in_the_environment_of_the_build(
    store, system
):
    # A Gradle daemon that an earlier build started takes the environment of the client of each
    # build, so the variable of the agent reaches the guard, and the age of the daemon says
    # nothing about it. For an adb call, the same environment counts for nothing.
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    env = {**detach(system, agent_started="2001"), "ANDROID_SERIAL": "emulator-5554"}
    assert "another holder" in gradle_check(system, env=env)
    assert check(system, "-s", "emulator-5554", "shell", env=env) is None


def test_the_agent_in_the_environment_of_a_build_wins_over_an_agent_above_the_daemon(
    store, system
):
    # The client of another agent started the daemon, and still runs above it.
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    system.parents[ME] = OTHER_AGENT
    env = {"CLAUDE_PID": str(AGENT), "ANDROID_SERIAL": "emulator-5554"}
    assert gradle_check(system, env=env) is None
    env["ANDROID_SERIAL"] = "emulator-5556"
    assert "another holder" in gradle_check(system, env=env)


def test_a_long_list_of_tasks_is_shortened_in_the_refusal(store, system):
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    tasks = [f":m{number}:installDebug" for number in range(5)]
    refusal = gradle_check(system, env={"ANDROID_SERIAL": "emulator-5554"}, tasks=tasks)
    assert refusal.startswith(
        "gradle :m0:installDebug :m1:installDebug :m2:installDebug and 2 more: refused:"
    )


@pytest.mark.parametrize(
    ("tasks", "command"),
    [
        ([":app:installDebug"], "gradle install"),
        (
            [":app:uninstallAll", ":a:connectedDebugAndroidTest", ":b:installDebugAndroidTest"],
            "gradle connectedAndroidTest install uninstall",
        ),
        ([":lib:jar"], "gradle"),
    ],
)
def test_the_log_has_the_words_of_a_build_and_never_its_task_names(tasks, command):
    call = guard.gradle_call(tasks, "emulator-5554", named=True)
    assert call.command == command
    assert COMMAND.fullmatch(command)


def test_a_refused_build_is_in_the_log_with_the_caller(
    machine, store, system, worktree, log_path, monkeypatch
):
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5554")
    lease(store, system, "e2e_phone", other(), serial="emulator-5554")
    tasks = [":app:installDebug", ":app:uninstallAll", ":app:connectedDebugAndroidTest"]
    assert guard.main(["gradle", "--", *tasks]) == guard.EXIT_REFUSED
    (event,) = logged(log_path)
    assert (event.command, event.serial, event.reason, event.action) == (
        "gradle connectedAndroidTest install uninstall",
        "emulator-5554",
        OTHER_HOLDER,
        REFUSED,
    )
    assert (event.owner, event.owner_pid, event.agent, event.session) == (
        worktree,
        AGENT,
        "claude",
        "session-1",
    )


def test_guard_gradle_prints_nothing_when_the_build_may_run(machine, capsys):
    assert guard.main(["gradle", "--", ":app:installDebug"]) == 0
    assert capsys.readouterr() == ("", "")


def test_guard_gradle_needs_a_task(capsys):
    assert guard.main(["gradle", "--"]) == 2
    assert guard.USAGE in capsys.readouterr().err


def test_a_guard_that_fails_lets_the_build_run_and_says_so(machine, config_path, capsys):
    write_config(config_path, "[guard]\nstrict = 7\n")
    assert guard.main(["gradle", "--", ":app:installDebug"]) == 0
    assert capsys.readouterr().err.startswith(
        "banksman: adb guard: the device tasks of this build are not checked: "
    )


def test_a_build_of_the_holder_touches_its_holding(machine, store, system, state_dir, monkeypatch):
    needs = [Need((Choice(name, "emulator"),)) for name in ("e2e_phone", "e2e_tablet")]
    store.grant(needs, mine())
    store.observe([SerialSeen("e2e_phone", "emulator", "emulator-5554", system.now)])
    system.advance(61)
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5554")
    assert guard.main(["gradle", "--", ":app:installDebug"]) == 0
    phone, tablet = touched(state_dir, "e2e_phone", "e2e_tablet")
    assert phone["awake"]["touched"] == tablet["awake"]["touched"] == system.now


def test_the_serial_option_of_the_connected_tests_comes_before_android_serial(store, system):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    env = {"ANDROID_SERIAL": "emulator-5554"}
    assert gradle_check(system, env=env) is None
    refusal = gradle_check(system, env=env, serials=["emulator-5556"])
    assert refusal.startswith(
        "gradle :app:connectedDebugAndroidTest: refused: another holder leases emulator-5556"
    )


def test_the_serial_option_names_no_device_of_an_install(store, system):
    # An install reads only ANDROID_SERIAL, so without the variable it acts on every device.
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    tablet = Transport("emulator-5556", "device", transport_id="5")
    refusal = gradle_check(system, serials=["emulator-5554"], adb=listing(EMULATOR, tablet))
    assert refusal.startswith(
        "gradle :app:installDebug: refused: another holder leases emulator-5556"
    )
    # Connected tests alone, with the option, act on the device of the option only.
    tests = [":app:connectedDebugAndroidTest"]
    assert gradle_check(system, serials=["emulator-5554"], tasks=tests) is None


def test_a_warned_build_still_uses_the_leases_of_its_own_devices(store, system, warn):
    granted = lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    decision = gradle(system, adb=listing(EMULATOR, PHONE))
    assert (decision.verdict.action, decision.verdict.serial) == (WARNED, "R5CR1234ABC")
    assert [each.lease_id for each in decision.used] == [granted.lease_id]


def test_a_warned_build_of_the_holder_touches_its_holding(
    machine, store, system, state_dir, warn, monkeypatch
):
    lease(store, system, "e2e_phone", mine(), serial="emulator-5554")
    system.advance(61)
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5554,emulator-5556")
    assert guard.main(["gradle", "--", ":app:installDebug"]) == 0
    (phone,) = touched(state_dir, "e2e_phone")
    assert phone["awake"]["touched"] == system.now


def test_guard_gradle_reads_the_serial_options_before_the_tasks(machine, store, system, capsys):
    lease(store, system, "e2e_tablet", other(), serial="emulator-5556")
    tasks = ["--", ":app:connectedDebugAndroidTest"]
    assert guard.main(["gradle", "--serial", "emulator-5554", "--serial", "emulator-5556", *tasks])
    assert "another holder leases emulator-5556" in capsys.readouterr().err
