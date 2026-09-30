import json
import os
import subprocess
import sys

import pytest
from helpers import SRC, edit_lease

from banksman import SCHEMA_VERSION, __version__
from banksman.cli import main
from banksman.store import Store
from banksman.system import Machine

OWNER = "/work/tree-a"


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
    assert json.loads(capsys.readouterr().out) == {
        "schema": SCHEMA_VERSION,
        "leases": [],
        "unreadable": [],
    }


def test_status_table(capsys):
    assert main(["status"]) == 0
    assert capsys.readouterr().out.strip() == "No leases."


def test_status_lists_the_leases(capsys, state_dir):
    Store(state_dir, Machine()).acquire("phone-1", "device", OWNER)
    assert main(["status", "--json"]) == 0
    (lease,) = json.loads(capsys.readouterr().out)["leases"]
    assert {key: lease[key] for key in ("resource", "kind", "state", "owner", "owner_pid")} == {
        "resource": "phone-1",
        "kind": "device",
        "state": "ready",
        "owner": OWNER,
        "owner_pid": None,
    }
    assert lease["acquired_at"].endswith("Z")

    assert main(["status"]) == 0
    header, row = capsys.readouterr().out.splitlines()
    assert header.split() == ["RESOURCE", "KIND", "STATE", "OWNER", "SINCE"]
    assert row.split()[:4] == ["phone-1", "device", "ready", OWNER]


def test_status_reaps_first(capsys, state_dir):
    Store(state_dir, Machine()).acquire("phone-1", "device", OWNER)
    _age_lease(state_dir, "phone-1", 21 * 60)
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["leases"] == []


def test_status_removes_terminal_control_sequences(capsys, state_dir):
    Store(state_dir, Machine()).snapshot()
    (state_dir / "phone\x1b[2J-1.json").write_text("{")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "phone-1" in out


def test_reap_reports_what_it_took_back(capsys, state_dir):
    Store(state_dir, Machine()).acquire("phone-1", "device", OWNER)
    _age_lease(state_dir, "phone-1", 21 * 60)
    assert main(["reap", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": SCHEMA_VERSION,
        "reaped": [
            {
                "resource": "phone-1",
                "kind": "device",
                "owner": OWNER,
                "void_reason": "idle",
                "outcome": "released",
            }
        ],
    }


def test_reap_with_nothing_to_do(capsys):
    assert main(["reap"]) == 0
    assert capsys.readouterr().out.strip() == "Nothing to reap."


def test_an_unsafe_state_directory_is_an_error(capsys, state_dir):
    state_dir.mkdir()
    state_dir.chmod(0o777)
    assert main(["status"]) == 1
    assert capsys.readouterr().err.startswith("banksman: group or others can write to")


def test_a_killed_owner_frees_its_lease(state_dir):
    # End to end: a real owner process, the real process list, the real lock, and the
    # command in its own process.
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        Store(state_dir, Machine()).acquire("phone-1", "device", OWNER, owner_pid=owner.pid)
        # The last touch was 6 minutes ago: past the grace period, not yet idle.
        _age_lease(state_dir, "phone-1", 6 * 60)
        assert _reap_in_child(state_dir) == []
        owner.kill()
        owner.wait()
        (reaped,) = _reap_in_child(state_dir)
        assert (reaped["resource"], reaped["void_reason"], reaped["outcome"]) == (
            "phone-1",
            "owner_gone",
            "released",
        )
        assert not (state_dir / "phone-1.json").exists()
    finally:
        owner.kill()
        owner.wait()


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "usage: banksman" in capsys.readouterr().out


def test_abbreviated_flags_are_refused():
    with pytest.raises(SystemExit) as exc:
        main(["status", "--js"])
    assert exc.value.code == 2


def _age_lease(state_dir, resource, seconds):
    def change(data):
        data["awake"]["touched"] -= seconds
        data["touched_at"] -= seconds

    edit_lease(state_dir, resource, change)


def _reap_in_child(state_dir):
    result = subprocess.run(
        [sys.executable, "-m", "banksman", "reap", "--json"],
        env={**os.environ, "BANKSMAN_STATE_DIR": str(state_dir), "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)["reaped"]

