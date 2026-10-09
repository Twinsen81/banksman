"""The Android presets: discovery of Android emulators and Android devices; and adb for the guard.

This is the only module that runs `adb`. Discovery only reads: it lists the devices, reads
system properties and the accounts of a device, asks a running emulator for its AVD name, and
reads the AVD files. Each preset returns the same document that a discover hook prints. The adb
guard, and the preset for remote devices, use adb through this module too.
"""

from __future__ import annotations

import os
import pwd
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from banksman.config import Android
from banksman.errors import BanksmanError

ADB_TIMEOUT_SECONDS = 15.0
DISCOVER_SCHEMA = 1
GOOGLE_ACCOUNT_TYPE = "com.google"
# The facts that the presets give.
FACTS = (
    "avd",
    "display_name",
    "manufacturer",
    "model",
    "codename",
    "api",
    "image",
    "abi",
    "form",
    "serial",
    "running",
)
_MAX_OUTPUT = 1024 * 1024
_MAX_INI_FILE = 64 * 1024
_EMULATOR_PREFIX = "emulator-"
# The serial of a device that a program forwards to the adb server from a port on this machine,
# such as a remote device of Android Device Streaming.
FORWARDED_PREFIX = "localhost:"
_PROPERTY = re.compile(r"^\[([^\]]+)\]: \[(.*)\]$")
_ACCOUNTS_HEADER = re.compile(r"^\s*Accounts: [0-9]+\s*$")
_ACCOUNT = re.compile(r"^\s*Account \{name=(?P<name>.*), type=(?P<type>[^,}]*)\}\s*$")
_API = re.compile(r"android-([0-9]+)(?![0-9])")

# Runs a program with a timeout and returns what it printed, or raises AndroidError.
Run = Callable[[Sequence[str], float], str]
Document = dict[str, Any]


class AndroidError(BanksmanError):
    """A program of the Android SDK did not give an answer."""


@dataclass(frozen=True)
class Transport:
    """A device or an emulator as `adb devices -l` lists it."""

    serial: str
    state: str
    # The address of a USB device, such as usb:1-1. An emulator, and a device that adb reaches
    # over TCP/IP, have none.
    devpath: str = ""
    product: str = ""
    model: str = ""
    device: str = ""
    transport_id: str = ""


def emulators(settings: Android, run: Run | None = None, *, accounts: bool = True) -> Document:
    """Find the AVDs, with the serial and the accounts of each one that runs."""
    run = run_program if run is None else run
    adb = adb_path(settings)
    notes: list[str] = []
    home = avd_home(settings)
    avds = _avds(home, notes)
    running: dict[str, str] = {}
    booting: set[str] = set()
    listed = True
    try:
        for serial, state in _devices(run, adb):
            if not serial.startswith(_EMULATOR_PREFIX) or state not in ("device", "offline"):
                continue
            # adb lists an emulator as offline until the adb daemon in it starts, for example
            # while it boots. Its console answers already, so its AVD and its serial are known
            # from the start.
            name = _avd_name(run, adb, serial, shell=state == "device")
            if name is None:
                notes.append(f"cannot read the AVD name of the running {serial}")
            else:
                running[name] = serial
                if state != "device":
                    booting.add(serial)
    except AndroidError as exc:
        listed = False
        notes.append(f"cannot list the running emulators, so none is shown as running: {exc}")
    instances = []
    for name, config, target in avds:
        instance: Document = {"name": name, "facts": _avd_facts(name, config, target)}
        serial = running.get(name)
        # An emulator that does not run must be started before use. Without the list of running
        # emulators, that is not known, so the fact is left out.
        if listed:
            instance["facts"]["running"] = serial is not None
        if serial is not None:
            instance["facts"]["serial"] = serial
            # The accounts of an emulator that boots cannot be read yet, so they are not known.
            if accounts and serial not in booting:
                instance.update(_accounts(run, adb, serial))
        instances.append(instance)
    return {"schema": DISCOVER_SCHEMA, "instances": instances, "notes": notes}


def avd_name(settings: Android, serial: str, run: Run | None = None) -> str | None:
    """Return the name of the AVD that runs as the emulator with this serial, or None."""
    if not serial.startswith(_EMULATOR_PREFIX):
        return None
    run = run_program if run is None else run
    return _avd_name(run, adb_path(settings), serial)


def devices(settings: Android, run: Run | None = None, *, accounts: bool = True) -> Document:
    """Find the attached physical devices, with their facts and their accounts."""
    run = run_program if run is None else run
    adb = adb_path(settings)
    notes: list[str] = []
    try:
        listed = _devices(run, adb)
    except AndroidError as exc:
        notes.append(f"cannot list the devices: {exc}")
        return {"schema": DISCOVER_SCHEMA, "instances": [], "notes": notes}
    instances = []
    for serial, state in listed:
        if serial.startswith(_EMULATOR_PREFIX):
            continue
        # Such a device has no USB address, and it gets another port on every connection, so
        # it would come back under another name each time.
        if serial.startswith(FORWARDED_PREFIX):
            notes.append(
                f"{serial} is a port forward, so it is not offered as a device; a kind with the"
                " android-remote preset offers it"
            )
            continue
        if state != "device":
            notes.append(f"{serial} is {state}, so it is not offered")
            continue
        instance: Document = {"name": serial, "facts": {"serial": serial, "running": True}}
        try:
            getprop = run(_adb(adb, serial, "shell", "getprop"), ADB_TIMEOUT_SECONDS)
        except AndroidError as exc:
            instance["note"] = f"cannot read the properties: {exc}"
        else:
            instance["facts"].update(_device_facts(getprop))
        if accounts:
            found = _accounts(run, adb, serial)
            if "note" in instance and "note" in found:
                found["note"] = f"{instance['note']}; {found['note']}"
            instance.update(found)
        instances.append(instance)
    return {"schema": DISCOVER_SCHEMA, "instances": instances, "notes": notes}


def describe(
    settings: Android, serial: str, *, form: str | None = "phone", run: Run | None = None
) -> Document:
    """Return the facts that the system properties of a connected device give.

    `form` is the form when the properties do not name one.
    """
    run = run_program if run is None else run
    getprop = run(_adb(adb_path(settings), serial, "shell", "getprop"), ADB_TIMEOUT_SECONDS)
    return _device_facts(getprop, form)


def signed_in(settings: Android, serial: str, run: Run | None = None) -> Document:
    """Return the accounts of a connected device as a discover document gives them: the list,
    or None and a note."""
    run = run_program if run is None else run
    return _accounts(run, adb_path(settings), serial)


def transports(
    settings: Android, server: Sequence[str] = (), run: Run | None = None
) -> list[Transport]:
    """List the devices and emulators that an adb server knows.

    `server` holds the options of adb that name the server, such as `-P 5038`.
    """
    run = run_program if run is None else run
    return parse_transports(
        run([adb_path(settings), *server, "devices", "-l"], ADB_TIMEOUT_SECONDS)
    )


def parse_transports(text: str) -> list[Transport]:
    """Return the devices in the output of `adb devices -l`."""
    found = []
    for line in text.splitlines():
        fields = line.split()
        # Skip the title line and the lines about starting the adb server.
        if len(fields) < 2 or line.startswith(("List of devices", "*")):
            continue
        serial, state, *rest = fields
        values = {}
        for field in rest:
            key, _, value = field.partition(":")
            if key in ("product", "model", "device", "transport_id"):
                values[key] = value
            elif key == "usb":
                values["devpath"] = field
        found.append(Transport(serial, state, **values))
    return found


def adb_path(settings: Android) -> str:
    return str(sdk(settings) / "platform-tools" / "adb")


def sdk(settings: Android) -> Path:
    # Not $ANDROID_HOME: the agents and scheduled jobs of one user can each have a different
    # environment, and every caller must find the same devices.
    if settings.sdk is not None:
        return settings.sdk
    if sys.platform == "darwin":
        return _home() / "Library" / "Android" / "sdk"
    return _home() / "Android" / "Sdk"


def avd_home(settings: Android) -> Path:
    # Not $ANDROID_AVD_HOME, for the same reason as the SDK.
    return _home() / ".android" / "avd" if settings.avd_home is None else settings.avd_home


def parse_accounts(text: str) -> tuple[list[str] | None, str | None]:
    """Return the Google accounts in the output of `dumpsys account`, and a note.

    The output is not a stable interface. When it has no list of accounts, the accounts are
    unknown, never an empty list, so the device is not offered for work that needs an account.
    """
    names: list[str] = []
    found_list = False
    in_list = False
    for line in text.splitlines():
        if _ACCOUNTS_HEADER.match(line):
            found_list = in_list = True
            continue
        match = _ACCOUNT.match(line) if in_list else None
        if match is None:
            in_list = False
            continue
        if match["type"].strip() == GOOGLE_ACCOUNT_TYPE and match["name"] not in names:
            names.append(match["name"])
    if not found_list:
        return None, "cannot read the accounts: dumpsys account printed no list of accounts"
    return names, None


def parse_properties(text: str) -> dict[str, str]:
    properties = {}
    for line in text.splitlines():
        match = _PROPERTY.match(line.strip())
        if match is not None:
            properties[match.group(1)] = match.group(2)
    return properties


def run_program(command: Sequence[str], timeout: float) -> str:
    # Not at the top: the adb guard imports this module for every adb call, and needs these
    # only for some.
    import subprocess
    import tempfile

    # The output goes to a file, not a pipe: the adb server that `adb devices` can start keeps
    # running, and a pipe that it inherits would keep this command waiting.
    with tempfile.TemporaryFile() as output:
        try:
            status = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            ).returncode
        except FileNotFoundError:
            raise AndroidError(f"{command[0]} does not exist; set sdk in [android]") from None
        except OSError as exc:
            raise AndroidError(f"cannot run {command[0]}: {exc.strerror}") from exc
        except subprocess.TimeoutExpired:
            raise AndroidError(f"{_shown(command)} did not end within {timeout:g} s") from None
        if status != 0:
            raise AndroidError(f"{_shown(command)} failed with exit status {status}")
        output.seek(0)
        return output.read(_MAX_OUTPUT).decode(errors="replace")


def _devices(run: Run, adb: str) -> list[tuple[str, str]]:
    listed = []
    for line in run([adb, "devices"], ADB_TIMEOUT_SECONDS).splitlines():
        fields = line.split()
        # Skip the title line and the lines about starting the adb server.
        if len(fields) >= 2 and not line.startswith(("List of devices", "*")):
            listed.append((fields[0], fields[1]))
    return listed


def _avd_name(run: Run, adb: str, serial: str, *, shell: bool = True) -> str | None:
    try:
        # The emulator console prints the name, then "OK".
        lines = run(_adb(adb, serial, "emu", "avd", "name"), ADB_TIMEOUT_SECONDS).splitlines()
        if lines and lines[0].strip() and lines[0].strip() != "OK":
            return lines[0].strip()
    except AndroidError:
        pass
    # The properties need the adb daemon in the emulator.
    if not shell:
        return None
    try:
        properties = parse_properties(
            run(_adb(adb, serial, "shell", "getprop"), ADB_TIMEOUT_SECONDS)
        )
    except AndroidError:
        return None
    return properties.get("ro.boot.qemu.avd_name") or properties.get("ro.kernel.qemu.avd_name")


def _accounts(run: Run, adb: str, serial: str) -> Document:
    try:
        output = run(_adb(adb, serial, "shell", "dumpsys", "account"), ADB_TIMEOUT_SECONDS)
    except AndroidError as exc:
        return {"accounts": None, "note": f"cannot read the accounts: {exc}"}
    accounts, note = parse_accounts(output)
    return {"accounts": accounts} if note is None else {"accounts": accounts, "note": note}


def _device_facts(getprop: str, form: str | None = "phone") -> Document:
    properties = parse_properties(getprop)
    facts: Document = {}
    # The manufacturer as the device gives it, for example "samsung" or "Google". A marketing
    # name such as "Galaxy S24+" is not a property on every device, so it is not a fact.
    manufacturer = properties.get("ro.product.manufacturer") or properties.get(
        "ro.product.vendor.manufacturer"
    )
    if manufacturer:
        facts["manufacturer"] = manufacturer
    if properties.get("ro.product.model"):
        facts["model"] = properties["ro.product.model"]
    if properties.get("ro.product.device"):
        facts["codename"] = properties["ro.product.device"]
    if properties.get("ro.build.version.sdk", "").isdigit():
        facts["api"] = int(properties["ro.build.version.sdk"])
    if properties.get("ro.product.cpu.abi"):
        facts["abi"] = properties["ro.product.cpu.abi"]
    characteristics = properties.get("ro.build.characteristics", "")
    for word, named in (("watch", "watch"), ("tablet", "tablet"), ("tv", "tv")):
        if word in characteristics.split(","):
            facts["form"] = named
            break
    else:
        if form is not None:
            facts["form"] = form
    return facts


def _avds(home: Path, notes: list[str]) -> list[tuple[str, dict[str, str], str | None]]:
    try:
        names = sorted(path.name for path in home.iterdir() if path.suffix == ".ini")
    except FileNotFoundError:
        notes.append(f"{home} does not exist, so there are no AVDs")
        return []
    except OSError as exc:
        notes.append(f"cannot read {home}: {exc.strerror}")
        return []
    avds = []
    for file_name in names:
        name = file_name[: -len(".ini")]
        try:
            entry = _ini(home / file_name)
            directory = _avd_directory(home, name, entry)
            config = _ini(directory / "config.ini")
        except (OSError, UnicodeDecodeError):
            notes.append(f"cannot read the files of the AVD {name}")
            continue
        avds.append((name, config, entry.get("target")))
    return avds


def _avd_directory(home: Path, name: str, entry: dict[str, str]) -> Path:
    if entry.get("path") and os.path.isabs(entry["path"]):
        path = Path(entry["path"])
        if path.is_dir():
            return path
    if entry.get("path.rel"):
        # Relative to the directory that holds the AVD home, usually ~/.android.
        path = home.parent / entry["path.rel"]
        if path.is_dir():
            return path
    return home / f"{name}.avd"


def _avd_facts(name: str, config: dict[str, str], target: str | None) -> Document:
    facts: Document = {"avd": name}
    # The name that the AVD manager shows, for example "Pixel 6 Pro API 33".
    if config.get("avd.ini.displayname"):
        facts["display_name"] = config["avd.ini.displayname"]
    if config.get("hw.device.manufacturer"):
        facts["manufacturer"] = config["hw.device.manufacturer"]
    for text in (config.get("image.sysdir.1"), target):
        match = _API.search(text or "")
        if match is not None:
            facts["api"] = int(match.group(1))
            break
    if config.get("tag.id"):
        facts["image"] = config["tag.id"]
    if config.get("abi.type"):
        facts["abi"] = config["abi.type"]
    form = _avd_form(config)
    if form is not None:
        facts["form"] = form
    return facts


def _avd_form(config: dict[str, str]) -> str | None:
    tags = f"{config.get('tag.id', '')},{config.get('tag.ids', '')}"
    if "wear" in tags:
        return "watch"
    if "tv" in tags:
        return "tv"
    try:
        width, height, density = (
            int(config[key]) for key in ("hw.lcd.width", "hw.lcd.height", "hw.lcd.density")
        )
    except (KeyError, ValueError):
        return None
    if density <= 0:
        return None
    # The smallest width in density-independent pixels: 600 and more is a tablet, as for the
    # layouts of Android itself.
    return "tablet" if min(width, height) * 160 / density >= 600 else "phone"


def _ini(path: Path) -> dict[str, str]:
    with open(path, "rb") as file:
        text = file.read(_MAX_INI_FILE).decode()
    values = {}
    for line in text.splitlines():
        key, equals, value = line.partition("=")
        if equals:
            values[key.strip()] = value.strip()
    return values


def _adb(adb: str, serial: str, *arguments: str) -> list[str]:
    return [adb, "-s", serial, *arguments]


def _shown(command: Sequence[str]) -> str:
    return " ".join([os.path.basename(command[0]), *command[1:]])


def _home() -> Path:
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError as exc:
        raise AndroidError(
            f"cannot find the home directory of user {os.geteuid()}; set sdk and avd_home in"
            " [android]"
        ) from exc

