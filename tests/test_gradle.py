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

from banksman import cli
from banksman.cli import main
from banksman.lease import Holder
from banksman.store import Store
from banksman.system import Machine

INIT_SCRIPT, BUILD_SLOT_SCRIPT = (
    resources.files("banksman").joinpath(name).read_text(encoding="utf-8")
    for name in (cli.GRADLE_INIT_SCRIPT, cli.GRADLE_BUILD_SLOT_SCRIPT)
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
            dict(env) if path else {**env, "PATH": os.environ["PATH"]},
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
