import json

import pytest

from banksman import SCHEMA_VERSION, __version__
from banksman.cli import main


def test_version_json(capsys):
    assert main(["version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"schema": SCHEMA_VERSION, "version": __version__}


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"banksman {__version__} (schema {SCHEMA_VERSION})"


def test_status_json_is_an_empty_pool(capsys):
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"schema": SCHEMA_VERSION, "leases": []}


def test_status_table(capsys):
    assert main(["status"]) == 0
    assert capsys.readouterr().out.strip() == "No leases."


def test_abbreviated_flags_are_refused():
    with pytest.raises(SystemExit) as exc:
        main(["status", "--js"])
    assert exc.value.code == 2


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "usage: banksman" in capsys.readouterr().out

