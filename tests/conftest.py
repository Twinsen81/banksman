import pytest
from helpers import FakeSystem

from banksman.config import CONFIG_ENV
from banksman.store import QUARANTINE_DIR_ENV, STATE_DIR_ENV, Store


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
def config_path(tmp_path, monkeypatch):
    # No test may read the real configuration of the machine. The file does not exist until a
    # test writes it.
    path = tmp_path / "config.toml"
    monkeypatch.setenv(CONFIG_ENV, str(path))
    return path


@pytest.fixture
def system():
    return FakeSystem()


@pytest.fixture
def store(state_dir, system):
    return Store(state_dir, system)

