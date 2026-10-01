"""Fakes and helpers that the tests share."""

import json
import os
import subprocess
import sys
from pathlib import Path

import banksman
from banksman.system import Process

# The source directory, for tests that run banksman in a child process.
SRC = str(Path(banksman.__file__).resolve().parents[1])


class FakeSystem:
    """A machine whose clock, boot, and processes the test sets.

    A process has its parent in `parents` and its group in `groups`; by default its parent is
    process 1, and it leads its own group. A signal ends the process that it reaches, unless
    the process ignores that signal.
    """

    def __init__(self) -> None:
        self.boot = "boot-1"
        self.now = 10_000.0
        self.wall = 1_790_000_000.0
        self.processes: dict[int, str] = {}
        self.parents: dict[int, int] = {}
        self.groups: dict[int, int] = {}
        self.ignores: dict[int, set[int]] = {}
        self.signals: list[tuple[str, int, int]] = []

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

    def process_table(self):
        known = {os.getpid(): "start-self", **self.processes}
        return {
            pid: Process(pid, self.parents.get(pid, 1), self.groups.get(pid, pid), started)
            for pid, started in known.items()
        }

    def signal(self, pid: int, signum: int) -> None:
        self.signals.append(("pid", pid, signum))
        self._deliver([pid], signum)

    def signal_group(self, pgid: int, signum: int) -> None:
        self.signals.append(("group", pgid, signum))
        self._deliver([pid for pid in self.processes if self.groups.get(pid, pid) == pgid], signum)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds

    def spawn(self, pid: int, parent: int = 1, group: int | None = None) -> None:
        self.processes[pid] = f"start-{pid}"
        self.parents[pid] = parent
        self.groups[pid] = pid if group is None else group

    def end(self, *pids: int) -> None:
        for pid in pids:
            self.processes.pop(pid, None)
            # Like the kernel, the system gives the children of an ended process to process 1.
            for child, parent in self.parents.items():
                if parent == pid:
                    self.parents[child] = 1

    def _deliver(self, pids, signum) -> None:
        for pid in pids:
            if pid == os.getpid():
                raise AssertionError("the test process was signalled")
            if signum not in self.ignores.get(pid, set()):
                self.end(pid)


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


def drain(data, reason="idle", deadline=None) -> None:
    """Change lease data into a draining lease, as a reaper writes one."""
    data.update(state="draining", void_reason=reason)
    if deadline is None:
        deadline = data["awake"]["touched"] + 60 * 60
    data["awake"]["drain_deadline"] = deadline


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
