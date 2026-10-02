"""The inventory: which discovered instances and accounts agents may use on this machine.

`banksman admin discover` writes it, and the operator can edit it. It is policy, not a cache:
it holds only names, and what is on the machine is read again whenever it is needed. A name
that a person refused stays refused when discover runs again, until a person allows it.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import pwd
import stat
import tempfile
import tomllib
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from banksman.config import CONFIG_ENV
from banksman.errors import BanksmanError
from banksman.lease import KIND_NAME, RESOURCE_NAME

INVENTORY_ENV = "BANKSMAN_INVENTORY"
_FILE_NAME = "inventory.toml"
_HEADER = """\
# The discovered resources that agents may use on this machine. "banksman admin discover"
# writes this file, and you can edit it. Agents may use an instance or an account only when it
# is under "allowed". A name under "refused" stays refused when discover runs again, until you
# allow it.
"""


class InventoryError(BanksmanError):
    """The inventory file cannot be used."""


@dataclass(frozen=True)
class Decisions:
    allowed: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()


@dataclass(frozen=True)
class Inventory:
    kinds: Mapping[str, Decisions] = field(default_factory=dict)
    accounts: Decisions = Decisions()

    def allows(self, kind: str, name: str) -> bool:
        return name in self.kinds.get(kind, Decisions()).allowed

    def allows_account(self, name: str) -> bool:
        return name in self.accounts.allowed


def default_inventory_path() -> Path:
    override = os.environ.get(INVENTORY_ENV)
    if override:
        return Path(override)
    # Next to a configuration file for tests, so that a test never reads the real inventory.
    config = os.environ.get(CONFIG_ENV)
    if config:
        return Path(config).with_name(_FILE_NAME)
    # The home directory from the user database, as for the configuration file: every caller
    # must apply the same policy.
    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise InventoryError(
            f"cannot find the home directory of user {os.geteuid()}; set {INVENTORY_ENV}"
        ) from exc
    return Path(home) / ".config" / "banksman" / _FILE_NAME


def load_inventory(path: Path | None = None) -> Inventory:
    """Read the inventory. Without a file, agents may use no discovered instance."""
    path = default_inventory_path() if path is None else Path(path)
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return Inventory()
    except OSError as exc:
        raise InventoryError(f"cannot open {path}: {exc.strerror}") from exc
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
        raise InventoryError(f"{path}: {exc}") from exc
    try:
        return _inventory(data)
    except _Invalid as exc:
        raise InventoryError(f"{path}: {exc}") from None


def save_inventory(inventory: Inventory, path: Path | None = None) -> Path:
    path = default_inventory_path() if path is None else Path(path)
    try:
        _check_unique(inventory)
    except _Invalid as exc:
        raise InventoryError(f"not written to {path}: {exc}") from None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".inventory-", dir=path.parent)
    except OSError as exc:
        raise InventoryError(f"cannot write {path}: {exc.strerror}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(to_toml(inventory))
        # rename is atomic: a command that reads the inventory sees the old file or the new one.
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
    return path


def to_toml(inventory: Inventory) -> str:
    sections = [_HEADER]
    for kind in sorted(inventory.kinds):
        sections.append(_section(f"kinds.{kind}", inventory.kinds[kind]))
    sections.append(_section("accounts", inventory.accounts))
    return "\n".join(sections)


def preselected(decisions: Decisions, name: str, patterns: Sequence[str], select_all: bool) -> bool:
    """Whether discover selects a name at first.

    A refusal wins over a pattern and over --all: only a person who selects the name again
    allows it. So a new pattern or --all can never silently widen access to a refused name.
    """
    if name in decisions.refused:
        return False
    if name in decisions.allowed:
        return True
    return select_all or matches(patterns, name)


def matches(patterns: Sequence[str], name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def decide(
    decisions: Decisions,
    seen: Iterable[str],
    selected: Collection[str],
    *,
    refuse_unselected: bool,
) -> Decisions:
    """Return the decisions after discover: the selected names are allowed.

    A person who sees a name and leaves it unselected refuses it (`refuse_unselected`). Without
    a person, an unselected name gets no decision, so that a pattern can still select it later.
    A name that discover did not see keeps its decision, so that a device that is unplugged
    during discover keeps its place.
    """
    seen = set(seen)
    allowed = {name for name in decisions.allowed if name not in seen or name in selected}
    allowed |= seen & set(selected)
    refused = {name for name in decisions.refused if name not in allowed}
    if refuse_unselected:
        refused |= seen - allowed
    return Decisions(allowed=tuple(sorted(allowed)), refused=tuple(sorted(refused)))


def _section(title: str, decisions: Decisions) -> str:
    # Names are resource names, which JSON writes as valid TOML strings.
    return (
        f"[{title}]\n"
        f"allowed = {_names(decisions.allowed)}\n"
        f"refused = {_names(decisions.refused)}\n"
    )


def _names(names: Sequence[str]) -> str:
    if not names:
        return "[]"
    return "[\n" + "".join(f"    {json.dumps(name)},\n" for name in names) + "]"


class _Invalid(Exception):
    def __init__(self, key: str, problem: str) -> None:
        super().__init__(f"{key}: {problem}")


def _inventory(data: dict[str, object]) -> Inventory:
    _check_keys(data, None, ("kinds", "accounts"))
    kinds = data.get("kinds", {})
    if not isinstance(kinds, dict):
        raise _Invalid("kinds", "must be a table")
    for name in kinds:
        if KIND_NAME.fullmatch(name) is None:
            raise _Invalid(f"kinds.{name}", "is not a valid kind name")
    inventory = Inventory(
        kinds={name: _decisions(value, f"kinds.{name}") for name, value in kinds.items()},
        accounts=_decisions(data.get("accounts", {}), "accounts"),
    )
    _check_unique(inventory)
    return inventory


def _check_unique(inventory: Inventory) -> None:
    # Each resource has one lease file, so a name belongs to one kind. A refusal counts too:
    # a device that a person refused as one kind must not come back as another.
    owners: dict[str, str] = {}
    for kind in sorted(inventory.kinds):
        decisions = inventory.kinds[kind]
        for name in (*decisions.allowed, *decisions.refused):
            other = owners.setdefault(name, kind)
            if other != kind:
                raise _Invalid(f"kinds.{kind}", f"{name!r} is also listed under kinds.{other}")


def _decisions(value: object, where: str) -> Decisions:
    if not isinstance(value, dict):
        raise _Invalid(where, "must be a table")
    _check_keys(value, where, ("allowed", "refused"))
    allowed = _name_list(value.get("allowed", []), f"{where}.allowed")
    refused = _name_list(value.get("refused", []), f"{where}.refused")
    both = sorted(set(allowed) & set(refused))
    if both:
        raise _Invalid(where, f"{both[0]!r} is both allowed and refused")
    return Decisions(allowed=allowed, refused=refused)


def _name_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(name, str) and RESOURCE_NAME.fullmatch(name) for name in value
    ):
        raise _Invalid(where, "must be a list of resource names")
    return tuple(dict.fromkeys(value))


def _check_keys(table: Mapping[str, object], where: str | None, known: tuple[str, ...]) -> None:
    for key in table:
        if key not in known:
            name = key if where is None else f"{where}.{key}"
            raise _Invalid(name, f"unknown key; the keys here are {', '.join(known)}")


def _check_file(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise InventoryError(f"{path} is not a regular file")
    # The inventory decides what agents may use, so a file that another user can change could
    # give an agent that user's choice of devices and accounts.
    if info.st_uid not in (os.geteuid(), 0):
        raise InventoryError(f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InventoryError(f"group or others can write to {path}")

