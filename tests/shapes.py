"""The recorded shape of every JSON document that banksman prints, and of a line of the log.

Readers ignore the fields that they do not know, so a new field only needs a line here. A field
that is removed or renamed, or that gets another type, breaks the readers: raise OUTPUT_SCHEMA
too. A field that keeps its type but changes its meaning, for example seconds that become
milliseconds, keeps its shape, so this check cannot find it, and it needs a new number as well.

A shape is a type, None for null, a tuple of shapes that a value can have, a dict for an object
with exactly these keys (a key that ends in "?" can be missing), a list with one shape for the
items of an array, or MapOf for an object whose keys are names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MapOf:
    value: Any


NUMBER = (int, float)
TEXT = (str, None)

USER = {"pid": int, "pgid": (int, None)}
MOMENT = {"at": str, "in": int}
LEASE = {
    "resource": str,
    "kind": str,
    "state": str,
    "owner": str,
    "owner_pid": (int, None),
    "agent": TEXT,
    "issue": TEXT,
    "session": TEXT,
    "serial": TEXT,
    "handle": TEXT,
    "acquired_at": str,
    "touched_at": str,
    "free_by": ({"expected": (MOMENT, None), "latest": MOMENT, "abandoned": MOMENT}, None),
    "drain_deadline": (MOMENT, None),
    "void_reason": TEXT,
    "users": [USER],
    "accounts": [str],
    "purpose?": TEXT,
    "label?": TEXT,
}
EVENT = {
    "at": NUMBER,
    "event": str,
    "resource": str,
    "kind": TEXT,
    "lease_id": TEXT,
    "holding": TEXT,
    "state": TEXT,
    "owner": TEXT,
    "owner_pid": (int, None),
    "agent": TEXT,
    "issue": TEXT,
    "session": TEXT,
    "purpose": TEXT,
    "accounts": [str],
    "reason": TEXT,
    "held": (*NUMBER, None),
    "longest_quiet": (*NUMBER, None),
    "drained": (bool, None),
    "end": (bool, None),
    "running": [USER],
    "stopping": (bool, None),
    "problem": TEXT,
    "action": TEXT,
    "command": TEXT,
    "serial": TEXT,
}
# `log --json` shows the time as text, and the purpose only with --verbose.
SHOWN_EVENT = {**EVENT, "at": str, "purpose?": TEXT}
del SHOWN_EVENT["purpose"]
# A group of events of `log --count --json`, and of the guard events in `held --json`.
COUNT = {
    "count": int,
    "first": str,
    "last": str,
    "event": str,
    "action": TEXT,
    "reason": TEXT,
    "resource": str,
    "serial": TEXT,
    "owner": TEXT,
    "agent": TEXT,
    "commands": [str],
}
NAMES = {"kind": str, "name": str}
DECISIONS = {"allowed": [str], "refused": [str]}

HELD_LEASE = {
    "label": TEXT,
    "lease_id": str,
    "resource": str,
    "kind": str,
    "state": str,
    "serial": TEXT,
    "handle": TEXT,
}

SHAPES: dict[str, Any] = {
    "version": {"schema": int, "version": str, "lease_schema": int},
    "status": {
        "schema": int,
        "resources": [
            {
                "resource": str,
                "kind": TEXT,
                "state": str,
                "present": bool,
                "paid": bool,
                "serial": TEXT,
                "handle": TEXT,
                "ends": (MOMENT, None),
                "facts": dict,
                "lease": (LEASE, None),
                "started_by": ({"agent": str, "pid": int, "running": bool}, None),
                "error?": str,
            }
        ],
        "notes": [str],
    },
    # With --count, `log` prints counts in place of the events.
    "log": {
        "schema": int,
        "events?": [SHOWN_EVENT],
        "counts?": [COUNT],
        "skipped": int,
    },
    "whoami": {
        "schema": int,
        "owner": str,
        "owner_pid": (int, None),
        "agent": TEXT,
        "issue": TEXT,
        "session": TEXT,
    },
    "held": {
        "schema": int,
        "owner_pid": int,
        "leases": [HELD_LEASE],
        "guard": ([COUNT], None),
    },
    "wait": {"schema": int, "leases": [HELD_LEASE]},
    "reap": {
        "schema": int,
        "reaped": [
            {
                "resource": str,
                "kind": str,
                "owner": str,
                "void_reason": TEXT,
                "outcome": str,
                "running": [USER],
            }
        ],
    },
    "acquire": {
        "schema": int,
        "parts": [
            {
                "part": TEXT,
                "lease_id": str,
                "resource": str,
                "kind": str,
                "state": str,
                "serial": TEXT,
                "handle": TEXT,
                "paid": bool,
                "accounts": [str],
                "kept": bool,
                "previous": str,
                "started": bool,
            }
        ],
    },
    "admin discover": {
        "schema": int,
        "inventory_path": str,
        "written": bool,
        "instances": [
            {
                "kind": str,
                "name": str,
                "selected": bool,
                "before": str,
                "facts": dict,
                "accounts_known": bool,
                "note": TEXT,
            }
        ],
        "accounts": [{"name": str, "on": [str], "selected": bool}],
        "notes": [{"kind": str, "note": str}],
        "missing": [NAMES],
        "outside_patterns": [NAMES],
        "inventory": {
            "kinds": MapOf(DECISIONS),
            "accounts": DECISIONS,
            "tags": MapOf([str]),
        },
    },
    "admin doctor": {
        "schema": int,
        "version": str,
        "checks": [{"check": str, "status": str, "detail": str, "fix": TEXT}],
    },
    "log line": {"schema": int, **EVENT},
}

# A line of the trace has a schema of its own, TRACE_SCHEMA: no command prints it.
_TRACED_LEASE = {"resource": str, "lease_id": str, "state": str}
TRACE_STEPS = {
    "discovery": {
        "step": str,
        "kind": str,
        "accounts": bool,
        "seconds": NUMBER,
        "instances": [
            {
                "name": str,
                "running": (bool, None),
                "serial": TEXT,
                "input_age": (int, None),
                "unmanaged": bool,
                "started_by": (int, None),
            }
        ],
        "notes": [str],
        "failure": TEXT,
    },
    "search": {
        "step": str,
        "parts": [
            {
                "part": TEXT,
                "where": [str],
                "accounts": int,
                "matches": [str],
                "no_match": [{"resource": str, "clause": TEXT}],
            }
        ],
        "left_out": [{"resource": str, "kind": str, "reason": str}],
        "absent": [{"resource": str, "kind": str}],
    },
    "grant": {
        "step": str,
        "outcome": str,
        "granted": [str],
        "parts": [
            [
                {
                    "resource": str,
                    "kept": bool,
                    "skipped": TEXT,
                    "lease_id": TEXT,
                    "state": TEXT,
                }
            ]
        ],
    },
    "reap": {
        "step": str,
        "leases": [{**_TRACED_LEASE, "void": TEXT}],
        "ending": [{"resource": str, "kind": str, "reason": str}],
    },
    "hook": {
        "step": str,
        "hook": TEXT,
        "kind": str,
        "resource": TEXT,
        "command": [str],
        "status": (int, None),
        "failure": TEXT,
        "seconds": NUMBER,
        "output": [str],
    },
    "guard": {
        "step": str,
        "action": str,
        "reason": TEXT,
        "serial": TEXT,
        "leases": [{**_TRACED_LEASE, "holding": str}],
        "used": [str],
        "touched": [str],
        "adb": TEXT,
        "error": TEXT,
    },
}
TRACE_LINE = {
    "schema": int,
    "version": str,
    "at": NUMBER,
    "pid": int,
    "continues": (int, None),
    "command": [str],
    "caller": (
        {
            "owner": str,
            "owner_pid": (int, None),
            "agent": TEXT,
            "issue": TEXT,
            "session": TEXT,
        },
        None,
    ),
    "status": (int, None),
    "error": TEXT,
    "seconds": NUMBER,
    "stderr": [str],
    "stderr_left_out": int,
    "steps": [tuple(TRACE_STEPS.values())],
    "steps_left_out": int,
}


def check_trace_line(line: object) -> None:
    """Fail when a line of the trace does not have its recorded shape, also each of its steps."""
    problems = _problems(TRACE_LINE, line, "trace line")
    assert isinstance(line, dict)
    for index, step in enumerate(line.get("steps", [])):
        name = step.get("step") if isinstance(step, dict) else None
        if name not in TRACE_STEPS:
            problems.append(f"trace line.steps[{index}] has no recorded step {name!r}")
        else:
            problems += _problems(TRACE_STEPS[name], step, f"trace line.steps[{index}]")
    assert not problems, (
        f"A line of the trace does not match its shape in tests/shapes.py: {problems}. Record a"
        " new field there. A field that is gone, renamed, or of another type breaks readers:"
        " change its shape, and also raise TRACE_SCHEMA."
    )


def check(name: str, document: object) -> None:
    """Fail when *document*, as a reader gets it, does not have the recorded shape *name*."""
    problems = _problems(SHAPES[name], json.loads(json.dumps(document)), name)
    assert not problems, (
        f"The JSON of {name!r} does not match its shape in tests/shapes.py: {problems}. Record a"
        " new field there. A field that is gone, renamed, or of another type breaks readers:"
        " change its shape, and also raise OUTPUT_SCHEMA."
    )


def _problems(shape: Any, value: Any, path: str) -> list[str]:
    if isinstance(shape, tuple):
        options = [_problems(each, value, path) for each in shape]
        return [] if any(not each for each in options) else min(options, key=len)
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            return [f"{path} is not an object"]
        keys = {key.removesuffix("?"): key.endswith("?") for key in shape}
        problems = [f"{path}.{key} is not recorded" for key in value if key not in keys]
        problems += [
            f"{path}.{key} is missing"
            for key, optional in keys.items()
            if not optional and key not in value
        ]
        for key, each in shape.items():
            name = key.removesuffix("?")
            if name in value:
                problems += _problems(each, value[name], f"{path}.{name}")
        return problems
    if isinstance(shape, list):
        if not isinstance(value, list):
            return [f"{path} is not an array"]
        return [
            problem
            for index, each in enumerate(value)
            for problem in _problems(shape[0], each, f"{path}[{index}]")
        ]
    if isinstance(shape, MapOf):
        if not isinstance(value, dict):
            return [f"{path} is not an object"]
        return [
            problem
            for key, each in value.items()
            for problem in _problems(shape.value, each, f"{path}.{key}")
        ]
    if shape is None:
        return [] if value is None else [f"{path} is not null"]
    # bool is a subclass of int in Python, but not in JSON.
    if isinstance(value, bool) and shape is not bool:
        return [f"{path} is not {shape.__name__}"]
    return [] if isinstance(value, shape) else [f"{path} is not {shape.__name__}"]
