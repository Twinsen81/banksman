"""Fakes and helpers that the tests share."""

import contextlib
import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import banksman
from banksman.system import Machine, Process

# The source directory, for tests that run banksman in a child process.
SRC = str(Path(banksman.__file__).resolve().parents[1])


class FakeSystem:
    """A machine whose clock, boot, and processes the test sets.

    A process has its parent in `parents`, its group in `groups`, and its executable in
    `programs`; by default its parent is process 1, and it leads its own group. A signal ends the
    process that it reaches, unless the process ignores that signal.
    """

    def __init__(self) -> None:
        self.boot = "boot-1"
        self.now = 10_000.0
        self.wall = 1_790_000_000.0
        self.processes: dict[int, str] = {}
        self.parents: dict[int, int] = {}
        self.groups: dict[int, int] = {}
        self.programs: dict[int, str] = {}
        self.arguments: dict[int, tuple[str, ...]] = {}
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
            pid: Process(
                pid,
                self.parents.get(pid, 1),
                self.groups.get(pid, pid),
                started,
                self.programs.get(pid, "/bin/sh"),
            )
            for pid, started in known.items()
        }

    def commands(self):
        return {
            pid: replace(process, arguments=self.arguments.get(pid, ()))
            for pid, process in self.process_table().items()
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

    def spawn(
        self, pid: int, parent: int = 1, group: int | None = None, program: str = "/bin/sh"
    ) -> None:
        self.processes[pid] = f"start-{pid}"
        self.parents[pid] = parent
        self.groups[pid] = pid if group is None else group
        self.programs[pid] = program

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


def table_rows(text: str) -> list[dict[str, str]]:
    """Return the rows of a table that banksman printed, by the names in its header.

    Every column starts where its name starts in the header, so a cell can have spaces.
    """
    header, *lines = text.splitlines()
    names, starts, position = [], [], 0
    for name in header.split("  "):
        if not name.strip():
            position += len(name) + 2
            continue
        start = header.index(name.strip(), position)
        names.append(name.strip())
        starts.append(start)
        position = start + len(name.strip())
    ends = [*starts[1:], None]
    return [
        {name: line[start:end].strip() for name, start, end in zip(names, starts, ends)}
        for line in lines
        if line.strip()
    ]


FAKE_ADB = """\
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls"), "a") as calls:
    calls.write(" ".join(sys.argv[1:]) + "\\n")
answer = json.load(open(os.path.join(here, "answers.json"))).get(" ".join(sys.argv[1:]))
if answer is None:
    sys.exit(1)
sys.stdout.write(answer)
"""


FAKE_EMULATOR = """\
import fcntl, json, os, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
tools = os.path.join(os.path.dirname(here), "platform-tools")
arguments = sys.argv[1:]
with open(os.path.join(here, "calls"), "a") as calls:
    calls.write(" ".join(arguments) + "\\n")
with open(os.path.join(here, "environments"), "a") as environments:
    environments.write(json.dumps({key: os.environ.get(key) for key in (
        "ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_AVD_HOME", "ANDROID_SERIAL",
        "BANKSMAN_LEASE")}) + "\\n")
with open(os.path.join(here, "pids"), "a") as pids:
    pids.write(f"{os.getpid()}\\n")
behavior = json.load(open(os.path.join(here, "behavior.json")))
avd = arguments[arguments.index("-avd") + 1]
serial = "emulator-" + arguments[arguments.index("-port") + 1]
print(f"INFO | starting {avd} as {serial}", flush=True)
if behavior.get("exit") is not None:
    print(behavior.get("say", "ERROR | the fake emulator fails"), flush=True)
    sys.exit(behavior["exit"])


def change(present, booted=False):
    # As adb sees the emulator: listed while it runs, offline until its boot completes.
    with open(os.path.join(tools, "answers.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = os.path.join(tools, "answers.json")
        answers = json.load(open(path))
        devices = answers.get("devices", "List of devices attached\\n").splitlines()
        devices = [line for line in devices if not line.startswith(serial + "\\t")]
        for key in [key for key in answers if key.startswith(f"-s {serial} ")]:
            del answers[key]
        if present:
            devices.append(f"{serial}\\t{'device' if booted else 'offline'}")
            answers[f"-s {serial} emu avd name"] = behavior.get("name", avd) + "\\r\\nOK\\r\\n"
        if booted:
            answers[f"-s {serial} shell getprop sys.boot_completed"] = "1\\n"
            answers[f"-s {serial} shell dumpsys account"] = "  Accounts: 0\\n"
        answers["devices"] = "\\n".join(devices) + "\\n"
        with open(path + ".tmp", "w") as file:
            json.dump(answers, file)
        os.replace(path + ".tmp", path)


def end(signum=None, frame=None):
    change(False)
    sys.exit(0)


signal.signal(signal.SIGTERM, signal.SIG_IGN if behavior.get("ignore_term") else end)
if behavior.get("listed", True):
    change(True)
boot = behavior.get("boot", 0)
begun = time.monotonic()
booted = False
while time.monotonic() < begun + behavior.get("life", 60):
    if boot is not None and not booted and time.monotonic() >= begun + boot:
        change(True, booted=True)
        booted = True
    time.sleep(0.05)
end()
"""


class FakeSdk:
    """An Android SDK whose adb answers from a table that the test sets, and an AVD home.

    No test runs the real adb or a real emulator. The fake adb records each call in `calls`. The
    fake emulator records its arguments, its environment, and its pid; it answers the fake adb as
    an emulator that boots, until SIGTERM or its life ends, as `emulator_behaves` sets.
    """

    def __init__(self, root: Path, avds=()) -> None:
        self.sdk = root / "sdk"
        self.avd_home = root / "avd"
        tools = self.sdk / "platform-tools"
        tools.mkdir(parents=True)
        adb = tools / "adb"
        adb.write_text(f"#!{sys.executable}\n{FAKE_ADB}")
        adb.chmod(0o755)
        emulators = self.sdk / "emulator"
        emulators.mkdir()
        emulator = emulators / "emulator"
        emulator.write_text(f"#!{sys.executable}\n{FAKE_EMULATOR}")
        emulator.chmod(0o755)
        self.emulator_behaves()
        self.avd_home.mkdir(parents=True)
        for name in avds:
            directory = self.avd_home / f"{name}.avd"
            directory.mkdir()
            (self.avd_home / f"{name}.ini").write_text(f"path={directory}\n")
            (directory / "config.ini").write_text("tag.id=google_apis\n")
        self.answer({})

    def answer(self, answers) -> None:
        (self.sdk / "platform-tools" / "answers.json").write_text(json.dumps(answers))

    def running(self, emulators) -> None:
        """Answer as adb does while these emulators run, by serial and AVD name."""
        devices = "".join(f"{serial}\tdevice\n" for serial in emulators)
        answers = {"devices": f"List of devices attached\n{devices}"}
        for serial, name in emulators.items():
            answers[f"-s {serial} emu avd name"] = f"{name}\r\nOK\r\n"
            answers[f"-s {serial} shell dumpsys account"] = "  Accounts: 0\n"
        self.answer(answers)

    def calls(self) -> list[str]:
        path = self.sdk / "platform-tools" / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def config(self) -> str:
        return f'[android]\nsdk = "{self.sdk}"\navd_home = "{self.avd_home}"\n'

    def emulator_behaves(self, **behavior) -> None:
        """Set how the next fake emulators behave: `boot`, the seconds until the boot completes,
        or None for never; `exit` and `say` to fail at once; `ignore_term`; `listed`, whether adb
        lists it at all; `name`, the AVD name that its console gives; and `life` in seconds."""
        (self.sdk / "emulator" / "behavior.json").write_text(json.dumps(behavior))

    def emulator_calls(self) -> list[str]:
        path = self.sdk / "emulator" / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def emulator_environments(self) -> list[dict]:
        path = self.sdk / "emulator" / "environments"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def emulator_pids(self) -> list[int]:
        path = self.sdk / "emulator" / "pids"
        return [int(line) for line in path.read_text().splitlines()] if path.exists() else []

    def start_emulator(self, avd: str, port: int, **behavior) -> subprocess.Popen:
        """Start a fake emulator as a person or a project script does, outside banksman."""
        if behavior:
            self.emulator_behaves(**behavior)
        return subprocess.Popen(
            [str(self.sdk / "emulator" / "emulator"), "-avd", avd, "-port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop_emulators(self) -> None:
        """End the fake emulators of this SDK that still run: the test started them, also
        through banksman."""
        fake = str(self.sdk / "emulator" / "emulator")
        for pid, process in Machine().commands().items():
            if fake in process.arguments:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)


FAKE_ANDROID = """\
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
arguments = sys.argv[1:]
with open(os.path.join(here, "calls"), "a") as calls:
    calls.write(" ".join(arguments) + "\\n")
# The options that every call gets do not choose the answer.
key = " ".join(
    argument for argument in arguments if not argument.startswith(("--project=", "--sdk="))
)
answers = json.load(open(os.path.join(here, "answers.json")))
answer = answers.get(key)
if answer is None:
    # A key that ends in * matches every call that starts with the rest of it.
    answer = next(
        (
            value
            for pattern, value in answers.items()
            if pattern.endswith("*") and key.startswith(pattern[:-1])
        ),
        None,
    )
if answer is None:
    sys.stderr.write("Error: INVALID_ARGUMENT: the fake has no answer for this call\\n")
    sys.exit(1)
for index, chunk in enumerate(answer.get("chunks", [])):
    if index:
        time.sleep(answer.get("pause", 0))
    sys.stdout.write(chunk)
    sys.stdout.flush()
for path, text in answer.get("append", {}).items():
    with open(path, "a") as file:
        file.write(text)
if answer.get("err"):
    sys.stderr.write(answer["err"])
sys.exit(answer.get("status", 0))
"""


class FakeAndroidCli:
    """An android command line tool that answers from a table that the test sets.

    No test runs the real tool or calls the network. The fake records each call in `calls`, and
    chooses the answer by the arguments without --project and --sdk; a key that ends in *
    matches by its start. An answer prints its chunks
    with a pause between them, appends text to files, as the daemon of a connection writes its
    line into the properties file, and exits with its status.
    """

    PROJECT = "my-project"

    def __init__(self, root: Path) -> None:
        self.directory = root / "android-cli"
        self.directory.mkdir(parents=True)
        self.path = self.directory / "android"
        self.path.write_text(f"#!{sys.executable}\n{FAKE_ANDROID}")
        self.path.chmod(0o755)
        self.state = root / "android-cli-state"
        self.state.mkdir()
        self.answer({})

    @property
    def connections_file(self) -> Path:
        return self.state / "active-connections.properties"

    def answer(self, answers) -> None:
        (self.directory / "answers.json").write_text(json.dumps(answers))

    def connected(self, ports) -> None:
        """Write the properties file as the daemons write it for these reservations."""
        lines = "".join(
            f"projects/{self.PROJECT}/deviceSessions/session-{handle}={port}\n"
            for handle, port in ports.items()
        )
        self.connections_file.write_text(f"#Fri Oct 09 09:44:08 CEST 2026\n{lines}")

    def calls(self) -> list[str]:
        path = self.directory / "calls"
        if not path.exists():
            return []
        # Every call names the project and the SDK; the tests compare the rest.
        return [
            " ".join(
                word for word in line.split() if not word.startswith(("--project=", "--sdk="))
            )
            for line in path.read_text().splitlines()
        ]

    def full_calls(self) -> list[str]:
        path = self.directory / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def config(self) -> str:
        """The lines of [android] that name the tool and its files."""
        return f'cli = "{self.path}"\ncli_state = "{self.state}"\n'
