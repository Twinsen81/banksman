import sys

import pytest
import shapes
from helpers import FakeSystem

from banksman import cli
from banksman.config import CONFIG_ENV
from banksman.inventory import INVENTORY_ENV
from banksman.store import LOG_ENV, QUARANTINE_DIR_ENV, STATE_DIR_ENV, Store


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
