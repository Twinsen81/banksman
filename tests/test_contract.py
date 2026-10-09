"""The contract of the JSON output: the recorded shapes, and the check that guards them.

The fixture in conftest.py checks every JSON document that a test makes the CLI print. These
tests show that the check finds what breaks a reader, and cover the line of the log, which no
command prints as it is.
"""

import re

import pytest
import shapes

from banksman.cli import main
from banksman.history import ACQUIRE, RELEASE, Event, Process

VERSION = {"schema": 6, "version": "0.1.0", "lease_schema": 6}
FREE = {
    "resource": "phone-1",
    "kind": None,
    "state": "free",
    "present": True,
    "serial": None,
    "facts": {},
    "lease": None,
}


@pytest.mark.parametrize(
    "document,problem",
    [
        ({**VERSION, "label": "x"}, "version.label is not recorded"),
        ({"schema": 6, "version": "0.1.0"}, "version.lease_schema is missing"),
        ({**VERSION, "schema": "6"}, "version.schema is not int"),
        ({**VERSION, "schema": True}, "version.schema is not int"),
        ({**VERSION, "version": None}, "version.version is not str"),
    ],
)
def test_the_check_finds_a_field_that_is_new_gone_or_of_another_type(document, problem):
    with pytest.raises(AssertionError, match=re.escape(problem)):
        shapes.check("version", document)


def test_the_check_looks_into_arrays_and_objects():
    document = {"schema": 6, "resources": [{**FREE, "present": "yes"}], "notes": []}
    with pytest.raises(AssertionError, match=re.escape("status.resources[0].present is not bool")):
        shapes.check("status", document)


def test_the_check_allows_null_and_optional_fields_where_the_shape_does():
    shapes.check("status", {"schema": 6, "resources": [FREE], "notes": []})
    shapes.check("status", {"schema": 6, "resources": [{**FREE, "error": "bad"}], "notes": []})


def test_every_json_document_that_a_command_prints_is_checked(monkeypatch):
    monkeypatch.setitem(shapes.SHAPES, "version", {"schema": int, "version": str})
    with pytest.raises(AssertionError, match=re.escape("version.lease_schema is not recorded")):
        main(["version", "--json"])


@pytest.mark.parametrize(
    "event",
    [
        Event(1_790_000_000.0, ACQUIRE, "phone-1"),
        Event(
            1_790_000_000.5,
            RELEASE,
            "phone-1",
            kind="device",
            lease_id="lease-1",
            holding="lease-1",
            state="ready",
            owner="/work/tree-a",
            owner_pid=101,
            agent="codex",
            issue="#123",
            session="session-1",
            purpose="verify the layout",
            accounts=("qa@example.test",),
            reason="owner",
            held=12.5,
            longest_quiet=3.0,
            drained=True,
            running=(Process(201, 300),),
            stopping=False,
            problem="the hook failed",
        ),
    ],
)
def test_a_line_of_the_log_has_its_shape(event):
    shapes.check("log line", event.to_json())
