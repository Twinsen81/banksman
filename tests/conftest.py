import pytest
from helpers import FakeSystem

from banksman.store import STATE_DIR_ENV, Store


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    # No test may use the real state directory of the machine.
    path = tmp_path / "state"
    monkeypatch.setenv(STATE_DIR_ENV, str(path))
    return path


@pytest.fixture
def system():
    return FakeSystem()


@pytest.fixture
def store(state_dir, system):
    return Store(state_dir, system)

