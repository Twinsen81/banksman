"""Fakes and helpers that the tests share."""

import json
from pathlib import Path

import banksman

# The source directory, for tests that run banksman in a child process.
SRC = str(Path(banksman.__file__).resolve().parents[1])


class FakeSystem:
    """A machine whose clock, boot, and processes the test sets."""

    def __init__(self) -> None:
        self.boot = "boot-1"
        self.now = 10_000.0
        self.wall = 1_790_000_000.0
        self.processes: dict[int, str] = {}

    def boot_id(self) -> str:
        return self.boot

    def clock(self) -> float:
        return self.now

    def wall_clock(self) -> float:
        return self.wall

    def running(self, pids):
        return {pid: self.processes[pid] for pid in pids if pid in self.processes}

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds


def edit_lease(state_dir: Path, resource: str, change) -> None:
    """Change a lease file in place, as a crash or another banksman could leave it."""
    path = state_dir / f"{resource}.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))
