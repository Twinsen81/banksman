"""The android-remote preset: remote physical devices of Android Device Streaming.

This is the only module that runs the android command line tool. That tool reserves a device in
a lab, and forwards it to the local adb server as `localhost:<port>`. One instance is one model
of the catalogue, named `<codename>:<api>`. The service allows one reservation of a model for
each account, so one instance for each model loses nothing.

Discovery calls no network. It reads the catalogue of models that `banksman admin discover`
saves, the file in which the tool lists its live connections, the file of the reservations that
banksman made, and adb. Only `admin discover`, a start, a new connection, and a take-back run the
tool. What the tool prints and what its files hold is untrusted, so every value is checked.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import pwd
import re
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from banksman import android, hooks
from banksman.config import Android, Kind
from banksman.errors import BanksmanError
from banksman.lease import RESOURCE_NAME
from banksman.sanitize import clean
from banksman.store import STATE_DIR_ENV, lasting_dir

REMOTE_DIR_ENV = "BANKSMAN_REMOTE_DIR"
# The facts that only this preset gives: the id of the reservation of a connected device, and
# when that reservation ends.
FACTS = ("handle", "ends")
CLI_TIMEOUT_SECONDS = 60.0
FILE_SCHEMA = 1
# The service ends a reservation at the latest this long after its creation.
MAX_RESERVATION_SECONDS = 3 * 60 * 60
_MAX_OUTPUT = 1024 * 1024
_MAX_FILE = 1024 * 1024
_MAX_CONNECTIONS_FILE = 64 * 1024
_MAX_LINE = 300
_POLL_SECONDS = 0.2
# How long adb may take to list a device after the tool has connected it.
_ADB_WAIT_SECONDS = 15.0
_ADB_POLL_SECONDS = 0.5
_KILL_WAIT_SECONDS = 5.0
_LOCK_WAIT_SECONDS = 10.0
_CONNECTIONS_FILE = "active-connections.properties"
_CODENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_HANDLE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_TARGET = re.compile(r"(?P<codename>[A-Za-z0-9][A-Za-z0-9_-]{0,63})/(?P<api>[0-9]{1,4})")
_NAME = re.compile(r"(?P<codename>[A-Za-z0-9][A-Za-z0-9_-]{0,63}):(?P<api>[0-9]{1,4})")
_GROUP = re.compile(r"(?P<manufacturer>.+?) \[[0-9]+\]")
_CREATED = re.compile(
    rf"New reservation created with id (?P<handle>{_HANDLE})\.(?:[ \t]+Reservation ends at"
    r" (?P<ends>[^\n]+))?"
)
_CONNECTED = re.compile(r"Device connected on port (?P<port>[0-9]{1,5})\b")
_ALREADY = re.compile(
    rf"Reservation {_HANDLE} is already connected on port (?P<port>[0-9]{{1,5}})\."
)
_EXTENDED = re.compile(r"Reservation extended\. New expire time: (?P<ends>[^\n]+)")
_GONE = re.compile(
    rf"Reservation {_HANDLE} has already ended\.|Invalid reservation ID {_HANDLE}\."
)
_CANCELLED = "Reservation cancelled successfully."
_NO_RESERVATIONS = "No reservations found."
_CONNECTION = re.compile(
    rf"projects/(?P<project>.+)/deviceSessions/session-(?P<handle>{_HANDLE})"
    r"\s*[=:]\s*(?P<port>[0-9]{1,5})"
)
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FORMS = (
    (re.compile(r"\b(tab|tablet)\b", re.IGNORECASE), "tablet"),
    (re.compile(r"\bwatch\b", re.IGNORECASE), "watch"),
)


class RemoteError(BanksmanError):
    """The android command line tool, or one of the files of this preset, gave no answer."""


class Ended(RemoteError):
    """The reservation has ended, so its device cannot be connected again."""


@dataclass(frozen=True)
class Model:
    codename: str
    api: int
    manufacturer: str | None = None
    model: str | None = None

    @property
    def name(self) -> str:
        """The name of the instance. The catalogue writes codename/api, but a resource name
        cannot have '/'."""
        return f"{self.codename}:{self.api}"

    @property
    def target(self) -> str:
        return f"{self.codename}/{self.api}"


@dataclass(frozen=True)
class Session:
    """A reservation that banksman started, or adopted at a start, as its file records it."""

    handle: str
    resource: str
    # Whether banksman created the reservation. The reaper ends only those.
    created: bool
    # When banksman first recorded the reservation, in wall-clock time. The service ends the
    # reservation at the latest 3 hours after its creation, so the record is kept that long.
    at: float
    port: int | None = None
    # When the reservation ends, in wall-clock time, or None when that is not known. The holder
    # can extend the reservation by hand, so this can be too early, and only status shows it.
    ends: float | None = None


@dataclass(frozen=True)
class Reservation:
    """A reservation of the account, as `android device remote list` shows it."""

    handle: str
    state: str
    expires: str
    model: str
    codename: str
    api: str


@dataclass(frozen=True)
class Answer:
    # None when the tool did not end in time.
    status: int | None
    text: str

    def first_line(self) -> str:
        line = next((line for line in _lines(self.text) if line.strip()), "")
        return _short(line)


@dataclass(frozen=True)
class Started:
    handle: str
    serial: str
    ends: float | None
    created: bool


# Runs the tool with a timeout, and calls the watcher, if any, with what it has printed so far.
Run = Callable[[Sequence[str], float, Callable[[str], None] | None], Answer]
Document = dict[str, Any]


def run_cli(
    command: Sequence[str], timeout: float, watch: Callable[[str], None] | None = None
) -> Answer:
    """Run the tool. Both streams go to one file: normal output and soft errors go to standard
    output, and errors of the service to standard error."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in hooks.VARIABLES and key != hooks.ANDROID_SERIAL
    }
    with tempfile.TemporaryFile() as output:
        try:
            # A file, not a pipe: the daemon that the tool starts for a connection keeps
            # running, and a pipe that it inherits would keep this command waiting. A process
            # group of its own, so that a timeout ends the tool and what it started in that
            # group; the daemon starts a session of its own, and keeps the connection.
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                cwd="/",
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise RemoteError(
                f"{command[0]} does not exist; set cli in [android] to the path that"
                " `command -v android` prints"
            ) from None
        except OSError as exc:
            raise RemoteError(f"cannot run {command[0]}: {exc.strerror}") from exc
        give_up = time.monotonic() + timeout
        try:
            while True:
                left = give_up - time.monotonic()
                try:
                    status = process.wait(
                        timeout=max(0.0, min(left, _POLL_SECONDS) if watch else left)
                    )
                    break
                except subprocess.TimeoutExpired:
                    if watch is not None:
                        watch(_printed(output))
                    if time.monotonic() >= give_up:
                        _kill(process)
                        return Answer(None, _printed(output))
        except BaseException:
            _kill(process)
            raise
        return Answer(status, _printed(output))


def _printed(output: Any) -> str:
    # pread does not move the offset of the file, which the tool shares for its writes.
    data = os.pread(output.fileno(), _MAX_OUTPUT, 0)
    return _ESCAPES.sub("", data.decode(errors="replace"))


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_KILL_WAIT_SECONDS)


def command(settings: Android, kind: Kind, *arguments: str) -> list[str]:
    """Return a command of the tool for the project of a kind.

    The tool and its connection daemon get the SDK of banksman, so that they use the same adb,
    never one that the PATH finds, which can be the wrapper of the adb guard, or another
    version that restarts the adb server.
    """
    if settings.cli is None or kind.project is None:
        raise RemoteError(f"kind {kind.name} needs cli in [android] and its project")
    return [
        str(settings.cli),
        "device",
        "remote",
        *arguments,
        f"--project={kind.project}",
        f"--sdk={android.sdk(settings)}",
    ]


def parse_models(text: str) -> list[Model]:
    """Return the models in the tree that `android device remote models` prints."""
    models: list[Model] = []
    seen: set[str] = set()
    manufacturer = None
    for line in _lines(text):
        # The characters of the tree come first; a name starts with a letter or a digit.
        content = re.sub(r"^[^A-Za-z0-9]+", "", line).strip()
        group = _GROUP.fullmatch(content)
        if group is not None:
            manufacturer = group["manufacturer"].strip()
            continue
        words = content.split()
        first = len(words)
        while first > 0 and _TARGET.fullmatch(words[first - 1]):
            first -= 1
        name = " ".join(words[:first])
        if first == len(words) or not name:
            continue
        for word in words[first:]:
            target = _TARGET.fullmatch(word)
            assert target is not None
            model = Model(target["codename"], int(target["api"]), manufacturer, name)
            if model.name not in seen:
                seen.add(model.name)
                models.append(model)
    return models


def parse_reservations(text: str) -> list[Reservation]:
    """Return the rows of the table that `android device remote list` prints."""
    found = []
    for line in _lines(text):
        fields = re.split(r"\s{2,}", line.strip())
        if len(fields) != 7 or fields[0] == "Reservation":
            continue
        handle, state, expires, _, model, codename, api = fields
        if re.fullmatch(_HANDLE, handle) and api.isdigit():
            found.append(Reservation(handle, state, expires, model, codename, api))
    return found


def parse_connections(text: str, project: str) -> dict[str, int]:
    """Return the port of each live connection of the project, by the id of its reservation,
    from the properties file of the tool."""
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        # A Java properties file escapes ':' and '=' in a key.
        match = _CONNECTION.fullmatch(line.replace("\\:", ":").replace("\\=", "="))
        if match is not None and match["project"] == project and _port(match["port"]):
            found[match["handle"]] = int(match["port"])
    return found


def time_of_day(text: str, now: float) -> float | None:
    """Return the wall-clock time of a local time of day such as `9:56 AM` that the tool
    prints, or None when the text is not such a time.

    The tool prints no date. A reservation lasts at most 3 hours, so the time is the next one
    with that time of day: today, or tomorrow when it has passed by more than a minute.
    """
    # The tool puts a narrow no-break space before AM, and prints it as "?" when the locale of
    # its environment has no Unicode encoding.
    words = re.sub(r"[^0-9A-Za-z:]+", " ", text).strip()
    for layout in ("%I:%M %p", "%H:%M"):
        try:
            parsed = datetime.strptime(words, layout)
        except ValueError:
            continue
        local = datetime.fromtimestamp(now).astimezone()
        moment = local.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        if moment < local - timedelta(minutes=1):
            moment += timedelta(days=1)
        return moment.timestamp()
    return None


def utc_text(wall: float) -> str:
    moment = datetime.fromtimestamp(wall, timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def form_of(model: str) -> str:
    """Return the form of a model by the words of its name in the catalogue."""
    for pattern, form in _FORMS:
        if pattern.search(model):
            return form
    return "phone"


def model_of(resource: str) -> Model:
    match = _NAME.fullmatch(resource)
    if match is None:
        raise RemoteError(f"{resource} is not the name of a remote model, such as tokay:34")
    return Model(match["codename"], int(match["api"]))


# The files of the preset.


def default_remote_dir() -> Path:
    override = os.environ.get(REMOTE_DIR_ENV)
    if override:
        return Path(override)
    # Next to a state directory for tests, as the log and the quarantines.
    state_dir = os.environ.get(STATE_DIR_ENV)
    if state_dir:
        return Path(state_dir).with_name(f"{Path(state_dir).name}-remote")
    # Outside /tmp, as the log: a reservation outlives a restart of the machine, and the
    # catalogue must not depend on cleaners of temporary files.
    return lasting_dir(REMOTE_DIR_ENV) / "remote"


def cli_state(settings: Android) -> Path:
    if settings.cli_state is not None:
        return settings.cli_state
    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise RemoteError(
            f"cannot find the home directory of user {os.geteuid()}; set cli_state in [android]"
        ) from exc
    return Path(home) / ".android" / "cli" / "remote-devices"


def read_connections(settings: Android, project: str) -> dict[str, int]:
    """Return the live connections of the project, by reservation id. Each daemon of a
    connection writes its line, and removes it when it ends."""
    path = cli_state(settings) / _CONNECTIONS_FILE
    try:
        with open(path, "rb") as file:
            data = file.read(_MAX_CONNECTIONS_FILE)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RemoteError(f"cannot read {path}: {exc.strerror}") from exc
    return parse_connections(data.decode(errors="replace"), project)


def save_catalogue(kind: Kind, models: Sequence[Model], fetched: float) -> Path:
    path = _catalogue_path(kind)
    document = {
        "schema": FILE_SCHEMA,
        "project": kind.project,
        "fetched_at": fetched,
        "models": [
            {
                "codename": model.codename,
                "api": model.api,
                "manufacturer": model.manufacturer,
                "model": model.model,
            }
            for model in models
        ],
    }
    _write(path, document)
    return path


def load_catalogue(kind: Kind) -> tuple[list[Model], float]:
    """Return the models of the catalogue of a kind, and when it was fetched."""
    renew = f"run banksman admin discover --kind {kind.name}"
    path = _catalogue_path(kind)
    data = _read(path)
    if data is None:
        raise RemoteError(f"kind {kind.name} has no catalogue of models yet: {renew}")
    if (
        not isinstance(data, dict)
        or data.get("schema") != FILE_SCHEMA
        or not isinstance(data.get("models"), list)
        or not _is_time(data.get("fetched_at"))
    ):
        raise RemoteError(f"the catalogue of kind {kind.name} is not valid: {renew}")
    if data.get("project") != kind.project:
        raise RemoteError(f"the catalogue of kind {kind.name} is of another project: {renew}")
    models = []
    for entry in data["models"]:
        if not isinstance(entry, dict):
            continue
        codename, api = entry.get("codename"), entry.get("api")
        if (
            not isinstance(codename, str)
            or _CODENAME.fullmatch(codename) is None
            or not isinstance(api, int)
            or isinstance(api, bool)
            or not 0 < api < 10_000
        ):
            continue
        models.append(
            Model(codename, api, _text(entry.get("manufacturer")), _text(entry.get("model")))
        )
    return models, data["fetched_at"]


def load_sessions(kind: Kind) -> dict[str, Session]:
    """Return the reservations that banksman started or adopted for a kind, by id."""
    data = _read(_sessions_path(kind))
    if data is None:
        return {}
    if (
        not isinstance(data, dict)
        or data.get("schema") != FILE_SCHEMA
        or not isinstance(data.get("sessions"), list)
    ):
        raise RemoteError(f"{_sessions_path(kind)} is not valid")
    sessions = {}
    for entry in data["sessions"]:
        session = _session(entry)
        if session is not None:
            sessions[session.handle] = session
    return sessions


@contextmanager
def changing_sessions(
    kind: Kind, clock: Callable[[], float] = time.time
) -> Iterator[dict[str, Session]]:
    """Change the file of the reservations of a kind under its lock.

    Two starts of different models can run at the same time, and each writes the file. The
    records of reservations that have ended go.
    """
    directory = _directory()
    lock = directory / f".{kind.name}-sessions.lock"
    fd = _take_lock(lock)
    try:
        sessions = load_sessions(kind)
        yield sessions
        now = clock()
        kept = [session for session in sessions.values() if _kept_until(session) > now]
        _write(
            _sessions_path(kind),
            {"schema": FILE_SCHEMA, "sessions": [_session_json(each) for each in kept]},
        )
    finally:
        os.close(fd)


# Discovery.


def discover(
    kind: Kind, settings: Android, *, accounts: bool = True
) -> tuple[Document, list[str]]:
    """Find the models of a kind, and which of them have a live connection on this machine.

    Return the discover document, and the notes that agents may see: they name no instance.
    """
    models, fetched = load_catalogue(kind)
    shown = [
        f"kind {kind.name}: the catalogue of models was fetched {_age(time.time() - fetched)}"
        f" ago; banksman admin discover --kind {kind.name} renews it"
    ]
    notes: list[str] = []
    assert kind.project is not None
    try:
        connections = read_connections(settings, kind.project)
    except RemoteError as exc:
        notes.append(str(exc))
        connections = {}
    try:
        sessions = load_sessions(kind)
    except RemoteError as exc:
        notes.append(str(exc))
        sessions = {}
    try:
        live = {
            each.serial for each in android.transports(settings) if each.state == "device"
        }
    except android.AndroidError as exc:
        notes.append(f"cannot list the devices, so no remote device is shown as running: {exc}")
        live = None
    catalogue = {model.name: model for model in models}
    running: dict[str, tuple[str, str, Document]] = {}
    for handle, port in sorted(connections.items()):
        serial = f"localhost:{port}"
        # A connection whose daemon ended, or that adb dropped, is not live.
        if live is None or serial not in live:
            continue
        try:
            # The facts are what the device gives: the catalogue can name a model differently.
            facts = android.describe(settings, serial, form=None)
        except android.AndroidError as exc:
            notes.append(f"cannot read the properties of the remote device on {serial}: {exc}")
            facts = {}
        session = sessions.get(handle)
        name = session.resource if session is not None else None
        if name is None and "codename" in facts and "api" in facts:
            name = f"{facts['codename']}:{facts['api']}"
        # A device of a partner lab has a codename in the catalogue that is not a property of
        # the device, so only the record of a start that banksman performed maps it.
        if name is None or (name not in catalogue and session is None):
            notes.append(
                f"the remote device on {serial} (reservation {handle}) is not a model of the"
                " catalogue, so it is not offered"
            )
            continue
        if name in running:
            notes.append(f"{name} has two connections; only {running[name][0]} is used")
            continue
        running[name] = (serial, handle, facts)
    for name in running:
        if name not in catalogue:
            # banksman started it, and a newer catalogue no longer lists the model.
            catalogue[name] = model_of(name)
    instances = []
    for name, model in catalogue.items():
        facts: Document = {"codename": model.codename, "api": model.api}
        if model.manufacturer:
            facts["manufacturer"] = model.manufacturer
        if model.model:
            facts["model"] = model.model
            facts["form"] = form_of(model.model)
        if live is not None:
            facts["running"] = name in running
        instance: Document = {"name": name, "facts": facts}
        if name in running:
            serial, handle, found = running[name]
            # The codename and the API level name the model in the catalogue, so they stay.
            facts.update(
                {key: value for key, value in found.items() if key not in ("codename", "api")}
            )
            facts.update(serial=serial, handle=handle)
            session = sessions.get(handle)
            if session is not None and session.ends is not None:
                facts["ends"] = utc_text(session.ends)
            if accounts:
                instance.update(android.signed_in(settings, serial))
        instances.append(instance)
    return {"schema": android.DISCOVER_SCHEMA, "instances": instances, "notes": notes}, shown


# The tool: the catalogue, the reservations, and the lifecycle of a reservation.


def renew_catalogue(
    kind: Kind, settings: Android, *, run: Run | None = None, clock: Callable[[], float] = time.time
) -> int:
    """Fetch the models of the project with the tool, and save them as the catalogue of the
    kind. Return how many models it has."""
    run = run_cli if run is None else run
    answer = run(command(settings, kind, "models"), CLI_TIMEOUT_SECONDS, None)
    if answer.status != 0:
        raise RemoteError(_failure("models", answer))
    models = parse_models(answer.text)
    if not models:
        raise RemoteError("android device remote models listed no model")
    save_catalogue(kind, models, clock())
    return len(models)


def reservations(
    kind: Kind,
    settings: Android,
    *,
    run: Run | None = None,
    timeout: float = CLI_TIMEOUT_SECONDS,
) -> list[Reservation]:
    """Return the live reservations of the account in the project."""
    run = run_cli if run is None else run
    answer = run(command(settings, kind, "list"), timeout, None)
    if answer.status == 0:
        return parse_reservations(answer.text)
    # The tool exits with status 1 when there is no reservation.
    if answer.status is not None and _NO_RESERVATIONS in answer.text:
        return []
    raise RemoteError(_failure("list", answer))


def start(
    kind: Kind,
    settings: Android,
    resource: str,
    *,
    until: float,
    deadline: float,
    record: Callable[[str], None],
    run: Run | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Started:
    """Reserve the model of an instance, connect it, and extend the reservation to `until`.

    `deadline` is when the start must have ended. Both are wall-clock times. `record` gets the
    id of the reservation as soon as the tool prints it, so that a take-back can end it also
    when this command ends in the middle. When the service already has a reservation of the
    model for the account, the tool connects that one, and banksman adopts it: it does not end
    a reservation that it did not create. When a step after the id fails, a reservation that
    banksman created is removed.
    """
    run = run_cli if run is None else run
    model = model_of(resource)
    assert kind.project is not None

    def left() -> float:
        seconds = deadline - clock()
        if seconds <= 0:
            raise RemoteError(f"the start of {resource} did not end by its boot deadline")
        return seconds

    # A reservation that is live on this machine, or that the account already has, is not one
    # that banksman creates: the tool prints the same text when it adopts one.
    before = set(read_connections(settings, kind.project))
    listed = reservations(kind, settings, run=run, timeout=min(left(), CLI_TIMEOUT_SECONDS))
    before |= {each.handle for each in listed}
    known: list[Session] = []

    def remember(text: str) -> None:
        match = _CREATED.search(text)
        if known or match is None:
            return
        handle = match["handle"]
        with changing_sessions(kind, clock) as sessions:
            # A reservation that banksman created stays banksman's to end, also when a later
            # start adopts it, for example after a release and a lost connection.
            session = sessions.get(handle) or Session(
                handle, resource, created=handle not in before, at=clock()
            )
            sessions[handle] = session
        known.append(session)
        record(handle)

    create = command(settings, kind, "create", model.target, "--connect")
    try:
        answer = run(create, left(), remember)
        remember(answer.text)
        if answer.status is None:
            raise RemoteError(
                f"android device remote create did not end by the boot deadline of {resource}"
            )
        if answer.status != 0 or not known:
            raise RemoteError(_failure("create", answer))
        session = known[0]
        connected = _CONNECTED.search(answer.text)
        if connected is None or not _port(connected["port"]):
            raise RemoteError(
                f"android device remote create did not connect {resource}: {answer.first_line()}"
            )
        created = _CREATED.search(answer.text)
        assert created is not None
        ends = time_of_day(created["ends"], clock()) if created["ends"] else None
        # The service caps a reservation at 3 hours after its creation, without an error, so
        # banksman reads what it got. Without a known end, it asks for the time from now: the
        # cap makes a longer reservation harmless.
        minutes = math.ceil((until - (clock() if ends is None else ends)) / 60)
        if minutes > 0:
            extended = run(
                command(settings, kind, "extend", session.handle, f"--duration={minutes}"),
                min(left(), CLI_TIMEOUT_SECONDS),
                None,
            )
            match = _EXTENDED.search(extended.text)
            if extended.status != 0 or match is None:
                raise RemoteError(_failure("extend", extended))
            ends = time_of_day(match["ends"], clock())
        serial = f"localhost:{connected['port']}"
        with changing_sessions(kind, clock) as sessions:
            sessions[session.handle] = replace(session, port=int(connected["port"]), ends=ends)
        _wait_for_adb(settings, serial, min(deadline, clock() + _ADB_WAIT_SECONDS), clock, sleep)
    except BaseException as exc:
        if known and known[0].created:
            problem = _remove_quietly(kind, settings, known[0].handle, run, clock)
            if problem is not None and isinstance(exc, RemoteError):
                raise RemoteError(
                    f"{exc}; the reservation {known[0].handle} could not be removed ({problem}),"
                    " so it ends by itself, at the latest 3 hours after its creation"
                ) from None
        raise
    return Started(session.handle, serial, ends, session.created)


def connect(
    kind: Kind,
    settings: Android,
    handle: str,
    *,
    run: Run | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Connect the device of a reservation again, and return its new serial.

    The daemon of a connection ends when adb drops it, for example after `adb kill-server`, and
    after a restart of the machine. It does not connect again by itself.
    """
    run = run_cli if run is None else run
    answer = run(command(settings, kind, "connect", handle), CLI_TIMEOUT_SECONDS, None)
    match = _CONNECTED.search(answer.text) or _ALREADY.search(answer.text)
    if answer.status == 0 and match is not None and _port(match["port"]):
        port = int(match["port"])
        with changing_sessions(kind, clock) as sessions:
            if handle in sessions:
                sessions[handle] = replace(sessions[handle], port=port)
        serial = f"localhost:{port}"
        _wait_for_adb(settings, serial, clock() + _ADB_WAIT_SECONDS, clock, sleep)
        return serial
    if answer.status is not None and _GONE.search(answer.text):
        raise Ended(answer.first_line())
    raise RemoteError(_failure("connect", answer))


def remove(kind: Kind, settings: Android, handle: str, *, run: Run | None = None) -> None:
    """End a reservation. A reservation that has ended already counts as removed."""
    run = run_cli if run is None else run
    answer = run(command(settings, kind, "remove", handle), CLI_TIMEOUT_SECONDS, None)
    if answer.status == 0 and _CANCELLED in answer.text:
        return
    if answer.status is not None and _GONE.search(answer.text):
        return
    raise RemoteError(_failure("remove", answer))


def take_back(
    kind: Kind,
    settings: Android,
    handle: str,
    *,
    run: Run | None = None,
    clock: Callable[[], float] = time.time,
) -> str | None:
    """End the reservation of a void lease, when banksman created it.

    Return None when the reservation is gone or is not banksman's to end, or why that cannot be
    confirmed.
    """
    try:
        session = load_sessions(kind).get(handle)
    except RemoteError as exc:
        return str(exc)
    if session is None or not session.created:
        return None
    # Only the tool can confirm that a reservation has ended: the holder can extend it by hand,
    # so the recorded end can be too early. A reservation older than the longest one that the
    # service allows is gone without a call.
    if _kept_until(session) > clock():
        try:
            remove(kind, settings, handle, run=run)
        except BanksmanError as exc:
            return str(exc)
    with changing_sessions(kind, clock) as sessions:
        sessions.pop(handle, None)
    return None


def _remove_quietly(
    kind: Kind, settings: Android, handle: str, run: Run, clock: Callable[[], float]
) -> str | None:
    try:
        remove(kind, settings, handle, run=run)
        with changing_sessions(kind, clock) as sessions:
            sessions.pop(handle, None)
    except BanksmanError as exc:
        return str(exc)
    return None


def _wait_for_adb(
    settings: Android,
    serial: str,
    until: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    problem = "adb does not list it"
    while True:
        try:
            states = {each.serial: each.state for each in android.transports(settings)}
        except android.AndroidError as exc:
            problem = str(exc)
        else:
            if states.get(serial) == "device":
                return
            if serial in states:
                problem = f"adb lists it as {states[serial]}"
        if clock() >= until:
            raise RemoteError(f"the remote device on {serial} is not ready: {problem}")
        sleep(_ADB_POLL_SECONDS)


def _failure(name: str, answer: Answer) -> str:
    if answer.status is None:
        return f"android device remote {name} did not end in time"
    line = answer.first_line()
    if not line:
        return f"android device remote {name} failed with exit status {answer.status}"
    return f"android device remote {name} failed: {line}"


def _lines(text: str) -> list[str]:
    # A progress line ends with a carriage return, and the next text overwrites it.
    return [line.rsplit("\r", 1)[-1] for line in text.replace("\r\n", "\n").split("\n")]


def _short(text: str) -> str:
    text = clean(" ".join(text.split()))
    return text if len(text) <= _MAX_LINE else text[: _MAX_LINE - 3] + "..."


def _port(text: str) -> bool:
    return 0 < int(text) < 65536


def _age(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 1:
        return "less than a minute"
    if minutes < 120:
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours"
    return f"{hours // 24} days"


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 128 or clean(value) != value:
        return None
    return value


def _is_time(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _session(entry: object) -> Session | None:
    if not isinstance(entry, dict):
        return None
    handle, resource = entry.get("handle"), entry.get("resource")
    port, ends, created, at = (
        entry.get("port"),
        entry.get("ends"),
        entry.get("created"),
        entry.get("at"),
    )
    if (
        not isinstance(handle, str)
        or re.fullmatch(_HANDLE, handle) is None
        or not isinstance(resource, str)
        or RESOURCE_NAME.fullmatch(resource) is None
        or not isinstance(created, bool)
        or not _is_time(at)
        or not (port is None or (isinstance(port, int) and not isinstance(port, bool)))
        or not (port is None or 0 < port < 65536)
        or not (ends is None or _is_time(ends))
    ):
        return None
    return Session(handle, resource, created, at, port, ends)


def _kept_until(session: Session) -> float:
    """Return when the record of a reservation can go: when the reservation has surely ended,
    whatever its holder extended by hand."""
    return session.at + MAX_RESERVATION_SECONDS + 60


def _session_json(session: Session) -> dict[str, object]:
    return {
        "handle": session.handle,
        "resource": session.resource,
        "created": session.created,
        "at": session.at,
        "port": session.port,
        "ends": session.ends,
    }


def _catalogue_path(kind: Kind) -> Path:
    return default_remote_dir() / f"{kind.name}-catalogue.json"


def _sessions_path(kind: Kind) -> Path:
    return default_remote_dir() / f"{kind.name}-sessions.json"


def _directory() -> Path:
    directory = default_remote_dir()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise RemoteError(f"cannot create {directory}: {exc.strerror}") from exc
    _check_owner(directory, os.lstat(directory), directory=True)
    return directory


def _check_owner(path: Path, info: os.stat_result, *, directory: bool = False) -> None:
    # The file of the reservations decides which reservations the reaper ends, so a file that
    # another user can change could make banksman end that user's choice of reservations.
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok:
        raise RemoteError(f"{path} is not a {'directory' if directory else 'regular file'}")
    if info.st_uid != os.geteuid():
        raise RemoteError(f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RemoteError(f"group or others can write to {path}")


def _read(path: Path) -> object | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RemoteError(f"cannot open {path}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as file:
        _check_owner(path, os.fstat(file.fileno()))
        data = file.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE:
        raise RemoteError(f"{path} is larger than {_MAX_FILE} bytes")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise RemoteError(f"{path} is not valid JSON") from None


def _write(path: Path, document: Mapping[str, object]) -> None:
    directory = _directory()
    data = (json.dumps(document, indent=2) + "\n").encode()
    try:
        fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    except OSError as exc:
        raise RemoteError(f"cannot write {path}: {exc.strerror}") from exc
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        # rename is atomic: a reader sees the old file or the new one.
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


def _take_lock(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise RemoteError(f"cannot open {path}: {exc.strerror}") from exc
    try:
        _check_owner(path, os.fstat(fd))
        give_up = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                # The lock is held for milliseconds, but not while its holder is stopped.
                if time.monotonic() >= give_up:
                    raise RemoteError(
                        f"another banksman process has held {path} for more than"
                        f" {_LOCK_WAIT_SECONDS:g} s; it can be stopped or hung"
                    ) from None
                time.sleep(0.01)
    except BaseException:
        os.close(fd)
        raise
