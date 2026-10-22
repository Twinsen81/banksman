import sys

import pytest
import shapes
from helpers import FakeSystem

from banksman import cli
from banksman.config import CONFIG_ENV
from banksman.identity import PID_VARIABLES, SESSION_VARIABLES
from banksman.inventory import INVENTORY_ENV
from banksman.lease import LEASE_VARIABLE
from banksman.store import (
    ENDED_ENV,
    LOG_ENV,
    QUARANTINE_DIR_ENV,
    STATE_DIR_ENV,
    TRACE_ENV,
    Store,
)


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    # No test may use the real state directory of the machine.
    path = tmp_path / "state"
    monkeypatch.setenv(STATE_DIR_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def quarantine_dir(tmp_path, monkeypatch):
    # No test may write quarantines into the real home directory.
    path = tmp_path / "home" / ".local" / "state" / "banksman" / "quarantine"
    monkeypatch.setenv(QUARANTINE_DIR_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def log_path(tmp_path, monkeypatch):
    # No test may write into the real log.
    path = tmp_path / "home" / ".local" / "state" / "banksman" / "log.jsonl"
    monkeypatch.setenv(LOG_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def ended_path(tmp_path, monkeypatch):
    # No test may write into the real record of the ends of leases.
    path = tmp_path / "home" / ".local" / "state" / "banksman" / "ended.json"
    monkeypatch.setenv(ENDED_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def trace_path(tmp_path, monkeypatch):
    # No test may write into the real trace.
    path = tmp_path / "home" / ".local" / "state" / "banksman" / "trace.jsonl"
    monkeypatch.setenv(TRACE_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def config_path(tmp_path, monkeypatch):
    # No test may read the real configuration of the machine. The file does not exist until a
    # test writes it.
    path = tmp_path / "config.toml"
    monkeypatch.setenv(CONFIG_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def inventory_path(tmp_path, monkeypatch):
    # No test may read or write the real inventory of the machine.
    path = tmp_path / "inventory.toml"
    monkeypatch.setenv(INVENTORY_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def no_agent_pid(monkeypatch):
    # The agent that runs the tests, and a banksman run around them, must not change the owner
    # process or the session of a test.
    for variable in (*PID_VARIABLES.values(), *SESSION_VARIABLES.values(), LEASE_VARIABLE):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def person(monkeypatch):
    """Let each command count its caller as a person: standard error is a terminal, and no agent
    process is above the caller. Under pytest, standard error is not a terminal, and an agent can
    run the tests."""
    monkeypatch.setattr(cli, "_stderr_is_terminal", lambda: True)
    monkeypatch.setattr(cli, "find_agent", lambda table, agents, pid, env: (None, None))


@pytest.fixture
def system():
    return FakeSystem()


@pytest.fixture
def store(state_dir, system):
    return Store(state_dir, system)


@pytest.fixture(autouse=True)
def output_shapes(monkeypatch):
    # Every JSON document that a test makes the CLI print must have its recorded shape, so that a
    # change of the output cannot pass without a decision about OUTPUT_SCHEMA.
    printed = cli._print_json

    def checked(payload):
        shapes.check(_command(), payload)
        printed(payload)

    monkeypatch.setattr(cli, "_print_json", checked)


def _command() -> str:
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_name.startswith("_cmd_") and frame.f_globals is vars(cli):
            return frame.f_code.co_name.removeprefix("_cmd_").replace("_", " ")
        frame = frame.f_back
    raise AssertionError("JSON was printed outside of a command")
