"""The adb guard: refuse an agent's device command when another holding leases the device.

The adb wrapper that `banksman admin adb-shim` prints runs `banksman guard adb -- ARGUMENT ...`
for every adb call. The guard decides from the lease files, without the lock, without reaping,
and without discovery, and then runs the real adb of the Android SDK in its own place. It reads
the process list and runs `adb devices` only when a decision needs them, because it runs before
every adb call, and a UI test makes many.

It is a guardrail, not a security boundary: a call through the full path of adb, a tool that
talks to the adb server itself, and the emulator console do not pass through it. It never
acquires a lease itself: that would make it a second way to get a device, and hide that two
holders want the same one.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property

from banksman import android
from banksman.android import Transport
from banksman.config import Android, Config, load_config
from banksman.errors import BanksmanError
from banksman.fencing import ancestors
from banksman.identity import find_agent
from banksman.lease import HELD, Lease
from banksman.sanitize import clean
from banksman.store import Snapshot, Unreadable, default_quarantine_dir, default_state_dir, peek
from banksman.system import Machine, Process, System

# The status of `check` for a lost lease: the caller must not use the device.
EXIT_REFUSED = 3
LEASE_VARIABLE = "BANKSMAN_LEASE"
# Set for the adb that the guard runs. A guard that finds it set runs as that adb: the adb of the
# SDK is the wrapper itself, and the two would run each other without end.
MARKER = "BANKSMAN_GUARD"
USAGE = "usage: banksman guard adb [--fallback-adb PATH] -- [ADB ARGUMENT ...]"
# The adb that the wrapper saved, for a call when the configuration cannot be read.
FALLBACK_OPTION = "--fallback-adb"
# What a command acts on: only the adb server or this machine, such as `adb devices`; one device;
# or every device, such as `adb kill-server`.
HOST = "host"
DEVICE = "device"
ALL = "all"
_HOST_COMMANDS = frozenset(
    {
        "devices",
        "connect",
        "pair",
        "mdns",
        "keygen",
        "pubkey",
        "start-server",
        "help",
        "--help",
        "/?",
        "version",
        "--version",
        "host-features",
        "track-devices",
        "server-status",
    }
)
# The state that `adb devices` shows for a USB device that adb may not open. adb never chooses
# such a device.
_NO_PERMISSION = "no"
# Commands, besides kill-server and disconnect without an address, that act on every device:
# `forward --remove-all` removes the port forwards of every device, not only of the chosen one.
_FOR_EVERY_DEVICE = (["reconnect", "offline"], ["forward", "--remove-all"])
# How `adb emu` reads the console port of an emulator from a serial, as sscanf with
# "emulator-%d" does.
_EMULATOR = re.compile(r"emulator-\s*\+?([0-9]+)")
_EXIT_NOT_FOUND = 127
_EXIT_CANNOT_RUN = 126
# The most leases that a refused command for every device names.
_SHOWN = 5

Listing = Callable[[], Sequence[Transport]]


@dataclass(frozen=True)
class Call:
    """What one adb command line asks for, read as adb reads it."""

    scope: str
    # The words that name the command in a message, such as "shell" or "reconnect offline".
    command: str = ""
    serial: str | None = None
    transport_id: str | None = None
    # -d gives "usb", and -e gives "local".
    transport: str | None = None
    # The options that name the adb server, such as -P 5038, so that `adb devices` asks the same
    # server.
    server: tuple[str, ...] = ()
    # `adb emu` talks to the console of an emulator, which it chooses by rules of its own.
    console: bool = False


def main(argv: Sequence[str]) -> int:
    """Run `banksman guard adb -- ARGUMENT ...`: check the call, then run the real adb."""
    if argv[:1] != ["adb"]:
        print(USAGE, file=sys.stderr)
        return 2
    arguments = list(argv[1:])
    fallback = None
    if arguments[:1] == [FALLBACK_OPTION] and len(arguments) > 1:
        fallback, arguments = arguments[1], arguments[2:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    env = dict(os.environ)
    if env.get(MARKER):
        _say(
            "adb guard: the adb of the Android SDK runs this guard again. Set sdk in [android] to"
            " the Android SDK, not to the directory of the adb wrapper"
        )
        return 1
    # Without a configuration, the adb that the wrapper saved is the best guess: the default SDK
    # can be missing, or have an adb of another version, which would restart the adb server.
    adb = fallback or android.adb_path(Android())
    try:
        config = load_config()
        adb = android.adb_path(config.android)
        refusal = check(arguments, config, env, Machine())
    except Exception as exc:
        # A broken guard that blocks every device is worse than a short time without
        # protection, so the call runs, and the person who reads the output sees why.
        reason = exc if isinstance(exc, (BanksmanError, OSError)) else repr(exc)
        _say(f"adb guard: this adb call is not checked: {reason}")
        refusal = None
    if refusal is not None:
        _say(refusal)
        return EXIT_REFUSED
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        android.exec_adb(adb, arguments, {**env, MARKER: "1"})
    except OSError as exc:
        _say(f"adb guard: cannot run {adb}: {exc.strerror}. Set sdk in [android]")
        return _EXIT_NOT_FOUND if isinstance(exc, FileNotFoundError) else _EXIT_CANNOT_RUN


def check(
    arguments: Sequence[str],
    config: Config,
    env: Mapping[str, str],
    system: System,
    listing: Listing | None = None,
) -> str | None:
    """Return why an adb call is refused, or None when adb may run it.

    `listing` lists the devices as `adb devices -l` does; by default it runs adb.
    """
    call = parse(arguments)
    if call.scope == HOST:
        return None
    snapshot = peek(default_state_dir(), default_quarantine_dir())

    def adb_devices() -> Sequence[Transport]:
        return android.transports(config.android, call.server)

    caller = Caller(env, snapshot.leases, system, config.holder.agents)
    return decide(
        call, caller, _devices(snapshot, config), config.guard.strict, listing or adb_devices
    )


def parse(arguments: Sequence[str]) -> Call:
    """Read an adb command line with the rules of adb itself.

    A command line that adb refuses, for example `-s` without a serial, is a host command: adb
    prints its own error, and acts on nothing.
    """
    serial = transport_id = transport = None
    server: list[str] = []
    rest = list(arguments)
    while rest:
        word = rest.pop(0)
        if word in ("server", "nodaemon", "fork-server"):
            return Call(HOST, word)
        if word in ("--reply-fd", "--one-device", "-L"):
            if not rest:
                return Call(HOST)
            value = rest.pop(0)
            if word == "-L":
                server += [word, value]
        elif word.startswith(("-s", "-t")):
            # adb takes the value in the same word only when it starts with a digit.
            if word[2:3].isdigit():
                value = word[2:]
            elif len(word) == 2 and rest:
                value = rest.pop(0)
            else:
                return Call(HOST)
            if word.startswith("-s"):
                serial = value
            elif not value.isdigit():
                return Call(HOST)
            else:
                # Transport id 0 means no transport id.
                transport_id = str(int(value)) if int(value) else None
        elif word in ("-d", "-e"):
            transport = "usb" if word == "-d" else "local"
        elif word.startswith(("-H", "-P")):
            if len(word) > 2:
                server.append(word)
            elif rest:
                server += [word, rest.pop(0)]
            else:
                return Call(HOST)
        elif word not in ("-a", "--exit-on-write-error"):
            rest.insert(0, word)
            break
    if not rest or rest[0] in _HOST_COMMANDS or rest[:2] == ["forward", "--list"]:
        return Call(HOST, rest[0] if rest else "")
    if rest[0] == "kill-server":
        return Call(ALL, rest[0], server=tuple(server))
    if rest == ["disconnect"] or rest[:2] in _FOR_EVERY_DEVICE:
        return Call(ALL, " ".join(rest[:2]), server=tuple(server))
    if rest[0] == "disconnect":
        # It disconnects the device that it names, whatever -s says.
        return Call(DEVICE, rest[0], serial=rest[1], server=tuple(server))
    # A wait such as wait-for-device can come before the command.
    console = (rest[1:2] if rest[0].startswith("wait-for-") else rest[:1]) == ["emu"]
    return Call(DEVICE, rest[0], serial, transport_id, transport, tuple(server), console)


class Caller:
    """The process that runs adb, and the holdings that it acts for.

    It reads the process list only when a question needs it.
    """

    def __init__(
        self, env: Mapping[str, str], leases: Sequence[Lease], system: System, agents: Sequence[str]
    ) -> None:
        self.env = env
        self.leases = leases
        self.system = system
        self.agents = agents
        self.pid = os.getpid()

    def token_holding(self) -> str | None:
        """Return the holding of the lease that `banksman run` gives its command, if any."""
        lease_id = self.env.get(LEASE_VARIABLE)
        if not lease_id:
            return None
        return next((lease.holding for lease in self.leases if lease.lease_id == lease_id), None)

    def is_agent(self) -> bool:
        """Whether a known agent process is among the processes above the caller."""
        return find_agent(self._table, self.agents, self.pid)[0] is not None

    def descends_from(self, pid: int | None, started: str | None) -> bool:
        process = None if pid is None else self._table.get(pid)
        return process is not None and process.started == started and pid in self._above

    @cached_property
    def holdings(self) -> set[str]:
        """Return the holdings of the lease id that the caller has, and of the owner processes
        above it."""
        found = {
            lease.holding
            for lease in self.leases
            if self.descends_from(lease.owner_pid, lease.owner_started)
        }
        token = self.token_holding()
        return found if token is None else found | {token}

    @cached_property
    def _table(self) -> Mapping[int, Process]:
        return self.system.process_table()

    @cached_property
    def _above(self) -> set[int]:
        return set(ancestors(self._table, self.pid))


def decide(
    call: Call, caller: Caller, devices: Snapshot, strict: bool, listing: Listing
) -> str | None:
    """Return why a call is refused, or None when adb may run it.

    `devices` holds the leases that can name a device.
    """
    if call.scope == HOST:
        return None
    if call.scope == ALL:
        running = [lease for lease in devices.leases if lease.state in HELD and lease.serial]
        if not running or not caller.is_agent():
            return None
        others = [lease for lease in running if lease.holding not in caller.holdings]
        return _every_device_refused(call, others) if others else None
    if not strict and not devices.leases and not devices.unreadable:
        return None
    serial = target(call, caller.env, devices, listing)
    if serial is None:
        return None
    leases, unreadable = _leases_of(serial, devices)
    if not leases and not unreadable:
        if strict and caller.is_agent():
            return (
                f"adb {call.command}: refused: {serial} is not a device of your leases, and the"
                " adb guard is strict. Get a device with banksman acquire"
            )
        return None
    # The lease id that `banksman run` gives its command decides without the process list.
    token = caller.token_holding()
    if not unreadable and all(lease.state in HELD and lease.holding == token for lease in leases):
        return None
    if not caller.is_agent():
        return None
    for lease in leases:
        if lease.state in HELD and lease.holding in caller.holdings:
            continue
        # The on_acquire hook of the acquire that resets the instance, or the on_void hook of
        # the reaper that takes it back, which can run inside a command of another agent.
        if caller.descends_from(lease.reaper_pid, lease.reaper_started):
            continue
        return _refused(call, serial, lease, mine=lease.holding in caller.holdings)
    if unreadable:
        return (
            f"adb {call.command}: refused: the lease file of {serial} cannot be read, so the"
            f" device is out of use: {unreadable[0].error}"
        )
    return None


def target(
    call: Call, env: Mapping[str, str], devices: Snapshot, listing: Listing
) -> str | None:
    """Return the serial of the device that adb chooses for a call, or None when adb finds no
    single device, and fails by itself.

    The order is the order of adb: -t, then -s, then -d or -e, then ANDROID_SERIAL, then the
    only device. `adb emu` has rules of its own.
    """
    if call.console:
        return _console(call, env, listing)
    if call.transport_id is not None:
        return _one(
            [found for found in _usable(listing()) if found.transport_id == call.transport_id]
        )
    serial = call.serial
    if serial is None and call.transport is None:
        serial = env.get("ANDROID_SERIAL") or None
    if serial is not None:
        return _serial(serial, devices, listing)
    usable = _usable(listing())
    if call.transport is not None:
        # A USB device has an address. An emulator, and a device over TCP/IP, have none.
        usable = [found for found in usable if bool(found.devpath) == (call.transport == "usb")]
    return _one(usable)


def _console(call: Call, env: Mapping[str, str], listing: Listing) -> str | None:
    """Return the emulator whose console `adb emu` talks to, or None when it finds none.

    adb takes the console port from the serial of -s or ANDROID_SERIAL, and without one, it takes
    the only emulator that it knows, also when other devices are connected. It ignores -t, -d,
    and -e.
    """
    serial = call.serial
    if serial is None and call.transport is None:
        serial = env.get("ANDROID_SERIAL") or None
    if serial is None:
        emulators = [found.serial for found in listing() if _EMULATOR.match(found.serial)]
        if len(emulators) != 1:
            return None
        serial = emulators[0]
    port = _EMULATOR.match(serial)
    return None if port is None else f"emulator-{int(port.group(1))}"


def _serial(given: str, devices: Snapshot, listing: Listing) -> str | None:
    known = {lease.serial for lease in devices.leases if lease.serial}
    known |= {lease.resource for lease in devices.leases}
    known |= {entry.resource for entry in devices.unreadable}
    if given in known:
        return given
    if ":" not in given:
        # adb also finds a device over TCP/IP by its host alone, for example 192.0.2.7 for
        # 192.0.2.7:5555.
        for serial in sorted(known):
            address = _address(serial, None)
            if address is not None and address[1] is not None and address[0] == given:
                return serial
        return given
    # A qualifier such as model:Pixel_8, a USB address, or an address over TCP/IP in another
    # form: only adb knows which device has it.
    return _one([found for found in _usable(listing()) if matches(found, given)])


def matches(transport: Transport, given: str) -> bool:
    """Whether adb chooses this device for `adb -s <given>`, by the rules of adb itself."""
    if given == transport.serial:
        return True
    if not transport.devpath:
        address = given[4:] if given.startswith(("tcp:", "udp:")) else given
        own = _address(transport.serial, None)
        # A given address without a port takes the port of the device.
        if own is not None and _address(address, own[1]) == own:
            return True
    qualified = {
        f"{key}:{value}"
        for key, value in (
            ("product", transport.product),
            ("model", transport.model),
            ("device", transport.device),
        )
        if value
    }
    return given in qualified or given == transport.devpath


def _address(text: str, port: int | None) -> tuple[str, int | None] | None:
    """Return the host and the port of `host`, `host:port`, `[ipv6]`, or `[ipv6]:port`."""
    if text.startswith("["):
        host, bracket, rest = text[1:].partition("]")
        if not bracket:
            return None
    elif text.count(":") == 1:
        host, _, number = text.partition(":")
        rest = f":{number}"
    else:
        host, rest = text, ""
    if rest:
        if not rest.startswith(":") or not rest[1:].isdigit():
            return None
        port = int(rest[1:])
    return (host, port) if host else None


def _usable(transports: Sequence[Transport]) -> list[Transport]:
    return [found for found in transports if found.state != _NO_PERMISSION]


def _one(transports: Sequence[Transport]) -> str | None:
    return transports[0].serial if len(transports) == 1 else None


def _devices(snapshot: Snapshot, config: Config) -> Snapshot:
    """Return the leases that can name a device: not the leases of counted kinds or of kinds
    that list their instances, such as build slots or ports."""

    def device(lease: Lease) -> bool:
        kind = config.kinds.get(lease.kind)
        return lease.serial is not None or kind is None or kind.discovered

    return Snapshot([lease for lease in snapshot.leases if device(lease)], snapshot.unreadable)


def _leases_of(serial: str, devices: Snapshot) -> tuple[list[Lease], list[Unreadable]]:
    held = [lease for lease in devices.leases if lease.state in HELD and lease.serial == serial]
    # Discovery keeps the serial up to date in held leases only. A lease that is not held can
    # keep a serial that another instance has now, so its serial counts only while no held lease
    # has it.
    by_serial = held or [
        lease for lease in devices.leases if lease.state not in HELD and lease.serial == serial
    ]
    # The name of a physical device is its serial.
    named = [lease for lease in devices.leases if lease.resource == serial]
    leases = by_serial + [lease for lease in named if lease not in by_serial]
    return leases, [entry for entry in devices.unreadable if entry.resource == serial]


def _refused(call: Call, serial: str, lease: Lease, *, mine: bool) -> str:
    device = serial if lease.resource == serial else f"{serial} ({lease.resource})"
    if mine:
        return (
            f"adb {call.command}: refused: your lease of {device} is lost: it is {lease.state}."
            " Stop using the device, and acquire one again"
        )
    return (
        f"adb {call.command}: refused: another holder leases {device}, and the lease is"
        f" {lease.state}: {_holder(lease)}. Use a device of your own lease; banksman status"
        " shows who holds what"
    )


def _every_device_refused(call: Call, others: Sequence[Lease]) -> str:
    shown = [f"{lease.serial} ({_holder(lease)})" for lease in others[:_SHOWN]]
    if len(others) > _SHOWN:
        shown.append(f"{len(others) - _SHOWN} more")
    return (
        f"adb {call.command}: refused: it acts on every device, also on devices that other"
        f" holders lease and that run now: {', '.join(shown)}"
    )


def _holder(lease: Lease) -> str:
    # Only a refusal needs the console, so the guard does not import it before every adb call.
    from banksman.console import holder_label

    return holder_label(lease)


def _say(text: str) -> None:
    print(clean(f"banksman: {text}"), file=sys.stderr)
