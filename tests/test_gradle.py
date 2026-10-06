"""The Gradle init script that holds a build slot while each build runs.

The tests that run a real Gradle need BANKSMAN_TEST_GRADLE, the path of a `gradle` program
whose distribution is on this machine. They run it offline, with a Gradle user home of their
own, and stop the daemons that they started.
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

SCRIPT = resources.files("banksman").joinpath(cli.GRADLE_INIT_SCRIPT).read_text(encoding="utf-8")
GRADLE = os.environ.get("BANKSMAN_TEST_GRADLE")
needs_gradle = pytest.mark.skipif(
    not GRADLE, reason="set BANKSMAN_TEST_GRADLE to a gradle program to run a real build"
)
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


def test_admin_gradle_init_prints_the_init_script(capsys):
    assert main(["admin", "gradle-init"]) == 0
    assert capsys.readouterr().out == SCRIPT


def test_the_init_script_calls_only_commands_that_exist():
    # The script calls the command, so the flags that it uses are part of the contract.
    assert set(re.findall(r'"(--[a-z-]+)"', SCRIPT)) == {
        "--all",
        "--owner-pid",
        "--where",
        "--for",
        "--wait",
    }
    assert '"kind=build"' in SCRIPT
    assert f"status == {cli.EXIT_BUSY}" in SCRIPT
    parser = cli._build_parser()
    parser.parse_args(["release", "--all", "--owner-pid", "1"])
    parser.parse_args(
        ["acquire", "--where", "kind=build", "--owner-pid", "1", "--for", "gradle", "--wait", "30m"]
    )


@pytest.fixture
def gradle(tmp_path, config_path):
    write_config(config_path, CONFIG)
    project = tmp_path / "project"
    project.mkdir()
    (project / "settings.gradle").write_text("rootProject.name = 'sample'\n")
    (project / "build.gradle").write_text(BUILD)
    script = tmp_path / "build-slot.gradle"
    script.write_text(SCRIPT)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "banksman"
    program.write_text(f'#!/bin/sh\nPYTHONPATH="{SRC}" exec "{sys.executable}" -m banksman "$@"\n')
    program.chmod(0o755)
    home = tmp_path / "gradle-home"
    home.mkdir()
    env = {**os.environ, "GRADLE_USER_HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def command(*arguments, path=True):
        return (
            [GRADLE, "--offline", "--configuration-cache", "-I", str(script), *arguments],
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


def events(capsys):
    assert main(["log", "--json"]) == 0
    return [event["event"] for event in json.loads(capsys.readouterr().out)["events"]]


@needs_gradle
def test_a_build_holds_a_slot_while_it_runs_also_from_the_configuration_cache(gradle, capsys):
    for _ in range(2):
        result = gradle("work")
        assert result.returncode == 0, result.stdout + result.stderr
        assert (gradle.project / "seen.txt").read_text() == "build-0.json"
        (gradle.project / "seen.txt").unlink()
    assert "Configuration cache entry reused" in result.stdout
    assert Store(cli.default_state_dir(), Machine()).snapshot().leases == []
    assert events(capsys) == ["acquire", "release", "acquire", "release"]


@needs_gradle
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


@needs_gradle
def test_a_build_that_gets_no_slot_fails_as_a_limit_of_the_machine(gradle):
    Store(cli.default_state_dir(), Machine()).acquire("build-0", "build", Holder("/w/b"))
    # The operator sets the wait for every build in the Gradle user home.
    (gradle.home / "gradle.properties").write_text("banksman.buildSlotWait=1s\n")
    result = gradle("work")
    assert result.returncode != 0
    assert "no build slot became free in 1s. This is a limit of the machine" in result.stderr
    assert not (gradle.project / "seen.txt").exists()


@needs_gradle
def test_a_build_fails_when_banksman_cannot_give_a_slot(gradle, config_path):
    write_config(config_path, "[holder]\nagents = []\n")
    result = gradle("work")
    assert result.returncode != 0
    assert "cannot get a build slot: the configuration declares no kind build" in result.stderr


@needs_gradle
def test_a_build_without_banksman_on_the_path_runs_as_before(gradle):
    result = gradle("work", path=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (gradle.project / "seen.txt").read_text() == ""

