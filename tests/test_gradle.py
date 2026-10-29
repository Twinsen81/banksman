"""The Gradle init script, and the build-slot part that holds a slot while each build runs.

The tests that run a real Gradle need BANKSMAN_TEST_GRADLE, the path of a `gradle` program
whose distribution is on this machine. With Gradle 7.4 or later, they test the build slots; with
an older Gradle, they test that a build runs as it does without banksman. Gradle 9 needs Java 17
or later, and Gradle 6.1 needs Java 13 or earlier, so test each Gradle in its own run, with the
JAVA_HOME that it needs. The tests run Gradle offline, with a Gradle user home of their own,
and stop the daemons that they started.
"""

import json
import os
import re
import subprocess
import sys
import time
from importlib import resources

import pytest
from helpers import SRC, write_config

from banksman import cli, guard, printed
from banksman.cli import main
from banksman.lease import Holder, SerialSeen
from banksman.store import Store
from banksman.system import Machine

INIT_SCRIPT, BUILD_SLOT_SCRIPT = (
    resources.files("banksman").joinpath(name).read_text(encoding="utf-8")
    for name in (printed.GRADLE_INIT_SCRIPT, printed.GRADLE_BUILD_SLOT_SCRIPT)
)
GRADLE = os.environ.get("BANKSMAN_TEST_GRADLE")
# The first version that applies the build-slot part, and the first with --configuration-cache.
BUILD_SLOTS = (7, 4)
CONFIGURATION_CACHE = (6, 6)
CONFIG = (
    '[holder]\nagents = []\n[kinds.build]\ncount = 1\nowner_grace = "0s"\nidle_timeout = "off"\n'
)
# The task records the lease files that exist while it runs.
BUILD = """
tasks.register('work') {
    def state = System.getenv('BANKSMAN_STATE_DIR')
    def seen = file('seen.txt')
    doLast { seen.text = (new File(state).list() ?: []).findAll { it.endsWith('.json') }.sort().join(',') }
}
"""


def test_admin_gradle_init_prints_the_init_script_and_the_build_slot_part(capsys):
    assert main(["admin", "gradle-init"]) == 0
    assert capsys.readouterr().out == INIT_SCRIPT
    assert main(["admin", "gradle-init", "--build-slot"]) == 0
    assert capsys.readouterr().out == BUILD_SLOT_SCRIPT


def test_the_init_script_imports_only_classes_that_old_gradle_has():
    # Gradle compiles the whole init script, also on a Gradle that cannot run the build-slot
    # part, and an import that does not resolve fails every build of the user.
    assert set(re.findall(r"^import (\S+)", INIT_SCRIPT, re.MULTILINE)) == {
        "org.gradle.api.GradleException",
        "org.gradle.util.GradleVersion",
    }


def test_the_build_slot_part_calls_only_commands_that_exist():
    # The script calls the command, so the flags that it uses are part of the contract.
    assert set(re.findall(r'"(--[a-z-]+)"', BUILD_SLOT_SCRIPT)) == {
        "--all",
        "--owner-pid",
        "--where",
        "--for",
        "--wait",
    }
    assert '"kind=build"' in BUILD_SLOT_SCRIPT
    assert f"status == {cli.EXIT_BUSY}" in BUILD_SLOT_SCRIPT
    parser = cli._build_parser()
    parser.parse_args(["release", "--all", "--owner-pid", "1"])
    parser.parse_args(
        ["acquire", "--where", "kind=build", "--owner-pid", "1", "--for", "gradle", "--wait", "30m"]
    )


@pytest.fixture(scope="session")
def gradle_version(tmp_path_factory):
    if not GRADLE:
        pytest.skip("set BANKSMAN_TEST_GRADLE to a gradle program to run a real build")
    home = tmp_path_factory.mktemp("gradle-version-home")
    output = subprocess.run(
        [GRADLE, "--version"],
        env={**os.environ, "GRADLE_USER_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    found = re.search(r"^Gradle (\d+)\.(\d+)", output, re.MULTILINE)
    assert found, output
    return int(found[1]), int(found[2])


@pytest.fixture
def installation(tmp_path, config_path, gradle_version):
    write_config(config_path, CONFIG)
    project = tmp_path / "project"
    project.mkdir()
    (project / "settings.gradle").write_text("rootProject.name = 'sample'\n")
    (project / "build.gradle").write_text(BUILD)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "banksman"
    program.write_text(f'#!/bin/sh\nPYTHONPATH="{SRC}" exec "{sys.executable}" -m banksman "$@"\n')
    program.chmod(0o755)
    # The files go where the setup saves them, so that Gradle finds them as it does on a machine.
    home = tmp_path / "gradle-home"
    (home / "init.d").mkdir(parents=True)
    (home / "init.d" / "banksman-build-slot.gradle").write_text(INIT_SCRIPT)
    (home / "banksman").mkdir()
    (home / "banksman" / "build-slot.gradle").write_text(BUILD_SLOT_SCRIPT)
    env = {**os.environ, "GRADLE_USER_HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    cache = ["--configuration-cache"] if gradle_version >= CONFIGURATION_CACHE else []

    def command(*arguments, path=True):
        return (
            [GRADLE, "--offline", *cache, *arguments],
            dict(env) if path else {**env, "PATH": path_without_banksman()},
        )

    def build(*arguments, path=True):
        args, environment = command(*arguments, path=path)
        return subprocess.run(
            args, cwd=project, env=environment, capture_output=True, text=True, timeout=300
        )

    build.command = command
    build.project = project
    build.home = home
    yield build
    subprocess.run(
        [GRADLE, "--stop"], env=env, cwd=project, capture_output=True, timeout=120, check=False
    )


@pytest.fixture
def gradle(gradle_version, request):
    if gradle_version < BUILD_SLOTS:
        pytest.skip("the build slots need Gradle 7.4 or later")
    return request.getfixturevalue("installation")


@pytest.fixture
def old_gradle(gradle_version, request):
    if gradle_version >= BUILD_SLOTS:
        pytest.skip("needs a Gradle older than 7.4")
    return request.getfixturevalue("installation")


def path_without_banksman():
    """Return the PATH of the test without the directories that have a banksman, so that a build
    without banksman on the PATH runs as on a machine without it, also here."""
    return ":".join(
        directory
        for directory in os.environ["PATH"].split(":")
        if not os.path.exists(os.path.join(directory, "banksman"))
    )


def events(capsys):
    assert main(["log", "--json"]) == 0
    return [event["event"] for event in json.loads(capsys.readouterr().out)["events"]]


def test_a_build_holds_a_slot_while_it_runs_also_from_the_configuration_cache(gradle, capsys):
    for _ in range(2):
        result = gradle("work")
        assert result.returncode == 0, result.stdout + result.stderr
        assert (gradle.project / "seen.txt").read_text() == "build-0.json"
        (gradle.project / "seen.txt").unlink()
    assert "Configuration cache entry reused" in result.stdout
    assert Store(cli.default_state_dir(), Machine()).snapshot().leases == []
    assert events(capsys) == ["acquire", "release", "acquire", "release"]


def test_a_build_waits_for_a_slot_and_says_so(gradle):
    held = Store(cli.default_state_dir(), Machine()).acquire("build-0", "build", Holder("/w/b"))
    args, env = gradle.command("work")
    output = gradle.project.parent / "output.txt"
    with open(output, "w") as sink:
        process = subprocess.Popen(
            args, cwd=gradle.project, env=env, stdout=sink, stderr=subprocess.STDOUT, text=True
        )
        try:
            deadline = time.monotonic() + 120
            while "waiting up to 30m" not in output.read_text():
                assert process.poll() is None, output.read_text()
                assert time.monotonic() < deadline, output.read_text()
                time.sleep(0.5)
            Store(cli.default_state_dir(), Machine()).release("build-0", held.lease_id)
            assert process.wait(timeout=120) == 0, output.read_text()
        finally:
            process.kill()
            process.wait()
    assert "every matching resource is in use: build-0" in output.read_text()
    assert (gradle.project / "seen.txt").read_text() == "build-0.json"


def test_a_build_that_gets_no_slot_fails_as_a_limit_of_the_machine(gradle):
    Store(cli.default_state_dir(), Machine()).acquire("build-0", "build", Holder("/w/b"))
    # The operator sets the wait for every build in the Gradle user home.
    (gradle.home / "gradle.properties").write_text("banksman.buildSlotWait=1s\n")
    result = gradle("work")
    assert result.returncode != 0
    assert "no build slot became free in 1s. This is a limit of the machine" in result.stderr
    assert not (gradle.project / "seen.txt").exists()


def test_a_build_fails_when_banksman_cannot_give_a_slot(gradle, config_path):
    write_config(config_path, "[holder]\nagents = []\n")
    result = gradle("work")
    assert result.returncode != 0
    assert "cannot get a build slot: the configuration declares no kind build" in result.stderr


def test_a_build_without_banksman_on_the_path_runs_as_before(gradle):
    result = gradle("work", path=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (gradle.project / "seen.txt").read_text() == ""


def test_a_build_fails_when_the_build_slot_part_is_missing(gradle):
    (gradle.home / "banksman" / "build-slot.gradle").unlink()
    result = gradle("work")
    assert result.returncode != 0
    assert "does not exist, so this build cannot hold a build slot" in result.stderr
    assert "banksman admin gradle-init --build-slot" in result.stderr
    assert not (gradle.project / "seen.txt").exists()


def test_a_build_on_gradle_older_than_7_4_runs_as_without_banksman(old_gradle, capsys):
    # The info build comes first: a build that reuses a configuration cache entry does not run
    # the init script.
    result = old_gradle("work", "--info")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is older than 7.4, so this build holds no build slot" in result.stdout
    result = old_gradle("work")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "build slot" not in result.stdout + result.stderr
    assert (old_gradle.project / "seen.txt").read_text() == ""
    assert events(capsys) == []


# The adb guard for Gradle: the init script, and the part that checks the device tasks of a build.

GUARD_INIT_SCRIPT, GUARD_SCRIPT = (
    resources.files("banksman").joinpath(name).read_text(encoding="utf-8")
    for name in (printed.GRADLE_GUARD_INIT_SCRIPT, printed.GRADLE_GUARD_SCRIPT)
)
# A task of the Android Gradle plugin, by the name of its class, in the buildSrc of the project:
# the guard knows the device tasks of the plugin by the names of their classes.
FAKE_PLUGIN_TASK = """
package com.android.build.gradle.internal.tasks

import org.gradle.api.DefaultTask
import org.gradle.api.file.RegularFileProperty
import org.gradle.api.tasks.OutputFile
import org.gradle.api.tasks.TaskAction

abstract class InstallVariantTask extends DefaultTask {
    @OutputFile
    abstract RegularFileProperty getInstalled()

    @TaskAction
    void install() {
        installed.get().asFile.text = name
    }
}
"""
# The connected tests of the plugin take their devices from --serial, before ANDROID_SERIAL.
FAKE_PLUGIN_TEST_TASK = """
package com.android.build.gradle.internal.tasks

import org.gradle.api.DefaultTask
import org.gradle.api.file.RegularFileProperty
import org.gradle.api.tasks.Internal
import org.gradle.api.tasks.OutputFile
import org.gradle.api.tasks.TaskAction
import org.gradle.api.tasks.options.Option

abstract class DeviceProviderInstrumentTestTask extends DefaultTask {
    @OutputFile
    abstract RegularFileProperty getTested()

    @Internal
    List<String> serials = []

    @Option(option = "serial", description = "The serial of the device to test against.")
    void setSerialOption(List<String> serials) {
        this.serials = serials
    }

    @TaskAction
    void test() {
        tested.get().asFile.text = serials.join(',')
    }
}
"""
# With -PtakeDevice, a task before the install leases its device for another holder, as a
# holder can while the build compiles.
GUARDED_BUILD = """
tasks.register('installDebug', com.android.build.gradle.internal.tasks.InstallVariantTask) {
    installed = layout.projectDirectory.file('installed.txt')
}
tasks.register('connectedDebugAndroidTest',
        com.android.build.gradle.internal.tasks.DeviceProviderInstrumentTestTask) {
    tested = layout.projectDirectory.file('tested.txt')
}
tasks.register('takeDevice', Exec) {
    commandLine(providers.gradleProperty('python').get(), '-I', file('take.py').absolutePath)
}
if (providers.gradleProperty('takeDevice').isPresent()) {
    tasks.named('installDebug') { dependsOn 'takeDevice' }
}
tasks.register('work') {
    def seen = file('seen.txt')
    doLast { seen.text = 'worked' }
}
"""
TAKE_DEVICE = f"""
import sys
sys.path.insert(0, {SRC!r})
from banksman import cli
from banksman.lease import Holder, SerialSeen
from banksman.store import Store
from banksman.system import Machine
store = Store(cli.default_state_dir(), Machine())
store.acquire("e2e_phone", "emulator", Holder("/w/b", issue="#123", agent="codex"))
store.observe([SerialSeen("e2e_phone", "emulator", "emulator-5554", Machine().clock())])
"""
# An adb that lists one emulator, for a build that names no device.
LISTING_ADB = """#!/bin/sh
case "$*" in
*devices*) printf 'List of devices attached\\nemulator-5554 device transport_id:1\\n' ;;
*) exit 1 ;;
esac
"""


def test_admin_gradle_guard_prints_the_init_script_and_the_part(capsys):
    assert main(["admin", "gradle-guard"]) == 0
    assert capsys.readouterr().out == GUARD_INIT_SCRIPT
    assert main(["admin", "gradle-guard", "--part"]) == 0
    assert capsys.readouterr().out == GUARD_SCRIPT


def test_the_guard_init_script_imports_only_classes_that_old_gradle_has():
    assert set(re.findall(r"^import (\S+)", GUARD_INIT_SCRIPT, re.MULTILINE)) == {
        "org.gradle.util.GradleVersion"
    }


def test_the_guard_part_calls_only_commands_that_exist():
    assert '["guard", "gradle"]' in GUARD_SCRIPT
    assert f'SERIAL_OPTION = "{guard.SERIAL_OPTION}"' in GUARD_SCRIPT
    assert f"REFUSED = {guard.EXIT_REFUSED}" in GUARD_SCRIPT
    cli._build_parser().parse_args(
        ["guard", "gradle", "--serial", "emulator-5554", "--", ":app:installDebug"]
    )


@pytest.fixture
def guarded(tmp_path, config_path, gradle_version, request):
    """A project with a device task of the plugin, and the two files of the guard for Gradle."""
    if gradle_version < BUILD_SLOTS:
        pytest.skip("the guard for Gradle needs Gradle 7.4 or later")
    # The daemon itself passes as the agent, so that the test needs no agent process.
    write_config(config_path, '[holder]\nagents = ["java"]\n')
    project = tmp_path / "project"
    plugin = project / "buildSrc/src/main/groovy/com/android/build/gradle/internal/tasks"
    plugin.mkdir(parents=True)
    (plugin / "InstallVariantTask.groovy").write_text(FAKE_PLUGIN_TASK)
    (plugin / "DeviceProviderInstrumentTestTask.groovy").write_text(FAKE_PLUGIN_TEST_TASK)
    (project / "settings.gradle").write_text("rootProject.name = 'sample'\n")
    (project / "build.gradle").write_text(GUARDED_BUILD)
    (project / "take.py").write_text(TAKE_DEVICE)
    (project / "gradle.properties").write_text(f"python={sys.executable}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "banksman"
    program.write_text(f'#!/bin/sh\nPYTHONPATH="{SRC}" exec "{sys.executable}" -m banksman "$@"\n')
    program.chmod(0o755)
    home = tmp_path / "gradle-home"
    (home / "init.d").mkdir(parents=True)
    (home / "init.d" / "banksman-gradle-guard.gradle").write_text(GUARD_INIT_SCRIPT)
    (home / "banksman").mkdir()
    (home / "banksman" / "gradle-guard.gradle").write_text(GUARD_SCRIPT)
    env = {**os.environ, "GRADLE_USER_HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def build(*arguments, path=True, **variables):
        environment = dict(env) if path else {**env, "PATH": path_without_banksman()}
        return subprocess.run(
            [GRADLE, "--offline", "--configuration-cache", *arguments],
            cwd=project,
            env={**environment, **variables},
            capture_output=True,
            text=True,
            timeout=300,
        )

    build.project = project
    build.home = home
    build.program = program
    yield build
    subprocess.run(
        [GRADLE, "--stop"], env=env, cwd=project, capture_output=True, timeout=120, check=False
    )


def other_holder_leases(serial, resource="e2e_phone"):
    store = Store(cli.default_state_dir(), Machine())
    granted = store.acquire(resource, "emulator", Holder("/w/b", issue="#123", agent="codex"))
    store.observe([SerialSeen(resource, "emulator", serial, Machine().clock())])
    return granted


def test_a_build_on_the_device_of_another_holder_fails_before_its_device_task_runs(
    guarded, capsys
):
    other_holder_leases("emulator-5554")
    result = guarded("installDebug", ANDROID_SERIAL="emulator-5554")
    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "banksman: gradle :installDebug: refused: another holder leases emulator-5554 (e2e_phone),"
        " and the lease is ready: " in result.stderr
    )
    assert not (guarded.project / "installed.txt").exists()
    assert main(["log", "--json"]) == 0
    (event,) = json.loads(capsys.readouterr().out)["events"]
    assert (event["event"], event["command"], event["serial"]) == (
        "guard",
        "gradle install",
        "emulator-5554",
    )


def test_the_check_runs_also_when_gradle_reuses_the_configuration_cache(guarded):
    result = guarded("installDebug", ANDROID_SERIAL="emulator-5554")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (guarded.project / "installed.txt").read_text() == "installDebug"
    (guarded.project / "installed.txt").unlink()
    granted = other_holder_leases("emulator-5554")
    result = guarded("installDebug", ANDROID_SERIAL="emulator-5554")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "refused: another holder leases emulator-5554" in result.stderr
    assert not (guarded.project / "installed.txt").exists()
    Store(cli.default_state_dir(), Machine()).release("e2e_phone", granted.lease_id)
    result = guarded("installDebug", ANDROID_SERIAL="emulator-5554")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Configuration cache entry reused" in result.stdout
    assert (guarded.project / "installed.txt").read_text() == "installDebug"


def test_a_build_under_banksman_run_passes_on_its_own_device(guarded):
    store = Store(cli.default_state_dir(), Machine())
    granted = store.acquire("e2e_phone", "emulator", Holder("/w/a"))
    store.observe([SerialSeen("e2e_phone", "emulator", "emulator-5554", Machine().clock())])
    result = guarded(
        "installDebug", ANDROID_SERIAL="emulator-5554", BANKSMAN_LEASE=granted.lease_id
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (guarded.project / "installed.txt").read_text() == "installDebug"
    assert "banksman" not in result.stdout + result.stderr


def test_a_build_that_names_no_device_is_checked_on_every_device_that_adb_lists(
    guarded, config_path, tmp_path
):
    adb = tmp_path / "sdk" / "platform-tools" / "adb"
    adb.parent.mkdir(parents=True)
    adb.write_text(LISTING_ADB)
    adb.chmod(0o755)
    write_config(
        config_path, f'[holder]\nagents = ["java"]\n[android]\nsdk = "{tmp_path / "sdk"}"\n'
    )
    other_holder_leases("emulator-5554")
    result = guarded("installDebug")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "refused: another holder leases emulator-5554" in result.stderr
    assert "set ANDROID_SERIAL to the serial of your device" in result.stderr
    assert not (guarded.project / "installed.txt").exists()


def test_a_build_without_a_device_task_asks_nothing(guarded, config_path):
    # In strict mode, a check would at least say that it cannot list the devices.
    write_config(config_path, '[holder]\nagents = ["java"]\n[guard]\nstrict = true\n')
    for arguments in (["work"], ["--dry-run", "installDebug"]):
        result = guarded(*arguments)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "banksman" not in result.stdout + result.stderr
    assert (guarded.project / "seen.txt").read_text() == "worked"
    assert not (guarded.project / "installed.txt").exists()


def test_a_build_without_banksman_on_the_path_runs_and_says_that_it_is_not_checked(guarded):
    other_holder_leases("emulator-5554")
    result = guarded("installDebug", path=False, ANDROID_SERIAL="emulator-5554")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "banksman: banksman is not on the PATH, so the device tasks of this build are not"
        " checked: :installDebug" in result.stdout
    )
    assert (guarded.project / "installed.txt").read_text() == "installDebug"


def test_the_serial_option_of_the_connected_tests_is_checked_before_android_serial(guarded):
    store = Store(cli.default_state_dir(), Machine())
    granted = store.acquire("e2e_phone", "emulator", Holder("/w/a"))
    store.observe([SerialSeen("e2e_phone", "emulator", "emulator-5554", Machine().clock())])
    other_holder_leases("emulator-5556", resource="e2e_tablet")
    own = {"ANDROID_SERIAL": "emulator-5554", "BANKSMAN_LEASE": granted.lease_id}
    result = guarded("connectedDebugAndroidTest", "--serial", "emulator-5556", **own)
    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "banksman: gradle :connectedDebugAndroidTest: refused: another holder leases"
        " emulator-5556" in result.stderr
    )
    assert not (guarded.project / "tested.txt").exists()
    result = guarded(
        "connectedDebugAndroidTest", "--serial=emulator-5554", BANKSMAN_LEASE=granted.lease_id
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (guarded.project / "tested.txt").read_text() == "emulator-5554"


def test_a_device_task_is_checked_again_right_before_it_runs(guarded):
    # The pre-check passes, a task of the build leases the device for another holder, and the
    # check before the install refuses. The second build reuses the configuration cache, with
    # the check stored in it.
    for _ in range(2):
        result = guarded("installDebug", "-PtakeDevice=1", ANDROID_SERIAL="emulator-5554")
        assert result.returncode != 0, result.stdout + result.stderr
        assert "> Task :takeDevice" in result.stdout
        assert "gradle :installDebug: refused: another holder leases emulator-5554" in result.stderr
        assert not (guarded.project / "installed.txt").exists()
        store = Store(cli.default_state_dir(), Machine())
        (held,) = store.snapshot().leases
        store.release("e2e_phone", held.lease_id)
    assert "Configuration cache entry reused" in result.stdout


def test_a_banksman_that_cannot_start_leaves_the_build_unchecked_and_says_so(guarded):
    # For example after an upgrade of Python removed the interpreter of its launcher.
    guarded.program.write_text("#!/nonexistent/python3\n")
    other_holder_leases("emulator-5554")
    result = guarded("installDebug", ANDROID_SERIAL="emulator-5554")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "banksman: the device tasks of this build are not checked: cannot run" in result.stdout
    assert (guarded.project / "installed.txt").read_text() == "installDebug"


def test_a_build_runs_when_the_part_of_the_guard_is_missing_and_says_so(guarded):
    (guarded.home / "banksman" / "gradle-guard.gradle").unlink()
    other_holder_leases("emulator-5554")
    result = guarded("installDebug", ANDROID_SERIAL="emulator-5554")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "does not exist, so the device tasks of this build are not checked" in result.stdout
    assert "banksman admin gradle-guard --part" in result.stdout
    assert (guarded.project / "installed.txt").read_text() == "installDebug"
