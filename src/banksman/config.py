"""The machine configuration: the kinds of resources, and their timeouts and hooks.

The operator writes it. banksman only reads it.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from banksman.errors import BanksmanError
from banksman.lease import KIND_NAME, PROGRAM_NAME, RESOURCE_NAME, Timeouts

CONFIG_ENV = "BANKSMAN_CONFIG"
MAX_COUNT = 1000
MAX_RANK = 1000
ANDROID_EMULATOR = "android-emulator"
ANDROID_DEVICE = "android-device"
PRESETS = (ANDROID_EMULATOR, ANDROID_DEVICE)
# The agents whose commands banksman recognizes. Each is the last part of the path of the
# agent's executable, as the process list shows it.
DEFAULT_AGENTS = ("claude", "codex")
# An issue id such as abc-123 in a branch name or a directory name.
DEFAULT_ISSUE_PATTERN = r"\b[A-Za-z][A-Za-z0-9]*-[0-9]+\b"

_TIMEOUT_KEYS = ("boot_timeout", "owner_grace", "idle_timeout", "hard_cap", "drain_timeout")
_SOURCE_KEYS = ("count", "instances", "discover", "preset")
_KIND_KEYS = (
    *_SOURCE_KEYS,
    "preselect",
    "rank",
    "on_acquire",
    "on_void",
    "stop",
    *_TIMEOUT_KEYS,
)
_MAX_PATTERNS = 100
_MAX_PATTERN_LENGTH = 256
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DURATION = re.compile(r"([0-9]{1,7})([smh])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 60 * 60}
_OFF = "off"


class ConfigError(BanksmanError):
    """The configuration file cannot be used."""


@dataclass(frozen=True)
class Kind:
    name: str
    timeouts: Timeouts = Timeouts()
    # A counted kind has this many interchangeable instances.
    count: int | None = None
    # The instances that the operator lists by name with the `instances` key, for example
    # port numbers.
    listed: tuple[str, ...] = ()
    # When a request matches instances of several kinds, the kinds with a lower rank are chosen
    # first.
    rank: int = 0
    # The command that resets an instance before a run gets it.
    on_acquire: tuple[str, ...] | None = None
    # The command that ends a void instance. The reaper ends an instance only if the operator
    # declares one.
    on_void: tuple[str, ...] | None = None
    # Whether the reaper may signal the scripts of a void lease. Off unless the operator turns
    # it on, because a stopped process that its user did not expect costs more than a blocked
    # resource.
    stop: bool = False
    # The command that finds the instances of the kind and their facts.
    discover: tuple[str, ...] | None = None
    # A discovery that banksman ships, used instead of a discover command.
    preset: str | None = None
    # Name patterns of the instances that `admin discover` selects at first. They give no
    # access by themselves: only the inventory does.
    preselect: tuple[str, ...] = ()

    @property
    def discovered(self) -> bool:
        """Whether the instances come from discovery, and agents may use only allowed ones."""
        return self.discover is not None or self.preset is not None

    def instances(self) -> tuple[str, ...]:
        """Return the instances that the configuration declares.

        A counted kind has `<name>-0`, `<name>-1`, and so on. A kind with neither `count` nor
        `instances` gets its instances from discovery.
        """
        if self.count is not None:
            return tuple(f"{self.name}-{index}" for index in range(self.count))
        return self.listed


@dataclass(frozen=True)
class HolderRules:
    """How banksman finds the holder of a lease from the caller."""

    agents: tuple[str, ...] = DEFAULT_AGENTS
    issue_pattern: re.Pattern[str] = re.compile(DEFAULT_ISSUE_PATTERN)


@dataclass(frozen=True)
class Android:
    """Where the Android presets find the SDK and the emulators. None means the default place."""

    sdk: Path | None = None
    avd_home: Path | None = None


@dataclass(frozen=True)
class Guard:
    """How the adb guard decides."""

    # Also refuse an agent's device command on a device that no lease of its holding has, for
    # example a free device, or a personal phone that the inventory does not allow.
    strict: bool = False


@dataclass(frozen=True)
class Config:
    defaults: Timeouts = Timeouts()
    kinds: Mapping[str, Kind] = field(default_factory=dict)
    holder: HolderRules = HolderRules()
    # Patterns of the accounts that `admin discover` selects at first.
    account_preselect: tuple[str, ...] = ()
    android: Android = Android()
    guard: Guard = Guard()


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    # The home directory from the user database, not $HOME or $XDG_CONFIG_HOME: the agents,
    # sandboxes, and scheduled jobs of one user can each have a different environment, and
    # the reaper takes resources back with the hooks in this file.
    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise ConfigError(
            f"cannot find the home directory of user {os.geteuid()}; set {CONFIG_ENV}"
        ) from exc
    return Path(home) / ".config" / "banksman" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Read the configuration. Without a file, there are no kinds and the default timeouts."""
    path = default_config_path() if path is None else Path(path)
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        raise ConfigError(f"cannot open {path}: {exc.strerror}") from exc
    try:
        _check_file(path, os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as file:
        raw = file.read()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        return _config(data)
    except _Invalid as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _check_file(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{path} is not a regular file")
    # Hooks run as the user, so a file that another user can change could make banksman run
    # that user's commands. A file of root is accepted, as by ssh: root can change any file,
    # and a configuration that a system manager writes, such as Nix, belongs to root.
    if info.st_uid not in (os.geteuid(), 0):
        raise ConfigError(f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(f"group or others can write to {path}")


def parse_duration(text: str) -> int:
    """Return the seconds of a duration such as `90s`, `20m`, or `3h`."""
    match = _DURATION.fullmatch(text)
    if match is None:
        raise ValueError(f"{text!r} is not a duration such as 90s, 20m, or 3h")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


class _Invalid(Exception):
    def __init__(self, key: str, problem: str) -> None:
        super().__init__(f"{key}: {problem}")


def _config(data: dict[str, object]) -> Config:
    _check_keys(data, None, ("defaults", "kinds", "holder", "accounts", "android", "guard"))
    table = _table(data.get("defaults", {}), "defaults")
    _check_keys(table, "defaults", _TIMEOUT_KEYS)
    defaults = _timeouts(table, "defaults", Timeouts())
    table = _table(data.get("kinds", {}), "kinds")
    kinds = {name: _kind(name, value, defaults) for name, value in table.items()}
    _check_unique_instances(kinds)
    accounts = _table(data.get("accounts", {}), "accounts")
    _check_keys(accounts, "accounts", ("preselect",))
    return Config(
        defaults=defaults,
        kinds=kinds,
        holder=_holder(_table(data.get("holder", {}), "holder")),
        account_preselect=_patterns(accounts.get("preselect"), "accounts.preselect"),
        android=_android(_table(data.get("android", {}), "android")),
        guard=_guard(_table(data.get("guard", {}), "guard")),
    )


def _holder(table: dict[str, object]) -> HolderRules:
    _check_keys(table, "holder", ("agents", "issue_pattern"))
    agents = table.get("agents", list(DEFAULT_AGENTS))
    # An empty list is allowed: banksman then recognizes no agent, and only the touch-based
    # rules free a lease.
    if (
        not isinstance(agents, list)
        or len(agents) > _MAX_PATTERNS
        or not all(isinstance(name, str) and PROGRAM_NAME.fullmatch(name) for name in agents)
    ):
        raise _Invalid(
            "holder.agents",
            'must be a list of program names, such as ["claude", "codex"]: the last part of the'
            " path of each agent's executable",
        )
    pattern = table.get("issue_pattern", DEFAULT_ISSUE_PATTERN)
    if not isinstance(pattern, str) or not 0 < len(pattern) <= _MAX_PATTERN_LENGTH:
        raise _Invalid(
            "holder.issue_pattern",
            f"must be a regular expression of 1 to {_MAX_PATTERN_LENGTH} characters",
        )
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise _Invalid(
            "holder.issue_pattern", f"is not a valid regular expression: {exc}"
        ) from None
    return HolderRules(agents=tuple(agents), issue_pattern=compiled)


def _android(table: dict[str, object]) -> Android:
    _check_keys(table, "android", ("sdk", "avd_home"))
    return Android(
        sdk=_directory(table.get("sdk"), "android.sdk"),
        avd_home=_directory(table.get("avd_home"), "android.avd_home"),
    )


def _guard(table: dict[str, object]) -> Guard:
    _check_keys(table, "guard", ("strict",))
    return Guard(strict=_boolean(table.get("strict", False), "guard.strict"))


def _directory(value: object, where: str) -> Path | None:
    if value is None:
        return None
    # The same reason as for a hook program: every caller must find the same directory.
    if not isinstance(value, str) or "\x00" in value or not os.path.isabs(value):
        raise _Invalid(where, "must be an absolute path")
    return Path(value)


def _patterns(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= _MAX_PATTERNS
        or not all(
            isinstance(pattern, str)
            and 0 < len(pattern) <= _MAX_PATTERN_LENGTH
            and _CONTROL.search(pattern) is None
            for pattern in value
        )
    ):
        raise _Invalid(
            where,
            f'must be a list of 1 to {_MAX_PATTERNS} name patterns, such as ["qa_*"], where'
            " * matches any text and ? matches one character",
        )
    return tuple(value)


def _kind(name: str, value: object, defaults: Timeouts) -> Kind:
    where = f"kinds.{name}"
    if KIND_NAME.fullmatch(name) is None:
        raise _Invalid(
            where,
            "a kind name starts with a lowercase letter, has only lowercase letters, digits,"
            " '_', and '-', and has at most 32 characters",
        )
    table = _table(value, where)
    _check_keys(table, where, _KIND_KEYS)
    sources = [key for key in _SOURCE_KEYS if key in table]
    if len(sources) > 1:
        raise _Invalid(
            f"{where}.{sources[1]}",
            "a kind gets its instances from only one of count, instances, discover, and preset",
        )
    preset = table.get("preset")
    if preset is not None and preset not in PRESETS:
        raise _Invalid(f"{where}.preset", f"must be one of {', '.join(PRESETS)}")
    discover = _command(table.get("discover"), f"{where}.discover")
    preselect = _patterns(table.get("preselect"), f"{where}.preselect")
    if preselect and discover is None and preset is None:
        raise _Invalid(
            f"{where}.preselect",
            "applies only to a kind whose instances come from discover or a preset",
        )
    # Physical devices are scarcer than emulators, so a request that does not name a kind gets
    # an instance of another kind first.
    rank = table.get("rank", 1 if preset == ANDROID_DEVICE else 0)
    return Kind(
        name=name,
        timeouts=_timeouts(table, where, defaults),
        count=_count(table.get("count"), f"{where}.count"),
        listed=_instances(table.get("instances"), f"{where}.instances"),
        rank=_rank(rank, f"{where}.rank"),
        on_acquire=_command(table.get("on_acquire"), f"{where}.on_acquire"),
        on_void=_command(table.get("on_void"), f"{where}.on_void"),
        stop=_boolean(table.get("stop", False), f"{where}.stop"),
        discover=discover,
        preset=preset,  # type: ignore[arg-type]
        preselect=preselect,
    )


def _check_unique_instances(kinds: Mapping[str, Kind]) -> None:
    # Each resource has one lease file, so two kinds must not declare the same instance.
    owners: dict[str, str] = {}
    for kind in kinds.values():
        for instance in kind.instances():
            other = owners.setdefault(instance, kind.name)
            if other != kind.name:
                key = "instances" if kind.listed else "count"
                raise _Invalid(
                    f"kinds.{kind.name}.{key}", f"{instance!r} is also an instance of kind {other}"
                )


def _timeouts(table: dict[str, object], where: str, base: Timeouts) -> Timeouts:
    values = {
        key: _duration(table[key], key, f"{where}.{key}") for key in _TIMEOUT_KEYS if key in table
    }
    return replace(base, **values)


def _duration(value: object, key: str, where: str) -> int | None:
    # Only the idle timeout can be off: every lease must end at its hard cap, and a booting
    # lease at its boot deadline.
    can_be_off = key == "idle_timeout"
    if can_be_off and value == _OFF:
        return None
    expected = 'a duration such as "20m"' + (', or "off"' if can_be_off else "")
    if not isinstance(value, str):
        raise _Invalid(where, f"must be {expected}")
    try:
        seconds = parse_duration(value)
    except ValueError:
        raise _Invalid(where, f"must be {expected}") from None
    # An owner grace of 0 frees the resource as soon as its owner ends, which suits a holder
    # whose end is exact, such as a build. Any other timeout of 0 would void every lease at once,
    # or quarantine a void lease before its scripts can see that it is lost.
    if seconds == 0 and key != "owner_grace":
        raise _Invalid(where, "must be longer than 0 seconds")
    return seconds


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise _Invalid(where, "must be true or false")
    return value


def _count(value: object, where: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_COUNT:
        raise _Invalid(where, f"must be a whole number from 1 to {MAX_COUNT}")
    return value


def _rank(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_RANK:
        raise _Invalid(where, f"must be a whole number from 0 to {MAX_RANK}")
    return value


def _instances(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_COUNT
        or not all(isinstance(name, str) and RESOURCE_NAME.fullmatch(name) for name in value)
    ):
        raise _Invalid(
            where,
            f'must be a list of 1 to {MAX_COUNT} resource names, such as ["9101", "9102"]. A'
            " resource name starts with a letter or a digit, has only letters, digits, '.',"
            " '_', ':', '@', '+', and '-', and has at most 128 characters",
        )
    seen: set[str] = set()
    for name in value:
        if name in seen:
            raise _Invalid(where, f"lists {name!r} twice")
        seen.add(name)
    return tuple(value)


def _command(value: object, where: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(part, str) and "\x00" not in part for part in value)
    ):
        raise _Invalid(
            where,
            'must be a list of strings: a program and its arguments, such as ["/path/to/program"]',
        )
    # A hook runs in the environment of whichever command reaps, so a program that the PATH of
    # that command finds, or a path that a shell would expand, can work for one caller and fail
    # for another.
    if not os.path.isabs(value[0]):
        raise _Invalid(
            where, f"the program must be an absolute path, such as /usr/bin/env, not {value[0]!r}"
        )
    return tuple(value)


def _table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _Invalid(where, "must be a table")
    return value


def _check_keys(table: dict[str, object], where: str | None, known: tuple[str, ...]) -> None:
    for key in table:
        if key not in known:
            name = key if where is None else f"{where}.{key}"
            raise _Invalid(name, f"unknown key; the keys here are {', '.join(known)}")
