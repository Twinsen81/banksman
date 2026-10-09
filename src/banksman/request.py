"""Requests by properties: which resources match the `--where` clauses, in the order of choice.

A resource has facts from discovery, which an agent cannot set, and tags from the operator. A
request has one or more parts, and each part is an AND of clauses. Only what the configuration
declares or the inventory allows, and what is present now, can match: a request never widens
access.
"""

from __future__ import annotations

import fnmatch
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from banksman import android, remote
from banksman.config import Config, Kind
from banksman.discovery import (
    ACCOUNT,
    FACT_NAME,
    KIND,
    PAID,
    RUNNING,
    TAG,
    FactValue,
    Found,
    serials_seen,
)
from banksman.discovery import discover as discover_now
from banksman.errors import BanksmanError
from banksman.inventory import Inventory
from banksman.lease import SerialSeen

# Attributes that are known also when no instance has them now, so that a request for one waits
# for a match instead of failing.
KNOWN = frozenset({KIND, ACCOUNT, TAG, PAID, *android.FACTS, *remote.FACTS})
# The name of a part of a request. It becomes the prefix of the KEY=value lines of the grant.
PART_NAME = re.compile(r"[a-z][a-z0-9_]{0,15}")
_NUMBER = re.compile(r"-?[0-9]{1,10}")
_CLAUSE = re.compile(r"([^<>!=~]*)(>=|<=|!=|=|~)(.*)", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_VALUE_LENGTH = 256

Discover = Callable[[Kind, Config], Found]


@dataclass(frozen=True)
class Clause:
    attribute: str
    operator: str
    value: str

    def __str__(self) -> str:
        return f"{self.attribute}{self.operator}{self.value}"


@dataclass(frozen=True)
class Part:
    """One resource that a request needs: its clauses, and how many accounts on it."""

    clauses: tuple[Clause, ...] = ()
    # Named when the request has several parts.
    name: str | None = None
    accounts: int = 0


@dataclass(frozen=True)
class Candidate:
    """A permitted resource that is present now and matches a part of a request."""

    resource: str
    kind: str
    rank: int
    facts: Mapping[str, FactValue] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    # The allowed accounts that are signed in on the instance now, or None when its accounts
    # are not known.
    accounts: tuple[str, ...] | None = None
    # Each use costs money, so a request gets it only with --paid.
    paid: bool = False
    # It runs, but banksman did not start it (see Instance).
    unmanaged: bool = False

    @property
    def cold(self) -> bool:
        """Whether the instance does not run now, so that its holder must start it."""
        return self.facts.get(RUNNING) is False


@dataclass(frozen=True)
class Search:
    # For each part, the matching resources, free or held, in the order in which banksman
    # chooses them.
    candidates: tuple[tuple[Candidate, ...], ...]
    # What agents may see of the notes of discovery.
    notes: tuple[str, ...] = ()
    # The allowed instances that discovery does not find now, as (kind, name).
    absent: tuple[tuple[str, str], ...] = ()
    # What the discovery found about the serials of the instances, for the leases.
    serials: tuple[SerialSeen, ...] = ()


def parse_clause(text: str) -> Clause:
    """Return the clause of a `--where` argument such as `form=tablet` or `api>=33`."""
    match = _CLAUSE.fullmatch(text)
    if match is None or FACT_NAME.fullmatch(match.group(1)) is None:
        raise ValueError(
            "a clause is an attribute, an operator (=, !=, ~, >=, or <=), and a value, such as"
            " form=tablet or api>=33. Quote it in a shell, because > and < redirect"
        )
    attribute, operator, value = match.groups()
    if not 0 < len(value) <= _MAX_VALUE_LENGTH or _CONTROL.search(value):
        raise ValueError(
            f"the value of {attribute} must have 1 to {_MAX_VALUE_LENGTH} characters and no"
            " control characters"
        )
    if operator in (">=", "<=") and (attribute == TAG or _NUMBER.fullmatch(value) is None):
        raise ValueError(f"{operator} compares whole numbers only, such as api{operator}33")
    return Clause(attribute, operator, value)


def check_kinds(clauses: Sequence[Clause], config: Config) -> None:
    """Refuse a request for a kind that the configuration does not declare."""
    for clause in clauses:
        if clause.attribute == KIND and clause.operator == "=" and not _kind(config, clause.value):
            declared = ", ".join(sorted(config.kinds)) or "none"
            raise BanksmanError(
                f"the configuration declares no kind {clause.value}; the kinds are: {declared}"
            )


def search(
    config: Config,
    inventory: Inventory,
    parts: Sequence[Part],
    *,
    discover: Discover = discover_now,
    clock: Callable[[], float] = time.monotonic,
) -> Search:
    """Find, for each part, the permitted resources that are present now and match every clause
    of the part.

    Discovery runs once for each kind that the `kind` clauses of a part allow. An attribute that
    is neither known nor a fact that discovery found now for the kinds of its part is an error,
    never an empty match that waits forever. A part that needs accounts matches only resources
    with that many allowed accounts signed in now.
    """
    check_kinds([clause for part in parts for clause in part.clauses], config)
    kinds_of = [_kinds(config, part) for part in parts]
    wanted = {kind.name for kinds in kinds_of for kind in kinds}
    declared = {name: kind.name for kind in config.kinds.values() for name in kind.instances()}
    found_facts: dict[str, set[str]] = {}
    notes: list[str] = []
    absent: list[tuple[str, str]] = []
    resources: list[Candidate] = []
    serials: list[SerialSeen] = []
    for kind in config.kinds.values():
        if kind.name not in wanted:
            continue
        found_facts[kind.name] = set()
        if not kind.discovered:
            # The configuration that declares the instances is the permission.
            resources.extend(
                Candidate(
                    name,
                    kind.name,
                    kind.rank,
                    {KIND: kind.name, PAID: kind.paid},
                    inventory.tags_of(name),
                    paid=kind.paid,
                )
                for name in kind.instances()
            )
            continue
        # The awake time of the leases, so that a lease can tell which discovery is newer.
        started = clock()
        found = discover(kind, config)
        serials.extend(serials_seen(found, started))
        notes.extend(_shown_notes(found))
        for instance in found.instances:
            found_facts[kind.name].update(instance.facts)
            # A name has one lease file, so a name that another kind declares is not this kind's.
            if declared.get(instance.name, kind.name) != kind.name:
                continue
            if not inventory.allows(kind.name, instance.name):
                continue
            facts = dict(instance.facts)
            accounts = None
            # Whether an allowed account is signed in now. When the accounts are not known, the
            # fact is left out, so that no clause on it matches.
            if instance.accounts is not None:
                accounts = tuple(
                    sorted(name for name in instance.accounts if inventory.allows_account(name))
                )
                facts[ACCOUNT] = bool(accounts)
            tags = inventory.tags_of(instance.name)
            resources.append(
                Candidate(
                    instance.name,
                    kind.name,
                    kind.rank,
                    facts,
                    tags,
                    accounts,
                    kind.paid,
                    instance.unmanaged,
                )
            )
        present = {instance.name for instance in found.instances}
        allowed = inventory.kinds[kind.name].allowed if kind.name in inventory.kinds else ()
        absent.extend((kind.name, name) for name in allowed if name not in present)
    candidates = []
    for part, kinds in zip(parts, kinds_of):
        names = {kind.name for kind in kinds}
        _check_attributes(part.clauses, set().union(*(found_facts[name] for name in names)))
        matching = [
            candidate
            for candidate in resources
            if candidate.kind in names
            and all(matches(clause, candidate.facts, candidate.tags) for clause in part.clauses)
            and (
                part.accounts == 0
                or (candidate.accounts is not None and len(candidate.accounts) >= part.accounts)
            )
        ]
        # A paid resource last, whatever the ranks are: a request gets one only when the user
        # agreed to the cost, and only when nothing free can serve it. Then lower ranks first, so
        # a request that does not name a kind gets, for example, an emulator before a physical
        # device. Then an instance that runs before one that must be started, which saves the
        # time and the memory of a start, and one that banksman started before one that it did
        # not, which a person or another program can still use.
        matching.sort(
            key=lambda candidate: (
                candidate.paid,
                candidate.rank,
                candidate.cold,
                candidate.unmanaged,
                candidate.resource,
            )
        )
        candidates.append(tuple(matching))
    return Search(tuple(candidates), tuple(notes), tuple(absent), tuple(serials))


def _shown_notes(found: Found) -> list[str]:
    """Return what agents may see of the notes of a discovery.

    A note of a hook or a preset can name an instance that the inventory does not allow, such as
    a personal phone that is not authorized. So agents see only why the discovery failed, and how
    many other notes it has. The operator sees them with banksman admin discover.
    """
    shown = [] if found.failure is None else [f"kind {found.kind}: {found.failure}"]
    shown += found.shown
    hidden = len(found.notes) - len(shown)
    if hidden:
        notes = "1 other note" if hidden == 1 else f"{hidden} other notes"
        them = "it" if hidden == 1 else "them"
        shown.append(
            f"kind {found.kind}: discovery has {notes}; banksman admin discover shows {them}"
        )
    return shown


def _kinds(config: Config, part: Part) -> list[Kind]:
    kind_clauses = [clause for clause in part.clauses if clause.attribute == KIND]
    return [
        kind
        for kind in config.kinds.values()
        if all(matches(clause, {KIND: kind.name}) for clause in kind_clauses)
    ]


def matches(
    clause: Clause, facts: Mapping[str, FactValue], tags: Sequence[str] = ()
) -> bool:
    """Whether a resource with these facts and tags satisfies the clause.

    A fact that the resource does not have matches no clause, with any operator, so that an
    unknown value never passes for a known one.
    """
    if clause.attribute == TAG:
        found = any(_same(tag, clause.operator, clause.value) for tag in tags)
        return not found if clause.operator == "!=" else found
    fact = facts.get(clause.attribute)
    if fact is None:
        return False
    if clause.operator in (">=", "<="):
        # A whole number only: true and false are not numbers.
        if isinstance(fact, bool) or not isinstance(fact, int):
            return False
        number = int(clause.value)
        return fact >= number if clause.operator == ">=" else fact <= number
    if clause.operator == "!=":
        return not _same(fact, "=", clause.value)
    return _same(fact, clause.operator, clause.value)


def _same(fact: FactValue, operator: str, value: str) -> bool:
    # Text is compared without regard to case, because devices give, for example, both
    # "samsung" and "Google" as the manufacturer.
    if isinstance(fact, bool):
        text = "true" if fact else "false"
    elif isinstance(fact, int):
        if operator != "~":
            return _NUMBER.fullmatch(value) is not None and int(value) == fact
        text = str(fact)
    else:
        text = fact
    if operator == "~":
        return fnmatch.fnmatchcase(text.casefold(), value.casefold())
    return text.casefold() == value.casefold()


def _kind(config: Config, name: str) -> bool:
    return any(kind.casefold() == name.casefold() for kind in config.kinds)


def _check_attributes(clauses: Sequence[Clause], found: set[str]) -> None:
    for clause in clauses:
        if clause.attribute not in KNOWN and clause.attribute not in found:
            names = ", ".join(sorted(KNOWN | found))
            raise BanksmanError(
                f"unknown attribute {clause.attribute}; the attributes are: {names}"
            )
