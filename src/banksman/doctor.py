"""The checks of an install, for `banksman admin doctor`.

Every file that banksman prints for the operator to save drifts after an upgrade. The doctor
compares each saved copy with what this banksman prints, and checks the configuration, the
inventory, the Android SDK, the PATH, and the directories of banksman.

It changes nothing: it does not reap, it creates no directory, and it writes no log and no
trace. It reads the lease files as the adb guard does, and it runs discovery without the
accounts, `adb version`, `adb devices -l`, and `launchctl print` or `crontab -l` for the reap
job. It never calls the network.
"""

from __future__ import annotations

import os
import platform
import pwd
import shlex
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from banksman import __version__, android, emulator, job, printed
from banksman.config import (
    ANDROID_EMULATOR,
    ANDROID_REMOTE,
    PRESETS,
    Config,
    HolderRules,
    Kind,
    default_config_path,
    load_config,
)
from banksman.discovery import discover
from banksman.errors import BanksmanError
from banksman.identity import find_holder
from banksman.inventory import Decisions, Inventory, default_inventory_path, load_inventory
from banksman.lease import QUARANTINED
from banksman.sanitize import clean
from banksman.store import (
    default_ended_path,
    default_log_path,
    default_quarantine_dir,
    default_state_dir,
    default_trace_path,
    peek,
)
from banksman.system import Machine

OK = "ok"
WARN = "warn"
FAIL = "fail"
# The check does not apply, for example to an optional part that the operator did not install.
SKIP = "skip"
_LABELS = {OK: "ok", WARN: "warn", FAIL: "FAIL", SKIP: "skip"}

# The places in the home directory where the setup in the documentation saves the files.
ADB_WRAPPER = Path(".local/share/banksman/bin/adb")
GRADLE_INIT = Path(".gradle/init.d/banksman-build-slot.gradle")
GRADLE_BUILD_SLOT = Path(".gradle/banksman/build-slot.gradle")
SKILLS = {
    "claude": Path(".claude/skills/banksman/SKILL.md"),
    "codex": Path(".codex/skills/banksman/SKILL.md"),
}
# The kind that the build-slot part asks for.
BUILD_KIND = "build"
_MAX_FILE = 1024 * 1024
_SHOWN = 5
_NOTES = 3
_NO_CONFIG = "the configuration does not load"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    # What repairs a failure or a warning, often a command.
    fix: str | None = None


@dataclass(frozen=True)
class Shell:
    """What the doctor needs to know about the shell that runs it."""

    home: Path
    # The value of PATH.
    path: str
    # How the shell started banksman: sys.argv[0].
    program: str


class _Unreadable(Exception):
    pass


def home_directory() -> Path:
    # From the user database, as the configuration: every caller of one user finds the same
    # files.
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        raise BanksmanError(f"cannot find the home directory of user {os.geteuid()}") from None


def run(shell: Shell) -> list[Check]:
    checks = [_python(), _guarded("banksman", _banksman, shell)]
    config_check, config, config_path = _config()
    checks.append(config_check)
    if config is None:
        checks.append(Check("inventory", SKIP, _NO_CONFIG))
    else:
        inventory_check, inventory = _inventory(config)
        checks.append(inventory_check)
        if inventory is not None:
            checks += [
                _guarded(f"kind:{kind.name}", _kind, kind, config, inventory)
                for kind in config.kinds.values()
                if kind.discovered
            ]
    path_check, wrapper = _adb_on_path(shell, config)
    return [
        *checks,
        _guarded("android-sdk", _android, config, config_path, wrapper),
        path_check,
        _guarded("adb-wrapper", _adb_wrapper, wrapper, config),
        *_gradle(shell.home, config, config_path),
        *_skills(shell.home),
        _guarded("reap-job", _reap_job, shell, config),
        _guarded("state-dir", _place, "state-dir", default_state_dir),
        _guarded("quarantine-dir", _place, "quarantine-dir", default_quarantine_dir),
        _guarded("log", _place, "log", default_log_path, False),
        _guarded("lease-ends", _place, "lease-ends", default_ended_path, False),
        _guarded("trace", _trace, config, config_path),
        _guarded("quarantine", _quarantine),
        _guarded("holder", _holder, config),
    ]


def failed(checks: Sequence[Check]) -> bool:
    return any(check.status == FAIL for check in checks)


def report(checks: Sequence[Check]) -> str:
    width = max(len(check.name) for check in checks)
    lines = []
    for check in checks:
        # The details name paths and quote errors and the output of programs.
        lines.append(f"{_LABELS[check.status]:<4}  {check.name:<{width}}  {clean(check.detail)}")
        if check.fix is not None:
            lines.append(f"{'':<4}  {'':<{width}}  fix: {clean(check.fix)}")
    counts = {status: sum(check.status == status for check in checks) for status in _LABELS}
    lines.append("")
    lines.append(f"Failures: {counts[FAIL]}. Warnings: {counts[WARN]}.")
    return "\n".join(lines)


def _guarded(name: str, check: Callable[..., Check], *arguments: object) -> Check:
    # One check that cannot run must not hide the others.
    try:
        return check(*arguments)
    except (BanksmanError, OSError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        return Check(name, FAIL, str(reason))


def _python() -> Check:
    return Check("python", OK, f"Python {platform.python_version()} at {sys.executable}")


def _banksman(shell: Shell) -> Check:
    installed = f"banksman {__version__} from {Path(__file__).resolve().parent}"
    # Only absolute directories, as the adb wrapper: the wrapper and the Gradle scripts run the
    # banksman of the PATH.
    on_path = next(iter(_on_path(shell.path, "banksman", absolute=True)), None)
    if on_path is None:
        return Check(
            "banksman",
            WARN,
            f"{installed}; banksman is not on the PATH, so the adb wrapper and the Gradle"
            " scripts cannot run it",
            "put the directory of banksman on the PATH",
        )
    program = Path(shell.program)
    if program.name == "banksman" and os.path.realpath(program) != os.path.realpath(on_path):
        return Check(
            "banksman",
            WARN,
            f"{installed}; the banksman first on the PATH is {on_path}, not {program}, so the"
            " adb wrapper, the Gradle scripts, and agents can run another install",
        )
    return Check("banksman", OK, f"{installed}; first on the PATH: {on_path}")


def _config() -> tuple[Check, Config | None, Path | None]:
    try:
        path = default_config_path()
        config = load_config(path)
    except BanksmanError as exc:
        return Check("config", FAIL, str(exc)), None, None
    if not os.path.lexists(path):
        return Check("config", WARN, f"{path} does not exist, so no kind is declared"), config, path
    if not config.kinds:
        return Check("config", WARN, f"{path} declares no kind"), config, path
    return Check("config", OK, f"{path}: kinds {_some(list(config.kinds))}"), config, path


def _inventory(config: Config) -> tuple[Check, Inventory | None]:
    discovered = [kind.name for kind in config.kinds.values() if kind.discovered]
    if not discovered:
        return Check("inventory", SKIP, "no kind of the configuration is discovered"), None
    try:
        path = default_inventory_path()
        inventory = load_inventory(path)
    except BanksmanError as exc:
        return Check("inventory", FAIL, str(exc)), None
    if not os.path.lexists(path):
        # Without an inventory, agents may use no discovered instance.
        return (
            Check(
                "inventory",
                FAIL,
                f"{path} does not exist, so agents can get no instance of the kinds"
                f" {_some(discovered)}",
                "banksman admin discover",
            ),
            inventory,
        )
    instances = sum(len(decisions.allowed) for decisions in inventory.kinds.values())
    accounts = len(inventory.accounts.allowed)
    return (
        Check(
            "inventory", OK, f"{path}: {instances} allowed instances, {accounts} allowed accounts"
        ),
        inventory,
    )


def _kind(kind: Kind, config: Config, inventory: Inventory) -> Check:
    name = f"kind:{kind.name}"
    found = discover(kind, config, accounts=False)
    if found.failure is not None:
        # The discovery of a remote kind fails when its catalogue of models is missing or not
        # valid, and the notes for agents do not say how to fetch it.
        fix = None
        if kind.preset == ANDROID_REMOTE:
            fix = f"banksman admin discover --kind {kind.name}"
        return Check(name, WARN, f"discovery failed: {found.failure}", fix)
    names = {instance.name for instance in found.instances}
    allowed = inventory.kinds.get(kind.name, Decisions()).allowed
    # The notes are text, not a warning: many of them name a normal state, such as a personal
    # phone that is not authorized. The doctor is for the operator, who may see them all.
    notes = "".join(f"; note: {note}" for note in found.notes[:_NOTES])
    if len(found.notes) > _NOTES:
        notes += f"; and {len(found.notes) - _NOTES} more notes"
    if not allowed:
        return Check(
            name,
            WARN,
            f"no instance is allowed; discovery finds {len(names)} now{notes}",
            f"banksman admin discover --kind {kind.name}",
        )
    present = [each for each in allowed if each in names]
    listed = f": {_some(present)}" if present else ""
    return Check(
        name, OK, f"discovery finds {len(present)} of {len(allowed)} allowed{listed}{notes}"
    )


def _android(config: Config | None, config_path: Path | None, wrapper: Path | None) -> Check:
    if config is None:
        return Check("android-sdk", SKIP, _NO_CONFIG)
    presets = {kind.preset for kind in config.kinds.values() if kind.preset in PRESETS}
    if not presets and config.android.sdk is None and wrapper is None:
        return Check(
            "android-sdk", SKIP, "no kind uses an Android preset, and no adb wrapper is saved"
        )
    fix = f"set sdk in [android] of {config_path} to the Android SDK"
    adb = android.adb_path(config.android)
    if not os.path.isfile(adb):
        return Check("android-sdk", FAIL, f"{adb} does not exist", fix)
    try:
        said = android.version(config.android)
    except android.AndroidError as exc:
        return Check("android-sdk", FAIL, str(exc), fix)
    program = emulator.program(config.android)
    if ANDROID_EMULATOR in presets and not program.is_file():
        return Check(
            "android-sdk",
            FAIL,
            f"{program} does not exist, so acquire --start cannot start an emulator",
            fix,
        )
    if presets:
        # Discovery only notes a listing that fails, and then finds no device, which looks like
        # a machine without devices.
        try:
            listed = android.transports(config.android)
        except android.AndroidError as exc:
            return Check(
                "android-sdk",
                FAIL,
                f"{adb} works, but it cannot list the devices, so discovery finds none: {exc}",
                "run adb devices to see why, for example an adb server that does not start",
            )
        said += f"; it lists {len(listed)} devices and emulators"
    return Check("android-sdk", OK, f"{adb}: {said}")


def _adb_on_path(shell: Shell, config: Config | None) -> tuple[Check, Path | None]:
    """Check that the adb that the shell runs is the adb wrapper. Return the wrapper too."""
    name = "adb-path"
    found = _on_path(shell.path, "adb", executable=False)
    runs = next((each for each in found if os.access(each, os.X_OK)), None)
    wrapper = next((each for each in found if _is_wrapper(each)), None)
    if wrapper is not None:
        return _wrapper_on_path(name, wrapper, runs), wrapper
    saved = shell.home / ADB_WRAPPER
    # A file there is meant as the wrapper, also when it is not one: the redirection of a save
    # empties the file before banksman runs, so a save that failed leaves an empty file, and a
    # shell runs an empty file as a script that does nothing.
    if os.path.lexists(saved):
        listed = next((each for each in found if _same_file(each, saved)), None)
        if listed is not None:
            return _wrapper_on_path(name, listed, runs), listed
        return (
            Check(
                name,
                WARN,
                f"the adb wrapper {saved} is not on the PATH of this shell, so adb calls here"
                " skip the guard",
                f"put {saved.parent} first on the PATH of the agents",
            ),
            saved,
        )
    if config is not None and config.guard.strict is not None:
        return (
            Check(
                name,
                WARN,
                "[guard] sets strict, but no adb on the PATH is the adb wrapper, so the guard"
                " checks no adb call",
                "save the adb wrapper as banksman admin adb-shim describes, first on the PATH",
            ),
            None,
        )
    return Check(
        name, SKIP, "the adb guard is not installed: no adb on the PATH is the wrapper"
    ), None


def _wrapper_on_path(name: str, wrapper: Path, runs: Path | None) -> Check:
    if wrapper == runs:
        return Check(name, OK, f"the adb first on the PATH is the adb wrapper {wrapper}")
    if runs is None or not os.access(wrapper, os.X_OK):
        return Check(
            name,
            FAIL,
            f"the adb wrapper {wrapper} is not executable, so adb calls skip the guard",
            f"chmod +x {_quoted(wrapper)}",
        )
    return Check(
        name,
        FAIL,
        f"{runs} comes before the adb wrapper {wrapper} on the PATH, so adb calls skip the guard",
        f"put {wrapper.parent} on the PATH before {runs.parent}",
    )


def _adb_wrapper(wrapper: Path | None, config: Config | None) -> Check:
    name = "adb-wrapper"
    if wrapper is None:
        return Check(name, SKIP, "no adb wrapper is saved")
    fix = f"banksman admin adb-shim > {_quoted(wrapper)}"
    try:
        text = _read(wrapper)
    except _Unreadable as exc:
        return Check(name, FAIL, f"cannot read {wrapper}: {exc}", fix)
    if not text:
        return Check(
            name, FAIL, f"{wrapper} is empty, so each adb call through it does nothing", fix
        )
    adb = printed.saved_adb(text)
    if adb is None:
        return Check(name, FAIL, _differs(wrapper), fix)
    if config is not None and adb != (expected := android.adb_path(config.android)):
        return Check(
            name, FAIL, f"{wrapper} runs {adb}, but the adb of the configuration is {expected}", fix
        )
    return Check(name, OK, f"{wrapper} is what this banksman prints, and runs {adb}")


def _gradle(home: Path, config: Config | None, config_path: Path | None) -> list[Check]:
    init, part = home / GRADLE_INIT, home / GRADLE_BUILD_SLOT
    init_fix = f"banksman admin gradle-init > {_quoted(init)}"
    part_fix = (
        f"mkdir -p {_quoted(part.parent)} &&"
        f" banksman admin gradle-init --build-slot > {_quoted(part)}"
    )
    # Build slots are optional, but one file without the other is a setup that does not work.
    if os.path.lexists(part):
        no_init = Check(
            "gradle-init",
            WARN,
            f"{part} is saved, but {init} is not, so builds hold no build slot",
            init_fix,
        )
    else:
        no_init = Check(
            "gradle-init", SKIP, f"build slots are not installed: {init} does not exist"
        )
    if os.path.lexists(init):
        no_part = Check(
            "gradle-build-slot",
            FAIL,
            f"every build on Gradle 7.4 and later fails: {init} applies {part}, which does not"
            " exist",
            part_fix,
        )
    else:
        no_part = Check(
            "gradle-build-slot", SKIP, f"build slots are not installed: {part} does not exist"
        )
    init_check = _saved("gradle-init", init, printed.gradle_init(), init_fix, no_init)
    if init_check.status == OK and config is not None and BUILD_KIND not in config.kinds:
        init_check = Check(
            "gradle-init",
            FAIL,
            f"every Gradle build fails: {init} asks for a slot of the kind {BUILD_KIND}, and the"
            " configuration does not declare it",
            f"declare [kinds.{BUILD_KIND}] in {config_path}, or remove {init}",
        )
    return [init_check, _saved("gradle-build-slot", part, printed.build_slot(), part_fix, no_part)]


def _skills(home: Path) -> list[Check]:
    checks = []
    for agent, relative in SKILLS.items():
        name, path = f"skill:{agent}", home / relative
        checks.append(
            _saved(
                name,
                path,
                printed.agent_guide(),
                f"banksman agent-guide > {_quoted(path)}",
                Check(name, SKIP, f"{path} does not exist"),
            )
        )
    return checks


def _saved(name: str, path: Path, expected: str, fix: str, missing: Check) -> Check:
    try:
        text = _read(path)
    except _Unreadable as exc:
        return Check(name, FAIL, f"cannot read {path}: {exc}", fix)
    if text is None:
        return missing
    if text != expected:
        return Check(name, FAIL, _differs(path), fix)
    return Check(name, OK, f"{path} is what this banksman prints")


def _differs(path: Path) -> str:
    return f"{path} is not what this banksman prints, for example a copy from an earlier version"


def _reap_job(shell: Shell, config: Config | None) -> Check:
    name = "reap-job"
    if job.LAUNCHD:
        path = shell.home / job.LAUNCH_AGENT
        domain = f"gui/{os.getuid()}"
        load = f"launchctl bootstrap {domain} {_quoted(path)}"
        # The directory exists only after a program has saved a job of its own there.
        save = f"mkdir -p {_quoted(path.parent)} && banksman admin reap-agent > {_quoted(path)}"
        fix = f"{save} && {load}"
        try:
            text = _read(path)
        except _Unreadable as exc:
            return Check(name, FAIL, f"cannot read {path}: {exc}", fix)
        if text is None:
            return _no_reap_job(name, f"{path} does not exist", fix, config)
        # launchd keeps a loaded job until it is unloaded, also when the file changes.
        fix = f"launchctl bootout {domain}/{job.LABEL}; {fix}"
        command = job.saved_launchd(text)
        if command is None:
            return Check(name, FAIL, _differs(path), fix)
        where = str(path)
    else:
        fix = "add the line that banksman admin reap-agent prints with crontab -e"
        try:
            table = job.crontab()
        except job.JobError as exc:
            return _no_reap_job(name, f"cannot read the crontab: {exc}", fix, config)
        command, found = job.saved_cron(table)
        if command is None:
            if found:
                return Check(
                    name,
                    FAIL,
                    "the line of the crontab that runs banksman reap is not what this banksman"
                    " prints",
                    "replace it with the line that banksman admin reap-agent prints",
                )
            return _no_reap_job(name, "no line of the crontab runs banksman reap", fix, config)
        where = "the crontab"
    runs = shlex.join([*command, "reap"])
    if not (os.path.isfile(command[0]) and os.access(command[0], os.X_OK)):
        return Check(
            name, FAIL, f"{where} runs {runs}, but {command[0]} is not an executable file", fix
        )
    current = job.program(shell.program)
    if _real(command) != _real(current):
        return Check(
            name,
            WARN,
            f"{where} runs {runs}, but this banksman is {shlex.join(current)}, so the job can run"
            " another install",
            fix,
        )
    if job.LAUNCHD and not job.loaded():
        return Check(
            name,
            WARN,
            f"{where} is what this banksman prints, but launchd has not loaded it, so it does not"
            " run before the next login",
            load,
        )
    return Check(name, OK, f"{where} runs {runs} every 5 minutes")


def _no_reap_job(name: str, detail: str, fix: str, config: Config | None) -> Check:
    kinds = [] if config is None else list(config.kinds.values())
    unused = [kind.name for kind in kinds if kind.unused_timeout is not None]
    free = [kind.name for kind in kinds if kind.free_timeout is not None]
    effects = []
    if unused:
        effects.append(f"the unused timeout of the kinds {_some(unused)} ends no emulator")
    if free:
        effects.append(
            f"the free timeout of the kinds {_some(free)} ends an emulator only when a banksman"
            " command runs"
        )
    if not effects:
        return Check(name, SKIP, f"the reap job is not installed: {detail}")
    return Check(name, WARN, f"{detail}, so {', and '.join(effects)}", fix)


def _real(command: Sequence[str]) -> list[str]:
    return [os.path.realpath(command[0]), *command[1:]]


def _place(name: str, where: Callable[[], Path], directory: bool = True) -> Check:
    path = where()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        parent = path.parent
        while not os.path.lexists(parent) and parent != parent.parent:
            parent = parent.parent
        if os.access(parent, os.W_OK | os.X_OK):
            return Check(name, OK, f"{path} does not exist yet, and banksman can create it")
        return Check(
            name, FAIL, f"{path} does not exist, and this user cannot create it in {parent}"
        )
    what = "directory" if directory else "regular file"
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        return Check(name, FAIL, f"{path} is not a {what}")
    if info.st_uid != os.geteuid():
        return Check(name, FAIL, f"{path} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return Check(
            name, FAIL, f"group or others can write to {path}", f"chmod go-w {_quoted(path)}"
        )
    if not os.access(path, os.W_OK | (os.X_OK if directory else 0)):
        return Check(name, FAIL, f"this user cannot write to {path}", f"chmod u+w {_quoted(path)}")
    return Check(name, OK, str(path))


def _trace(config: Config | None, config_path: Path | None) -> Check:
    name = "trace"
    if config is None:
        return Check(name, SKIP, _NO_CONFIG)
    if not config.log.trace:
        return Check(name, OK, "the trace is off")
    off = f"after the test, set trace = false in [log] of {config_path}"
    place = _place(name, default_trace_path, False)
    if place.status == FAIL:
        return Check(name, FAIL, f"the trace is on, but {place.detail}", place.fix or off)
    # An operator turns the trace on for a test, and must not leave it on by accident: it keeps
    # what the log keeps out on purpose.
    return Check(
        name,
        WARN,
        f"the trace is on: {default_trace_path()} gets a line for every banksman command and every"
        " adb call through the guard, with full adb command lines and the purposes of other"
        " agents",
        off,
    )


def _quarantine() -> Check:
    snapshot = peek(default_state_dir(), default_quarantine_dir())
    quarantined = [lease.resource for lease in snapshot.leases if lease.state == QUARANTINED]
    unreadable = [f"{each.resource} ({each.error})" for each in snapshot.unreadable]
    problems = []
    if quarantined:
        problems.append(f"quarantined: {_some(quarantined)}; banksman status shows why")
    if unreadable:
        problems.append(f"lease files that cannot be read: {_some(unreadable)}")
    if problems:
        return Check(
            "quarantine",
            WARN,
            "; ".join(problems),
            "when the scripts of the resource have ended:"
            " banksman admin release --force --resource <resource>",
        )
    return Check("quarantine", OK, "no lease is quarantined")


def _holder(config: Config | None) -> Check:
    rules = HolderRules() if config is None else config.holder
    holder = find_holder(Machine().process_table(), rules)
    agent = "none" if holder.agent is None else f"{holder.agent} (pid {holder.owner_pid})"
    return Check(
        "holder", OK, f"owner {holder.owner}; agent {agent}; issue {holder.issue or 'none'}"
    )


def _on_path(
    search_path: str, program: str, *, absolute: bool = False, executable: bool = True
) -> list[Path]:
    found = []
    for directory in search_path.split(os.pathsep):
        if absolute and not directory.startswith("/"):
            continue
        # An empty entry is the working directory, as for the shell.
        candidate = Path(directory or ".") / program
        if candidate.is_file() and (not executable or os.access(candidate, os.X_OK)):
            found.append(candidate)
    return found


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _is_wrapper(path: Path) -> bool:
    try:
        head = _read_bytes(path, 512)
    except _Unreadable:
        return False
    marker = printed.adb_shim_marker().encode()
    return head is not None and marker in head.splitlines()[:3]


def _read(path: Path) -> str | None:
    """Return the text of a saved file, or None when it does not exist."""
    raw = _read_bytes(path, _MAX_FILE)
    try:
        return None if raw is None else raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unreadable("it is not UTF-8 text") from None


def _read_bytes(path: Path, limit: int) -> bytes | None:
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Unreadable(exc.strerror) from exc
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise _Unreadable("it is not a regular file")
        return file.read(limit)


def _some(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_SHOWN])
    return shown if len(names) <= _SHOWN else f"{shown}, and {len(names) - _SHOWN} more"


def _quoted(path: Path) -> str:
    return shlex.quote(str(path))

