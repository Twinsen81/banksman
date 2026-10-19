"""banksman admin doctor, with a temporary home directory, a fake SDK, and a PATH of the test."""

import json
import os
import sys

import pytest
from helpers import FakeAndroidCli, drain, edit_lease, hook, write_config

from banksman import doctor, job, printed
from banksman.cli import main
from banksman.inventory import Decisions, Inventory, save_inventory
from banksman.lease import Holder
from banksman.store import Store
from banksman.system import Machine

FAKE_ADB = """#!/bin/sh
case $1 in
version)
    echo "Android Debug Bridge version 1.0.41"
    echo "Version 99.0.0-test"
    echo "Installed as $0"
    ;;
devices) echo "List of devices attached" ;;
*) exit 1 ;;
esac
"""
# An adb whose server does not start: only adb version works.
ADB_WITHOUT_SERVER = """#!/bin/sh
[ "$1" = version ] && echo "Android Debug Bridge version 1.0.41" && exit 0
exit 1
"""
PRINT_ARGUMENT = "import sys; print(sys.argv[1])"
LAB = {"schema": 1, "instances": [{"name": "rack-1"}, {"name": "rack-2"}]}
BUILD = '[kinds.build]\ncount = 2\nowner_grace = "0s"\nidle_timeout = "off"\n'


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    # No test may read the files that the operator saved in the real home directory.
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(doctor, "home_directory", lambda: path)
    return path


class FakeJobs:
    """launchctl and crontab, as the test sets them: no job is loaded, and no crontab exists."""

    def __init__(self) -> None:
        self.loaded = False
        self.crontab: str | None = None
        self.calls: list[list[str]] = []

    def __call__(self, command):
        self.calls.append(list(command))
        if command[0] == "crontab":
            return (1, "") if self.crontab is None else (0, self.crontab)
        return (0 if self.loaded else 113, "")


@pytest.fixture(autouse=True)
def jobs(monkeypatch):
    # No test may read the jobs of the real user. The tests run as on macOS unless they say so.
    fake = FakeJobs()
    monkeypatch.setattr(job, "run_program", fake)
    monkeypatch.setattr(job, "LAUNCHD", True)
    return fake


@pytest.fixture(autouse=True)
def banksman_bin(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    program = directory / "banksman"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    set_path(monkeypatch, directory)
    return directory


@pytest.fixture
def sdk(tmp_path):
    root = tmp_path / "sdk"
    adb = root / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text(FAKE_ADB)
    adb.chmod(0o755)
    return root


def set_path(monkeypatch, *directories):
    # The system directories stay, for ps.
    monkeypatch.setenv("PATH", ":".join([*map(str, directories), "/usr/bin", "/bin"]))


def configure(config_path, text="", sdk=None):
    android = "" if sdk is None else f'[android]\nsdk = "{sdk}"\n'
    write_config(config_path, f"[holder]\nagents = []\n{android}{text}")


def lab_kind(document=LAB):
    return f"[kinds.lab]\ndiscover = {json.dumps(hook(PRINT_ARGUMENT, json.dumps(document)))}\n"


def save_wrapper(directory, adb):
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / "adb"
    wrapper.write_text(printed.adb_shim(str(adb)))
    wrapper.chmod(0o755)
    return wrapper


def save(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def checks(capsys, status=0):
    assert main(["admin", "doctor", "--json"]) == status
    return {check["check"]: check for check in json.loads(capsys.readouterr().out)["checks"]}


def statuses(found):
    return {name: check["status"] for name, check in found.items()}


def test_a_complete_setup_passes(
    capsys, config_path, sdk, home, tmp_path, monkeypatch, banksman_bin
):
    configure(config_path, BUILD + lab_kind(), sdk=sdk)
    save_inventory(Inventory(kinds={"lab": Decisions(allowed=("rack-1", "rack-3"))}))
    wrapper = save_wrapper(tmp_path / "guard", sdk / "platform-tools" / "adb")
    set_path(monkeypatch, wrapper.parent, sdk / "platform-tools", banksman_bin)
    save(home / doctor.GRADLE_INIT, printed.gradle_init())
    save(home / doctor.GRADLE_BUILD_SLOT, printed.build_slot())
    save(home / doctor.SKILLS["claude"], printed.agent_guide())

    found = checks(capsys)
    assert statuses(found) == {
        "python": "ok",
        "banksman": "ok",
        "config": "ok",
        "inventory": "ok",
        "kind:lab": "ok",
        "android-sdk": "ok",
        "adb-path": "ok",
        "adb-wrapper": "ok",
        "gradle-init": "ok",
        "gradle-build-slot": "ok",
        "skill:claude": "ok",
        "skill:codex": "skip",
        "state-dir": "ok",
        "quarantine-dir": "ok",
        "log": "ok",
        "lease-ends": "ok",
        "trace": "ok",
        "reap-job": "skip",
        "quarantine": "ok",
        "holder": "ok",
    }
    assert found["trace"]["detail"] == "the trace is off"
    assert found["kind:lab"]["detail"] == "discovery finds 1 of 2 allowed: rack-1"
    assert found["android-sdk"]["detail"].endswith(
        "Android Debug Bridge version 1.0.41, Version 99.0.0-test"
    )
    assert all(check["fix"] is None for check in found.values())


def test_the_doctor_changes_nothing(
    capsys, state_dir, quarantine_dir, log_path, ended_path, trace_path
):
    found = checks(capsys)
    assert found["config"]["status"] == "warn"
    # Without a configuration, nothing optional is installed, and nothing fails.
    assert {status for status in statuses(found).values()} == {"ok", "warn", "skip"}
    for path in (state_dir, quarantine_dir, log_path, ended_path, trace_path):
        assert not path.exists()


def test_a_trace_that_is_on_is_a_warning(capsys, config_path, trace_path):
    configure(config_path, "[log]\ntrace = true\n")
    found = checks(capsys)["trace"]
    assert found["status"] == "warn"
    assert found["detail"].startswith(f"the trace is on: {trace_path} gets a line for every")
    assert found["fix"] == f"after the test, set trace = false in [log] of {config_path}"
    # The doctor itself adds no line.
    assert not trace_path.exists()


def test_a_trace_that_is_on_and_cannot_be_written_fails(capsys, config_path, trace_path):
    configure(config_path, "[log]\ntrace = true\n")
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text("")
    trace_path.chmod(0o666)
    found = checks(capsys, status=1)["trace"]
    assert found["status"] == "fail"
    assert found["detail"] == f"the trace is on, but group or others can write to {trace_path}"
    assert found["fix"] == f"chmod go-w {trace_path}"


def test_the_text_shows_each_check_its_fix_and_the_counts(capsys, home, config_path):
    save(home / doctor.SKILLS["codex"], "an old guide\n")
    assert main(["admin", "doctor"]) == 1
    lines = capsys.readouterr().out.splitlines()
    skill = next(index for index, line in enumerate(lines) if "skill:codex" in line)
    assert lines[skill].split()[:2] == ["FAIL", "skill:codex"]
    path = home / doctor.SKILLS["codex"]
    assert lines[skill + 1].split() == ["fix:", "banksman", "agent-guide", ">", str(path)]
    assert lines[-1] == "Failures: 1. Warnings: 1."


def test_a_stale_skill_file_fails(capsys, home):
    guide = printed.agent_guide()
    save(home / doctor.SKILLS["codex"], guide.replace("banksman", "banksman (an older one)", 1))
    found = checks(capsys, status=1)
    assert found["skill:codex"]["status"] == "fail"
    assert "is not what this banksman prints" in found["skill:codex"]["detail"]
    assert found["skill:claude"]["status"] == "skip"


def test_a_stale_wrapper_fails(capsys, config_path, sdk, tmp_path, monkeypatch, banksman_bin):
    configure(config_path, sdk=sdk)
    wrapper = save_wrapper(tmp_path / "guard", sdk / "platform-tools" / "adb")
    # A wrapper of an earlier version, which did not stop a loop through the adb of the SDK.
    text = wrapper.read_text()
    start, end = text.index('if [ -n "${BANKSMAN_GUARD:-}" ]'), text.index("# Not `command -v`")
    wrapper.write_text(text[:start] + text[end:])
    set_path(monkeypatch, wrapper.parent, banksman_bin)

    found = checks(capsys, status=1)
    assert found["adb-path"]["status"] == "ok"
    assert found["adb-wrapper"] == {
        "check": "adb-wrapper",
        "status": "fail",
        "detail": f"{wrapper} is not what this banksman prints, for example a copy from an"
        " earlier version",
        "fix": f"banksman admin adb-shim > {wrapper}",
    }


def test_a_wrapper_that_runs_another_adb_fails(
    capsys, config_path, sdk, tmp_path, monkeypatch, banksman_bin
):
    configure(config_path, sdk=sdk)
    wrapper = save_wrapper(tmp_path / "guard", tmp_path / "old sdk" / "platform-tools" / "adb")
    set_path(monkeypatch, wrapper.parent, banksman_bin)
    found = checks(capsys, status=1)
    assert found["adb-wrapper"]["status"] == "fail"
    assert found["adb-wrapper"]["detail"] == (
        f"{wrapper} runs {tmp_path / 'old sdk' / 'platform-tools' / 'adb'}, but the adb of the"
        f" configuration is {sdk / 'platform-tools' / 'adb'}"
    )


def test_a_wrapper_behind_the_sdk_on_the_path_fails(
    capsys, config_path, sdk, tmp_path, monkeypatch, banksman_bin
):
    configure(config_path, sdk=sdk)
    wrapper = save_wrapper(tmp_path / "guard", sdk / "platform-tools" / "adb")
    set_path(monkeypatch, sdk / "platform-tools", wrapper.parent, banksman_bin)
    found = checks(capsys, status=1)
    assert found["adb-path"] == {
        "check": "adb-path",
        "status": "fail",
        "detail": f"{sdk / 'platform-tools' / 'adb'} comes before the adb wrapper {wrapper} on"
        " the PATH, so adb calls skip the guard",
        "fix": f"put {wrapper.parent} on the PATH before {sdk / 'platform-tools'}",
    }
    # The wrapper itself is right.
    assert found["adb-wrapper"]["status"] == "ok"


def test_a_wrapper_that_is_not_executable_fails(
    capsys, config_path, sdk, tmp_path, monkeypatch, banksman_bin
):
    configure(config_path, sdk=sdk)
    wrapper = save_wrapper(tmp_path / "guard", sdk / "platform-tools" / "adb")
    wrapper.chmod(0o644)
    set_path(monkeypatch, wrapper.parent, sdk / "platform-tools", banksman_bin)
    found = checks(capsys, status=1)
    assert found["adb-path"]["status"] == "fail"
    assert found["adb-path"]["fix"] == f"chmod +x {wrapper}"


def test_a_saved_wrapper_that_is_not_on_the_path_is_a_warning(capsys, config_path, sdk, home):
    configure(config_path, sdk=sdk)
    wrapper = save_wrapper((home / doctor.ADB_WRAPPER).parent, sdk / "platform-tools" / "adb")
    found = checks(capsys)
    assert found["adb-path"]["status"] == "warn"
    assert found["adb-path"]["fix"] == f"put {wrapper.parent} first on the PATH of the agents"
    assert found["adb-wrapper"]["status"] == "ok"


@pytest.mark.parametrize("on_path", [True, False])
def test_an_empty_file_in_place_of_the_wrapper_fails(
    capsys, config_path, sdk, home, monkeypatch, banksman_bin, on_path
):
    # The redirection of a save empties the file before banksman runs, so a save whose banksman
    # did not run leaves an empty file, and adb then does nothing.
    configure(config_path, sdk=sdk)
    wrapper = save(home / doctor.ADB_WRAPPER, "")
    wrapper.chmod(0o755)
    if on_path:
        set_path(monkeypatch, wrapper.parent, sdk / "platform-tools", banksman_bin)
    found = checks(capsys, status=1)
    assert found["adb-path"]["status"] == ("ok" if on_path else "warn")
    assert found["adb-wrapper"] == {
        "check": "adb-wrapper",
        "status": "fail",
        "detail": f"{wrapper} is empty, so each adb call through it does nothing",
        "fix": f"banksman admin adb-shim > {wrapper}",
    }


def test_strict_mode_without_a_wrapper_is_a_warning(capsys, config_path):
    configure(config_path, "[guard]\nstrict = true\n")
    assert checks(capsys)["adb-path"]["status"] == "warn"


def test_a_missing_adb_fails(capsys, config_path, tmp_path):
    configure(config_path, sdk=tmp_path / "missing")
    found = checks(capsys, status=1)
    assert found["android-sdk"]["status"] == "fail"
    assert found["android-sdk"]["detail"] == (
        f"{tmp_path / 'missing' / 'platform-tools' / 'adb'} does not exist"
    )


def test_an_adb_that_cannot_list_the_devices_fails(capsys, config_path, sdk):
    (sdk / "platform-tools" / "adb").write_text(ADB_WITHOUT_SERVER)
    configure(config_path, '[kinds.phone]\npreset = "android-device"\n', sdk=sdk)
    save_inventory(Inventory(kinds={"phone": Decisions(allowed=("R5CR1234ABC",))}))
    found = checks(capsys, status=1)
    assert found["android-sdk"]["status"] == "fail"
    assert found["android-sdk"]["detail"] == (
        f"{sdk / 'platform-tools' / 'adb'} works, but it cannot list the devices, so discovery"
        " finds none: adb devices -l failed with exit status 1"
    )
    # Discovery only notes the cause, and the kind shows the note.
    assert found["kind:phone"] == {
        "check": "kind:phone",
        "status": "ok",
        "detail": "discovery finds 0 of 1 allowed; note: cannot list the devices: adb devices"
        " failed with exit status 1",
        "fix": None,
    }


def test_a_kind_that_starts_emulators_needs_the_emulator(capsys, config_path, sdk, tmp_path):
    (tmp_path / "avd").mkdir()
    configure(
        config_path,
        f'avd_home = "{tmp_path / "avd"}"\n[kinds.emulator]\npreset = "android-emulator"\n',
        sdk=sdk,
    )
    found = checks(capsys, status=1)
    assert found["android-sdk"]["detail"] == (
        f"{sdk / 'emulator' / 'emulator'} does not exist, so acquire --start cannot start an"
        " emulator"
    )


def test_a_configuration_with_an_error_fails(capsys, config_path):
    write_config(config_path, '[kinds.lab]\ncount = "two"\n')
    found = checks(capsys, status=1)
    assert found["config"]["status"] == "fail"
    assert found["config"]["detail"].startswith(f"{config_path}: ")
    # The checks that need the configuration do not run.
    assert (found["inventory"]["status"], found["android-sdk"]["status"]) == ("skip", "skip")
    assert found["holder"]["status"] == "ok"


def test_a_missing_inventory_fails_when_a_kind_is_discovered(capsys, config_path, inventory_path):
    configure(config_path, lab_kind())
    found = checks(capsys, status=1)
    assert found["inventory"] == {
        "check": "inventory",
        "status": "fail",
        "detail": f"{inventory_path} does not exist, so agents can get no instance of the kinds"
        " lab",
        "fix": "banksman admin discover",
    }
    assert found["kind:lab"]["detail"] == "no instance is allowed; discovery finds 2 now"


def test_a_failed_discovery_is_a_warning(capsys, config_path):
    configure(config_path, f"[kinds.lab]\ndiscover = {json.dumps(hook('raise SystemExit(4)'))}\n")
    save_inventory(Inventory(kinds={"lab": Decisions(allowed=("rack-1",))}))
    found = checks(capsys)
    assert found["kind:lab"]["status"] == "warn"
    assert found["kind:lab"]["detail"].startswith("discovery failed: ")


def test_doctor_gives_the_command_that_fetches_the_catalogue_of_a_remote_kind(
    capsys, config_path, tmp_path, sdk
):
    # The notes for agents only say that the operator can fetch it.
    configure(
        config_path,
        f'{FakeAndroidCli(tmp_path).config()}'
        '[kinds.remote]\npreset = "android-remote"\nproject = "lab-project"\n',
        sdk=sdk,
    )
    save_inventory(Inventory(kinds={"remote": Decisions(allowed=("tokay:34",))}))
    found = checks(capsys)
    assert found["kind:remote"] == {
        "check": "kind:remote",
        "status": "warn",
        "detail": "discovery failed: kind remote has no catalogue of models yet; only the"
        " operator can fetch it",
        "fix": "banksman admin discover --kind remote",
    }


def test_the_init_script_without_the_build_slot_part_fails(capsys, config_path, home):
    configure(config_path, BUILD)
    save(home / doctor.GRADLE_INIT, printed.gradle_init())
    found = checks(capsys, status=1)
    assert found["gradle-init"]["status"] == "ok"
    assert found["gradle-build-slot"]["status"] == "fail"
    assert found["gradle-build-slot"]["detail"].startswith(
        "every build on Gradle 7.4 and later fails: "
    )


def test_the_init_script_without_the_build_kind_fails(capsys, config_path, home):
    configure(config_path)
    save(home / doctor.GRADLE_INIT, printed.gradle_init())
    save(home / doctor.GRADLE_BUILD_SLOT, printed.build_slot())
    found = checks(capsys, status=1)
    assert found["gradle-init"]["status"] == "fail"
    assert found["gradle-init"]["fix"] == (
        f"declare [kinds.build] in {config_path}, or remove {home / doctor.GRADLE_INIT}"
    )
    assert found["gradle-build-slot"]["status"] == "ok"


def test_a_stale_build_slot_part_fails_and_a_part_alone_is_a_warning(capsys, config_path, home):
    configure(config_path, BUILD)
    save(home / doctor.GRADLE_BUILD_SLOT, "// an older build-slot part\n")
    found = checks(capsys, status=1)
    assert statuses(found)["gradle-init"] == "warn"
    assert statuses(found)["gradle-build-slot"] == "fail"


def test_quarantined_leases_are_a_warning(capsys, state_dir):
    Store(state_dir, Machine()).acquire("emu-1", "emulator", Holder("/work/tree"))

    def quarantine(data):
        drain(data)
        data["state"] = "quarantined"

    edit_lease(state_dir, "emu-1", quarantine)
    found = checks(capsys)
    assert found["quarantine"]["status"] == "warn"
    assert found["quarantine"]["detail"] == "quarantined: emu-1; banksman status shows why"


def test_a_state_directory_that_others_can_write_fails(capsys, state_dir):
    state_dir.mkdir()
    state_dir.chmod(0o777)
    found = checks(capsys, status=1)
    assert found["state-dir"]["detail"] == f"group or others can write to {state_dir}"
    assert found["state-dir"]["fix"] == f"chmod go-w {state_dir}"


def test_another_banksman_first_on_the_path_is_a_warning(capsys, tmp_path, monkeypatch):
    other = tmp_path / "other" / "banksman"
    save(other, "#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(other), "admin", "doctor"])
    found = checks(capsys)
    assert found["banksman"]["status"] == "warn"
    assert (
        f"the banksman first on the PATH is {tmp_path / 'bin' / 'banksman'}, not {other}"
        in (found["banksman"]["detail"])
    )


@pytest.mark.parametrize("adb", ["/opt/sdk/platform-tools/adb", "/opt/my sdk/it's/adb"])
def test_a_saved_wrapper_gives_its_adb(adb):
    assert printed.saved_adb(printed.adb_shim(adb)) == adb


def test_a_changed_wrapper_gives_no_adb():
    text = printed.adb_shim("/opt/sdk/platform-tools/adb")
    assert printed.saved_adb(text.replace("adb=/opt", "adb=/opt/a /opt")) is None
    assert printed.saved_adb(text + "echo more\n") is None
    assert printed.saved_adb("") is None



# The reap job.

TIMED = (
    '[kinds.emulator]\npreset = "android-emulator"\nunused_timeout = "3h"\n'
    '[kinds.spare]\npreset = "android-emulator"\nfree_timeout = "30m"\n'
)


def reap_job(capsys):
    # The other checks fail without an inventory and an emulator, and do not matter here.
    main(["admin", "doctor", "--json"])
    found = json.loads(capsys.readouterr().out)["checks"]
    return next(check for check in found if check["check"] == "reap-job")


def this_banksman():
    # Under pytest, the program is not named banksman, so the job runs this Python.
    return job.program(sys.argv[0])


def test_without_a_reap_job_the_timeouts_of_the_kinds_end_less(capsys, config_path, sdk, home):
    configure(config_path, TIMED, sdk=sdk)
    check = reap_job(capsys)
    path = home / job.LAUNCH_AGENT
    assert check["status"] == "warn"
    assert check["detail"] == (
        f"{path} does not exist, so the unused timeout of the kinds emulator ends no emulator,"
        " and the free timeout of the kinds spare ends an emulator only when a banksman command"
        " runs"
    )
    assert check["fix"] == (
        f"mkdir -p {path.parent} && banksman admin reap-agent > {path} &&"
        f" launchctl bootstrap gui/{os.getuid()} {path}"
    )


def test_a_saved_reap_job_that_launchd_has_loaded_passes(capsys, config_path, sdk, home, jobs):
    configure(config_path, TIMED, sdk=sdk)
    path = save(home / job.LAUNCH_AGENT, job.launchd(this_banksman()))
    jobs.loaded = True
    check = reap_job(capsys)
    assert (check["status"], check["detail"]) == (
        "ok",
        f"{path} runs {sys.executable} -m banksman reap every 5 minutes",
    )
    assert jobs.calls == [["/bin/launchctl", "print", f"gui/{os.getuid()}/banksman.reap"]]


def test_a_saved_reap_job_that_launchd_has_not_loaded_is_a_warning(capsys, home):
    path = save(home / job.LAUNCH_AGENT, job.launchd(this_banksman()))
    check = reap_job(capsys)
    assert check["status"] == "warn"
    assert "launchd has not loaded it" in check["detail"]
    assert check["fix"] == f"launchctl bootstrap gui/{os.getuid()} {path}"


def test_a_saved_reap_job_that_differs_or_cannot_run_fails(capsys, home, jobs):
    jobs.loaded = True
    printed_job = job.launchd(this_banksman())
    path = save(home / job.LAUNCH_AGENT, printed_job.replace("300", "600"))
    check = reap_job(capsys)
    assert (check["status"], check["detail"]) == ("fail", doctor._differs(path))
    assert check["fix"].startswith(f"launchctl bootout gui/{os.getuid()}/banksman.reap; ")
    save(path, job.launchd(("/no/such/banksman",)))
    check = reap_job(capsys)
    assert check["status"] == "fail"
    assert "/no/such/banksman is not an executable file" in check["detail"]


def test_a_reap_job_of_another_banksman_is_a_warning(capsys, home, jobs, banksman_bin):
    jobs.loaded = True
    other = str(banksman_bin / "banksman")
    save(home / job.LAUNCH_AGENT, job.launchd((other,)))
    check = reap_job(capsys)
    assert check["status"] == "warn"
    assert f"runs {other} reap, but this banksman is {sys.executable} -m banksman" in (
        check["detail"]
    )
    # The job is loaded, so launchd must unload it before it loads the new file.
    assert check["fix"].startswith(f"launchctl bootout gui/{os.getuid()}/banksman.reap; ")


def test_the_reap_job_of_cron_is_a_line_of_the_crontab(
    capsys, config_path, sdk, monkeypatch, jobs
):
    monkeypatch.setattr(job, "LAUNCHD", False)
    configure(config_path, TIMED, sdk=sdk)
    check = reap_job(capsys)
    assert check["status"] == "warn"
    assert check["detail"].startswith("no line of the crontab runs banksman reap, so")
    jobs.crontab = f"MAILTO=me\n{job.cron_line(this_banksman())}\n"
    check = reap_job(capsys)
    assert (check["status"], check["detail"]) == (
        "ok",
        f"the crontab runs {sys.executable} -m banksman reap every 5 minutes",
    )
    jobs.crontab = "*/10 * * * * /opt/banksman reap\n"
    assert reap_job(capsys)["status"] == "fail"
    # launchd is not asked on such a system.
    assert {call[0] for call in jobs.calls} == {"crontab"}


def test_a_crontab_that_cannot_be_read_matters_only_with_a_timeout(
    capsys, config_path, sdk, monkeypatch
):
    monkeypatch.setattr(job, "LAUNCHD", False)

    def missing(command):
        raise job.JobError("crontab is not on the PATH")

    monkeypatch.setattr(job, "run_program", missing)
    configure(config_path, sdk=sdk)
    check = reap_job(capsys)
    assert (check["status"], check["detail"]) == (
        "skip",
        "the reap job is not installed: cannot read the crontab: crontab is not on the PATH",
    )
    configure(config_path, TIMED, sdk=sdk)
    assert reap_job(capsys)["status"] == "warn"
