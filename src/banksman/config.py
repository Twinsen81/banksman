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
from banksman.lease import KIND_NAME, RESOURCE_NAME, Timeouts

CONFIG_ENV = "BANKSMAN_CONFIG"
MAX_COUNT = 1000

_TIMEOUT_KEYS = ("boot_timeout", "owner_grace", "idle_timeout", "hard_cap")
_KIND_KEYS = ("count", "instances", "on_void", *_TIMEOUT_KEYS)
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
    # The command that ends a void instance. The reaper ends an instance only if the operator
    # declares one.
    on_void: tuple[str, ...] | None = None

    def instances(self) -> tuple[str, ...]:
        """Return the instances that the configuration declares.

        A counted kind has `<name>-0`, `<name>-1`, and so on. A kind with neither `count` nor
        `instances` gets its instances from discovery.
        """
        if self.count is not None:
            return tuple(f"{self.name}-{index}" for index in range(self.count))
        return self.listed


@dataclass(frozen=True)
class Config:
    defaults: Timeouts = Timeouts()
    kinds: Mapping[str, Kind] = field(default_factory=dict)


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
    # that user's commands.
    if info.st_uid != os.geteuid():
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
    _check_keys(data, None, ("defaults", "kinds"))
    table = _table(data.get("defaults", {}), "defaults")
    _check_keys(table, "defaults", _TIMEOUT_KEYS)
    defaults = _timeouts(table, "defaults", Timeouts())
    table = _table(data.get("kinds", {}), "kinds")
    kinds = {name: _kind(name, value, defaults) for name, value in table.items()}
    _check_unique_instances(kinds)
    return Config(defaults=defaults, kinds=kinds)


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
    count = _count(table.get("count"), f"{where}.count")
    listed = _instances(table.get("instances"), f"{where}.instances")
    if count is not None and listed:
        raise _Invalid(f"{where}.instances", "a kind has either count or instances, not both")
    return Kind(
        name=name,
        timeouts=_timeouts(table, where, defaults),
        count=count,
        listed=listed,
        on_void=_command(table.get("on_void"), f"{where}.on_void"),
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
    # whose end is exact, such as a build. Any other timeout of 0 would void every lease at once.
    if seconds == 0 and key != "owner_grace":
        raise _Invalid(where, "must be longer than 0 seconds")
    return seconds


def _count(value: object, where: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_COUNT:
        raise _Invalid(where, f"must be a whole number from 1 to {MAX_COUNT}")
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
        or not value[0]
    ):
        raise _Invalid(
            where,
            'must be a list of strings: a program and its arguments, such as ["/path/to/program"]',
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
