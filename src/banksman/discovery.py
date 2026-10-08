"""Discovery: the instances of a kind and their facts, found by its discover hook or a preset.

A hook prints one JSON document, and a preset returns the same document:

    {"schema": 1,
     "instances": [{"name": "R5CR1234ABC",
                    "facts": {"form": "phone", "api": 35},
                    "accounts": ["qa@example.test"],
                    "note": "..."}],
     "notes": ["..."]}

The values come from devices and from programs that banksman does not control, so every one
is checked, and a value that is not valid is left out, never guessed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from banksman import android, hooks
from banksman.config import ANDROID_DEVICE, ANDROID_EMULATOR, Config, Kind
from banksman.errors import BanksmanError
from banksman.lease import RESOURCE_NAME, SerialSeen
from banksman.sanitize import clean

DISCOVER_SCHEMA = android.DISCOVER_SCHEMA
FACT_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_FACT_LENGTH = 128
MAX_NOTE_LENGTH = 300
MAX_INSTANCES = 1000
_MAX_NOTES = 50
_MAX_FACTS = 50
_MAX_ACCOUNTS = 100
_MAX_FACT_NUMBER = 10**9
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Attributes that banksman sets itself: the kind of an instance, whether an allowed account is
# signed in on it, and its tags from the inventory. A hook cannot set them, so it cannot make an
# instance look like one of another kind, or like one with an allowed account.
KIND = "kind"
ACCOUNT = "account"
TAG = "tag"
SERIAL = "serial"
RUNNING = "running"

FactValue = str | int | bool


@dataclass(frozen=True)
class Instance:
    name: str
    facts: Mapping[str, FactValue] = field(default_factory=dict)
    # The accounts on the instance, or None when they are not known. An instance with unknown
    # accounts is not offered for work that needs an account.
    accounts: tuple[str, ...] | None = None
    note: str | None = None


@dataclass(frozen=True)
class Found:
    kind: str
    instances: tuple[Instance, ...] = ()
    notes: tuple[str, ...] = ()
    # Why the discovery of the kind failed as a whole, also in `notes`. banksman writes it, and it
    # names no instance. The other notes can name an instance that agents may not use, such as a
    # personal phone that is not authorized.
    failure: str | None = None


def discover(kind: Kind, config: Config, *, accounts: bool = True) -> Found:
    """Find the instances of a kind now. A failure becomes a note, never an exception.

    Without `accounts`, the presets do not read the accounts, which takes the most time, and
    the accounts are not known.
    """
    try:
        if kind.preset == ANDROID_EMULATOR:
            return parse(kind.name, android.emulators(config.android, accounts=accounts))
        if kind.preset == ANDROID_DEVICE:
            return parse(kind.name, android.devices(config.android, accounts=accounts))
    except BanksmanError as exc:
        return _failed(kind.name, note_text(str(exc)))
    failure, output = hooks.discover(kind)
    if failure is not None:
        return _failed(kind.name, note_text(failure))
    try:
        document = json.loads(output)
    except (ValueError, UnicodeDecodeError):
        return _failed(kind.name, "the discover hook printed text that is not JSON")
    return parse(kind.name, document)


def serial_of(facts: Mapping[str, FactValue]) -> str | None:
    """Return the serial in the facts of an instance when it is a valid resource name.

    A serial comes from a device or a hook. banksman prints it, puts it into the environment of
    commands, and replaces arguments with it, so only the characters of a resource name pass.
    """
    serial = facts.get(SERIAL)
    valid = isinstance(serial, str) and RESOURCE_NAME.fullmatch(serial) is not None
    return serial if valid else None  # type: ignore[return-value]


def serials_seen(found: Found, at: float) -> tuple[SerialSeen, ...]:
    """Return what a discovery that started at `at` says about the serials of its instances.

    An instance with a valid serial has that serial, and one without a serial that does not run
    has none. Every other case says nothing, so a lease keeps its serial: a discovery that
    failed, an instance that it does not find, and an instance whose `running` fact is missing.
    """
    seen = []
    for instance in found.instances:
        serial = serial_of(instance.facts)
        if serial is not None:
            seen.append(SerialSeen(instance.name, found.kind, serial, at))
        elif instance.facts.get(RUNNING) is False:
            seen.append(SerialSeen(instance.name, found.kind, None, at))
    return tuple(seen)


def serial_now(
    kind: Kind, config: Config, resource: str, known: str | None, clock: Callable[[], float]
) -> tuple[SerialSeen, ...]:
    """Find the serial of one instance now, without its accounts.

    For an emulator whose serial is known, one question to that emulator confirms it. Otherwise
    the discovery of the kind runs, and the result is about all its instances.
    """
    at = clock()
    other = None
    if kind.preset == ANDROID_EMULATOR and known is not None:
        other = android.avd_name(config.android, known)
        if other == resource:
            return (SerialSeen(resource, kind.name, known, at),)
    if not kind.discovered:
        return ()
    seen = serials_seen(discover(kind, config, accounts=False), at)
    if other is not None and resource not in {each.resource for each in seen}:
        # Another emulator answers at the known serial, so the instance does not have it now,
        # also when the discovery fails.
        seen += (SerialSeen(resource, kind.name, None, at),)
    return seen


def parse(kind: str, document: object) -> Found:
    """Return the valid instances of a discover document, with notes about what was left out."""
    if (
        not isinstance(document, dict)
        or document.get("schema") != DISCOVER_SCHEMA
        or not isinstance(document.get("instances"), list)
    ):
        return _failed(
            kind,
            f'the discover output is not an object with "schema": {DISCOVER_SCHEMA} and a list'
            ' of "instances"',
        )
    notes = [note_text(note) for note in _texts(document.get("notes"))][:_MAX_NOTES]
    raw = document["instances"]
    if len(raw) > MAX_INSTANCES:
        notes.append(f"only the first {MAX_INSTANCES} instances are used")
    instances: list[Instance] = []
    for data in raw[:MAX_INSTANCES]:
        instance = _instance(kind, data, notes)
        if instance is None:
            continue
        if any(other.name == instance.name for other in instances):
            notes.append(f"{instance.name} is listed twice; only the first is used")
            continue
        instances.append(instance)
    return Found(kind, tuple(instances), tuple(notes))


def _failed(kind: str, failure: str) -> Found:
    return Found(kind, notes=(failure,), failure=failure)


def note_text(text: str) -> str:
    cleaned = clean(" ".join(text.split()))
    return cleaned if len(cleaned) <= MAX_NOTE_LENGTH else cleaned[: MAX_NOTE_LENGTH - 3] + "..."


def _instance(kind: str, data: object, notes: list[str]) -> Instance | None:
    if not isinstance(data, dict):
        notes.append("an instance is not a JSON object, so it is left out")
        return None
    name = data.get("name")
    # A name becomes the name of a lease file, so it is never repaired, only refused. It can be
    # anything, so the note does not show it.
    if not isinstance(name, str) or RESOURCE_NAME.fullmatch(name) is None:
        notes.append("an instance has a name that is not a valid resource name, so it is left out")
        return None
    facts = _facts(name, data.get("facts", {}), notes)
    facts[KIND] = kind
    note = data.get("note")
    return Instance(
        name=name,
        facts=facts,
        accounts=_accounts(name, data.get("accounts"), notes),
        note=note_text(note) if isinstance(note, str) and note.strip() else None,
    )


def _facts(name: str, raw: object, notes: list[str]) -> dict[str, FactValue]:
    if not isinstance(raw, dict):
        notes.append(f"{name}: facts is not a JSON object, so the instance has no facts")
        return {}
    facts: dict[str, FactValue] = {}
    for key, value in list(raw.items())[:_MAX_FACTS]:
        if not isinstance(key, str) or FACT_NAME.fullmatch(key) is None:
            notes.append(f"{name}: a fact has a name that is not valid, so it is left out")
        elif key in (ACCOUNT, TAG):
            notes.append(f"{name}: banksman sets the fact {key} itself, so it is left out")
        elif _fact_value(value):
            facts[key] = value
        else:
            notes.append(f"{name}: the fact {key} has a value that is not valid, so it is left out")
    return facts


def _fact_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) <= _MAX_FACT_NUMBER
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_FACT_LENGTH
        and _CONTROL.search(value) is None
    )


def _accounts(name: str, raw: object, notes: list[str]) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        notes.append(f"{name}: accounts is not a list, so its accounts are not known")
        return None
    valid = [
        account
        for account in raw[:_MAX_ACCOUNTS]
        if isinstance(account, str) and RESOURCE_NAME.fullmatch(account)
    ]
    if len(valid) < min(len(raw), _MAX_ACCOUNTS):
        # The names can be anything, and they are often personal, so the note does not show them.
        left_out = min(len(raw), _MAX_ACCOUNTS) - len(valid)
        notes.append(
            f"{name}: {left_out} account names are not valid resource names, so they are left out"
        )
    return tuple(dict.fromkeys(valid))


def _texts(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [text for text in raw if isinstance(text, str) and text.strip()]

