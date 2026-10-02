"""Joint requests: one resource, and the accounts on it, for each part of a request.

A run that needs several resources at the same time gets them all or none. Two runs that each
hold a part of what the other needs would otherwise wait for each other. A resource and an
account go to one part only. Each part takes its options in the order of choice, and an
earlier part chooses before a later one.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from banksman.errors import BanksmanError

MAX_PARTS = 8
MAX_ACCOUNTS = 10
# The search runs under the lock of the lease store, and other commands wait at most 10 seconds
# for that lock. Requests of a usual size need milliseconds, but parts that match up to 1000
# instances each could need many seconds to prove that they cannot be met together.
MAX_SEARCH_SECONDS = 1.0


@dataclass(frozen=True)
class Option:
    resource: str
    # The accounts on the resource that the part may get, in the order of choice.
    accounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Part:
    # The resources that the part may get, in the order of choice.
    options: tuple[Option, ...]
    # How many accounts on its resource the part needs.
    accounts: int = 0


@dataclass(frozen=True)
class Pick:
    resource: str
    accounts: tuple[str, ...] = ()


class TooComplex(BanksmanError):
    """The search for an assignment took too long."""


def assign(parts: Sequence[Part]) -> list[Pick] | None:
    """Return one pick for each part, or None when the parts cannot all be met together."""
    return _Search(parts).run()


class _Search:
    def __init__(self, parts: Sequence[Part]) -> None:
        self.parts = parts
        self.deadline = time.monotonic() + MAX_SEARCH_SECONDS

    def run(self) -> list[Pick] | None:
        return self._pick(0, frozenset(), frozenset())

    def _pick(
        self, index: int, resources: frozenset[str], accounts: frozenset[str]
    ) -> list[Pick] | None:
        if index == len(self.parts):
            return []
        if time.monotonic() > self.deadline:
            raise TooComplex(
                "banksman cannot find resources for this request in time: ask for fewer parts,"
                " or make the clauses of the parts more specific"
            )
        # Without this check, parts that match the same resources could make the search try
        # every order of those resources before it finds that a later part cannot be met.
        if not self._possible(index, resources, accounts):
            return None
        part = self.parts[index]
        for option in part.options:
            if option.resource in resources:
                continue
            free = [account for account in option.accounts if account not in accounts]
            for chosen in combinations(free, part.accounts):
                rest = self._pick(index + 1, resources | {option.resource}, accounts | set(chosen))
                if rest is not None:
                    return [Pick(option.resource, chosen), *rest]
        return None

    def _possible(self, index: int, resources: frozenset[str], accounts: frozenset[str]) -> bool:
        # Two necessary conditions, each a matching: every part that is left gets its own
        # resource with enough free accounts on it, and every account that the parts need is a
        # different account.
        hosts: list[list[str]] = []
        seats: list[list[str]] = []
        for part in self.parts[index:]:
            usable = [
                option
                for option in part.options
                if option.resource not in resources
                and sum(account not in accounts for account in option.accounts) >= part.accounts
            ]
            hosts.append([option.resource for option in usable])
            reachable = list(
                dict.fromkeys(
                    account
                    for option in usable
                    for account in option.accounts
                    if account not in accounts
                )
            )
            seats.extend([reachable] * part.accounts)
        return _matched(hosts) and _matched(seats)


def _matched(demands: Sequence[Sequence[str]]) -> bool:
    """Whether each demand can get its own item from its list."""
    owner: dict[str, int] = {}

    def augment(demand: int, seen: set[str]) -> bool:
        for item in demands[demand]:
            if item in seen:
                continue
            seen.add(item)
            if item not in owner or augment(owner[item], seen):
                owner[item] = demand
                return True
        return False

    return all(augment(demand, set()) for demand in range(len(demands)))
