"""Fakes and helpers that the tests share."""

import json
import os
import subprocess
import sys
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
        # Like the real machine, the process that asks always runs.
        known = {os.getpid(): "start-self", **self.processes}
        return {pid: known[pid] for pid in pids if pid in known}

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds


def write_config(path: Path, text: str) -> Path:
    """Write a configuration file that banksman accepts, whatever the umask is."""
    path.write_text(text)
    path.chmod(0o600)
    return path


def hook(code: str, *args: str) -> list[str]:
    """Return a hook command that runs Python code in a new interpreter."""
    return [sys.executable, "-c", code, *args]


def edit_lease(state_dir: Path, resource: str, change) -> None:
    """Change a lease file in place, as a crash or another banksman could leave it."""
    path = state_dir / f"{resource}.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def age_lease(state_dir: Path, resource: str, seconds: float) -> None:
    """Move the last touch of a lease into the past."""

    def change(data):
        data["awake"]["touched"] -= seconds
        data["touched_at"] -= seconds

    edit_lease(state_dir, resource, change)


def reap_in_child(state_dir: Path) -> list:
    """Run `banksman reap --json` in its own process and return what it reaped."""
    result = subprocess.run(
        [sys.executable, "-m", "banksman", "reap", "--json"],
        env={**os.environ, "BANKSMAN_STATE_DIR": str(state_dir), "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)["reaped"]
