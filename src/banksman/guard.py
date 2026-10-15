"""The adb guard: refuse an agent's device command when another holding leases the device.

The adb wrapper that `banksman admin adb-shim` prints runs `banksman guard adb -- ARGUMENT ...`
for every adb call. The guard only decides: it prints the adb to run, or refuses the call, and
the wrapper runs adb itself, so that a banksman that cannot start never blocks adb. The guard
decides from the lease files, without the lock, without reaping, and without discovery. It reads
the process list and runs `adb devices` only when a decision needs them, because it runs before
every adb call, and a UI test makes many. It writes to the log only when it refuses a call, or
lets a call run with a warning. It takes the lock of the lease store only then, and when a call
of the holder touches the lease of its device, which is due about once a minute.

It is a guardrail, not a security boundary: a call through the full path of adb, a tool that
talks to the adb server itself, and the emulator console do not pass through it. It never
acquires a lease itself: that would make it a second way to get a device, and hide that two
holders want the same one.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property

from banksman import android
from banksman.android import Transport
from banksman.config import STRICT, Android, Config, HolderRules, load_config
from banksman.errors import BanksmanError
from banksman.fencing import ancestors
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
from banksman.identity import find_agent, find_holder
from banksman.lease import HELD, RESOURCE_NAME, Holder, Lease
from banksman.sanitize import clean
from banksman.store import (
    Snapshot,
    Store,
    Unreadable,
    default_log_path,
    default_quarantine_dir,
    default_state_dir,
    log_lock,
    peek,
)
from banksman.system import Machine, Process, System

# The status of `check` for a lost lease: the caller must not use the device.
EXIT_REFUSED = 3
LEASE_VARIABLE = "BANKSMAN_LEASE"
USAGE = "usage: banksman guard adb [--fallback-adb PATH] -- [ADB ARGUMENT ...]"
# The adb that the wrapper saved, for a configuration that names no SDK or cannot be read.
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
# The most leases that a refused command for every device names.
_SHOWN = 5
# The longest wait for the lock of the lease store before an event goes to the log. A holder keeps
# the lock for milliseconds, so a longer wait means a holder that is stopped or hung, and the adb
# call must not wait for it.
LOG_LOCK_WAIT = 0.5
# How long after the last touch of a lease a device call of the holder touches it again, unless
# the idle timeout is short. Each touch takes the lock and writes the lease files of the holding,
# and the idle timeout is minutes long.
TOUCH_EVERY = 60.0

Listing = Callable[[], Sequence[Transport]]
_GUIDE = "Get a device through banksman: run banksman agent-guide and follow it"


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


@dataclass(frozen=True)
class Verdict:
    """A call that the guard refuses, or lets run with a warning."""

    action: str
    reason: str
    text: str
    command: str
    caller: Caller
    serial: str | None = None
    # The lease of the device, or for a command for every device, the leases of the running
    # devices of other holders.
    leases: tuple[Lease, ...] = ()


@dataclass(frozen=True)
class Decision:
    """What the guard decides for one call: a refusal or a warning, or no verdict when the call
    runs."""

    verdict: Verdict | None = None
    # The held leases of the holding whose lease of the device let the call run, also the leases
    # of its other resources. The call is use of the holding, so it touches them.
    used: tuple[Lease, ...] = ()


RUNS = Decision()


def main(argv: Sequence[str]) -> int:
    """Run `banksman guard adb -- ARGUMENT ...` for the adb wrapper.

    Print the path of the adb to run and exit with status 0 when the call may run, or exit with
    status 3 when it is refused.
    """
    if argv[:1] != ["adb"]:
        print(USAGE, file=sys.stderr)
        return 2
    arguments = list(argv[1:])
    fallback = None
    if arguments[:1] == [FALLBACK_OPTION] and len(arguments) > 1:
        fallback, arguments = arguments[1], arguments[2:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    # Without an sdk in the configuration, the adb that the wrapper saved is the best guess: the
    # default SDK can be missing, or have an adb of another version, which would restart the adb
    # server.
    adb = fallback or android.adb_path(Android())
    system = Machine()
    try:
        config = load_config()
        if config.android.sdk is not None or fallback is None:
            adb = android.adb_path(config.android)
        decision = check(arguments, config, os.environ, system)
    except Exception as exc:
        # A broken guard that blocks every device is worse than a short time without
        # protection, so the call runs, and the person who reads the output sees why.
        _say(f"adb guard: this adb call is not checked: {_reason(exc)}")
        decision = RUNS
    verdict = decision.verdict
    # A touch that fails changes nothing for the call. The guard says nothing about it either,
    # because a script can read standard error with the output of adb, and the next call of the
    # holder tries again.
    with contextlib.suppress(Exception):
        touch(decision.used, system)
    if verdict is not None:
        _say(verdict.text)
        try:
            record(verdict)
        except Exception as exc:
            # The decision stands: a log that cannot be written never refuses a call.
            _say(f"adb guard: warning: this adb call is not in the log: {_reason(exc)}")
        if verdict.action == REFUSED:
            return EXIT_REFUSED
    print(adb)
    return 0


def check(
    arguments: Sequence[str],
    config: Config,
    env: Mapping[str, str],
    system: System,
    listing: Listing | None = None,
) -> Decision:
    """Return whether an adb call is refused or warned, and the leases of the caller that it
    uses.

    `listing` lists the devices as `adb devices -l` does; by default it runs adb.
    """
    call = parse(arguments)
    if call.scope == HOST:
        return RUNS
    snapshot = peek(default_state_dir(), default_quarantine_dir())

    def adb_devices() -> Sequence[Transport]:
        return android.transports(config.android, call.server)

    caller = Caller(env, snapshot.leases, system, config.holder)
    decision = decide(
        call, caller, _devices(snapshot, config), config.guard.strict, listing or adb_devices
    )
    if not decision.used:
        return decision
    # The other leases of the holding, such as a port, are not leases of a device, and their
    # idle timeout can be shorter than the one of the device.
    holdings = {lease.holding for lease in decision.used}
    held = [lease for lease in snapshot.leases if lease.state in HELD and lease.holding in holdings]
    return Decision(used=tuple(held))


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
    # adb waits, for example with wait-for-device, and then runs the command after the wait. The
    # wait only reads, so the command decides.
    wait = [rest.pop(0)] if rest and rest[0].startswith("wait-for-") else []
    options = tuple(server)
    if not rest or rest[0] in _HOST_COMMANDS or rest[:2] == ["forward", "--list"]:
        return Call(HOST, " ".join(wait + rest[:1]))
    if rest[0] == "kill-server":
        return Call(ALL, " ".join(wait + rest[:1]), server=options)
    if rest == ["disconnect"] or rest[:2] in _FOR_EVERY_DEVICE:
        return Call(ALL, " ".join(wait + rest[:2]), server=options)
    if rest[0] == "disconnect":
        # It disconnects the device that it names, whatever -s says.
        return Call(DEVICE, " ".join(wait + rest[:1]), serial=rest[1], server=options)
    command = " ".join(wait + rest[:1])
    return Call(DEVICE, command, serial, transport_id, transport, options, rest[0] == "emu")


class Caller:
    """The process that runs adb, and the holdings that it acts for.

    It reads the process list only when a question needs it.
    """

    def __init__(
        self, env: Mapping[str, str], leases: Sequence[Lease], system: System, rules: HolderRules
    ) -> None:
        self.env = env
        self.leases = leases
        self.system = system
        self.rules = rules
        self.pid = os.getpid()

    def token_holding(self) -> str | None:
        """Return the holding of the lease that `banksman run` gives its command, if any."""
        lease_id = self.env.get(LEASE_VARIABLE)
        if not lease_id:
            return None
        return next((lease.holding for lease in self.leases if lease.lease_id == lease_id), None)

    def is_agent(self) -> bool:
        """Whether the caller has an agent process: one above it, or one that its environment
        names."""
        return self._agent is not None

    def holder(self) -> Holder:
        """Return the caller as acquire would record it: the worktree, the agent, and the issue."""
        return find_holder(self._table, self.rules, env=self.env, pid=self.pid)

    def descends_from(self, pid: int | None, started: str | None) -> bool:
        process = None if pid is None else self._table.get(pid)
        if process is None or process.started != started:
            return False
        # A command that has left the process tree of its agent still acts for that agent.
        return pid in self._above or pid == self._agent

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

    @cached_property
    def _agent(self) -> int | None:
        return find_agent(self._table, self.rules.agents, self.pid, self.env)[0]


def decide(
    call: Call, caller: Caller, devices: Snapshot, strict: str | None, listing: Listing
) -> Decision:
    """Return whether a call is refused or warned, and the leases of the caller that it uses.

    `devices` holds the leases that can name a device, and `strict` is the mode of the guard.
    """
    if call.scope == HOST:
        return RUNS
    if call.scope == ALL:
        # Such a command breaks the devices that it acts on, rather than using them, so it
        # touches nothing.
        running = [lease for lease in devices.leases if lease.state in HELD and lease.serial]
        if not running or not caller.is_agent():
            return RUNS
        others = [lease for lease in running if lease.holding not in caller.holdings]
        return Decision(_every_device_refused(call, caller, others)) if others else RUNS
    if not strict and not devices.leases and not devices.unreadable:
        return RUNS
    serial = target(call, caller.env, devices, listing)
    if serial is None:
        return RUNS
    leases, unreadable = _leases_of(serial, devices)
    if not leases and not unreadable:
        if strict and caller.is_agent():
            return Decision(_not_leased(call, caller, serial, refuse=strict == STRICT))
        return RUNS
    # The lease id that `banksman run` gives its command decides without the process list.
    token = caller.token_holding()
    if not unreadable and all(lease.state in HELD and lease.holding == token for lease in leases):
        return Decision(used=tuple(leases))
    # A call without an agent process is a person's: it may use any device, and it is no
    # use by the holder.
    if not caller.is_agent():
        return RUNS
    for lease in leases:
        if lease.state in HELD and lease.holding in caller.holdings:
            continue
        # The on_acquire hook of the acquire that resets the instance, or the on_void hook of
        # the reaper that takes it back, which can run inside a command of another agent.
        if caller.descends_from(lease.reaper_pid, lease.reaper_started):
            continue
        return Decision(
            _refused(call, caller, serial, lease, mine=lease.holding in caller.holdings)
        )
    if unreadable:
        text = (
            f"adb {call.command}: refused: the lease file of {serial} cannot be read, so the"
            f" device is out of use: {unreadable[0].error}"
        )
        return Decision(Verdict(REFUSED, UNREADABLE, text, call.command, caller, serial))
    used = [lease for lease in leases if lease.state in HELD and lease.holding in caller.holdings]
    return Decision(used=tuple(used))


def touch(used: Sequence[Lease], system: System) -> list[Lease]:
    """Touch the holdings of the leases that a call uses, when a touch is due for one of the
    leases, and return the leases that it touched.

    A device call of the holder shows that the holder is alive and still uses the device, so
    the idle timeout counts the time without a banksman call and without a device call. The
    guard takes the lock only for a touch that is due by the lease files that it read without
    the lock. It records no owner process, so a call from a script never moves a lease to
    another owner.
    """
    now = system.clock()
    if not any(now - lease.touched > touch_interval(lease) for lease in used):
        return []
    store = Store(
        default_state_dir(),
        system,
        quarantine_dir=default_quarantine_dir(),
        lock_wait=LOG_LOCK_WAIT,
    )
    return store.touch_seen(used)


def touch_interval(lease: Lease) -> float:
    """Return how long after its last touch a call of the holder touches a lease again.

    A quarter of a short idle timeout, as `banksman run` checks, so that a holder that keeps
    using the device also keeps a lease of its holding with an idle timeout of a minute or less.
    """
    if lease.idle_timeout is None:
        return TOUCH_EVERY
    return min(TOUCH_EVERY, lease.idle_timeout / 4)


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


def _not_leased(call: Call, caller: Caller, serial: str, *, refuse: bool) -> Verdict:
    if refuse:
        text = (
            f"adb {call.command}: refused: {serial} is not a device of your leases, and the adb"
            f" guard is strict. {_GUIDE}"
        )
        return Verdict(REFUSED, NOT_LEASED, text, call.command, caller, serial)
    text = (
        f"warning: adb {call.command}: {serial} is not a device of your leases, and a strict adb"
        f" guard would refuse this call. {_GUIDE}"
    )
    return Verdict(WARNED, NOT_LEASED, text, call.command, caller, serial)


def _refused(call: Call, caller: Caller, serial: str, lease: Lease, *, mine: bool) -> Verdict:
    device = serial if lease.resource == serial else f"{serial} ({lease.resource})"
    if mine:
        text = (
            f"adb {call.command}: refused: your lease of {device} is lost: it is {lease.state}."
            " Stop using the device, and acquire one again"
        )
        return Verdict(REFUSED, LOST, text, call.command, caller, serial, (lease,))
    text = (
        f"adb {call.command}: refused: another holder leases {device}, and the lease is"
        f" {lease.state}: {_holder(lease)}. Use a device of your own lease; banksman status"
        " shows who holds what. Run banksman agent-guide and follow it"
    )
    return Verdict(REFUSED, OTHER_HOLDER, text, call.command, caller, serial, (lease,))


def _every_device_refused(call: Call, caller: Caller, others: Sequence[Lease]) -> Verdict:
    shown = [f"{lease.serial} ({_holder(lease)})" for lease in others[:_SHOWN]]
    if len(others) > _SHOWN:
        shown.append(f"{len(others) - _SHOWN} more")
    text = (
        f"adb {call.command}: refused: it acts on every device, also on devices that other"
        f" holders lease and that run now: {', '.join(shown)}"
    )
    return Verdict(REFUSED, EVERY_DEVICE, text, call.command, caller, leases=tuple(others))


def record(verdict: Verdict) -> None:
    """Add the events of a refused or warned call to the log, under the lock of the lease store."""
    found = events(verdict, verdict.caller.holder(), verdict.caller.system.wall_clock())
    if not found:
        return
    history = History(default_log_path())
    with log_lock(default_state_dir(), LOG_LOCK_WAIT):
        for event in found:
            history.append(event)


def events(verdict: Verdict, caller: Holder, at: float) -> list[Event]:
    """Return the events of a refused or warned call, with the caller as the holder.

    A command for every device has one event for each device of another holder that it would
    break, so that the history of that device shows who tried. The events have the words of the
    command only, never the rest of the command line.
    """
    command = verdict.command if COMMAND.fullmatch(verdict.command) else None

    def event(resource: str, lease: Lease | None, serial: str | None) -> Event:
        return Event(
            at,
            GUARD,
            resource,
            kind=None if lease is None else lease.kind,
            lease_id=None if lease is None else lease.lease_id,
            state=None if lease is None else lease.state,
            owner=caller.owner,
            owner_pid=caller.owner_pid,
            agent=caller.agent,
            issue=caller.issue,
            session=caller.session,
            reason=verdict.reason,
            action=verdict.action,
            command=command,
            serial=serial,
        )

    if verdict.reason == EVERY_DEVICE:
        return [event(lease.resource, lease, lease.serial) for lease in verdict.leases]
    # A serial that is not a valid name can come only from the command line of the caller, and
    # adb finds no device by it.
    serial = verdict.serial if verdict.serial and RESOURCE_NAME.fullmatch(verdict.serial) else None
    if verdict.leases:
        return [event(verdict.leases[0].resource, verdict.leases[0], serial)]
    return [] if serial is None else [event(serial, None, serial)]


def _holder(lease: Lease) -> str:
    # Only a refusal needs the console, so the guard does not import it before every adb call.
    from banksman.console import holder_label

    return holder_label(lease)


def _reason(exc: Exception) -> object:
    return exc if isinstance(exc, (BanksmanError, OSError)) else repr(exc)


def _say(text: str) -> None:
    print(clean(f"banksman: {text}"), file=sys.stderr)
